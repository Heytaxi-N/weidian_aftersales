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

from src.config import (
    LOGS_DIR,
    TIMEZONE,
    WECOM_WEBHOOK_URL_BUYER,
    D_AUTOFILL_ENABLED,
    D_AUTOFILL_LIMIT,
)
from src.db import get_conn
from src.logistics import weidian_trace as logistics_provider
from src.notify import render as card_render, wecom
from src.notify.templates import (
    BuyerPushPayload,
    DPushPayload,
    OverviewStats,
    render_b,
    render_b_group_header,
    render_c,
    render_d,
    render_overview,
    render_urgent,
)
from src.rules.engine import (
    LogisticsInfo,
    PENDING_STATUSES,
    RefundRecord,
    STATUS_PENDING_MERCHANT_RECEIVE,
    TIER_WARN_HOURS,
    classify_d,
    evaluate,
    evaluate_buyer,
    match_d,
    to_payload,
)
from src.weidian import buyer_client
from src.weidian import client as weidian_client
from src.weidian import order_supplier as weidian_supplier
from src.weidian import overview as weidian_overview

log = logging.getLogger("runner")

B_DAILY_QUOTA = 20


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
                    item_id, return_tracking_no, supplier_name, detail_url, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_at,
                    r.refund_id, r.order_id, r.refund_type, r.status,
                    r.deadline_at.isoformat() if r.deadline_at else None,
                    r.buyer_name, r.buyer_phone, r.receiver_name, r.receiver_phone,
                    r.item_title, r.item_id, r.return_tracking_no, r.supplier_name,
                    r.detail_url,
                    json.dumps(asdict(r), default=str, ensure_ascii=False),
                ),
            )


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
    """推 B 已签收，按 deadline 升序裁剪到当日剩余配额，按供货商分组发送。"""
    b_decisions = [d for d in decisions if "B" in d.scenarios]
    if not b_decisions:
        return 0

    used = _b_quota_used_today(now)
    quota = max(0, B_DAILY_QUOTA - used)
    log.info("B quota: used=%d quota_left=%d candidates=%d", used, quota, len(b_decisions))

    if quota == 0:
        log.info("B daily quota exhausted, skipping")
        return 0

    # 1. 按截止时间紧迫度排序选出配额内的候选
    def _sort_key(d):
        return (d.refund.deadline_at is None,
                d.refund.deadline_at or datetime.max.replace(tzinfo=TIMEZONE))
    b_decisions.sort(key=_sort_key)
    selected = b_decisions[:quota]

    # 2. 查供货商（仅对选中的候选）
    order_ids = list({d.refund.order_id for d in selected if d.refund.order_id})
    try:
        supplier_map = weidian_supplier.fetch_suppliers(order_ids)
    except Exception as e:
        log.warning("supplier lookup failed: %s — 全部归为无供货商分组", e)
        supplier_map = {}
    for d in selected:
        key = (d.refund.order_id, d.refund.item_id or "")
        d.refund.supplier_name = supplier_map.get(key)

    # 3. 按供货商分组，组内仍按 deadline 排序
    groups: dict[str | None, list] = {}
    for d in selected:
        groups.setdefault(d.refund.supplier_name, []).append(d)
    for g in groups.values():
        g.sort(key=_sort_key)

    # 组顺序：按"组内最紧迫的那笔"的 deadline 排，紧迫的组排前
    group_order = sorted(
        groups.keys(),
        key=lambda k: _sort_key(groups[k][0]),
    )
    log.info("B 分组: %s",
             {k or "(无)": len(v) for k, v in groups.items()})

    # 4. 渲染所有签收卡片（仍是按 refund 一张图）
    specs = []
    for sup in group_order:
        for d in groups[sup]:
            log_info = d.logistics
            if log_info is None:
                specs.append(None)
                continue
            signed_time = (log_info.signed_at.strftime("%Y-%m-%d %H:%M")
                           if log_info.signed_at else None)
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
        for sup in group_order:
            log.info("[dry-run] B 供货商 %s 笔数 %d", sup or "(无)", len(groups[sup]))
            for d in groups[sup]:
                log.info("[dry-run]   %s tracking=%s",
                         d.refund.refund_id,
                         d.refund.return_tracking_no)
        return 0

    valid_specs = [s for s in specs if s is not None]
    cards = card_render.render_many(valid_specs) if valid_specs else []
    # 把 None 占位回填
    card_iter = iter(cards)
    full_cards: list = []
    for s in specs:
        full_cards.append(next(card_iter) if s is not None else None)

    # 5. 按组发：① 组头单条 ② 组内所有笔合一条多行 markdown ③ 每笔一张图
    n = 0
    card_idx = 0
    for sup in group_order:
        members = groups[sup]
        payloads = [to_payload(d) for d in members]
        header = render_b_group_header(sup, len(members))
        try:
            wecom.send_markdown(header)
        except Exception as e:
            log.exception("B group header send failed (%s): %s", sup, e)
            card_idx += len(members)
            continue

        # 组内所有笔合并一条 text（用 text 而非 markdown，便于店主转发到微信。
        # render_b 输出本就是纯文本，无格式损失）
        lines_md = "\n".join(render_b(p) for p in payloads)
        try:
            wecom.send_text(lines_md)
        except Exception as e:
            log.exception("B group lines send failed (%s): %s", sup, e)
            card_idx += len(members)
            continue

        # 每笔一张图 + 写库（message_text 存单笔行，dashboard 看更干净）
        for d, payload in zip(members, payloads):
            card_path = full_cards[card_idx]
            card_idx += 1
            try:
                if card_path and Path(card_path).exists():
                    try:
                        wecom.send_image(card_path)
                    except Exception as e:
                        log.warning("card image send failed for %s: %s", payload.refund_id, e)
                _record_push(
                    payload.refund_id, "B", render_b(payload),
                    str(card_path) if card_path else None,
                )
                n += 1
            except Exception as e:
                log.exception("B push failed %s: %s", payload.refund_id, e)
    return n


