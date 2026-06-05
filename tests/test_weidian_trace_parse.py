from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.logistics.weidian_trace import _format_trace_text, _parse_signed


DUMP = Path(__file__).resolve().parent.parent / "data" / "debug" / "trace-144115509392416136"


def _load_dump() -> dict:
    """加载真实 dump 里的 trace 响应。"""
    for f in DUMP.glob("*kuaidi.getExpressStepInfo*.json"):
        wrapper = json.loads(f.read_text())
        return wrapper["body"]
    raise AssertionError("no trace dump fixture found")


def test_parse_signed_from_real_dump():
    body = _load_dump()
    events = body["result"]["data_json"]
    signed_at = _parse_signed(events)
    assert signed_at is not None
    assert signed_at.year == 2026 and signed_at.month == 5 and signed_at.day == 27
    assert signed_at.hour == 16 and signed_at.minute == 28


def test_parse_signed_returns_none_when_no_sign_event():
    events = [
        {"scanType": "派件中", "ftime": "2026-06-01 10:00:00"},
        {"scanType": "入中转", "ftime": "2026-06-01 08:00:00"},
    ]
    assert _parse_signed(events) is None


def test_parse_signed_picks_earliest():
    events = [
        {"scanType": "已签收", "ftime": "2026-06-02 10:00:00"},
        {"scanType": "已签收", "ftime": "2026-06-01 10:00:00"},  # 更早 — 我们要这个
    ]
    dt = _parse_signed(events)
    assert dt and dt.day == 1


def test_format_trace_text():
    body = _load_dump()
    events = body["result"]["data_json"]
    text = _format_trace_text("韵达快递", "435192795946670", events)
    assert "韵达快递" in text
    assert "435192795946670" in text
    assert "已签收" in text
    # 应限制条数
    assert text.count("> ") <= 9
