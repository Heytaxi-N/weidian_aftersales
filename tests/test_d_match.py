"""验证场景 D：买家版「待买家处理退货」↔ 卖家版「待商家收货」按客户手机号关联。"""
from __future__ import annotations

from src.rules.engine import _normalize_phone, classify_d, match_d
from src.weidian.buyer_client import BuyerRefundRecord
from src.weidian.client import RefundRecord


def _buyer(refund_no="B1", status="待买家处理退货", customer_phone="13800138000") -> BuyerRefundRecord:
    return BuyerRefundRecord(
        refund_no=refund_no, order_id="O" + refund_no, order_key="K" + refund_no,
        shop_name="供货商店", shop_id="1", item_title="商品", item_sku_title="黑;M",
        refund_status_str=status, add_time=None, update_time=None,
        customer_phone=customer_phone, customer_name="客户",
    )


def _seller(refund_id="S1", status="待商家收货", tracking="SF123",
            buyer_phone="13800138000", receiver_phone=None,
            express_type=10, item_title="卖家商品") -> RefundRecord:
    return RefundRecord(
        refund_id=refund_id, order_id="O" + refund_id, refund_type="退货退款",
        status=status, deadline_at=None,
        buyer_phone=buyer_phone, receiver_phone=receiver_phone,
        return_tracking_no=tracking, return_express_type=express_type,
        item_title=item_title,
    )


# === _normalize_phone ===

def test_normalize_strips_spaces_and_dashes():
    assert _normalize_phone("138 0013 8000") == "13800138000"
    assert _normalize_phone("138-0013-8000") == "13800138000"


def test_normalize_strips_country_code():
    assert _normalize_phone("8613800138000") == "13800138000"
    assert _normalize_phone("+86 13800138000") == "13800138000"


def test_normalize_empty():
    assert _normalize_phone(None) == ""
    assert _normalize_phone("") == ""


# === match_d ===

def test_match_basic():
    out = match_d([_buyer()], [_seller()])
    assert len(out) == 1
    assert out[0].return_tracking_no == "SF123"
    assert out[0].seller_refund_id == "S1"
    assert out[0].buyer_refund.refund_no == "B1"


def test_match_via_receiver_phone():
    """卖家侧客户号在 receiver_phone 而非 buyer_phone 时也能匹配。"""
    s = _seller(buyer_phone="19900000000", receiver_phone="13800138000")
    out = match_d([_buyer(customer_phone="13800138000")], [s])
    assert len(out) == 1


def test_match_with_normalization():
    """两侧手机号格式不同（带空格/国家码）仍匹配。"""
    out = match_d(
        [_buyer(customer_phone="+86 138 0013 8000")],
        [_seller(buyer_phone="13800138000")],
    )
    assert len(out) == 1


def test_no_match_different_phone():
    out = match_d([_buyer(customer_phone="13800138000")],
                  [_seller(buyer_phone="13900139000")])
    assert out == []


def test_skip_buyer_wrong_status():
    """买家侧非「待买家处理退货」不产出（店主无需操作）。"""
    out = match_d([_buyer(status="待商家处理退货")], [_seller()])
    assert out == []


def test_skip_seller_wrong_status():
    """卖家侧非「待商家收货」不进索引。"""
    out = match_d([_buyer()], [_seller(status="待商家处理")])
    assert out == []


def test_skip_seller_no_tracking():
    """卖家侧无退货单号不进索引。"""
    out = match_d([_buyer()], [_seller(tracking=None)])
    assert out == []


def test_skip_buyer_no_customer_phone():
    out = match_d([_buyer(customer_phone=None)], [_seller()])
    assert out == []