def _fetch_buyer_refunds():
    """抓买家版退款列表 + 补详情（倒计时、客户信息）。C 和 D 共用一次抓取。

    成功返回 list[BuyerRefundRecord]；失败 send_alert + 返回 None（不阻断卖家版主流程）。
    """
    try:
        refunds = buyer_client.fetch_refund_list()
    except buyer_client.WeidianNotLoggedIn as e:
        log.warning("buyer-side login expired: %s", e)
        wecom.send_alert(f"买家版登录失效，C/D 场景无法工作：{e}")
        return None
    except Exception as e:
        log.exception("buyer refund list fetch failed: %s", e)
        wecom.send_alert(f"买家版退款列表抓取失败：{e}")
        return None

    try:
        buyer_client.enrich_refunds(refunds)
    except buyer_client.WeidianNotLoggedIn as e:
        log.warning("buyer-side login expired during enrich: %s", e)
        wecom.send_alert(f"买家版登录失效（enrich 阶段）：{e}")
        return None
    except Exception as e:
        log.exception("buyer refund enrich failed: %s", e)
        # 不中断 — evaluate/match 会跳过缺字段的笔
    return refunds


def _push_buyer_c(buyer_refunds, dry_run: bool) -> int:
    """场景 C：买家版「待买家处理退货」剩余 ≤ 25h 的逐笔推送。

    走独立 webhook（WECOM_WEBHOOK_URL_BUYER）。
    """
    decisions = evaluate_buyer(buyer_refunds)
    log.info("C candidates: %d (buyer refunds total: %d)", len(decisions), len(buyer_refunds))

    n = 0
    for d in decisions:
        r = d.refund
        payload = BuyerPushPayload(
            refund_no=r.refund_no,
            shop_name=r.shop_name,
            item_title_first10=(r.item_title or "")[:10],
            item_sku_title=r.item_sku_title,
            hours_left=d.hours_left or 0.0,
            customer_name=r.customer_name,
            customer_phone=r.customer_phone,
            operate_status_str=r.operate_status_str,
        )
        msg = render_c(payload)
        if dry_run:
            log.info("[dry-run] C %s shop=%s hours_left=%.1f",
                     r.refund_no, r.shop_name, d.hours_left or 0)
            continue
        try:
            wecom.send_markdown(msg, webhook_url=WECOM_WEBHOOK_URL_BUYER)
            n += 1
        except Exception as e:
            log.exception("C push failed %s: %s", r.refund_no, e)
    return n


