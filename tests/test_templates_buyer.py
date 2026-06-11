"""验证买家版推送模板：C（临期，显示真实客户）+ D（待填退货单号）。"""
from __future__ import annotations

from src.notify.templates import BuyerPushPayload, DPushPayload, render_c, render_d


def test_render_c_shows_customer_not_owner():
    p = BuyerPushPayload(
        refund_no="R1", shop_name="供货商店",
        item_title_first10="北极狐 26春夏男",
        item_sku_title="藏青色;2XL", hours_left=22.5,
        customer_name="张客户", customer_phone="13800138000",
        operate_status_str="商家同意退货，请退回商品",
    )
    out = render_c(p)
    assert "剩余 22.5h" in out
    assert "张客户" in out and "13800138000" in out
    assert "商家同意退货，请退回商品" in out
    assert "R1" in out


def test_render_c_handles_missing_customer():
    p = BuyerPushPayload(
        refund_no="R1", shop_name="店",
        item_title_first10="商品", item_sku_title="黑;M", hours_left=5.0,
    )
    out = render_c(p)
    assert "收件人：— / —" in out


def test_render_d_includes_tracking():
    p = DPushPayload(
        refund_no="R1", shop_name="供货商店",
        item_title_first10="日版TN*F巅峰透",
        item_sku_title="黑色;L",
        customer_name="李客户", customer_phone="13400134000",
        return_tracking_no="772066336486325",
    )
    out = render_d(p)
    assert "772066336486325" in out
    assert "复制此单号去买家版填写" in out
    assert "李客户" in out and "13400134000" in out
    assert "供货商店" in out
    assert "退款编号" not in out   # 已去掉