def test_multiple_mixed():
    buyers = [
        _buyer(refund_no="B_hit", customer_phone="13800000001"),
        _buyer(refund_no="B_nomatch", customer_phone="13800000002"),
        _buyer(refund_no="B_wrongstatus", status="待商家处理退货", customer_phone="13800000003"),
        _buyer(refund_no="B_hit2", customer_phone="13800000004"),
    ]
    sellers = [
        _seller(refund_id="S_a", tracking="T1", buyer_phone="13800000001"),
        _seller(refund_id="S_b", tracking="T2", buyer_phone="13800000003"),  # 但买家侧状态不对
        _seller(refund_id="S_c", tracking="T3", buyer_phone="13800000004"),
        _seller(refund_id="S_d", tracking=None, buyer_phone="13800000002"),  # 无单号
    ]
    out = match_d(buyers, sellers)
    got = {(d.buyer_refund.refund_no, d.return_tracking_no) for d in out}
    assert got == {("B_hit", "T1"), ("B_hit2", "T3")}


# === classify_d：唯一 → 自动；多笔 → 转人工 ===

def test_classify_unique_goes_autofill():
    autofills, ambiguous = classify_d([_buyer()], [_seller(tracking="SF1", express_type=10)])
    assert len(autofills) == 1 and not ambiguous
    af = autofills[0]
    assert af.return_tracking_no == "SF1"
    assert af.return_express_type == 10


def test_classify_same_tracking_multiple_records_is_unique():
    """同一单号在卖家侧出现多条（buyer/receiver 双命中等）算一个 → 仍自动。"""
    sellers = [
        _seller(refund_id="S1", tracking="SF1", buyer_phone="13800138000",
                receiver_phone="13800138000"),  # 双命中同一单号
    ]
    autofills, ambiguous = classify_d([_buyer()], sellers)
    assert len(autofills) == 1 and not ambiguous


def test_classify_multiple_trackings_goes_ambiguous():
    """同手机号多个不同单号 → 转人工，候选齐全。"""
    sellers = [
        _seller(refund_id="S1", tracking="SF1", item_title="运动裤"),
        _seller(refund_id="S2", tracking="SF2", item_title="短袖"),
        _seller(refund_id="S3", tracking="SF3", item_title="皮肤衣"),
    ]
    autofills, ambiguous = classify_d([_buyer()], sellers)
    assert not autofills and len(ambiguous) == 1
    cands = ambiguous[0].candidates
    assert {c.tracking_no for c in cands} == {"SF1", "SF2", "SF3"}
    assert {c.seller_item_title for c in cands} == {"运动裤", "短袖", "皮肤衣"}


def test_classify_no_match_yields_nothing():
    autofills, ambiguous = classify_d(
        [_buyer(customer_phone="13800138000")],
        [_seller(buyer_phone="13911111111")],
    )
    assert not autofills and not ambiguous


def test_classify_skips_buyer_wrong_status():
    autofills, ambiguous = classify_d([_buyer(status="待商家处理退货")], [_seller()])
    assert not autofills and not ambiguous


def test_classify_unique_missing_express_type_still_autofill():
    """缺 express_type 不在 classify 层降级（由 runner 决定）；这里仍归 autofill 但 type=None。"""
    autofills, ambiguous = classify_d([_buyer()], [_seller(express_type=None)])
    assert len(autofills) == 1
    assert autofills[0].return_express_type is None


def test_classify_mixed_population():
    buyers = [
        _buyer(refund_no="B_uniq", customer_phone="13800000001"),
        _buyer(refund_no="B_ambi", customer_phone="13800000002"),
        _buyer(refund_no="B_none", customer_phone="13800000009"),
    ]
    sellers = [
        _seller(refund_id="Sa", tracking="T1", buyer_phone="13800000001"),
        _seller(refund_id="Sb1", tracking="T2", buyer_phone="13800000002"),
        _seller(refund_id="Sb2", tracking="T3", buyer_phone="13800000002"),
    ]
    autofills, ambiguous = classify_d(buyers, sellers)
    assert {a.buyer_refund.refund_no for a in autofills} == {"B_uniq"}
    assert {a.buyer_refund.refund_no for a in ambiguous} == {"B_ambi"}