def _send_buyer_heartbeat(buyer_refunds, pushed_c: int, pushed_d: int,
                          now: datetime, dry_run: bool) -> None:
    """买家版巡检心跳：每轮固定发一条，哪怕 C/D 都是 0，让店主有预期（系统跑了/没失败）。"""
    pending = [r for r in buyer_refunds
               if getattr(r, "refund_status_str", None) == "待买家处理退货"]
    lines = [
        f"📋 **买家版巡检 {now.strftime('%m-%d %H:%M')}**",
        f"> 待买家处理退货：{len(pending)} 笔",
        f"> C 临期提醒(≤25h)：{pushed_c}　D 待填单号：{pushed_d}",
    ]
    if pushed_c == 0 and pushed_d == 0:
        if pending:
            # 有待处理但都不满足触发条件，列出最紧迫几笔的倒计时让店主心里有数
            def _cd(r):
                return r.countdown_seconds if r.countdown_seconds is not None else 10**12
            top = sorted(pending, key=_cd)[:5]
            lines.append("> 本轮无需操作（最紧迫几笔倒计时）：")
            for r in top:
                h = (r.countdown_seconds or 0) / 3600
                lines.append(f">   · {(r.item_title or '')[:12]}　{h:.0f}h")
        else:
            lines.append("> ✅ 本轮无待办")
    msg = "\n".join(lines)
    if dry_run:
        log.info("[dry-run] buyer heartbeat: pending=%d c=%d d=%d",
                 len(pending), pushed_c, pushed_d)
        return
    try:
        wecom.send_markdown(msg, webhook_url=WECOM_WEBHOOK_URL_BUYER)
    except Exception as e:
        log.exception("buyer heartbeat send failed: %s", e)


def _push_d(buyer_refunds, seller_refunds, dry_run: bool) -> int:
    """场景 D：买家版「待买家处理退货」匹配卖家版退货单号 → 提醒店主去买家版填单号。

    走独立 webhook（WECOM_WEBHOOK_URL_BUYER）。每轮都推，不去重不限额。
    """
    decisions = match_d(buyer_refunds, seller_refunds)
    log.info("D candidates: %d", len(decisions))

    n = 0
    for d in decisions:
        r = d.buyer_refund
        payload = DPushPayload(
            refund_no=r.refund_no,
            shop_name=r.shop_name,
            item_title_first10=(r.item_title or "")[:10],
            item_sku_title=r.item_sku_title,
            customer_name=r.customer_name,
            customer_phone=r.customer_phone,
            return_tracking_no=d.return_tracking_no,
        )
        msg = render_d(payload)
        if dry_run:
            log.info("[dry-run] D %s shop=%s tracking=%s",
                     r.refund_no, r.shop_name, d.return_tracking_no)
            continue
        try:
            wecom.send_markdown(msg, webhook_url=WECOM_WEBHOOK_URL_BUYER)
            n += 1
        except Exception as e:
            log.exception("D push failed %s: %s", r.refund_no, e)
    return n


def _load_d_filled() -> set[str]:
    """已自动填过单号的买家版 refund_no（幂等用）。"""
    with get_conn() as conn:
        return {row["refund_id"]
                for row in conn.execute(
                    "SELECT refund_id FROM pushed_records WHERE scenario = 'D_FILLED'")}


