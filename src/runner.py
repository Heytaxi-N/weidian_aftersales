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
from datetime import datetime, time as dtime
from pathlib import Path

from src.config import LOGS_DIR, TIMEZONE
from src.db import get_conn
from src.logistics import weidian_trace as logistics_provider
from src.notify import render as card_render, wecom
from src.notify.templates import (
    OverviewStats,
    render_b,
    render_overview,
    render_urgent,
)
from src.rules.engine import (
    LogisticsInfo,
    PENDING_STATUSES,
    RefundRecord,
    STATUS_PENDING_MERCHANT_RECEIVE,
    TIER_WARN_HOURS,
    evaluate,
    to_payload,
)
from src.weidian import client as weidian_client
from src.weidian import overview as weidian_overview

log = logging.getLogger("runner")

B_DAILY_QUOTA = 10


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


def _b_quota_used_today(now: datetime) -> int:
    """今天 00:00 起已推 B 的条数。"""
    today_start = datetime.combine(now.date(), dtime(0, 0), tzinfo=TIMEZONE)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM pushed_records WHERE scenario = 'B' AND pushed_at >= ?",
            (today_start.isoformat(),),
        ).fetchone()
        return int(row["n"])


def _gather_logistics(refunds: list[RefundRecord]) -> dict[str, LogisticsInfo]:
    pairs: list[tuple[str, int]] = []
    seen: set[str] = set()
    for r in refunds:
        if r.status != STATUS_PENDING_MERCHANT_RECEIVE or not r.return_tracking_no:
            continue
        if r.return_tracking_no in seen:
            continue
        if not r.return_express_type:
            log.debug("skip %s (no express_type)", r.return_tracking_no)
            continue
        seen.add(r.return_tracking_no)
        pairs.append((r.return_tracking_no, r.return_express_type))

    results = logistics_provider.query_pairs(pairs)
    out: dict[str, LogisticsInfo] = {}
    for t, rec in results.items():
        signed_at = datetime.fromisoformat(rec["signed_at"]) if rec.get("signed_at") else None
        out[t] = LogisticsInfo(
            tracking_no=t,
            signed_at=signed_at,
            screenshot_path=rec.get("screenshot_path"),
            carrier=rec.get("carrier"),
            trace_text=rec.get("raw_text"),
        )
    return out


def _count_urgent(pending: list[RefundRecord], now: datetime, status: str) -> int:
    """临期 ≤48h 在某个状态下的笔数。"""
    n = 0
    for r in pending:
        if r.status != status or not r.deadline_at:
            continue
        hours_left = (r.deadline_at - now).total_seconds() / 3600.0
        if hours_left <= TIER_WARN_HOURS:
            n += 1
    return n


def _send_overview(pending: list[RefundRecord], now: datetime) -> None:
    """概览：4 tab 计数 + 待商家两类的临期数。"""
    try:
        counts = weidian_overview.fetch_counts()
    except Exception as e:
        log.warning("overview fetch failed: %s", e)
        return
    stats = OverviewStats(
        wait_seller_handle=counts.wait_seller_handle,
        wait_seller_handle_urgent=_count_urgent(pending, now, "待商家处理"),
        wait_seller_receive=counts.wait_seller_receive,
        wait_seller_receive_urgent=_count_urgent(pending, now, "待商家收货"),
        wait_buyer=counts.wait_buyer,
        wait_customer=counts.wait_customer,
    )
    msg = render_overview(stats)
    try:
        wecom.send_markdown(msg)
        log.info("overview sent")
    except Exception as e:
        log.warning("overview send failed: %s", e)


def _push_urgent(decisions, dry_run: bool) -> int:
    """推 A/A2 临期/紧急。每决策一条 markdown。返回成功数。"""
    n = 0
    for d in decisions:
        time_scenarios = [s for s in d.scenarios if s in ("A", "A2")]
        if not time_scenarios:
            continue
        payload = to_payload(d)
        msg = render_urgent(payload)
        if dry_run:
            log.info("[dry-run] urgent %s %s", payload.refund_id, time_scenarios)
            continue
        try:
            wecom.send_markdown(msg)
            for scn in time_scenarios:
                _record_push(payload.refund_id, scn, msg, None)
            n += 1
        except Exception as e:
            log.exception("urgent push failed %s: %s", payload.refund_id, e)
    return n


