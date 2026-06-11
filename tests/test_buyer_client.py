"""验证买家版 client：SSR HTML 解析 + 详情接口字段映射。"""
from __future__ import annotations

import html
import json

import pytest

from src.weidian.buyer_client import (
    BuyerRefundRecord,
    _items_to_records,
    enrich_refunds,
    parse_refund_list_html,
    WeidianApiError,
    WeidianNotLoggedIn,
)


def _build_html(refunds: list[dict]) -> str:
    """把 list 包成微店 SSR 的 HTML 结构：
    <script id="__rocker-render-inject__" data-obj="<HTML-escaped JSON>">
    """
    payload = {
        "list": {
            "status": {"code": 0, "message": "OK"},
            "result": refunds,
            "traceId": "trace-123",
        },
        "traceId": "trace-123",
        "themeCSSDom": None,
    }
    raw_json = json.dumps(payload, ensure_ascii=False)
    escaped = html.escape(raw_json, quote=True)
    return (
        '<!doctype html><html><head></head><body>'
        f'<script id="__rocker-render-inject__" data-obj="{escaped}"></script>'
        '</body></html>'
    )


def _sample_refund(refund_no="144115512907381021",
                   status="待买家处理退货",
                   shop_name="测试店",
                   item_title="测试商品 ABC",
                   item_sku="黑色;XL") -> dict:
    return {
        "addTime": "2026-06-10 20:55:28",
        "updateTime": "2026-06-11 11:25:59",
        "order_id": "848826601374407",
        "order_key": "k-abcdef",
        "shopInfo": {"seller_id": "1695880621", "shop_name": shop_name},
        "refund_info": {
            "refund_no": refund_no,
            "refund_status_str": status,
            "order_stage": 1,
        },
        "items": [{"item_title": item_title, "item_sku_title": item_sku, "item_id": 999}],
    }


def test_parse_refund_list_extracts_fields():
    html_text = _build_html([_sample_refund()])
    records = parse_refund_list_html(html_text)
    assert len(records) == 1
    r = records[0]
    assert r.refund_no == "144115512907381021"
    assert r.shop_name == "测试店"
    assert r.shop_id == "1695880621"
    assert r.item_title == "测试商品 ABC"
    assert r.item_sku_title == "黑色;XL"
    assert r.refund_status_str == "待买家处理退货"
    assert r.add_time is not None and r.add_time.year == 2026
    assert r.update_time is not None
    # 未补详情，倒计时等字段应为 None
    assert r.countdown_seconds is None
    assert r.receiver_name is None


def test_parse_refund_list_handles_multiple_records():
    html_text = _build_html([
        _sample_refund(refund_no="R1", status="待买家处理退货"),
        _sample_refund(refund_no="R2", status="待商家处理退货"),
        _sample_refund(refund_no="R3", status="待买家处理退货"),
    ])
    records = parse_refund_list_html(html_text)
    assert [r.refund_no for r in records] == ["R1", "R2", "R3"]
    assert [r.refund_status_str for r in records] == [
        "待买家处理退货", "待商家处理退货", "待买家处理退货",
    ]


def test_parse_refund_list_empty_result_is_ok():
    html_text = _build_html([])
    records = parse_refund_list_html(html_text)
    assert records == []


def test_parse_refund_list_missing_inject_script_raises():
    """页面结构变了或被重定向到登录页 → 抛错。"""
    bad_html = "<html><body>nothing useful</body></html>"
    with pytest.raises(WeidianApiError):
        parse_refund_list_html(bad_html)


def test_parse_refund_list_detects_login_redirect():
    """登录态丢失时页面包含登录关键字 → 抛 WeidianNotLoggedIn。"""
    login_html = "<html><body><h1>请登录后再访问</h1></body></html>"
    with pytest.raises(WeidianNotLoggedIn):
        parse_refund_list_html(login_html)


def test_items_to_records_directly():
    """frontRefundList API 返回的是 raw list（无 SSR 的 list/status 包装）。
    复用 _items_to_records 也能正常映射。"""
    raw = [_sample_refund(refund_no=f"R{i}") for i in range(3)]
    records = _items_to_records(raw)
    assert [r.refund_no for r in records] == ["R0", "R1", "R2"]
    assert all(r.shop_name == "测试店" for r in records)


def test_parse_refund_list_handles_missing_optional_fields():
    """addTime 缺失等情况：保持 None，不崩溃。"""
    sample = _sample_refund()
    sample["addTime"] = None
    sample["items"] = []   # 空 items
    sample["shopInfo"] = {}
    html_text = _build_html([sample])
    records = parse_refund_list_html(html_text)
    assert len(records) == 1
    r = records[0]
    assert r.add_time is None
    assert r.item_title == ""
    assert r.shop_name == ""


