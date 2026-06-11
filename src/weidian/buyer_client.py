"""
微店「买家版」退款列表抓取 —— 直调 weidian.com SSR + thor.weidian.com 详情接口。

接口（已抓包确认）：

  1. 退款列表（SSR HTML）
     GET https://weidian.com/user/order/refundList?type=5&spider_token=8ab3
     数据嵌在 <script id="__rocker-render-inject__" data-obj="{HTML-escaped JSON}">
     解析后 = {"list": {"status":{}, "result":[...], "traceId":"..."}, "traceId":"...", "themeCSSDom": ...}
     result[i] = {refund_info, shopInfo, items, order_id, order_key, addTime, updateTime, ...}

  2. 退款详情
     GET https://thor.weidian.com/refundplatform/getRefundDetail/1.0
         ?param={"refund_no": "...", "roleType": 1}
     result.refundCard.autoCountdownInSecond   ← C 触发用的"剩余秒数"
     result.refundCard.operateStatusStr        ← 如"商家同意退货，请退回商品"
     result.refundBasicInfo.{buyerName, buyerPhone, buyerAddress, ...}

复用卖家版的 cookie：_load_cookies_and_token() 加载 .weidian.com scope 的 cookies，
   买家版同样在 .weidian.com 下，可直接复用（已实测 buyer.order.list/1.1 返回 code:0）。
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import httpx

from src.config import TIMEZONE
from src.weidian.client import _load_cookies_and_token, WeidianNotLoggedIn, WeidianApiError

log = logging.getLogger(__name__)

BUYER_REFUND_LIST_API = "https://thor.weidian.com/refundplatform/buyer.frontRefundList/1.0"
BUYER_REFUND_LIST_URL = "https://weidian.com/user/order/refundList"   # SSR 后备方案（受 sid 绑定单店铺）
BUYER_REFUND_DETAIL_URL = "https://thor.weidian.com/refundplatform/getRefundDetail/1.0"
BUYER_ORDER_DETAIL_API = "https://thor.weidian.com/tradeview/buyer.getOrderDetailForApp/1.0"
BUYER_REFUND_DETAIL_PAGE_FMT = (
    "https://weidian.com/weidian-h5/aftersale/refund-detail.html"
    "?role=1&refund_num={refund_no}"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://weidian.com/",
    "Accept": "application/json, text/html, */*",
}

# 详情接口最小调用间隔，避免买家版接口限频
DETAIL_CALL_INTERVAL_SECONDS = 0.5

STATUS_PENDING_BUYER_RETURN = "待买家处理退货"


@dataclass
class BuyerRefundRecord:
    """买家版一笔退款。"""
    refund_no: str
    order_id: str
    order_key: str
    shop_name: str               # 供货商店名
    shop_id: str
    item_title: str
    item_sku_title: str          # 例 "藏青色;2XL"
    refund_status_str: str       # "待买家处理退货" / "待商家处理退货" / ...
    add_time: datetime | None    # 退款发起时间
    update_time: datetime | None # 状态变更时间
    # 走退款详情接口补全（懒加载）
    countdown_seconds: int | None = None     # autoCountdownInSecond（C 触发用）
    operate_status_str: str | None = None    # 如"商家同意退货，请退回商品"
    receiver_name: str | None = None         # 退款详情 buyerName（= 店主自己，少用）
    receiver_phone: str | None = None        # 退款详情 buyerPhone（= 店主自己）
    # 走订单详情接口补全：真实客户（代发收件人），用于 D 关联 + C/D 展示
    customer_name: str | None = None         # 订单 buyerInfo.name（收件人）
    customer_phone: str | None = None        # 订单 buyerInfo.telephone（收件人手机）


# <script id="__rocker-render-inject__" ... data-obj="<HTML-escaped JSON>" ...>
_INJECT_SCRIPT_RE = re.compile(
    r'<script[^>]*id="__rocker-render-inject__"[^>]*data-obj="([^"]+)"',
    re.DOTALL,
)


def _parse_dt(s: str | None) -> datetime | None:
    """微店时间戳 'YYYY-MM-DD HH:MM:SS' 转 aware datetime。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
    except ValueError:
        return None