def _autofill_d(buyer_refunds, seller_refunds, dry_run: bool) -> int:
    """场景 D 自动填单号：唯一匹配 → 自动提交；多笔 → 转人工提醒。

    受 D_AUTOFILL_ENABLED 控制（run 里判断）。走买家版 webhook。
    幂等：已 D_FILLED 的跳过。dry_run 不提交、不写库。
    """
    autofills, ambiguous = classify_d(buyer_refunds, seller_refunds)
    log.info("D autofill: %d unique, %d ambiguous", len(autofills), len(ambiguous))

    # 多笔 → 转人工
    for amb in ambiguous:
        r = amb.buyer_refund
        lines = [f"⚠️ **买家版待填单号（多个候选，请手动填）**",
                 f"> 供货商：{r.shop_name or '—'}",
                 f"> 商品：{(r.item_title or '')[:10]}　规格：{r.item_sku_title or '—'}",
                 f"> 收件人：{r.customer_name or '—'} / {r.customer_phone or '—'}",
                 f"> 退款编号：`{r.refund_no}`",
                 f"> 候选退货单号（同一客户多笔，需你判断）："]
        for c in amb.candidates:
            lines.append(f">   · `{c.tracking_no}`（{(c.seller_item_title or '')[:14]}）")
        msg = "\n".join(lines)
        if dry_run:
            log.info("[dry-run] D-ambiguous %s shop=%s candidates=%d",
                     r.refund_no, r.shop_name, len(amb.candidates))
        else:
            try:
                wecom.send_markdown(msg, webhook_url=WECOM_WEBHOOK_URL_BUYER)
            except Exception as e:
                log.exception("D-ambiguous notify failed %s: %s", r.refund_no, e)

    if not autofills:
        return 0

    # 唯一 → 自动填
    already = _load_d_filled()
    express_map: dict[int, str] = {}
    if not dry_run:
        try:
            express_map = buyer_client._express_id_to_company()
        except Exception as e:
            log.exception("拉承运商枚举失败，本轮 D 自动填跳过: %s", e)
            wecom.send_alert(f"D 自动填：承运商枚举拉取失败，已跳过本轮：{e}")
            return 0

    n = 0
    for af in autofills:
        r = af.buyer_refund
        if r.refund_no in already:
            continue
        # 缺承运商信息 → 无法安全自动，降级人工
        company = express_map.get(af.return_express_type) if af.return_express_type else None
        if dry_run:
            log.info("[dry-run] D-autofill %s ← 单号 %s (expressType=%s company=%s)",
                     r.refund_no, af.return_tracking_no, af.return_express_type, company)
            continue
        if not af.return_express_type or not company:
            log.warning("D-autofill %s 无法解析承运商(expressType=%s)，转人工",
                        r.refund_no, af.return_express_type)
            try:
                wecom.send_markdown(
                    f"⚠️ **买家版待填单号（承运商待确认，请手动填）**\n"
                    f"> 供货商：{r.shop_name or '—'}\n"
                    f"> 退货单号：`{af.return_tracking_no}`\n"
                    f"> 退款编号：`{r.refund_no}`",
                    webhook_url=WECOM_WEBHOOK_URL_BUYER)
            except Exception:
                pass
            continue
        if D_AUTOFILL_LIMIT and n >= D_AUTOFILL_LIMIT:
            log.info("D-autofill 达到灰度上限 %d，其余本轮不填", D_AUTOFILL_LIMIT)
            break
        try:
            buyer_client.submit_return_express(
                refund_no=r.refund_no,
                express_no=af.return_tracking_no,
                express_type=af.return_express_type,
                express_company=company,
            )
            _record_push(r.refund_no, "D_FILLED",
                         f"{company} {af.return_tracking_no} → {r.refund_no}", None)
            n += 1
            # 审计消息
            wecom.send_markdown(
                f"✅ **已自动填入退货单号**\n"
                f"> 供货商：{r.shop_name or '—'}\n"
                f"> 商品：{(r.item_title or '')[:10]}　规格：{r.item_sku_title or '—'}\n"
                f"> 收件人：{r.customer_name or '—'} / {r.customer_phone or '—'}\n"
                f"> 物流：{company}　单号：`{af.return_tracking_no}`\n"
                f"> 退款编号：`{r.refund_no}`",
                webhook_url=WECOM_WEBHOOK_URL_BUYER)
        except Exception as e:
            log.exception("D-autofill 提交失败 %s: %s", r.refund_no, e)
            try:
                wecom.send_alert(f"D 自动填失败 refund={r.refund_no} 单号={af.return_tracking_no}：{e}")
            except Exception:
                pass
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
    decisions = evaluate(pending, logistics, started)
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

    # 8. 买家版：抓一次（C/D 共用），推 C（临期）；D 按开关走自动填或纯提醒
    buyer_refunds = _fetch_buyer_refunds()
    pushed_c = pushed_d = 0
    if buyer_refunds is not None:
        pushed_c = _push_buyer_c(buyer_refunds, dry_run)
        log.info("C pushed: %d", pushed_c)
        if D_AUTOFILL_ENABLED:
            pushed_d = _autofill_d(buyer_refunds, pending, dry_run)
            log.info("D autofilled: %d", pushed_d)
        else:
            pushed_d = _push_d(buyer_refunds, pending, dry_run)
            log.info("D pushed: %d", pushed_d)
        # 买家版巡检心跳：每轮固定发一条，C/D 都 0 也发
        _send_buyer_heartbeat(buyer_refunds, pushed_c, pushed_d, started, dry_run)

    pushed_count = pushed_urgent + pushed_b + pushed_c + pushed_d

    # 9. 早报（仅 09:00 那轮）
    if daily_report and not dry_run:
        try:
            from src.report.daily import build_and_send
            build_and_send(pushed_this_run=pushed_count, snapshot_at=snapshot_at)
        except Exception as e:
            log.exception("daily report failed: %s", e)

    # 10. run_log
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO run_log(started_at, finished_at, ok, note) VALUES (?,?,?,?)",
            (snapshot_at, datetime.now(TIMEZONE).isoformat(), 1,
             f"refunds={len(refunds)} pending={len(pending)} "
             f"urgent={pushed_urgent} b={pushed_b} c={pushed_c} d={pushed_d}"),
        )

    log.info("=== run done urgent=%d B=%d C=%d D=%d ===",
             pushed_urgent, pushed_b, pushed_c, pushed_d)
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
