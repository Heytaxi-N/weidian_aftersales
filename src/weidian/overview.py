"""微店退款 4-tab 计数总览。

接口（抓包确认）：
  GET https://thor.weidian.com/refundplatform/seller.refundSearchCount/1.0
      ?param={}&wdtoken=<cookie>&_=<ms>

响应：
  {
    "waitSellerHandleRefundCount": 15,   // 待商家处理
    "waitSellerHandleGoodCount":   96,   // 待商家收货
    "waitBuyerCount":              21,   // 待买家处理
    "waitCustomerCount":            1,   // 客服介入
    "waitSellerCount":            111    // 上面 5/6 两个之和（不展示，仅校验）
  }
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import httpx

from src.weidian.client import _load_cookies_and_token, API_BASE

log = logging.getLogger(__name__)

COUNT_URL = f"{API_BASE}/seller.refundSearchCount/1.0"


@dataclass
class TabCounts:
    wait_seller_handle: int       # 待商家处理（tab=5）
    wait_seller_receive: int      # 待商家收货（tab=6）
    wait_buyer: int               # 待买家处理
    wait_customer: int            # 客服介入


def fetch_counts() -> TabCounts:
    cookies, wdtoken = _load_cookies_and_token()
    params = {
        "param": "{}",
        "wdtoken": wdtoken,
        "_": str(int(time.time() * 1000)),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Referer": "https://d.weidian.com/",
    }
    with httpx.Client(cookies=cookies, headers=headers) as c:
        r = c.get(COUNT_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    status = data.get("status") or {}
    if status.get("code") != 0:
        raise RuntimeError(f"refundSearchCount returned {status}")
    result = data.get("result") or {}
    return TabCounts(
        wait_seller_handle=int(result.get("waitSellerHandleRefundCount") or 0),
        wait_seller_receive=int(result.get("waitSellerHandleGoodCount") or 0),
        wait_buyer=int(result.get("waitBuyerCount") or 0),
        wait_customer=int(result.get("waitCustomerCount") or 0),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    c = fetch_counts()
    print(json.dumps(c.__dict__, indent=2, ensure_ascii=False))
