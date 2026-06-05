"""
DANGER ZONE — 微店退款详情页有 [同意退款] / [拒绝退款] 按钮，
误触发会造成实际业务损失（自动同意退款）。本模块严格遵守：

  1. 不调用 Playwright，不打开浏览器，不渲染 DOM
  2. 仅用 httpx + 已登录 cookies 调用微店内部 trace 接口
  3. 任何后续修改若引入 Playwright/click 操作，必须先在 PR 描述里
     说明为何不能纯接口实现，并独立 review

如需重新抓接口样本，用 scripts/trace_dump.sh —— 它只监听 XHR、
不主动 click 任何按钮，由用户手动操作浏览器完成点击。

接口（抓包确认）：
  GET https://thor.weidian.com/vexpress/kuaidi.getExpressStepInfo/1.1
      ?param={"expressId":<int>, "expressNo":"<str>"}
      &wdtoken=<cookie>
      &_=<ms>

  expressId 即微店内部承运商 ID，等同于 refundBasicInfo.expressType。
  从 client.fetch_refund_detail() 拿到的 RefundRecord.return_express_type。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta

import httpx

from src.config import TIMEZONE
from src.db import get_conn
from src.weidian.client import _load_cookies_and_token, API_BASE  # noqa: F401 -- API_BASE 不直接用，但保留可见关联

log = logging.getLogger(__name__)

TRACE_URL = "https://thor.weidian.com/vexpress/kuaidi.getExpressStepInfo/1.1"
CACHE_TTL_HOURS = 6
UNKNOWN_CACHE_MINUTES = 30  # 未知态短缓存（可能是临时拉不到）
MAX_TRACE_LINES = 8  # 推送消息里最多嵌入几条轨迹


def _read_cache(conn: sqlite3.Connection, tracking_no: str):
    row = conn.execute(
        "SELECT * FROM logistics_cache WHERE tracking_no = ?", (tracking_no,)
    ).fetchone()
    if not row or row["last_query_at"] is None:
        return None
    last = datetime.fromisoformat(row["last_query_at"])
    now = datetime.now(TIMEZONE)
    if row["status"] == "未知":
        if now - last > timedelta(minutes=UNKNOWN_CACHE_MINUTES):
            return None
    else:
        if now - last > timedelta(hours=CACHE_TTL_HOURS):
            return None
    return row


def _write_cache(conn: sqlite3.Connection, **kw):
    conn.execute(
        """INSERT INTO logistics_cache(tracking_no, carrier, status, signed_at,
                                       last_query_at, screenshot_path, raw_text)
           VALUES(:tracking_no, :carrier, :status, :signed_at,
                  :last_query_at, :screenshot_path, :raw_text)
           ON CONFLICT(tracking_no) DO UPDATE SET
             carrier=excluded.carrier, status=excluded.status,
             signed_at=excluded.signed_at, last_query_at=excluded.last_query_at,
             screenshot_path=excluded.screenshot_path, raw_text=excluded.raw_text""",
        kw,
    )


def _parse_signed(events: list[dict]) -> datetime | None:
    """events: 来自 result.data_json, 已按时间倒序。找最早的"已签收"事件取时间。"""
    signed_times: list[datetime] = []
    for ev in events:
        scan = (ev.get("scanType") or "").strip()
        if "签收" not in scan:
            continue
        ts = (ev.get("ftime") or ev.get("time") or "").strip()
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TIMEZONE)
            signed_times.append(dt)
        except ValueError:
            continue
    return min(signed_times) if signed_times else None


def _format_trace_text(carrier: str, tracking_no: str, events: list[dict]) -> str:
    """把轨迹列表渲染成给企业微信 markdown 的多行文本。"""
    lines = [f"**{carrier}** · `{tracking_no}`"]
    for ev in events[:MAX_TRACE_LINES]:
        t = (ev.get("ftime") or ev.get("time") or "").strip()
        scan = (ev.get("scanType") or "").strip()
        ctx = (ev.get("context") or "").strip()
        # 用括号截断过长 context
        if len(ctx) > 80:
            ctx = ctx[:80] + "..."
        prefix = "📦" if "签收" in scan else "·"
        lines.append(f"> {prefix} {t}  **{scan or '—'}**  {ctx}")
    if len(events) > MAX_TRACE_LINES:
        lines.append(f"> ... 共 {len(events)} 条事件")
    return "\n".join(lines)


def _call_trace(client: httpx.Client, express_no: str, express_id: int, wdtoken: str) -> dict:
    params = {
        "param": json.dumps({"expressId": express_id, "expressNo": express_no},
                            separators=(",", ":")),
        "wdtoken": wdtoken,
        "_": str(int(time.time() * 1000)),
    }
    r = client.get(TRACE_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _query_one(client: httpx.Client, express_no: str, express_id: int, wdtoken: str) -> dict:
    now = datetime.now(TIMEZONE)
    try:
        data = _call_trace(client, express_no, express_id, wdtoken)
    except Exception as e:
        log.warning("trace API failed for %s/%s: %s", express_no, express_id, e)
        return {
            "tracking_no": express_no, "carrier": "", "status": "未知",
            "signed_at": None, "last_query_at": now.isoformat(),
            "screenshot_path": None, "raw_text": f"trace API error: {e}",
        }

    status_block = data.get("status") or {}
    if status_block.get("code") != 0:
        msg = status_block.get("message", "")
        return {
            "tracking_no": express_no, "carrier": "", "status": "未知",
            "signed_at": None, "last_query_at": now.isoformat(),
            "screenshot_path": None, "raw_text": f"API non-zero: {msg}",
        }

    result = data.get("result") or {}
    carrier = result.get("express_company") or ""
    events = result.get("data_json") or []
    signed_at = _parse_signed(events)
    is_check = bool(result.get("ischeck"))

    if signed_at is not None or is_check:
        status = "签收"
    elif events:
        status = "运输中"
    else:
        status = "未知"

    trace_text = _format_trace_text(carrier, express_no, events) if events else ""

    return {
        "tracking_no": express_no,
        "carrier": carrier,
        "status": status,
        "signed_at": signed_at.isoformat() if signed_at else None,
        "last_query_at": now.isoformat(),
        "screenshot_path": None,   # 不截图（DANGER ZONE 规则禁用 Playwright）
        "raw_text": trace_text[:4000],
    }


def query_pairs(pairs: list[tuple[str, int]], *, use_cache: bool = True) -> dict[str, dict]:
    """批量查询。pairs: list of (tracking_no, express_type)。"""
    out: dict[str, dict] = {}
    to_fetch: list[tuple[str, int]] = []

    if use_cache:
        with get_conn() as conn:
            for tn, et in pairs:
                cached = _read_cache(conn, tn)
                if cached:
                    out[tn] = dict(cached)
                else:
                    to_fetch.append((tn, et))
    else:
        to_fetch = list(pairs)

    log.info("logistics: %d hit cache, %d to fetch", len(out), len(to_fetch))
    if not to_fetch:
        return out

    cookies, wdtoken = _load_cookies_and_token()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Referer": "https://d.weidian.com/",
        "Accept": "application/json, text/plain, */*",
    }
    with httpx.Client(cookies=cookies, headers=headers) as client:
        for i, (tn, et) in enumerate(to_fetch, 1):
            if et is None:
                log.warning("missing express_type for %s, skipping", tn)
                continue
            rec = _query_one(client, tn, et, wdtoken)
            out[tn] = rec
            with get_conn() as conn:
                _write_cache(conn, **rec)
            log.info("logistics %d/%d: %s [%s] -> %s signed=%s",
                     i, len(to_fetch), tn, rec.get("carrier") or "?",
                     rec.get("status"), rec.get("signed_at") or "—")
    return out


# 跟旧 sogou 接口兼容（只传 tracking_no list）—— 不推荐使用，因为缺 express_type
def query_many(tracking_nos: list[str], *, use_cache: bool = True, headless: bool = True) -> dict[str, dict]:  # noqa: ARG001
    raise NotImplementedError(
        "weidian_trace 需要 (tracking_no, express_type) 配对，请使用 query_pairs()"
    )


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) < 3:
        print("usage: python -m src.logistics.weidian_trace <expressNo> <expressType>",
              file=sys.stderr)
        sys.exit(1)
    express_no = sys.argv[1]
    express_type = int(sys.argv[2])
    r = query_pairs([(express_no, express_type)], use_cache=False)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
