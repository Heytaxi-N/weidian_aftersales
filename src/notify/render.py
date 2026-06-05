"""把物流签收信息渲染成一张小卡片图（≤100KB JPEG），用于附在 B 推送之后转发上游。

⚠️ 这里用 Playwright，但**只加载 about:blank** 并注入 HTML — 不会访问微店任何页面，
   因此不违反 weidian_trace.py 的 DANGER ZONE 规则（那条规则针对的是详情页的危险按钮）。

用户要求："截图只要显示签收 + 露出快递单号那个区域，不要截一大块"。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from jinja2 import Template
from playwright.sync_api import sync_playwright

from src.config import SCREENSHOTS_DIR

log = logging.getLogger(__name__)


CARD_HTML = Template("""\
<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body { margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont,
         "PingFang SC", "Helvetica Neue", sans-serif; background: #fff; }
  .card {
    width: 600px; padding: 18px 22px; box-sizing: border-box;
    border: 1px solid #e5e7eb; border-radius: 10px; background: #fff;
  }
  .header {
    font-size: 16px; font-weight: 600; color: #111827; margin-bottom: 10px;
    display: flex; gap: 8px; align-items: center;
  }
  .tracking {
    font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 15px;
    color: #1f2937; background: #f3f4f6; padding: 2px 8px; border-radius: 4px;
  }
  .signed {
    margin-top: 8px; padding: 12px 14px; background: #ecfdf5;
    border-left: 3px solid #10b981; border-radius: 4px;
    color: #065f46; font-size: 14px; line-height: 1.55;
  }
  .signed .time { font-weight: 600; color: #064e3b; }
  .ctx { color: #374151; margin-top: 4px; }
  .nodata {
    margin-top: 8px; padding: 12px 14px; background: #fef3c7;
    border-left: 3px solid #f59e0b; border-radius: 4px;
    color: #92400e; font-size: 14px;
  }
</style></head>
<body>
<div class="card" id="card">
  <div class="header">📦 {{ carrier }} · <span class="tracking">{{ tracking_no }}</span></div>
  {% if signed_time %}
  <div class="signed">
    <span class="time">{{ signed_time }} 已签收</span>
    {% if context %}<div class="ctx">{{ context }}</div>{% endif %}
  </div>
  {% else %}
  <div class="nodata">尚未签收</div>
  {% endif %}
</div>
</body></html>
""")


def _render_one(page, carrier: str, tracking_no: str,
                signed_time: str | None, context: str | None) -> Path:
    safe_tn = re.sub(r"[^a-zA-Z0-9]", "_", tracking_no)
    out = SCREENSHOTS_DIR / f"card-{safe_tn}-{int(time.time() * 1000)}.jpg"
    html = CARD_HTML.render(
        carrier=carrier or "—",
        tracking_no=tracking_no,
        signed_time=signed_time,
        context=(context or "")[:120],
    )
    page.set_content(html, wait_until="domcontentloaded")
    card = page.query_selector("#card")
    if card is None:
        raise RuntimeError("card element not found in rendered HTML")
    card.screenshot(path=str(out), type="jpeg", quality=82)
    return out


def render_signed_card(
    carrier: str,
    tracking_no: str,
    signed_time: str | None,
    context: str | None = None,
) -> Path:
    """渲染签收小卡片，返回 jpg 路径（独立 Playwright，单次用）。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 700, "height": 400},
                                  device_scale_factor=2)
        page = ctx.new_page()
        try:
            return _render_one(page, carrier, tracking_no, signed_time, context)
        finally:
            ctx.close()
            browser.close()


def render_many(specs: list[dict]) -> list[Path]:
    """批量渲染（复用一个浏览器实例）。每个 spec: {carrier, tracking_no, signed_time, context}"""
    out: list[Path] = []
    if not specs:
        return out
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 700, "height": 400},
                                  device_scale_factor=2)
        page = ctx.new_page()
        try:
            for s in specs:
                try:
                    out.append(_render_one(
                        page, s["carrier"], s["tracking_no"],
                        s.get("signed_time"), s.get("context")
                    ))
                except Exception as e:
                    log.warning("render failed for %s: %s", s.get("tracking_no"), e)
                    out.append(None)  # type: ignore[arg-type]
        finally:
            ctx.close()
            browser.close()
    return out


def render_from_trace(carrier: str, tracking_no: str, signed_at_iso: str | None,
                      trace_text: str | None = None) -> Path:
    """从 logistics 缓存里常见数据组装签收卡片。"""
    signed_time = None
    if signed_at_iso:
        # 转 "YYYY-MM-DD HH:MM" 友好显示
        signed_time = signed_at_iso.replace("T", " ")[:16]

    # 尝试从 trace_text 里抽一段签收事件描述作为 context
    context = None
    if trace_text:
        for line in trace_text.splitlines():
            if "签收" in line:
                # 砍掉模板前缀 "> 📦 时间  **已签收**  context..."
                m = re.search(r"\*\*[^*]*签收[^*]*\*\*\s*(.+)$", line)
                if m:
                    context = m.group(1).strip()
                break
    return render_signed_card(carrier, tracking_no, signed_time, context)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python -m src.notify.render <carrier> <tracking_no> "
              "[signed_at_iso] [context]")
        sys.exit(1)
    args = sys.argv[1:]
    carrier, tn = args[0], args[1]
    signed = args[2] if len(args) >= 3 else "2026-06-01T19:29:26"
    ctx_text = args[3] if len(args) >= 4 else "您的快件已按址投递，收件人：门口"
    p = render_signed_card(carrier, tn, signed.replace("T", " ")[:16], ctx_text)
    print(f"输出: {p}  size={p.stat().st_size} bytes")
