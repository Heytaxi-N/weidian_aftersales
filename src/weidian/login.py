"""
微店卖家后台登录 — 用账号密码 + 有头浏览器。

策略：
  1. 启动有头 Chromium，加载已有的 storage_state（如有）。
  2. 直接访问退款管理页 https://d.weidian.com/weidian-pc/...
  3. 如果被重定向到登录页：自动填账号密码并点登录。
  4. 之后给用户最多 5 分钟手动过滑块/短信验证码。
  5. 检测到回到目标页（URL 含 'refund' 或 'order'）即视为成功，保存 storage_state.json。

⚠️ 微店的登录 URL 和表单选择器可能随版本变化，下面用了几个候选选择器
   作为兜底。如果自动填表失败，浏览器仍会保持打开，用户手动登录后脚本一样能
   保存 storage_state（基于 URL 跳转判定）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from src.config import STORAGE_STATE_PATH, WEIDIAN_PASSWORD, WEIDIAN_USERNAME

log = logging.getLogger(__name__)

TARGET_URL = "https://d.weidian.com/weidian-pc/weidian-loader/#/pc-vue-refund-order/refund/order"
LOGIN_SUCCESS_URL_KEYWORDS = ("refund", "order", "weidian-pc/weidian-loader")
WAIT_FOR_HUMAN_SECONDS = 300

USERNAME_SELECTORS = [
    'input[type="tel"]',
    'input[name="account"]',
    'input[name="username"]',
    'input[placeholder*="手机"]',
    'input[placeholder*="账号"]',
]
PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[placeholder*="密码"]',
]
SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("登录")',
    'button:has-text("登 录")',
    '.login-btn',
    '#loginBtn',
]


def _try_fill(page: Page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1000):
                loc.fill(value, timeout=2000)
                return True
        except Exception:
            continue
    return False


def _try_click(page: Page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1000):
                loc.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def _is_logged_in(page: Page) -> bool:
    url = page.url.lower()
    return any(kw in url for kw in LOGIN_SUCCESS_URL_KEYWORDS) and "login" not in url


def login(headless: bool = False) -> Path:
    if not WEIDIAN_USERNAME or not WEIDIAN_PASSWORD:
        log.warning("WEIDIAN_USERNAME / WEIDIAN_PASSWORD 未配置，将打开浏览器供你手动登录")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx_kwargs = {
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "viewport": {"width": 1440, "height": 900},
        }
        if STORAGE_STATE_PATH.exists():
            ctx_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()

        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        if _is_logged_in(page):
            log.info("已登录，直接保存 storage_state")
            ctx.storage_state(path=str(STORAGE_STATE_PATH))
            browser.close()
            return STORAGE_STATE_PATH

        # 尝试自动填写
        if WEIDIAN_USERNAME and WEIDIAN_PASSWORD:
            filled_u = _try_fill(page, USERNAME_SELECTORS, WEIDIAN_USERNAME)
            filled_p = _try_fill(page, PASSWORD_SELECTORS, WEIDIAN_PASSWORD)
            log.info("自动填表：username=%s password=%s", filled_u, filled_p)
            if filled_u and filled_p:
                _try_click(page, SUBMIT_SELECTORS)
                page.wait_for_timeout(3000)

        # 等用户手动过滑块/验证码
        print(f"等待登录完成（最多 {WAIT_FOR_HUMAN_SECONDS} 秒）... 请在浏览器中完成验证码/滑块/扫码")
        deadline = time.time() + WAIT_FOR_HUMAN_SECONDS
        while time.time() < deadline:
            if _is_logged_in(page):
                log.info("登录成功，保存 storage_state")
                ctx.storage_state(path=str(STORAGE_STATE_PATH))
                browser.close()
                return STORAGE_STATE_PATH
            page.wait_for_timeout(2000)

        browser.close()
        raise TimeoutError("登录未完成（超时）。请重试 `./scripts/login.sh`。")


if __name__ == "__main__":
    import sys
    headless = "--headless" in sys.argv
    path = login(headless=headless)
    print(f"已保存 storage_state 到 {path}")