def _push_b(decisions, now: datetime, dry_run: bool) -> int:
    """推 B 已签收，按 deadline 升序裁剪到当日剩余配额。返回成功数。"""
    b_decisions = [d for d in decisions if "B" in d.scenarios]
    if not b_decisions:
        return 0

    used = _b_quota_used_today(now)
    quota = max(0, B_DAILY_QUOTA - used)
    log.info("B quota: used=%d quota_left=%d candidates=%d", used, quota, len(b_decisions))

    if quota == 0:
        log.info("B daily quota exhausted, skipping")
        return 0

    # 按截止时间最紧迫（最小 deadline）排序；无 deadline 排最后
    def _sort_key(d):
        return (d.refund.deadline_at is None,
                d.refund.deadline_at or datetime.max.replace(tzinfo=TIMEZONE))
    b_decisions.sort(key=_sort_key)
    selected = b_decisions[:quota]

    # 预先批量渲染所有签收卡片（共用一个浏览器）
    specs = []
    for d in selected:
        log_info = d.logistics
        if log_info is None:
            continue
        signed_time = (log_info.signed_at.strftime("%Y-%m-%d %H:%M")
                       if log_info.signed_at else None)
        # 从 trace_text 抠签收 context（短）
        context = None
        for line in (log_info.trace_text or "").splitlines():
            if "签收" in line:
                import re as _re
                m = _re.search(r"\*\*[^*]*签收[^*]*\*\*\s*(.+)$", line)
                if m:
                    context = m.group(1).strip()
                break
        specs.append({
            "carrier": log_info.carrier or "—",
            "tracking_no": log_info.tracking_no,
            "signed_time": signed_time,
            "context": context,
        })

    if dry_run:
        for d, spec in zip(selected, specs):
            log.info("[dry-run] B %s -> %s", d.refund.refund_id, spec.get("tracking_no"))
        return 0

    cards = card_render.render_many(specs)

    n = 0
    for d, card_path in zip(selected, cards):
        payload = to_payload(d)
        text = render_b(payload)
        try:
            wecom.send_markdown(text)
            if card_path and Path(card_path).exists():
                try:
                    wecom.send_image(card_path)
                except Exception as e:
                    log.warning("card image send failed for %s: %s", payload.refund_id, e)
            _record_push(payload.refund_id, "B", text, str(card_path) if card_path else None)
            n += 1
        except Exception as e:
            log.exception("B push failed %s: %s", payload.refund_id, e)
    return n


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

    # 3. 物流查询
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

    # 5. 发概览（即使没有提醒也发，让店主每天看到全局）
    if not dry_run:
        _send_overview(pending, started)

    # 6. 推 A/A2 临期紧急
    pushed_urgent = _push_urgent(decisions, dry_run)
    log.info("urgent pushed: %d", pushed_urgent)

    # 7. 推 B 已签收（限额）
    pushed_b = _push_b(decisions, started, dry_run)
    log.info("B pushed: %d", pushed_b)

    pushed_count = pushed_urgent + pushed_b

    # 8. 早报（仅 09:00 那轮）
    if daily_report and not dry_run:
        try:
            from src.report.daily import build_and_send
            build_and_send(pushed_this_run=pushed_count, snapshot_at=snapshot_at)
        except Exception as e:
            log.exception("daily report failed: %s", e)

    # 9. run_log
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO run_log(started_at, finished_at, ok, note) VALUES (?,?,?,?)",
            (snapshot_at, datetime.now(TIMEZONE).isoformat(), 1,
             f"refunds={len(refunds)} pending={len(pending)} urgent={pushed_urgent} b={pushed_b}"),
        )

    log.info("=== run done urgent=%d B=%d ===", pushed_urgent, pushed_b)
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
