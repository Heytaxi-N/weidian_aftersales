"""一次性工具：抓商品供货商相关的 XHR。

⚠️ 跟 trace_dump 同一思路：
  - 用有头浏览器加载微店后台主页
  - **只监听** XHR，**不主动点击任何按钮**
  - 由用户手动导航：商品管理 → 行内【设置供货商】按钮 → 跳到关联页面

本次策略：抓所有 weidian.com 域名的 JSON XHR（不限关键字），都存进 dump，
跑完后用 `grep -l <供货商名> data/debug/supplier/*.json` 定位接口。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import Request, Response, sync_playwright

from src.config import DATA_DIR, STORAGE_STATE_PATH

START_URL = "https://d.weidian.com/weidian-pc/weidian-loader/"
FALLBACK_URL = "https://d.weidian.com/weidian-pc/weidian-loader/"

# 命中以下任一关键字（字段名或值）就视为可能含供货商信息
EN_KEYWORDS = (
    "supplier", "supply", "vendor", "partner", "fxsource",
    "fxSupply", "source", "manufacturer", "wholesaler",
    "cooperation", "cooperate",
)
CN_KEYWORDS = (
    "合作供货商", "供货商", "供应商", "合作商", "厂商", "来源", "进货",
    "小鱼", "闲鱼",  # 用户实际供货商名（从截图截到的两个）
)


def _safe_filename(url: str) -> str:
    base = url.split("?")[0]
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", base).strip("_")
    return s[-100:]


def main() -> int:
    if not STORAGE_STATE_PATH.exists():
        print(f"未找到登录态 {STORAGE_STATE_PATH}，先跑 ./scripts/login.sh", file=sys.stderr)
        return 2

    dump_dir = DATA_DIR / "debug" / "supplier"
    dump_dir.mkdir(parents=True, exist_ok=True)

    captured: list[dict] = []
    seen_urls: set[str] = set()
    # url -> request info (method, post_data)
    request_info: dict[str, dict] = {}

    def on_request(req: Request) -> None:
        if "weidian.com" not in req.url and "vdiankr.com" not in req.url:
            return
        request_info[req.url] = {"method": req.method, "post_data": req.post_data}

    def on_response(resp: Response) -> None:
        url = resp.url
        if url in seen_urls:
            return
        if "weidian.com" not in url and "vdiankr.com" not in url and "vdian.com" not in url:
            return
        ct = (resp.headers or {}).get("content-type", "").lower()
        try:
            body = resp.text()
        except Exception:
            return

        # JSON 全收；HTML 只收非主页（短的）
        is_json = "json" in ct
        is_html = "html" in ct
        if not (is_json or is_html):
            return
        if is_html and len(body) > 200_000:
            # 太大的 HTML 不存（首页 SPA shell 没用）
            return

        seen_urls.add(url)
        body_lower = body.lower()
        hit_en = [k for k in EN_KEYWORDS if k.lower() in body_lower]
        hit_cn = [k for k in CN_KEYWORDS if k in body]
        req_info = request_info.get(url, {})
        entry = {
            "url": url,
            "method": req_info.get("method", "GET"),
            "post_data": req_info.get("post_data"),
            "status": resp.status,
            "ct": ct,
            "hit_en": hit_en,
            "hit_cn": hit_cn,
        }
        if is_json:
            try:
                entry["body"] = json.loads(body)
            except Exception:
                entry["body_raw"] = body[:5000]
        else:
            entry["body_raw"] = body[:50_000]
        captured.append(entry)
        kind = "JSON" if is_json else "HTML"
        hits_label = f"hits={hit_en + hit_cn}" if (hit_en or hit_cn) else "（无关键字命中，仍保留）"
        print(f"[捕获 {kind}] {url[:100]}  {hits_label}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(
            storage_state=str(STORAGE_STATE_PATH),
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        # 先试商品管理直链；若 404 / 跳转，再退回主页让用户自己导航
        try:
            page.goto(START_URL, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            page.goto(FALLBACK_URL, wait_until="domcontentloaded", timeout=20000)

        print()
        print("=" * 70)
        print("  操作步骤（这次走订单详情路径）：")
        print("  1. 进入「订单管理」")
        print("  2. 随便点开一个订单的【详情】 — 或者搜一个含供货商的订单号")
        print("  3. 滚到底，看到「合作供货商：xxx」那一行")
        print("  4. 回终端按 Enter")
        print()
        print("  ⚠️ 不要点【确认收货】【关闭交易】等会变更订单状态的按钮，只看")
        print("=" * 70)
        try:
            input("按 Enter 完成抓取... ")
        except (KeyboardInterrupt, EOFError):
            pass

        # 结束前抓当前页面的完整 HTML（万一供货商数据嵌在 HTML 里）
        try:
            current_url = page.url
            html = page.content()
            (dump_dir / "_final_page.html").write_text(html, encoding="utf-8")
            print(f"\n保存最终页面 HTML：{current_url[:120]}  ({len(html)} bytes)")
        except Exception as e:
            print(f"保存最终页面 HTML 失败：{e}")

        ctx.close()
        browser.close()

    for i, c in enumerate(captured, 1):
        fname = dump_dir / f"{i:02d}-{_safe_filename(c['url'])}.json"
        fname.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n抓到 {len(captured)} 条命中供货商关键字的 XHR，存到 {dump_dir}")
    for c in captured:
        print(f"  {c['url'][:120]}")
        print(f"    hits: {c['hit_en'] + c['hit_cn']}")
    if not captured:
        print("  （没抓到 — 可能没进入供货商字段加载，或微店没用上面这些字段名）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