# === 详情接口字段映射 ===

def _mock_detail_response(countdown=599379, op_str="商家同意退货，请退回商品",
                          buyer_name="张三", buyer_phone="13800138000") -> dict:
    """模仿 thor.weidian.com/refundplatform/getRefundDetail 真实响应结构。"""
    return {
        "refundCard": {
            "autoCountdownInSecond": countdown,
            "operateStatusStr": op_str,
            "operateStatusDesc": "商家已同意您的退货申请",
            "autoCountdownAction": "{} 后系统自动关闭退款",
        },
        "refundBasicInfo": {
            "buyerName": buyer_name,
            "buyerPhone": buyer_phone,
            "buyerAddress": "山东省青岛市...",
            "addTime": "2026-06-10 20:55:28",
        },
    }


def test_enrich_refunds_populates_countdown_and_receiver(monkeypatch):
    from src.weidian import buyer_client
    r = BuyerRefundRecord(
        refund_no="R1", order_id="O1", order_key="K1",
        shop_name="店", shop_id="1", item_title="商品", item_sku_title="规格",
        refund_status_str="待买家处理退货", add_time=None, update_time=None,
    )
    monkeypatch.setattr(
        buyer_client, "fetch_refund_detail",
        lambda refund_no, client=None: _mock_detail_response(),
    )
    monkeypatch.setattr(
        buyer_client, "fetch_order_customer",
        lambda order_id, client=None: ("李客户", "13700137000"),
    )
    # 抑制 _load_cookies_and_token（避免读 storage_state.json）
    monkeypatch.setattr(buyer_client, "_load_cookies_and_token", lambda: ({}, ""))
    # 抑制 httpx.Client 真正建立连接（enrich 用上下文管理器但不会被 fetch 调用，因为 mock 了）
    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def close(self): pass
    monkeypatch.setattr(buyer_client.httpx, "Client", lambda **kw: _FakeClient())
    # 避免 sleep
    monkeypatch.setattr(buyer_client.time, "sleep", lambda s: None)

    enrich_refunds([r])
    assert r.countdown_seconds == 599379
    assert r.operate_status_str == "商家同意退货，请退回商品"
    assert r.receiver_name == "张三"           # 退款详情 = 店主自己
    assert r.receiver_phone == "13800138000"
    assert r.customer_name == "李客户"          # 订单详情 = 真实客户
    assert r.customer_phone == "13700137000"


def test_enrich_refunds_skips_non_buyer_pending_status(monkeypatch):
    """状态不是「待买家处理退货」的不调详情接口。"""
    from src.weidian import buyer_client
    calls = []
    monkeypatch.setattr(
        buyer_client, "fetch_refund_detail",
        lambda refund_no, client=None: calls.append(refund_no) or {},
    )
    monkeypatch.setattr(buyer_client, "_load_cookies_and_token", lambda: ({}, ""))

    r = BuyerRefundRecord(
        refund_no="R1", order_id="O1", order_key="K1",
        shop_name="店", shop_id="1", item_title="商品", item_sku_title="规格",
        refund_status_str="待商家处理退货",
        add_time=None, update_time=None,
    )
    enrich_refunds([r])
    assert calls == []   # 没调
    assert r.countdown_seconds is None


def test_enrich_refunds_swallows_per_item_errors(monkeypatch):
    """一笔详情失败不阻塞其他笔。"""
    from src.weidian import buyer_client

    def fake_detail(refund_no, client=None):
        if refund_no == "R_FAIL":
            raise RuntimeError("boom")
        return _mock_detail_response(countdown=int(refund_no[1:]) * 100)

    monkeypatch.setattr(buyer_client, "fetch_refund_detail", fake_detail)
    monkeypatch.setattr(buyer_client, "fetch_order_customer",
                        lambda order_id, client=None: ("客户", "13700137000"))
    monkeypatch.setattr(buyer_client, "_load_cookies_and_token", lambda: ({}, ""))
    class _FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def close(self): pass
    monkeypatch.setattr(buyer_client.httpx, "Client", lambda **kw: _FakeClient())
    monkeypatch.setattr(buyer_client.time, "sleep", lambda s: None)

    refunds = [
        BuyerRefundRecord(
            refund_no=rn, order_id="O", order_key="K",
            shop_name="店", shop_id="1", item_title="商品", item_sku_title="规格",
            refund_status_str="待买家处理退货",
            add_time=None, update_time=None,
        )
        for rn in ["R10", "R_FAIL", "R20"]
    ]
    enrich_refunds(refunds)
    assert refunds[0].countdown_seconds == 1000
    assert refunds[1].countdown_seconds is None   # 失败保持 None
    assert refunds[2].countdown_seconds == 2000
