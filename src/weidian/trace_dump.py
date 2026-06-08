"""一次性工具：抓【跟踪物流】按钮触发的 XHR。

⚠️ 这是 dump-only 工具。本脚本：
  - 用有头浏览器打开微店退款详情页
  - **只监听** XHR 响应，**不主动点击任何按钮**
  - 由用户手动在浏览器里点【跟踪物流】
  - 把响应里包含物流关键字的 XHR 写到 data/debug/trace-{refund_no}/

用法：
    python -m src.weidian.trace_dump <refund_no> <order_id>

(refund_no 和 order_id 可在微店退款管理页面看到)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import Response, sync_playwright

from src.config import DATA_DIR, STORAGE_STATE_PATH

DETAIL_URL_FMT = (
    "https://d.weidian.com/weidian-pc/weidian-loader/"
    "#/pc-vue-refund-order/refund/detail?refund_no={refund_no}&orderId={order_id}"
)
LOGISTICS_KEYWORDS_LOWER = (
    "express", "tracking", "trace", "logistic", "waybill", "courier",
)
LOGISTICS_KEYWORDS_CN = (
    "已签收", "已送达", "已取件", "运单", "派送", "派件", "转运", "转运中心",
)


def _safe_filename(url: str) -> str:
    base = url.split("?")[0]
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", base).strip("_")
    return s[-100:]


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: python -m src.weidian.trace_dump <refund_no> <order_id>", file=sys.stderr)
        return 1
    refund_no, order_id = sys.argv[1], sys.argv[2]
    if not STORAGE_STATE_PATH.exists():
        print(f"未找到登录态 {STORAGE_STATE_PATH}，先跑 ./scripts/login.sh", file=sys.stderr)
        return 2

    dump_dir = DATA_DIR / "debug" / f"trace-{refund_no}"
    dump_dir.mkdir(parents=True, exist_ok=True)

    captured: list[dict] = []
    seen: set[str] = set()

    def on_response(resp: Response) -> None:
        url = resp.url
        if url in seen:
            return
        if "weidian.com" not in url:
            return
        ct = (resp.headers or {}).get("content-type", "")
        if "json" not in ct.lower():
            return
        try:
            body = resp.text()
        except Exception:
            return
        try:
            data = json.loads(body)
        except Exception:
            return
        body_low = body.lower()
        if not (any(kw in body_low for kw in LOGISTICS_KEYWORDS_LOWER)
                or any(kw in body for kw in LOGISTICS_KEYWORDS_CN)):
            return
        seen.add(url)
        captured.append({"url": url, "status": resp.status, "body": data})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(
            storage_state=str(STORAGE_STATE_PATH),
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        page.on("response", on_response)

        url = DETAIL_URL_FMT.format(refund_no=refund_no, order_id=order_id)
        print(f"\n→ 打开 {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        print()
        print("=" * 70)
        print("  现在请在弹出的浏览器里手动点击【跟踪物流】按钮")
        print("  ⚠️ 不要点击【同意退款】/【拒绝退款】！只点【跟踪物流】")
        print("  弹窗出现物流轨迹后，回到此终端按 Enter")
        print("=" * 70)
        try:
            input("按 Enter 完成抓取... ")
        except (KeyboardInterrupt, EOFError):
            pass

        ctx.close()
        browser.close()

    for i, c in enumerate(captured, 1):
        fname = dump_dir / f"{i:02d}-{_safe_filename(c['url'])}.json"
        fname.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n抓到 {len(captured)} 条命中物流关键字的 XHR，存到 {dump_dir}")
    for c in captured:
        print(f"  {c['url'][:130]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
