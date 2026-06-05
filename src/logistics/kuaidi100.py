from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from src.config import SCREENSHOTS_DIR, TIMEZONE
from src.db import get_conn

log = logging.getLogger(__name__)

CACHE_TTL_HOURS = 6
SIGNED_KEYWORDS = ("已签收", "签收", "已收件", "本人签收", "他人代签")
RESULT_SELECTOR = ".result-info, #result, .resultB, .info"
MAX_SCREENSHOT_BYTES = 1_800_000  # 留点 buffer，企业微信限 2MB


def _parse_signed_at(text: str) -> datetime | None:
    """从单条物流轨迹文本中抽取签收时间，例如 '2026-06-01 14:30:00 已签收，本人收'。"""
    if not any(kw in text for kw in SIGNED_KEYWORDS):
        return None
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})\D+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", text)
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=TIMEZONE)


def _read_cache(conn: sqlite3.Connection, tracking_no: str):
    row = conn.execute(
        "SELECT * FROM logistics_cache WHERE tracking_no = ?", (tracking_no,)
    ).fetchone()
    if not row:
        return None
    if row["last_query_at"] is None:
        return None
    last = datetime.fromisoformat(row["last_query_at"])
    if datetime.now(TIMEZONE) - last > timedelta(hours=CACHE_TTL_HOURS):
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


def _query_via_playwright(page: Page, tracking_no: str) -> tuple[str, str | None, datetime | None, str]:
    """返回 (carrier, status, signed_at, raw_text)。"""
    page.goto(f"https://www.kuaidi100.com/?nu={tracking_no}", wait_until="domcontentloaded", timeout=30000)
    # 等结果区域出现
    try:
        page.wait_for_selector(RESULT_SELECTOR, timeout=15000)
    except PWTimeout:
        log.warning("result selector not found for %s, capturing whole page", tracking_no)

    # 给前端一点时间渲染轨迹列表
    page.wait_for_timeout(2000)
    raw_text = page.evaluate("() => document.body.innerText") or ""

    # 抽取承运商
    carrier = ""
    m = re.search(r"([^\s]*快递|[^\s]*速递|[^\s]*物流|顺丰|圆通|中通|韵达|申通|百世|京东|EMS|德邦|极兔)", raw_text)
    if m:
        carrier = m.group(1)

    # 找最近一条带签收关键词的轨迹
    signed_at = None
    status = None
    for line in raw_text.splitlines():
        if any(kw in line for kw in SIGNED_KEYWORDS):
            signed_at = _parse_signed_at(line)
            status = "签收"
            break
    if status is None:
        status = "运输中"

    return carrier, status, signed_at, raw_text


def _screenshot(page: Page, path: Path) -> None:
    """JPEG 截图，控制在企业微信 2MB 上限内。
    优先截结果元素；退化为视口截图（不 full_page，避免几 MB 长图）。"""
    try:
        el = page.query_selector(RESULT_SELECTOR.split(",")[0].strip())
        if el:
            el.screenshot(path=str(path), type="jpeg", quality=75)
            if path.stat().st_size <= MAX_SCREENSHOT_BYTES:
                return
    except Exception as e:
        log.debug("element screenshot failed, fallback to viewport: %s", e)
    # 只截当前视口（默认 1280×1600），不 full_page
    page.screenshot(path=str(path), type="jpeg", quality=70, full_page=False, clip={
        "x": 0, "y": 0, "width": 1280, "height": 1600,
    })


def _query_one(page: Page, tracking_no: str) -> dict:
    carrier, status, signed_at, raw_text = _query_via_playwright(page, tracking_no)
    shot_path = SCREENSHOTS_DIR / f"{tracking_no}-{int(time.time())}.jpg"
    _screenshot(page, shot_path)
    record = {
        "tracking_no": tracking_no,
        "carrier": carrier,
        "status": status,
        "signed_at": signed_at.isoformat() if signed_at else None,
        "last_query_at": datetime.now(TIMEZONE).isoformat(),
        "screenshot_path": str(shot_path),
        "raw_text": raw_text[:4000],
    }
    with get_conn() as conn:
        _write_cache(conn, **record)
    return record


def query(tracking_no: str, *, use_cache: bool = True, headless: bool = True) -> dict:
    """查询单个运单号。"""
    with get_conn() as conn:
        if use_cache:
            cached = _read_cache(conn, tracking_no)
            if cached:
                return dict(cached)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 1600},
        )
        page = ctx.new_page()
        try:
            return _query_one(page, tracking_no)
        finally:
            ctx.close()
            browser.close()


def query_many(tracking_nos: list[str], *, use_cache: bool = True, headless: bool = True) -> dict[str, dict]:
    """批量查询，共用一个浏览器实例（比逐个 query 快很多）。"""
    out: dict[str, dict] = {}
    to_fetch: list[str] = []
    if use_cache:
        with get_conn() as conn:
            for t in tracking_nos:
                cached = _read_cache(conn, t)
                if cached:
                    out[t] = dict(cached)
                else:
                    to_fetch.append(t)
    else:
        to_fetch = list(tracking_nos)

    log.info("logistics: %d hit cache, %d to fetch", len(out), len(to_fetch))
    if not to_fetch:
        return out

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 1600},
        )
        page = ctx.new_page()
        try:
            for i, t in enumerate(to_fetch, 1):
                try:
                    out[t] = _query_one(page, t)
                    log.info("logistics %d/%d: %s -> %s",
                             i, len(to_fetch), t, out[t].get("status"))
                except Exception as e:
                    log.warning("logistics query failed for %s: %s", t, e)
        finally:
            ctx.close()
            browser.close()
    return out


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("usage: python -m src.logistics.kuaidi100 <tracking_no> [--no-headless]")
        sys.exit(1)
    headless = "--no-headless" not in sys.argv
    r = query(sys.argv[1], use_cache=False, headless=headless)
    print(json.dumps(r, ensure_ascii=False, indent=2))
