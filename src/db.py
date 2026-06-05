from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_snapshots (
    snapshot_at        TEXT NOT NULL,
    refund_id          TEXT NOT NULL,
    order_id           TEXT NOT NULL,
    refund_type        TEXT,
    status             TEXT,
    deadline_at        TEXT,
    buyer_name         TEXT,
    buyer_phone        TEXT,
    receiver_name      TEXT,
    receiver_phone     TEXT,
    item_title         TEXT,
    return_tracking_no TEXT,
    detail_url         TEXT,
    raw_json           TEXT,
    PRIMARY KEY (snapshot_at, refund_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_refund ON order_snapshots(refund_id, snapshot_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_status ON order_snapshots(status, snapshot_at);

CREATE TABLE IF NOT EXISTS pushed_records (
    refund_id        TEXT NOT NULL,
    scenario         TEXT NOT NULL,
    pushed_at        TEXT NOT NULL,
    message_text     TEXT,
    screenshot_path  TEXT,
    PRIMARY KEY (refund_id, scenario)
);

CREATE INDEX IF NOT EXISTS idx_pushed_at ON pushed_records(pushed_at);

CREATE TABLE IF NOT EXISTS logistics_cache (
    tracking_no      TEXT PRIMARY KEY,
    carrier          TEXT,
    status           TEXT,
    signed_at        TEXT,
    last_query_at    TEXT,
    screenshot_path  TEXT,
    raw_text         TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    note        TEXT
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    # 在调用时解析全局 DB_PATH，方便测试通过 monkeypatch 改写
    import src.db as _db_mod
    conn = sqlite3.connect(str(path or _db_mod.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(path: Path | str | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    init_schema()
    print(f"Schema initialized at {DB_PATH}")
