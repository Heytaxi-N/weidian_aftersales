from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.logistics.weidian_trace import _format_trace_text, _parse_signed


DUMP = Path(__file__).resolve().parent.parent / "data" / "debug"


def _load_dump() -> dict | None:
    """加载真实 dump 里的 trace 响应。data/debug 在 gitignore 里，外部 clone 会缺失。"""
    if not DUMP.exists():
        return None
    for f in DUMP.glob("**/kuaidi.getExpressStepInfo*.json"):
        wrapper = json.loads(f.read_text())
        return wrapper.get("body") if "body" in wrapper else wrapper
    for f in DUMP.glob("**/*kuaidi.getExpressStepInfo*.json"):
        wrapper = json.loads(f.read_text())
        return wrapper.get("body") if "body" in wrapper else wrapper
    return None


def _skip_if_no_dump():
    if _load_dump() is None:
        pytest.skip("trace dump fixture missing (data/debug/ is gitignored)")


def test_parse_signed_from_real_dump():
    _skip_if_no_dump()
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


def test_parse_signed_jd_tuotuo():
    """京东用「取件运单妥投」作为 scanType，应识别为签收。"""
    events = [
        {"scanType": "取件运单妥投", "ftime": "2026-05-31 03:21:03"},
        {"scanType": "配送员收货", "ftime": "2026-05-30 08:30:25"},
    ]
    dt = _parse_signed(events)
    assert dt is not None
    assert dt.day == 31 and dt.hour == 3


def test_parse_signed_fallback_to_last_time_when_ischeck():
    """ischeck=True 但 scanType 无已知关键字时，用 result.last_time 兜底。"""
    events = [
        {"scanType": "奇怪的承运商专用词", "ftime": "2026-06-03 12:00:00"},
    ]
    # events 解析失败
    assert _parse_signed(events) is None
    # 但传入 result 的 ischeck + last_time 应当 fallback
    dt = _parse_signed(events, result={"ischeck": True, "last_time": "2026-06-03 12:00:00"})
    assert dt is not None and dt.day == 3


def test_parse_signed_no_fallback_when_ischeck_false():
    events = [{"scanType": "派件中", "ftime": "2026-06-01 10:00:00"}]
    dt = _parse_signed(events, result={"ischeck": False, "last_time": "2026-06-01 10:00:00"})
    assert dt is None


def test_format_trace_text():
    _skip_if_no_dump()
    body = _load_dump()
    events = body["result"]["data_json"]
    # 用 dump 里的真实承运商和运单号（不在源码 hardcode 敏感号码）
    carrier = body["result"].get("express_company", "测试快递")
    tracking_no = body["result"].get("express_no", "TEST123")
    text = _format_trace_text(carrier, tracking_no, events)
    assert carrier in text
    assert tracking_no in text
    assert "已签收" in text or "签收" in text or "妥投" in text
    # 应限制条数
    assert text.count("> ") <= 9
