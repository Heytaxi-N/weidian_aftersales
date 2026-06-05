"""
微店退款列表抓取 —— 直调 thor.weidian.com 内部接口。

接口（抓包确认）：
  GET https://thor.weidian.com/refundplatform/seller/refundSearchList/1.0/
      ?param={"tabType":5,"isCheckAutoRefund":..., ...}
      &wdtoken=<cookie 同名 wdtoken>
      &_=<ms 时间戳>

字段（按真实响应映射）：
  refundNo                 → refund_id    （注意是字符串）
  orderId                  → order_id    （int，转 str）
  deadlineStr              → deadline_at （"YYYY-MM-DD HH:MM:SS"）
  buyerName / buyerTelephone
  buyerAddress             → receiver_* 留空（退货是买家寄回卖家，收件人=卖家自己）
  refundTypeStr            → refund_type ("退款退货" / "仅退款")
  refundStatusStr          → 用于推断 status
  itemInfoList[0].itemTitle → item_title

tabType:
  5 → 待商家处理
  6 → 待商家收货
  ⚠️ 退款单的"退货物流单号"目前不在列表接口里，需要单笔详情接口。
     已留 fetch_refund_detail() 的占位，等抓到详情样本再补。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import httpx

from src.config import STORAGE_STATE_PATH, TIMEZONE
from src.rules.engine import RefundRecord

log = logging.getLogger(__name__)

API_BASE = "https://thor.weidian.com/refundplatform"
REFUND_DETAIL_PAGE_URL_FMT = (
    "https://d.weidian.com/weidian-pc/weidian-loader/"
    "#/pc-vue-refund-order/refund/detail?refundNo={refund_no}"
)
PAGE_SIZE = 20

TAB_TYPES = {
    5: "待商家处理",
    6: "待商家收货",
}


class WeidianNotLoggedIn(RuntimeError):
    pass


class WeidianApiError(RuntimeError):
    pass


def _load_cookies_and_token() -> tuple[dict[str, str], str]:
    if not STORAGE_STATE_PATH.exists():
        raise WeidianNotLoggedIn("storage_state.json 不存在，请先运行 scripts/login.sh")
    ss = json.loads(STORAGE_STATE_PATH.read_text())
    cookies: dict[str, str] = {}
    wdtoken = ""
    for c in ss.get("cookies", []):
        if not c["domain"].endswith("weidian.com"):
            continue
        cookies[c["name"]] = c["value"]
        if c["name"] == "wdtoken":
            wdtoken = c["value"]
    if "login_token" not in cookies or not wdtoken:
        raise WeidianNotLoggedIn("storage_state 缺少 login_token 或 wdtoken — 重新登录")
    return cookies, wdtoken


def _call(client: httpx.Client, path: str, param: dict, wdtoken: str) -> dict:
    # 注意：路径用点号分隔（如 seller.refundSearchList），且 1.0 后不带斜杠
    url = f"{API_BASE}/{path}/1.0"
    params = {
        "param": json.dumps(param, separators=(",", ":"), ensure_ascii=False),
        "wdtoken": wdtoken,
        "_": str(int(time.time() * 1000)),
    }
    r = client.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    status = data.get("status") or {}
    if status.get("code") != 0:
        msg = status.get("message", "")
        log.error("API %s returned code=%s msg=%s url=%s",
                  path, status.get("code"), msg, str(r.request.url)[:300])
        if any(kw in msg for kw in ("登录", "login", "未授权", "token")):
            raise WeidianNotLoggedIn(f"API 返回: {msg}")
        raise WeidianApiError(f"{path} returned code={status.get('code')} message={msg}")
    return data


def _parse_deadline(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
    except ValueError:
        return None


def _status_from_str(refund_status_str: str, tab_label: str) -> str:
    """状态以 tab 为权威 —— tab=6 的列表里 refundStatusStr 末尾仍写"待商家处理"
       （描述"应该商家来处理"），但真实节点是"待商家收货"。"""
    return tab_label


def parse_refund_list_response(payload: dict, tab_label: str) -> list[RefundRecord]:
    items = (payload.get("result") or {}).get("list") or []
    out: list[RefundRecord] = []
    for it in items:
        refund_id = str(it.get("refundNo") or "").strip()
        order_id = str(it.get("orderId") or "").strip()
        if not refund_id or not order_id:
            continue
        first_item = (it.get("itemInfoList") or [{}])[0]
        item_title = first_item.get("itemTitle") or ""
        sku = first_item.get("itemSkuTitle")
        if sku:
            item_title = f"{item_title} ({sku})"
        out.append(RefundRecord(
            refund_id=refund_id,
            order_id=order_id,
            refund_type=it.get("refundTypeStr") or "",
            status=_status_from_str(it.get("refundStatusStr") or "", tab_label),
            deadline_at=_parse_deadline(it.get("deadlineStr")),
            buyer_name=it.get("buyerName"),
            buyer_phone=it.get("buyerTelephone"),
            # 退货场景下"收件人"=卖家自己；这里留空，等详情接口给出"寄件人=买家"信息
            receiver_name=it.get("buyerName"),
            receiver_phone=it.get("buyerTelephone"),
            item_title=item_title,
            return_tracking_no=None,  # TODO: 待详情接口补
            detail_url=REFUND_DETAIL_PAGE_URL_FMT.format(refund_no=refund_id),
        ))
    return out


def fetch_refund_detail(client: httpx.Client, refund_no: str, wdtoken: str) -> dict | None:
    """获取单笔退款详情：seller.getRefundDetailForPC，必须带 roleType=101。"""
    try:
        return _call(client, "seller.getRefundDetailForPC",
                     {"refundNo": refund_no, "roleType": 101}, wdtoken)
    except (WeidianApiError, httpx.HTTPError) as e:
        log.warning("detail fetch failed for %s: %s", refund_no, e)
        return None


def extract_tracking_info(detail: dict | None) -> tuple[str | None, str | None, int | None]:
    """返回 (express_no, express_company, express_type)。
    express_type 是微店内部承运商 ID（如 韵达=6），调 trace 接口需要带。"""
    if not detail:
        return None, None, None
    result = detail.get("result") or {}
    progress = result.get("refundProgress") or {}
    basic = progress.get("refundBasicInfo") or {}
    express_no = (basic.get("expressNo") or "").strip() or None
    express_type = basic.get("expressType")
    if not isinstance(express_type, int):
        express_type = None
    company = None
    active = (progress.get("activeRefundRecord") or {}).get("content") or {}
    if active.get("expressCompany"):
        company = active["expressCompany"].strip() or None
    return express_no, company, express_type


def extract_buyer_return_info(detail: dict | None) -> tuple[str | None, str | None, str | None]:
    """从详情里取退货寄件方信息：(buyer_name, buyer_phone, buyer_address)。"""
    if not detail:
        return None, None, None
    info = (detail.get("result") or {}).get("refundInfo") or {}
    return info.get("buyerName"), info.get("buyerPhone"), info.get("buyerAddress")


def fetch_all_refunds(*, fetch_details: bool = True) -> list[RefundRecord]:
    cookies, wdtoken = _load_cookies_and_token()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Referer": "https://d.weidian.com/",
        "Accept": "application/json, text/plain, */*",
    }
    all_records: list[RefundRecord] = []
    with httpx.Client(cookies=cookies, headers=headers, follow_redirects=False) as client:
        for tab_type, tab_label in TAB_TYPES.items():
            page_idx = 0  # 注意微店是 0-based
            while True:
                param = {
                    "tabType": tab_type,
                    "isCheckAutoRefund": "0",
                    "fxSupplySearch": {},
                    "timeSearch": {},
                    "feeSearch": {},
                    "refundSort": {"sortKey": "addTime", "sortType": 2},
                    "page": page_idx,
                    "pageSize": PAGE_SIZE,
                }
                try:
                    data = _call(client, "seller.refundSearchList", param, wdtoken)
                except WeidianNotLoggedIn:
                    raise
                except Exception as e:
                    log.exception("refundSearchList tab=%s page=%s failed: %s", tab_type, page_idx, e)
                    break
                page_records = parse_refund_list_response(data, tab_label)
                all_records.extend(page_records)
                total = (data.get("result") or {}).get("total") or 0
                log.info("tab=%s page=%s got=%d total=%d",
                         tab_type, page_idx, len(page_records), total)
                if len(page_records) < PAGE_SIZE or (page_idx + 1) * PAGE_SIZE >= total:
                    break
                page_idx += 1

        # 详情接口：仅对待商家收货那批拉一次，拿运单号 + 寄件方信息
        if fetch_details:
            for r in all_records:
                if r.status != "待商家收货":
                    continue
                detail = fetch_refund_detail(client, r.refund_id, wdtoken)
                if detail is None:
                    continue
                tracking_no, _carrier, express_type = extract_tracking_info(detail)
                r.return_tracking_no = tracking_no
                r.return_express_type = express_type
                # 退货是买家寄给商家，"收件人"信息其实就是买家自己
                rname, rphone, _addr = extract_buyer_return_info(detail)
                if rname:
                    r.receiver_name = rname
                if rphone:
                    r.receiver_phone = rphone

    log.info("fetched %d refunds total", len(all_records))
    return all_records


if __name__ == "__main__":
    import sys
    from dataclasses import asdict
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    no_details = "--no-details" in sys.argv
    recs = fetch_all_refunds(fetch_details=not no_details)
    print(f"抓到 {len(recs)} 条退款")
    for r in recs[:5]:
        print(json.dumps(asdict(r), default=str, ensure_ascii=False))
    by_status: dict[str, int] = {}
    for r in recs:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    print("\n按状态分布：", by_status)
