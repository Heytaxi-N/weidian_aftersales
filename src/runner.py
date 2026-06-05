"""
主流程：抓取 → 写快照 → 规则判定 → 物流查询 → 推送 → 记录。

调用：
  python -m src.runner                       # 普通跑
  python -m src.runner --with-daily-report   # 09:00 跑（含早报）
  python -m src.runner --dry-run             # 抓但不推送、不写 pushed_records
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from src.config import LOGS_DIR, TIMEZONE
from src.db import get_conn
from src.logistics import sogou as logistics_provider
from src.notify import wecom
from src.notify.templates import render_push
from src.rules.engine import (
    LogisticsInfo,
    PENDING_STATUSES,
    RefundRecord,
    STATUS_PENDING_MERCHANT_RECEIVE,
    evaluate,
    to_payload,
)
from src.weidian import client as weidian_client

log = logging.getLogger("runner")


def setup_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOGS_DIR / f"run-{datetime.now(TIMEZONE).strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(logfile, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def _write_snapshot(refunds: list[RefundRecord], snapshot_at: str) -> None:
    with get_conn() as conn:
        for r in refunds:
            conn.execute(
                """INSERT OR REPLACE INTO order_snapshots
                   (snapshot_at, refund_id, order_id, refund_type, status, deadline_at,
                    buyer_name, buyer_phone, receiver_name, receiver_phone, item_title,
                    return_tracking_no, detail_url, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_at,
                    r.refund_id, r.order_id, r.refund_type, r.status,
                    r.deadline_at.isoformat() if r.deadline_at else None,
                    r.buyer_name, r.buyer_phone, r.receiver_name, r.receiver_phone,
                    r.item_title, r.return_tracking_no, r.detail_url,
                    json.dumps(asdict(r), default=str, ensure_ascii=False),
                ),
            )


def _load_pushed() -> set[tuple[str, str]]:
    with get_conn() as conn:
        return {(row["refund_id"], row["scenario"])
                for row in conn.execute("SELECT refund_id, scenario FROM pushed_records")}


def _record_push(refund_id: str, scenario: str, message: str, screenshot: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO pushed_records
               (refund_id, scenario, pushed_at, message_text, screenshot_path)
               VALUES (?,?,?,?,?)""",
            (refund_id, scenario, datetime.now(TIMEZONE).isoformat(), message, screenshot),
        )


def _gather_logistics(refunds: list[RefundRecord]) -> dict[str, LogisticsInfo]:
    tracking_nos: list[str] = []
    seen: set[str] = set()
    for r in refunds:
        if r.status != STATUS_PENDING_MERCHANT_RECEIVE or not r.return_tracking_no:
            continue
        if r.return_tracking_no in seen:
            continue
        seen.add(r.return_tracking_no)
        tracking_nos.append(r.return_tracking_no)

    results = logistics_provider.query_many(tracking_nos)
    out: dict[str, LogisticsInfo] = {}
    for t, rec in results.items():
        signed_at = datetime.fromisoformat(rec["signed_at"]) if rec.get("signed_at") else None
        out[t] = LogisticsInfo(
            tracking_no=t,
            signed_at=signed_at,
            screenshot_path=rec.get("screenshot_path"),
        )
    return out


def run(*, dry_run: bool = False, daily_report: bool = False, skip_logistics: bool = False) -> int:
    setup_logging()
    started = datetime.now(TIMEZONE)
    snapshot_at = started.isoformat()
    log.info("=== run start (dry_run=%s, daily=%s) ===", dry_run, daily_report)

    # 1. 抓取
    try:
        refunds = weidian_client.fetch_all_refunds()
    except weidian_client.WeidianNotLoggedIn as e:
        log.error("login expired: %s", e)
        wecom.send_alert(f"微店登录失效，请运行 ./scripts/login.sh\n{e}")
        return 2
    except Exception as e:
        log.exception("fetch failed")
        wecom.send_alert(f"微店抓取失败：{e}")
        return 3

    pending = [r for r in refunds if r.status in PENDING_STATUSES]
    log.info("fetched %d refunds (%d pending)", len(refunds), len(pending))

    # 2. 写快照
    _write_snapshot(pending, snapshot_at)

    # 3. 物流查询（只查待商家收货且有运单号的）
    logistics: dict[str, LogisticsInfo] = {}
    if skip_logistics:
        log.info("skipping logistics query (--skip-logistics)")
    else:
        logistics = _gather_logistics(pending)
        log.info("logistics queried for %d tracking numbers", len(logistics))

    # 4. 规则判定
    already = _load_pushed() if not dry_run else set()
    decisions = evaluate(pending, logistics, already, started)
    log.info("decisions: %d push payloads", len(decisions))

    # 5. 推送 + 记录
    pushed_count = 0
    if not dry_run:
        for d in decisions:
            payload = to_payload(d)
            msg = render_push(payload)
            try:
                wecom.send_markdown(msg)
                if payload.screenshot_path and Path(payload.screenshot_path).exists():
                    try:
                        wecom.send_image(payload.screenshot_path)
                    except Exception as e:
                        log.warning("image send failed: %s", e)
                for scn in payload.scenarios:
                    _record_push(payload.refund_id, scn, msg, payload.screenshot_path)
                pushed_count += 1
            except Exception as e:
                log.exception("push failed for %s: %s", payload.refund_id, e)
    else:
        for d in decisions:
            log.info("[dry-run] would push %s %s", d.refund.refund_id, d.scenarios)

    # 6. 早报（仅 09:00 那轮）
    if daily_report and not dry_run:
        try:
            from src.report.daily import build_and_send
            build_and_send(pushed_this_run=pushed_count, snapshot_at=snapshot_at)
        except Exception as e:
            log.exception("daily report failed: %s", e)

    # 7. run_log
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO run_log(started_at, finished_at, ok, note) VALUES (?,?,?,?)",
            (snapshot_at, datetime.now(TIMEZONE).isoformat(), 1,
             f"refunds={len(refunds)} pending={len(pending)} pushed={pushed_count}"),
        )

    log.info("=== run done ok pushed=%d ===", pushed_count)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--with-daily-report", action="store_true")
    ap.add_argument("--skip-logistics", action="store_true")
    args = ap.parse_args()
    sys.exit(run(dry_run=args.dry_run, daily_report=args.with_daily_report,
                 skip_logistics=args.skip_logistics))


if __name__ == "__main__":
    main()
