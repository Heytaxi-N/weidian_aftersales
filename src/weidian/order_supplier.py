"""按 orderId 批量查商品对应的合作供货商。

接口（抓包确认）：
  POST https://thor.weidian.com/tradeview/seller.getOrderListForPC/1.0
  form-encoded: param=<JSON>&wdtoken=<cookie>

  param 关键字段：
    - orderIdList: 逗号或空格分隔的 order_id 字符串（用空格更稳）
    - statusList: [] 表示不限状态
    - pageSize: 单批最多 20
    - 其他必填空字段见 _build_param()

响应：
  result.orderList[].itemList[].contactInfo[0].shopName  ← 供货商名
                                              .telephone ← 供货商电话
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from src.weidian.client import _load_cookies_and_token

log = logging.getLogger(__name__)

URL = "https://thor.weidian.com/tradeview/seller.getOrderListForPC/1.0"
# 注意：orderIdList 字段名虽然带 List，实际只接受单个 ID（多 ID 任意分隔符都报 10004）。
# 所以是 N 个订单 N 次 POST，按使用上一般是 ≤10 笔候选，可接受。


def _build_param(order_id: str) -> dict:
    """构造 getOrderListForPC 的 param，按抓包形态填空字段。"""
    return {
        "listType": 0,
        "pageNum": 0,
        "pageSize": 20,
        "statusList": [],
        "refundStatusList": [],
        "channel": "pc",
        "topOrderType": 0,
        "shipRole": 0,
        "orderIdList": order_id,
        "itemTitle": "",
        "buyerName": "",
        "timeSearch": {},
        "orderBizType": "",
        "promotionType": "",
        "wttSearchConditionReqVO": {
            "helpSellerId": "", "groupId": "",
            "beginParticipateId": "", "endParticipateId": "",
            "excludeHelpSeller": False,
        },
        "shipType": "",
        "newGhSearchSellerRole": "7",
        "memberLevel": "all",
        "orderSpecialType": "",
        "repayStatus": "2",
        "pushStatus": "1",
        "bSellerId": "",
        "itemSource": "",
        "shipper": "",
        "nSellerName": "",
        "partnerName": "",
        "noteSearchCondition": {"buyerNote": ""},
        "specialOrderSearchCondition": {
            "notShowGroupUnsuccess": 0, "notShowFxOrder": 0,
            "notShowUnRepayOrder": 0, "notShowBuyerRepayOrder": 0,
            "showAllPeriodOrder": 0, "notShowTencentShopOrder": 0,
            "notShowWithoutTimelinessOrder": 0,
        },
        "orderType": 4,
    }


def _call_one(client: httpx.Client, wdtoken: str, order_id: str) -> dict:
    """单订单查询。"""
    param = _build_param(order_id)
    data = {
        "param": json.dumps(param, ensure_ascii=False, separators=(",", ":")),
        "wdtoken": wdtoken,
        "_": str(int(time.time() * 1000)),
    }
    r = client.post(URL, data=data, timeout=15)
    r.raise_for_status()
    body = r.json()
    status = body.get("status") or {}
    if status.get("code") != 0:
        raise RuntimeError(f"getOrderListForPC[{order_id}]: code={status.get('code')} msg={status.get('message')}")
    return body


def fetch_suppliers(order_ids: list[str]) -> dict[tuple[str, str], str]:
    """返回 {(order_id, item_id): supplier_name}。

    单 order_id 一次 POST（接口不接受批量）。对 N 笔候选订单要 N 次 HTTP。
    """
    out: dict[tuple[str, str], str] = {}
    unique = list(dict.fromkeys(str(o) for o in order_ids if o))
    if not unique:
        return out

    cookies, wdtoken = _load_cookies_and_token()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Referer": "https://d.weidian.com/",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    with httpx.Client(cookies=cookies, headers=headers) as c:
        for i, oid in enumerate(unique, 1):
            try:
                body = _call_one(c, wdtoken, oid)
            except Exception as e:
                log.warning("supplier fetch %s failed: %s", oid, e)
                continue
            for o in (body.get("result") or {}).get("orderList") or []:
                got_oid = str(o.get("orderId") or "")
                for it in o.get("itemList") or []:
                    iid = str(it.get("itemId") or "")
                    contact = (it.get("contactInfo") or [{}])[0]
                    name = (contact.get("shopName") or "").strip()
                    if got_oid and iid and name:
                        out[(got_oid, iid)] = name
            if i % 5 == 0 or i == len(unique):
                log.info("supplier fetched %d/%d (cum mappings=%d)", i, len(unique), len(out))
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python -m src.weidian.order_supplier <orderId> [<orderId> ...]")
        sys.exit(1)
    m = fetch_suppliers(sys.argv[1:])
    for (oid, iid), name in m.items():
        print(f"  ({oid}, item={iid}) -> {name}")
    print(f"\n共 {len(m)} 条映射")