def _items_to_records(items: list[dict]) -> list[BuyerRefundRecord]:
    """把买家版列表返回的 raw items 映射成 BuyerRefundRecord。

    SSR HTML 和 buyer.frontRefundList API 的元素结构完全一致，复用。
    """
    out: list[BuyerRefundRecord] = []
    for item in items:
        refund_info = item.get("refund_info") or {}
        shop_info = item.get("shopInfo") or {}
        sub_items = item.get("items") or []
        first_item = sub_items[0] if sub_items else {}

        out.append(BuyerRefundRecord(
            refund_no=str(refund_info.get("refund_no") or ""),
            order_id=str(item.get("order_id") or ""),
            order_key=str(item.get("order_key") or ""),
            shop_name=str(shop_info.get("shop_name") or ""),
            shop_id=str(shop_info.get("seller_id") or ""),
            item_title=str(first_item.get("item_title") or ""),
            item_sku_title=str(first_item.get("item_sku_title") or ""),
            refund_status_str=str(refund_info.get("refund_status_str") or ""),
            add_time=_parse_dt(item.get("addTime")),
            update_time=_parse_dt(item.get("updateTime")),
        ))
    return out


def parse_refund_list_html(html_text: str) -> list[BuyerRefundRecord]:
    """从买家版退款列表 SSR HTML 抽出 BuyerRefundRecord 列表（后备方案）。

    ⚠️ SSR 页受 cookie 中 `sid` 绑定的店铺过滤，只返回单店铺的退款，
    多供货商场景下会漏单。生产路径应使用 fetch_refund_list()。
    保留此函数是因为 SSR HTML 结构稳定、不依赖 API 变更，作为 fallback。

    抛 WeidianNotLoggedIn 如果检测到登录态丢失。
    """
    m = _INJECT_SCRIPT_RE.search(html_text)
    if not m:
        if "login" in html_text.lower()[:5000] or "登录" in html_text[:5000]:
            raise WeidianNotLoggedIn("买家版退款列表页要求登录 — 请重新登录")
        raise WeidianApiError("未在退款列表 HTML 中找到 __rocker-render-inject__ 脚本（页面结构可能变更）")

    raw_attr = m.group(1)
    decoded = html.unescape(raw_attr)
    try:
        data = json.loads(decoded)
    except json.JSONDecodeError as e:
        raise WeidianApiError(f"data-obj JSON 解析失败：{e}")

    list_block = data.get("list") or {}
    status = list_block.get("status") or {}
    if status.get("code") not in (0, None):
        raise WeidianApiError(
            f"refund list status code={status.get('code')} msg={status.get('message')}"
        )
    return _items_to_records(list_block.get("result") or [])


