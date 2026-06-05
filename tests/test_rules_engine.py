from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.rules.engine import (
    Decision,
    LogisticsInfo,
    RefundRecord,
    evaluate,
    to_payload,
)


NOW = datetime(2026, 6, 5, 9, 0)


def mk(refund_id: str, **kw) -> RefundRecord:
    base = dict(
        refund_id=refund_id,
        order_id="O" + refund_id,
        refund_type="退货退款",
        status="待商家处理",
        deadline_at=NOW + timedelta(hours=72),
        buyer_name="买家",
        buyer_phone="138****",
        return_tracking_no=None,
    )
    base.update(kw)
    return RefundRecord(**base)


class TestTimeBased:
    def test_no_scenario_if_far_from_deadline(self):
        r = mk("RF1", deadline_at=NOW + timedelta(hours=72))
        assert evaluate([r], {}, set(), NOW) == []

    def test_a_tier_between_24_and_48(self):
        r = mk("RF1", deadline_at=NOW + timedelta(hours=36))
        out = evaluate([r], {}, set(), NOW)
        assert len(out) == 1
        assert out[0].scenarios == ["A"]
        assert out[0].hours_left == pytest.approx(36.0)

    def test_a2_tier_when_under_24(self):
        r = mk("RF1", deadline_at=NOW + timedelta(hours=8))
        out = evaluate([r], {}, set(), NOW)
        assert out[0].scenarios == ["A2"]

    def test_overdue_still_a2(self):
        r = mk("RF1", deadline_at=NOW - timedelta(hours=2))
        out = evaluate([r], {}, set(), NOW)
        assert out[0].scenarios == ["A2"]
        assert out[0].hours_left < 0


class TestScenarioB:
    def test_b_fires_when_signed_2_days(self):
        r = mk(
            "RF1",
            status="待商家收货",
            return_tracking_no="YT123",
            deadline_at=NOW + timedelta(hours=120),  # 远离截止
        )
        logistics = {"YT123": LogisticsInfo("YT123", signed_at=NOW - timedelta(days=2, hours=1))}
        out = evaluate([r], logistics, set(), NOW)
        assert out[0].scenarios == ["B"]

    def test_b_not_fire_under_2_days(self):
        r = mk("RF1", status="待商家收货", return_tracking_no="YT123",
               deadline_at=NOW + timedelta(hours=120))
        logistics = {"YT123": LogisticsInfo("YT123", signed_at=NOW - timedelta(hours=47))}
        assert evaluate([r], logistics, set(), NOW) == []

    def test_b_not_fire_if_not_signed(self):
        r = mk("RF1", status="待商家收货", return_tracking_no="YT123",
               deadline_at=NOW + timedelta(hours=120))
        logistics = {"YT123": LogisticsInfo("YT123", signed_at=None)}
        assert evaluate([r], logistics, set(), NOW) == []

    def test_b_not_fire_if_status_not_pending_receive(self):
        r = mk("RF1", status="待商家处理", return_tracking_no="YT123",
               deadline_at=NOW + timedelta(hours=120))
        logistics = {"YT123": LogisticsInfo("YT123", signed_at=NOW - timedelta(days=5))}
        assert evaluate([r], logistics, set(), NOW) == []


class TestMerge:
    def test_a2_and_b_combined(self):
        r = mk(
            "RF1",
            status="待商家收货",
            return_tracking_no="YT123",
            deadline_at=NOW + timedelta(hours=8),
        )
        logistics = {"YT123": LogisticsInfo("YT123", signed_at=NOW - timedelta(days=5))}
        out = evaluate([r], logistics, set(), NOW)
        assert len(out) == 1
        assert set(out[0].scenarios) == {"A2", "B"}

    def test_a_and_b_combined(self):
        r = mk("RF1", status="待商家收货", return_tracking_no="YT123",
               deadline_at=NOW + timedelta(hours=36))
        logistics = {"YT123": LogisticsInfo("YT123", signed_at=NOW - timedelta(days=3))}
        out = evaluate([r], logistics, set(), NOW)
        assert set(out[0].scenarios) == {"A", "B"}


class TestDedup:
    def test_skip_already_pushed_a(self):
        r = mk("RF1", deadline_at=NOW + timedelta(hours=36))
        assert evaluate([r], {}, {("RF1", "A")}, NOW) == []

    def test_a_then_a2_upgrade(self):
        r = mk("RF1", deadline_at=NOW + timedelta(hours=12))
        out = evaluate([r], {}, {("RF1", "A")}, NOW)
        assert out[0].scenarios == ["A2"]

    def test_a2_dedup_no_repush(self):
        r = mk("RF1", deadline_at=NOW + timedelta(hours=12))
        assert evaluate([r], {}, {("RF1", "A2")}, NOW) == []

    def test_b_only_remaining_after_a2_pushed(self):
        r = mk("RF1", status="待商家收货", return_tracking_no="YT123",
               deadline_at=NOW + timedelta(hours=8))
        logistics = {"YT123": LogisticsInfo("YT123", signed_at=NOW - timedelta(days=5))}
        out = evaluate([r], logistics, {("RF1", "A2")}, NOW)
        assert out[0].scenarios == ["B"]


class TestPayloadConversion:
    def test_payload_carries_logistics_fields(self):
        r = mk("RF1", status="待商家收货", return_tracking_no="YT123",
               receiver_name="李四", receiver_phone="139****",
               item_title="冲锋衣",
               deadline_at=NOW + timedelta(hours=8))
        logistics = {"YT123": LogisticsInfo("YT123",
                                             signed_at=NOW - timedelta(days=5),
                                             screenshot_path="/tmp/x.png")}
        out = evaluate([r], logistics, set(), NOW)
        p = to_payload(out[0])
        assert p.receiver_name == "李四"
        assert p.return_tracking_no == "YT123"
        assert p.screenshot_path == "/tmp/x.png"
        assert set(p.scenarios) == {"A2", "B"}


class TestStatusFilter:
    def test_non_pending_statuses_ignored(self):
        r = mk("RF1", status="已退款", deadline_at=NOW + timedelta(hours=8))
        assert evaluate([r], {}, set(), NOW) == []
