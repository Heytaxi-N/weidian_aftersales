from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from jinja2 import Environment

env = Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)


@dataclass
class PushPayload:
    """规则引擎产出的一条合并后的推送决策。"""
    refund_id: str
    order_id: str
    refund_type: str            # 退货退款 / 仅退款
    status: str                 # 当前节点
    deadline_at: datetime | None
    hours_left: float | None    # None 表示不命中 A/A2
    scenarios: list[str]        # ["A"] / ["A2"] / ["B"] / ["A2","B"] ...
    buyer_name: str | None
    buyer_phone: str | None
    receiver_name: str | None = None
    receiver_phone: str | None = None
    item_title: str | None = None
    return_tracking_no: str | None = None
    signed_at: datetime | None = None
    screenshot_path: str | None = None
    detail_url: str | None = None


def _header(scenarios: list[str]) -> str:
    parts = []
    if "A2" in scenarios:
        parts.append("🚨 **紧急**")
    elif "A" in scenarios:
        parts.append("⏰ **临期**")
    if "B" in scenarios:
        parts.append("📦 **已签收待收货**")
    return " · ".join(parts) if parts else "📣 提醒"


PUSH_TEMPLATE = env.from_string("""\
{{ header }}
> 退款单：`{{ p.refund_id }}` (订单 #{{ p.order_id }})
> 买家：{{ p.buyer_name or '—' }} / {{ p.buyer_phone or '—' }}
> 申请类型：{{ p.refund_type or '—' }}
> 当前节点：<font color="warning">{{ p.status or '—' }}</font>
{% if p.hours_left is not none %}
> 剩余时间：<font color="{{ 'warning' if 'A2' in p.scenarios else 'comment' }}">{{ '%.1f' % p.hours_left }} 小时</font>
{% endif %}
{% if 'B' in p.scenarios %}

**📦 退货物流**
> 收件人：{{ p.receiver_name or '—' }} / {{ p.receiver_phone or '—' }}
> 商品：{{ p.item_title or '—' }}
> 运单号：`{{ p.return_tracking_no or '—' }}`
{% if p.signed_at %}
> 签收时间：{{ p.signed_at.strftime('%Y-%m-%d %H:%M') }}
{% endif %}
{% endif %}
{% if p.detail_url %}

[查看微店详情]({{ p.detail_url }})
{% endif %}
""")


def render_push(p: PushPayload) -> str:
    return PUSH_TEMPLATE.render(p=p, header=_header(p.scenarios)).strip()


@dataclass
class DailyReportStats:
    date_label: str          # "06-05"
    pushed_yesterday: int
    handled: int
    still_pending: list[tuple[str, float | None, list[str]]]  # (refund_id, hours_left, scenarios)
    new_today: int
    pushed_this_run: int


def _pending_line(rid: str, hours_left: float | None, scenarios: list[str]) -> str:
    tail = ""
    if hours_left is not None:
        urgency = ", 紧急" if "A2" in scenarios else ""
        tail = f" (剩 {hours_left:.1f}h{urgency})"
    return f">   · `{rid}`{tail}"


DAILY_TEMPLATE = env.from_string("""\
☀️ **售后早报 {{ s.date_label }}**

> 昨日推送：**{{ s.pushed_yesterday }}** 条
> 你已处理：<font color="info">**{{ s.handled }}** ✅</font>
> 仍未处理：<font color="warning">**{{ s.still_pending|length }}** ⚠️</font>
{{ pending_lines }}

> 今日新增待办：{{ s.new_today }} 条
> 本轮新推送：{{ s.pushed_this_run }} 条
""")


def render_daily(stats: DailyReportStats) -> str:
    pending_lines = "\n".join(_pending_line(rid, h, sc) for rid, h, sc in stats.still_pending)
    return DAILY_TEMPLATE.render(s=stats, pending_lines=pending_lines).strip()
