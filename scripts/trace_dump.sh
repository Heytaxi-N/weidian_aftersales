#!/usr/bin/env bash
# 一次性抓【跟踪物流】XHR。用法：./scripts/trace_dump.sh <refund_no> <order_id>
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$#" -lt 2 ]; then
    echo "usage: $0 <refund_no> <order_id>" >&2
    echo "  refund_no: 微店退款单号（在退款管理页面能看到）" >&2
    echo "  order_id: 关联订单号" >&2
    exit 1
fi
exec .venv/bin/python -m src.weidian.trace_dump "$@"
