from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
LOGS_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "shop.db"
STORAGE_STATE_PATH = DATA_DIR / "storage_state.json"

load_dotenv(ROOT / ".env")

WECOM_WEBHOOK_URL = os.getenv("WECOM_WEBHOOK_URL", "")
WECOM_WEBHOOK_URL_BUYER = os.getenv("WECOM_WEBHOOK_URL_BUYER", "")
WEIDIAN_USERNAME = os.getenv("WEIDIAN_USERNAME", "")
WEIDIAN_PASSWORD = os.getenv("WEIDIAN_PASSWORD", "")
WEIDIAN_SHOP_ID = os.getenv("WEIDIAN_SHOP_ID", "")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8765"))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))

for d in (DATA_DIR, SCREENSHOTS_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)
