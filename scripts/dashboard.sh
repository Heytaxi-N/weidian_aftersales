#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
echo "Dashboard 启动后请打开 http://localhost:${DASHBOARD_PORT:-8765}"
exec .venv/bin/python -m src.dashboard.app
