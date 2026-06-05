from __future__ import annotations

from src.logistics.sogou import _parse_events, _classify


YUANTONG_SAMPLE = """跟踪单号yt2548045394122包裹情况
公司名称

圆通速递

快递单号
查询
最新
2026-06-04 23:34:35
您的快件离开【温州转运中心】，已发往【佛山转运中心】
2026-06-04 23:32:44
您的快件已经到达【温州转运中心】
2026-06-04 21:22:41
您的快件离开【浙江省温州市乐清市乐成镇】，已发往【温州转运中心】
"""

SIGNED_SAMPLE = """跟踪单号x包裹情况
2026-06-03 14:30:00
您的快件已签收，本人收
2026-06-02 09:10:00
快件已派送
"""

ERROR_SAMPLE = "跟踪单号x包裹情况\n暂无该单号物流信息,请稍后再试,或检查公司和单号是否有误。"
TIMEOUT_SAMPLE = "跟踪单号x包裹情况\n查询超时，请稍后再试。"


def test_parse_yuantong_events():
    events = _parse_events(YUANTONG_SAMPLE)
    assert len(events) == 3
    dt0, desc0 = events[0]
    assert dt0.day == 4 and dt0.hour == 23 and dt0.minute == 34
    assert "温州转运中心" in desc0


def test_classify_in_transit():
    events = _parse_events(YUANTONG_SAMPLE)
    status, signed_at = _classify(YUANTONG_SAMPLE, events)
    assert status == "运输中"
    assert signed_at is None


def test_classify_signed():
    events = _parse_events(SIGNED_SAMPLE)
    status, signed_at = _classify(SIGNED_SAMPLE, events)
    assert status == "签收"
    assert signed_at is not None
    assert signed_at.day == 3 and signed_at.hour == 14


def test_classify_no_info():
    events = _parse_events(ERROR_SAMPLE)
    status, signed_at = _classify(ERROR_SAMPLE, events)
    assert status == "未知"
    assert signed_at is None


def test_classify_timeout():
    events = _parse_events(TIMEOUT_SAMPLE)
    status, signed_at = _classify(TIMEOUT_SAMPLE, events)
    assert status == "未知"
    assert signed_at is None


def test_signed_picked_over_in_transit():
    """混合场景：先有签收，后有转运（理论上不会，但代码该取最新一条签收）。"""
    sample = """2026-06-03 14:00:00
您的快件已签收
2026-06-02 12:00:00
您的快件已发出
"""
    events = _parse_events(sample)
    status, signed_at = _classify(sample, events)
    assert status == "签收"
    assert signed_at.day == 3
