from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from src.notify.templates import PushPayload

STATUS_PENDING_MERCHANT_ACTION = "待商家处理"
STATUS_PENDING_MERCHANT_RECEIVE = "待商家收货"
PENDING_STATUSES = {STATUS_PENDING_MERCHANT_ACTION, STATUS_PENDING_MERCHANT_RECEIVE}

TIER_URGENT_HOURS = 24.0       # A2
TIER_WARN_HOURS = 48.0         # A
SIGNED_THRESHOLD_DAYS = 2      # B


@dataclass
class RefundRecord:
    """规则引擎的输入：抓取到的一笔待处理退款。"""
    refund_id: str
    order_id: str
    refund_type: str
    status: str
    deadline_at: datetime | None
    buyer_name: str | None = None
    buyer_phone: str | None = None
    receiver_name: str | None = None
    receiver_phone: str | None = None
    item_title: str | None = None
    return_tracking_no: str | None = None
    return_express_type: int | None = None   # 微店内部承运商 ID，调 trace 接口要用
    detail_url: str | None = None


@dataclass
class LogisticsInfo:
    tracking_no: str
    signed_at: datetime | None
    screenshot_path: str | None = None
    carrier: str | None = None
    trace_text: str | None = None   # 可选：轨迹文本（用于嵌入推送消息）


@dataclass
class Decision:
    """规则判定结果：一个 refund_id 该推哪些场景。"""
    refund: RefundRecord
    scenarios: list[str] = field(default_factory=list)
    hours_left: float | None = None
    logistics: LogisticsInfo | None = None


def _classify_time(hours_left: float | None) -> str | None:
    if hours_left is None:
        return None
    if hours_left <= TIER_URGENT_HOURS:
        return "A2"
    if hours_left <= TIER_WARN_HOURS:
        return "A"
    return None


def evaluate(
    refunds: Iterable[RefundRecord],
    logistics_by_tracking: dict[str, LogisticsInfo],
    already_pushed: set[tuple[str, str]],
    now: datetime,
) -> list[Decision]:
    """对每个 refund 计算应推送的场景集合（合并 + 去重）。

    - refunds: 当前快照中所有待商家状态的退款
    - logistics_by_tracking: tracking_no -> 查询结果
    - already_pushed: 数据库里已有的 (refund_id, scenario) 元组集合
    - now: 当前时间
    """
    decisions: list[Decision] = []
    for r in refunds:
        if r.status not in PENDING_STATUSES:
            continue

        hours_left: float | None = None
        if r.deadline_at is not None:
            hours_left = (r.deadline_at - now).total_seconds() / 3600.0

        scenarios: list[str] = []

        time_scn = _classify_time(hours_left)
        if time_scn and (r.refund_id, time_scn) not in already_pushed:
            scenarios.append(time_scn)

        logistics: LogisticsInfo | None = None
        if (
            r.status == STATUS_PENDING_MERCHANT_RECEIVE
            and r.return_tracking_no
            and r.return_tracking_no in logistics_by_tracking
        ):
            logistics = logistics_by_tracking[r.return_tracking_no]
            if (
                logistics.signed_at is not None
                and (now - logistics.signed_at) >= timedelta(days=SIGNED_THRESHOLD_DAYS)
                and (r.refund_id, "B") not in already_pushed
            ):
                scenarios.append("B")

        if scenarios:
            decisions.append(
                Decision(refund=r, scenarios=scenarios, hours_left=hours_left, logistics=logistics)
            )
    return decisions


def to_payload(d: Decision) -> PushPayload:
    r = d.refund
    return PushPayload(
        refund_id=r.refund_id,
        order_id=r.order_id,
        refund_type=r.refund_type,
        status=r.status,
        deadline_at=r.deadline_at,
        hours_left=d.hours_left,
        scenarios=list(d.scenarios),
        buyer_name=r.buyer_name,
        buyer_phone=r.buyer_phone,
        receiver_name=r.receiver_name,
        receiver_phone=r.receiver_phone,
        item_title=r.item_title,
        return_tracking_no=r.return_tracking_no,
        signed_at=d.logistics.signed_at if d.logistics else None,
        screenshot_path=d.logistics.screenshot_path if d.logistics else None,
        detail_url=r.detail_url,
        carrier=d.logistics.carrier if d.logistics else None,
        trace_text=d.logistics.trace_text if d.logistics else None,
    )
