from __future__ import annotations

import re
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
C_TIMEOUT_SECONDS = 25 * 3600  # C：买家版「待买家处理退货」剩余 ≤ 25h 触发


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
    item_id: str | None = None               # 商品 ID，匹配供货商时用
    return_tracking_no: str | None = None
    return_express_type: int | None = None   # 微店内部承运商 ID，调 trace 接口要用
    detail_url: str | None = None
    supplier_name: str | None = None         # 合作供货商（按 order_id × item_id 查 getOrderListForPC）


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
    now: datetime,
) -> list[Decision]:
    """对每个 refund 计算应推送的场景集合（合并）。

    - refunds: 当前快照中所有待商家状态的退款
    - logistics_by_tracking: tracking_no -> 查询结果
    - now: 当前时间

    A/A2/B 均不在引擎层去重；B 的重复轰炸由 runner 的每日配额兜底。
    """
    decisions: list[Decision] = []
    for r in refunds:
        if r.status not in PENDING_STATUSES:
            continue

        hours_left: float | None = None
        if r.deadline_at is not None:
            hours_left = (r.deadline_at - now).total_seconds() / 3600.0

        scenarios: list[str] = []

        # A/A2 临期紧急：不做 dedup —— 只要还在 ≤48h 区间，每轮都推，
        # 保证紧迫单子持续在店主眼前直到处理掉
        time_scn = _classify_time(hours_left)
        if time_scn:
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


# === 买家版（场景 C）===

@dataclass
class BuyerDecision:
    """买家版规则判定结果。"""
    refund: "BuyerRefundRecord"  # forward ref，避免循环 import
    scenarios: list[str] = field(default_factory=list)
    hours_left: float | None = None


def evaluate_buyer(refunds: Iterable) -> list[BuyerDecision]:
    """对买家版退款判定 C：「待买家处理退货」且剩余倒计时 ≤ 25h。

    倒计时由微店服务端权威给出（refundCard.autoCountdownInSecond），本地不算时间差。
    refunds 元素需带 countdown_seconds 字段（已被 buyer_client.enrich_refunds 补全）。
    """
    out: list[BuyerDecision] = []
    for r in refunds:
        if getattr(r, "refund_status_str", None) != "待买家处理退货":
            continue
        cd = getattr(r, "countdown_seconds", None)
        if cd is None:
            continue
        if cd <= C_TIMEOUT_SECONDS:
            out.append(BuyerDecision(
                refund=r,
                scenarios=["C"],
                hours_left=cd / 3600.0,
            ))
    return out


# === 场景 D：卖家版退货单号 ↔ 买家版售后 关联 ===

def _normalize_phone(s: str | None) -> str:
    """归一化手机号用于跨侧匹配：去非数字 + 去前导国家码 86。"""
    if not s:
        return ""
    digits = re.sub(r"\D", "", str(s))
    if len(digits) > 11 and digits.startswith("86"):
        digits = digits[2:]
    return digits


@dataclass
class DDecision:
    """D 判定：一笔买家版「待买家处理退货」匹配到卖家版退货单号。"""
    buyer_refund: "BuyerRefundRecord"   # forward ref
    return_tracking_no: str             # 来自卖家版「待商家收货」
    seller_refund_id: str


def match_d(buyer_refunds: Iterable, seller_refunds: Iterable) -> list[DDecision]:
    """按客户手机号关联：买家版「待买家处理退货」↔ 卖家版「待商家收货」（有退货单号）。

    - buyer_refunds: BuyerRefundRecord 列表，需带 customer_phone（buyer_client.enrich_refunds 补全）
    - seller_refunds: RefundRecord 列表（run() 里的 pending）

    只对买家侧「待买家处理退货」产出 —— 店主已填过单号的（买家侧状态已流转）自动不推。
    卖家侧索引同时用 buyer_phone 和 receiver_phone（实测两者都可能是客户号）。
    """
    seller_idx: dict[str, "RefundRecord"] = {}
    for s in seller_refunds:
        if getattr(s, "status", None) != STATUS_PENDING_MERCHANT_RECEIVE:
            continue
        if not getattr(s, "return_tracking_no", None):
            continue
        for ph in (getattr(s, "buyer_phone", None), getattr(s, "receiver_phone", None)):
            n = _normalize_phone(ph)
            if n:
                seller_idx.setdefault(n, s)

    out: list[DDecision] = []
    for b in buyer_refunds:
        if getattr(b, "refund_status_str", None) != "待买家处理退货":
            continue
        n = _normalize_phone(getattr(b, "customer_phone", None))
        if not n:
            continue
        s = seller_idx.get(n)
        if s is not None:
            out.append(DDecision(
                buyer_refund=b,
                return_tracking_no=s.return_tracking_no,
                seller_refund_id=s.refund_id,
            ))
    return out
