#!/usr/bin/env bash
# 一次性抓商品供货商 XHR。用法：./scripts/supplier_dump.sh
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m src.weidian.supplier_dump
