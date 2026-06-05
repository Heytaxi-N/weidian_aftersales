"""每日早报：09:00 跑完 runner 主流程后调用 build_and_send()。"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from src.config import TIMEZONE
from src.db import get_conn
from src.notify import wecom
from src.notify.templates import DailyReportStats, render_daily
from src.rules.engine import (
    PENDING_STATUSES,
    TIER_URGENT_HOURS,
    TIER_WARN_HOURS,
)

log = logging.getLogger(__name__)


def _window_bounds(now: datetime) -> tuple[datetime, datetime]:
    today_9 = datetime.combine(now.date(), time(9, 0), tzinfo=TIMEZONE)
    yesterday_9 = today_9 - timedelta(days=1)
    return yesterday_9, today_9


def build_stats(*, now: datetime, snapshot_at: str, pushed_this_run: int) -> DailyReportStats:
    """统计口径见 plan：
       - 昨日推送 = pushed_at in [昨天9点, 今天9点)
       - 已处理 = 这些 refund_id 在 today snapshot 中不再属于 PENDING_STATUSES
       - 仍未处理 = 反之
       - 今日新增待办 = 在 today snapshot 中且不在 yesterday snapshot 中的 refund_id
    """
    y9, t9 = _window_bounds(now)

    with get_conn() as conn:
        # 1. 昨日推送的 refund_ids（去重）
        rows = conn.execute(
            "SELECT DISTINCT refund_id FROM pushed_records WHERE pushed_at >= ? AND pushed_at < ?",
            (y9.isoformat(), t9.isoformat()),
        ).fetchall()
        yesterday_pushed = [r["refund_id"] for r in rows]

        # 2. 当前快照里仍 pending 的 refund_ids（用本次 snapshot_at）
        rows = conn.execute(
            "SELECT refund_id, status, deadline_at FROM order_snapshots WHERE snapshot_at = ?",
            (snapshot_at,),
        ).fetchall()
        cur_pending = {r["refund_id"]: r for r in rows if r["status"] in PENDING_STATUSES}

        # 3. 上一份快照（前一天 21:00 那次）的 refund_ids — 用 distinct most-recent before snapshot_at
        rows = conn.execute(
            """SELECT DISTINCT refund_id FROM order_snapshots
               WHERE snapshot_at < ? AND snapshot_at >= ?""",
            (snapshot_at, (y9 - timedelta(days=1)).isoformat()),
        ).fetchall()
        prev_refunds = {r["refund_id"] for r in rows}

    handled, still_pending = [], []
    for rid in yesterday_pushed:
        if rid in cur_pending:
            row = cur_pending[rid]
            hours_left: float | None = None
            scenarios: list[str] = []
            if row["deadline_at"]:
                try:
                    deadline = datetime.fromisoformat(row["deadline_at"])
                    hours_left = (deadline - now).total_seconds() / 3600
                    if hours_left <= TIER_URGENT_HOURS:
                        scenarios.append("A2")
                    elif hours_left <= TIER_WARN_HOURS:
                        scenarios.append("A")
                except ValueError:
                    pass
            still_pending.append((rid, hours_left, scenarios))
        else:
            handled.append(rid)

    new_today = len(set(cur_pending.keys()) - prev_refunds)

    return DailyReportStats(
        date_label=now.strftime("%m-%d"),
        pushed_yesterday=len(yesterday_pushed),
        handled=len(handled),
        still_pending=still_pending,
        new_today=new_today,
        pushed_this_run=pushed_this_run,
    )


def build_and_send(*, pushed_this_run: int, snapshot_at: str) -> None:
    now = datetime.now(TIMEZONE)
    stats = build_stats(now=now, snapshot_at=snapshot_at, pushed_this_run=pushed_this_run)
    text = render_daily(stats)
    log.info("daily report: yesterday=%d handled=%d pending=%d new=%d",
             stats.pushed_yesterday, stats.handled, len(stats.still_pending), stats.new_today)
    wecom.send_markdown(text)
