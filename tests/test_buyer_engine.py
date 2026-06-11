"""验证场景 C：买家版「待买家处理退货」剩余倒计时 ≤ 25h 触发。"""
from __future__ import annotations

from src.rules.engine import C_TIMEOUT_SECONDS, evaluate_buyer
from src.weidian.buyer_client import BuyerRefundRecord


def _mk(refund_no: str, status: str = "待买家处理退货",
        countdown: int | None = 10 * 3600) -> BuyerRefundRecord:
    return BuyerRefundRecord(
        refund_no=refund_no,
        order_id="O" + refund_no,
        order_key="K" + refund_no,
        shop_name="测试供货商",
        shop_id="1",
        item_title="商品",
        item_sku_title="黑;M",
        refund_status_str=status,
        add_time=None,
        update_time=None,
        countdown_seconds=countdown,
    )


def test_c_fires_when_countdown_under_25h():
    r = _mk("R1", countdown=23 * 3600)
    out = evaluate_buyer([r])
    assert len(out) == 1
    assert out[0].scenarios == ["C"]
    assert abs(out[0].hours_left - 23.0) < 0.01


def test_c_fires_at_exact_boundary():
    """边界：剩余正好等于 25h 时触发。"""
    r = _mk("R1", countdown=C_TIMEOUT_SECONDS)
    out = evaluate_buyer([r])
    assert len(out) == 1


def test_c_skips_when_countdown_over_25h():
    r = _mk("R1", countdown=48 * 3600)
    assert evaluate_buyer([r]) == []


def test_c_skips_for_other_statuses():
    r = _mk("R1", status="待商家处理退货", countdown=10 * 3600)
    assert evaluate_buyer([r]) == []


def test_c_skips_when_countdown_missing():
    """详情没拿到倒计时（None）— 防御性跳过，不推。"""
    r = _mk("R1", countdown=None)
    assert evaluate_buyer([r]) == []


def test_c_filters_multiple():
    """多笔混合：只挑符合条件的。"""
    refunds = [
        _mk("R_fire", countdown=10 * 3600),                          # 命中
        _mk("R_too_late", countdown=48 * 3600),                       # 倒计时大
        _mk("R_wrong_status", status="待商家处理退货", countdown=5 * 3600),  # 状态不对
        _mk("R_missing", countdown=None),                             # 缺数据
        _mk("R_fire2", countdown=24 * 3600),                          # 命中
    ]
    out = evaluate_buyer(refunds)
    fired = {d.refund.refund_no for d in out}
    assert fired == {"R_fire", "R_fire2"}
