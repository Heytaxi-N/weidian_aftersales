#!/usr/bin/env bash
# 三项独立 smoke test：DB / 企业微信 / 物流
set -uo pipefail
cd "$(dirname "$0")/.."

echo "=== 1. DB 初始化 ==="
.venv/bin/python -m src.db || exit 1

echo
echo "=== 2. 单测 (规则引擎 + 物流解析) ==="
.venv/bin/python -m pytest tests/ -q || exit 1

echo
echo "=== 3. 企业微信 webhook ==="
if [ -z "${WECOM_WEBHOOK_URL:-}" ] && [ ! -f .env ]; then
    echo "跳过：需要先配置 .env 里的 WECOM_WEBHOOK_URL"
else
    .venv/bin/python -c "
from src.notify.wecom import send_text
send_text('🧪 售后系统 smoke test — 如果你看到这条，说明企业微信通了')
print('已发送测试消息')
" || echo "WeCom 测试失败 — 检查 WECOM_WEBHOOK_URL"
fi

echo
echo "=== 4. 物流查询（用一个随便编的运单号，验证 Playwright 能跑通即可） ==="
.venv/bin/python -c "
from src.logistics.sogou import query
r = query('YT2548045394122', use_cache=False, headless=True)
print('状态:', r.get('status'))
print('承运商:', r.get('carrier'))
print('截图:', r.get('screenshot_path'))
" || echo "物流查询失败"
