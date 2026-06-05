"""
通过搜狗搜索查询物流。

为什么不用快递100？
  - 免费网页查询对极兔/京东/顺丰/圆通等主流快递公司已封禁，提示"请下载 APP"。
  - 搜狗搜索内嵌的"包裹追踪"卡片不受此限制，圆通等能直接拿到完整轨迹。
  - 失败的（极兔/京东也常超时）就老老实实留空，让规则引擎决定降级。

策略：
  - URL: https://www.sogou.com/web?query={tracking_no}
  - 等待 .vrwrap（搜狗结果卡片）出现
  - 内部文本含 "暂无该单号物流信息" / "查询超时" → 视为未知（signed_at=None, status="未知"）
  - 否则提取所有 "yyyy-mm-dd hh:mm:ss <事件描述>" 行，找最新含 签收 关键字的 → signed_at
  - 截图只截 .vrwrap 元素本身（用户明确要求"只保留查询的部分"），JPEG 70 质量
"""
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
SIGNED_KEYWORDS = ("已签收", "签收", "本人签收", "他人代签", "已揽件签收")
WIDGET_SELECTOR = ".vrwrap"
ERROR_PHRASES = ("暂无该单号", "查询超时", "请检查公司和单号", "未查询到")
DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?")
MAX_SCREENSHOT_BYTES = 1_800_000


def _parse_events(text: str) -> list[tuple[datetime, str]]:
    """从 widget 文本里解析 (时间, 描述) 列表。"""
    events: list[tuple[datetime, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = DATE_RE.search(lines[i])
        if m:
            y, mo, d, h, mi, s = m.groups()
            try:
                dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=TIMEZONE)
            except ValueError:
                i += 1
                continue
            # 描述通常在下一行（最新事件结构是"时间\n描述"）
            desc = ""
            if i + 1 < len(lines):
                desc = lines[i + 1].strip()
            events.append((dt, desc))
            i += 2
            continue
        i += 1
    return events


def _classify(text: str, events: list[tuple[datetime, str]]) -> tuple[str, datetime | None]:
    """返回 (status, signed_at)。"""
    if any(p in text for p in ERROR_PHRASES):
        return "未知", None
    if not events:
        return "未知", None
    # 找最近一条签收事件
    for dt, desc in events:
        if any(kw in desc for kw in SIGNED_KEYWORDS):
            return "签收", dt
    return "运输中", None


def _read_cache(conn: sqlite3.Connection, tracking_no: str):
    row = conn.execute(
        "SELECT * FROM logistics_cache WHERE tracking_no = ?", (tracking_no,)
    ).fetchone()
    if not row or row["last_query_at"] is None:
        return None
    last = datetime.fromisoformat(row["last_query_at"])
    if datetime.now(TIMEZONE) - last > timedelta(hours=CACHE_TTL_HOURS):
        return None
    # 未知态的缓存只用 30 分钟（短期内 Sogou 可能恢复）
    if row["status"] == "未知" and datetime.now(TIMEZONE) - last > timedelta(minutes=30):
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


def _screenshot_widget(page: Page, path: Path) -> bool:
    """只截 .vrwrap 元素，不截整页。"""
    try:
        el = page.query_selector(WIDGET_SELECTOR)
        if not el:
            return False
        el.screenshot(path=str(path), type="jpeg", quality=75)
        return path.stat().st_size <= MAX_SCREENSHOT_BYTES
    except Exception as e:
        log.warning("widget screenshot failed: %s", e)
        return False


def _query_one(page: Page, tracking_no: str) -> dict:
    page.goto(f"https://www.sogou.com/web?query={tracking_no}",
              wait_until="domcontentloaded", timeout=20000)
    try:
        page.wait_for_selector(WIDGET_SELECTOR, timeout=8000)
    except PWTimeout:
        log.info("no .vrwrap for %s, treating as unknown", tracking_no)

    # 留时间给 JS 拉取物流数据
    page.wait_for_timeout(5000)

    widget = page.query_selector(WIDGET_SELECTOR)
    text = widget.inner_text() if widget else ""

    events = _parse_events(text)
    status, signed_at = _classify(text, events)

    # 推测 carrier
    carrier = ""
    for kw in ("极兔速递", "京东物流", "顺丰速运", "顺丰", "圆通速递", "中通快递", "韵达速递",
               "申通快递", "百世快递", "EMS", "德邦", "极兔"):
        if kw in text:
            carrier = kw
            break

    shot_path = SCREENSHOTS_DIR / f"{tracking_no}-{int(time.time())}.jpg"
    has_shot = _screenshot_widget(page, shot_path)

    record = {
        "tracking_no": tracking_no,
        "carrier": carrier,
        "status": status,
        "signed_at": signed_at.isoformat() if signed_at else None,
        "last_query_at": datetime.now(TIMEZONE).isoformat(),
        "screenshot_path": str(shot_path) if has_shot else None,
        "raw_text": text[:4000],
    }
    with get_conn() as conn:
        _write_cache(conn, **record)
    return record


def query(tracking_no: str, *, use_cache: bool = True, headless: bool = True) -> dict:
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
                    rec = _query_one(page, t)
                    out[t] = rec
                    log.info("logistics %d/%d: %s [%s] -> status=%s signed=%s",
                             i, len(to_fetch), t, rec.get("carrier") or "?",
                             rec.get("status"), rec.get("signed_at") or "—")
                except Exception as e:
                    log.warning("logistics query failed for %s: %s", t, e)
        finally:
            ctx.close()
            browser.close()
    return out


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("usage: python -m src.logistics.sogou <tracking_no> [--no-headless]")
        sys.exit(1)
    headless = "--no-headless" not in sys.argv
    r = query(sys.argv[1], use_cache=False, headless=headless)
    print(json.dumps(r, ensure_ascii=False, indent=2))