def fetch_refund_list() -> list[BuyerRefundRecord]:
    """直调 buyer.frontRefundList API 抓买家版**全部店铺**的退款列表。

    不受 sid cookie 绑定的店铺过滤，返回该买家账号下所有供货商的退款。
    （SSR 页 weidian.com/user/order/refundList 只能返回单店铺，已废弃。）
    """
    cookies, _ = _load_cookies_and_token()
    with httpx.Client(cookies=cookies, headers=HEADERS) as client:
        params = {
            "param": json.dumps({"from": "h5"}, separators=(",", ":"), ensure_ascii=False),
            "_": str(int(time.time() * 1000)),
        }
        r = client.get(BUYER_REFUND_LIST_API, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        status = data.get("status") or {}
        if status.get("code") != 0:
            msg = status.get("message", "")
            if any(kw in msg for kw in ("登录", "login", "未授权", "token")):
                raise WeidianNotLoggedIn(f"frontRefundList 要求登录：{msg}")
            raise WeidianApiError(
                f"buyer.frontRefundList code={status.get('code')} message={msg}"
            )
        records = _items_to_records(data.get("result") or [])
    log.info("buyer refund list (frontRefundList): %d records", len(records))
    return records


def fetch_refund_list_html(type_: int = 5) -> list[BuyerRefundRecord]:
    """[已废弃] 抓 SSR 页解析退款列表。

    ⚠️ SSR 页受 cookie sid 绑定的店铺过滤，**多供货商场景下会漏单**。
    保留仅作为 fetch_refund_list() 失败时的 fallback。生产代码请用 fetch_refund_list()。
    """
    cookies, _ = _load_cookies_and_token()
    with httpx.Client(cookies=cookies, headers=HEADERS) as client:
        r = client.get(
            BUYER_REFUND_LIST_URL,
            params={"type": type_, "spider_token": "8ab3"},
            timeout=20,
        )
        r.raise_for_status()
        records = parse_refund_list_html(r.text)
    log.warning("buyer refund list (SSR fallback, may miss multi-shop): %d records", len(records))
    return records


def fetch_refund_detail(refund_no: str, client: httpx.Client | None = None) -> dict:
    """调买家版退款详情接口（roleType=1），返回 result dict。

    可传入复用的 client（带 cookies），不传则单独建。
    """
    own_client = client is None
    if own_client:
        cookies, _ = _load_cookies_and_token()
        client = httpx.Client(cookies=cookies, headers=HEADERS)
    try:
        param = json.dumps({"refund_no": refund_no, "roleType": 1},
                           separators=(",", ":"), ensure_ascii=False)
        r = client.get(
            BUYER_REFUND_DETAIL_URL,
            params={"param": param, "_": str(int(time.time() * 1000))},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("status") or {}
        if status.get("code") != 0:
            msg = status.get("message", "")
            if any(kw in msg for kw in ("登录", "login", "未授权", "token")):
                raise WeidianNotLoggedIn(f"详情接口要求登录：{msg}")
            raise WeidianApiError(
                f"getRefundDetail code={status.get('code')} message={msg}"
            )
        return data.get("result") or {}
    finally:
        if own_client:
            client.close()


def fetch_order_customer(order_id: str, client: httpx.Client | None = None) -> tuple[str | None, str | None]:
    """调买家版订单详情，取真实客户（代发收件人）姓名 + 手机号。

    返回 (name, phone)。买家版退款详情里的 buyerName/buyerPhone 是店主自己，
    真实客户在订单详情的 buyerInfo（nameDesc="收件人"）。
    """
    own_client = client is None
    if own_client:
        cookies, _ = _load_cookies_and_token()
        client = httpx.Client(cookies=cookies, headers=HEADERS)
    try:
        param = json.dumps({"order_id": order_id, "from": "h5"},
                           separators=(",", ":"), ensure_ascii=False)
        r = client.get(
            BUYER_ORDER_DETAIL_API,
            params={"param": param, "_": str(int(time.time() * 1000))},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("status") or {}
        if status.get("code") != 0:
            msg = status.get("message", "")
            if any(kw in msg for kw in ("登录", "login", "未授权", "token")):
                raise WeidianNotLoggedIn(f"订单详情接口要求登录：{msg}")
            raise WeidianApiError(
                f"getOrderDetailForApp code={status.get('code')} message={msg}"
            )
        buyer_info = (data.get("result") or {}).get("buyerInfo") or {}
        return buyer_info.get("name") or None, buyer_info.get("telephone") or None
    finally:
        if own_client:
            client.close()


def enrich_refunds(refunds: Iterable[BuyerRefundRecord]) -> None:
    """对「待买家处理退货」的退款逐笔补详情字段（in-place）。

    每笔调两个接口：
      - getRefundDetail：倒计时、操作状态（C 用）
      - getOrderDetailForApp：真实客户姓名/手机（C 展示 + D 关联用）
    其他状态不调（无谓的接口调用）。单笔失败仅 log warning，不阻塞其他。
    """
    targets = [r for r in refunds
               if r.refund_status_str == STATUS_PENDING_BUYER_RETURN]
    if not targets:
        return

    cookies, _ = _load_cookies_and_token()
    with httpx.Client(cookies=cookies, headers=HEADERS) as client:
        for i, r in enumerate(targets):
            if i > 0:
                time.sleep(DETAIL_CALL_INTERVAL_SECONDS)
            try:
                result = fetch_refund_detail(r.refund_no, client=client)
                card = result.get("refundCard") or {}
                basic = result.get("refundBasicInfo") or {}
                r.countdown_seconds = card.get("autoCountdownInSecond")
                r.operate_status_str = card.get("operateStatusStr") or None
                r.receiver_name = basic.get("buyerName") or None
                r.receiver_phone = basic.get("buyerPhone") or None
            except Exception as e:
                log.warning("buyer refund detail failed for %s: %s", r.refund_no, e)

            time.sleep(DETAIL_CALL_INTERVAL_SECONDS)
            try:
                name, phone = fetch_order_customer(r.order_id, client=client)
                r.customer_name = name
                r.customer_phone = phone
            except Exception as e:
                log.warning("buyer order detail failed for %s: %s", r.order_id, e)
    log.info("buyer refunds enriched: %d", len(targets))


# === 自动填退货单号（写操作，唯一开放的微店写动作）===
#
# 抓包（从 weidian-h5/aftersale/logistics 页面 JS 逆向）：
#   反查物流公司枚举: GET  vexpress/seller.getSuggestExpressList/1.1
#                     param={"express_no": <单号>, "user_type": 1}
#                     → result.common_express = [{id, express_company}, ...]（id=承运商枚举）
#   提交单号:         POST refundplatform/buyer.submitExpressInfo/1.0
#                     body 表单 param=<JSON>，JSON =
#                       {refundNo, expressType:<id>, expressCompany:<名称>,
#                        expressNo:<单号>, operateType:1}（1=新填,2=编辑）
# ⚠️ POST 编码（表单 param= vs JSON body）与 expressType 枚举是否 == 卖家版
#    return_express_type，均需灰度首测（D_AUTOFILL_LIMIT=1）实测确认。

BUYER_SUGGEST_EXPRESS_API = "https://thor.weidian.com/vexpress/seller.getSuggestExpressList/1.1"
BUYER_SUBMIT_EXPRESS_API = "https://thor.weidian.com/refundplatform/buyer.submitExpressInfo/1.0"


def _express_id_to_company() -> dict[int, str]:
    """拉买家版承运商枚举，返回 {expressType id: 物流公司名}。"""
    cookies, _ = _load_cookies_and_token()
    with httpx.Client(cookies=cookies, headers=HEADERS) as client:
        param = json.dumps({"express_no": "", "user_type": 1},
                           separators=(",", ":"), ensure_ascii=False)
        r = client.get(BUYER_SUGGEST_EXPRESS_API,
                       params={"param": param, "_": str(int(time.time() * 1000))},
                       timeout=15)
        r.raise_for_status()
        data = r.json()
        if (data.get("status") or {}).get("code") != 0:
            raise WeidianApiError(f"getSuggestExpressList: {data.get('status')}")
        out: dict[int, str] = {}
        for e in (data.get("result") or {}).get("common_express") or []:
            if e.get("id") is not None and e.get("express_company"):
                out[int(e["id"])] = e["express_company"]
        return out


def submit_return_express(
    refund_no: str,
    express_no: str,
    express_type: int,
    express_company: str,
    *,
    client: httpx.Client | None = None,
) -> None:
    """把退货单号填进买家版售后（写操作）。

    调用方需先用 classify_d 确认唯一匹配，并解析好 express_type/express_company
    （见 resolve_express_company）。失败抛 WeidianApiError / WeidianNotLoggedIn。
    """
    if not (refund_no and express_no and express_type and express_company):
        raise WeidianApiError(
            f"submit_return_express 参数不全: refund_no={refund_no} "
            f"express_no={express_no} express_type={express_type} company={express_company}"
        )
    own_client = client is None
    if own_client:
        cookies, _ = _load_cookies_and_token()
        client = httpx.Client(cookies=cookies, headers=HEADERS)
    try:
        payload = {
            "refundNo": refund_no,
            "expressType": express_type,
            "expressCompany": express_company,
            "expressNo": express_no,
            "operateType": 1,   # 1=新填
        }
        param = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        # 微店 thor 网关惯例：POST 表单 param=<JSON 字符串>
        r = client.post(BUYER_SUBMIT_EXPRESS_API, data={"param": param}, timeout=15)
        r.raise_for_status()
        data = r.json()
        status = data.get("status") or {}
        if status.get("code") != 0:
            msg = status.get("message", "")
            if any(kw in msg for kw in ("登录", "login", "未授权", "token")):
                raise WeidianNotLoggedIn(f"submitExpressInfo 要求登录：{msg}")
            raise WeidianApiError(
                f"submitExpressInfo code={status.get('code')} message={msg}"
            )
    finally:
        if own_client:
            client.close()
