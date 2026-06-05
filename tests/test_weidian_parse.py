from __future__ import annotations

import json
from pathlib import Path

from src.weidian.client import parse_refund_list_response, _status_from_str

FIXTURES = Path(__file__).parent / "fixtures"


def _load_tab(tab_type: int) -> dict:
    # 真实 dump 中第一份 tab=N 的样本
    debug = Path(__file__).resolve().parent.parent / "data" / "debug"
    for f in sorted(debug.glob("*refundSearchList*.json")):
        body = json.loads(f.read_text())
        items = (body.get("result") or {}).get("list") or []
        if not items:
            continue
        status = items[0].get("refundStatusStr", "")
        if tab_type == 5 and "申请" in status:
            return body
        if tab_type == 6 and ("提交退货" in status or "待商家收货" in status or "退货物流" in status):
            return body
    raise AssertionError(f"no fixture for tabType={tab_type} in data/debug")


def test_parse_tab5_pending_action():
    body = _load_tab(5)
    records = parse_refund_list_response(body, "待商家处理")
    assert len(records) > 0
    r = records[0]
    assert r.refund_id and r.refund_id.isdigit()
    assert r.order_id and r.order_id.isdigit()
    assert r.deadline_at is not None
    assert r.buyer_name
    assert r.buyer_phone
    assert r.refund_type in ("退款退货", "仅退款")
    assert r.status == "待商家处理"
    assert r.item_title
    assert r.detail_url and r.refund_id in r.detail_url


def test_parse_tab6_pending_receive():
    body = _load_tab(6)
    records = parse_refund_list_response(body, "待商家收货")
    assert len(records) > 0
    r = records[0]
    assert r.status == "待商家收货"
    # 列表接口里没有运单号，应当为 None（详情接口接管）
    assert r.return_tracking_no is None


def test_status_uses_tab_label_authoritatively():
    """tab=6 列表里 refundStatusStr 末尾仍写"待商家处理"（描述商家应做事），
       但真实节点是"待商家收货"。一律以 tab 为准。"""
    assert _status_from_str("买家申请退货，待商家处理", "待商家处理") == "待商家处理"
    assert _status_from_str("买家提交退货物流，待商家处理", "待商家收货") == "待商家收货"
    assert _status_from_str("", "待商家收货") == "待商家收货"


def test_status_uses_tab_label_when_status_str_misleading():
    """tab=6 的 refundStatusStr 也写 '待商家处理'，但 tab 才是真状态。
       规则引擎依赖 status 区分 PENDING_MERCHANT_RECEIVE — 必须返回 tab 标签。"""
    body = _load_tab(6)
    records = parse_refund_list_response(body, "待商家收货")
    assert all(r.status == "待商家收货" for r in records), \
        f"got unique statuses: {set(r.status for r in records)}"
