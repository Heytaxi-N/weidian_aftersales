"""验证 B 配额逻辑：今天已推 N 条 → 本轮裁剪到 max(0, 20-N) 条。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from src import db, runner
from src.config import TIMEZONE
from src.rules.engine import Decision, LogisticsInfo, RefundRecord


NOW = datetime(2026, 6, 5, 21, 0, tzinfo=TIMEZONE)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """每个测试用临时 SQLite，避免污染开发库。"""
    test_db = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_schema(test_db)
    return test_db


def _seed_pushed_b(conn: sqlite3.Connection, n: int, when: datetime) -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO pushed_records(refund_id, scenario, pushed_at, message_text) "
            "VALUES (?,?,?,?)",
            (f"FAKE_{i}", "B", when.isoformat(), "seed"),
        )


def test_quota_used_today_empty(fresh_db):
    assert runner._b_quota_used_today(NOW) == 0


def test_quota_used_today_counts_b_only(fresh_db):
    today_morning = NOW.replace(hour=9, minute=0)
    with db.get_conn(fresh_db) as c:
        _seed_pushed_b(c, 4, today_morning)
        # 加几条非 B
        for i in range(3):
            c.execute(
                "INSERT INTO pushed_records(refund_id, scenario, pushed_at, message_text) "
                "VALUES (?,?,?,?)",
                (f"OTH_{i}", "A2", today_morning.isoformat(), "x"),
            )
    assert runner._b_quota_used_today(NOW) == 4


def test_quota_used_today_excludes_yesterday(fresh_db):
    yesterday = NOW - timedelta(days=1)
    today = NOW.replace(hour=9, minute=0)
    with db.get_conn(fresh_db) as c:
        for i in range(6):
            c.execute(
                "INSERT INTO pushed_records(refund_id, scenario, pushed_at, message_text) "
                "VALUES (?,?,?,?)",
                (f"Y_{i}", "B", yesterday.isoformat(), "x"),
            )
        for i in range(3):
            c.execute(
                "INSERT INTO pushed_records(refund_id, scenario, pushed_at, message_text) "
                "VALUES (?,?,?,?)",
                (f"T_{i}", "B", today.isoformat(), "x"),
            )
    assert runner._b_quota_used_today(NOW) == 3


def _mk_b_decision(rid: str, deadline_offset_h: float) -> Decision:
    r = RefundRecord(
        refund_id=rid, order_id="O" + rid, refund_type="退货退款",
        status="待商家收货",
        deadline_at=NOW + timedelta(hours=deadline_offset_h),
        return_tracking_no="TN" + rid,
    )
    log = LogisticsInfo(tracking_no="TN" + rid,
                        signed_at=NOW - timedelta(days=3),
                        carrier="圆通速递", trace_text="")
    return Decision(refund=r, scenarios=["B"], logistics=log, hours_left=deadline_offset_h)


def test_b_push_respects_quota(fresh_db, monkeypatch):
    # mock 渲染、推送、供货商查询，只测筛选 + quota 逻辑
    monkeypatch.setattr(runner.card_render, "render_many", lambda specs: [None] * len(specs))
    monkeypatch.setattr(runner.weidian_supplier, "fetch_suppliers", lambda ids: {})
    monkeypatch.setattr(runner.wecom, "send_markdown", lambda m: None)
    monkeypatch.setattr(runner.wecom, "send_text", lambda m: None)
    monkeypatch.setattr(runner.wecom, "send_image", lambda p: None)

    # 今天已推 17 条
    with db.get_conn(fresh_db) as c:
        for i in range(17):
            c.execute(
                "INSERT INTO pushed_records(refund_id, scenario, pushed_at, message_text) "
                "VALUES (?,?,?,?)",
                (f"SEED_{i}", "B", NOW.replace(hour=9).isoformat(), "x"),
            )

    # 候选 10 条
    decisions = [_mk_b_decision(f"R{i}", float(50 + i)) for i in range(10)]

    pushed = runner._push_b(decisions, NOW, dry_run=False)
    assert pushed == 3, f"剩余配额 20-17=3，应当只推 3 条，实际 {pushed}"


def test_b_push_picks_most_urgent_first(fresh_db, monkeypatch):
    monkeypatch.setattr(runner.card_render, "render_many", lambda specs: [None] * len(specs))
    monkeypatch.setattr(runner.weidian_supplier, "fetch_suppliers", lambda ids: {})
    sent_md: list[str] = []
    sent_text: list[str] = []
    monkeypatch.setattr(runner.wecom, "send_markdown", lambda m: sent_md.append(m))
    monkeypatch.setattr(runner.wecom, "send_text", lambda m: sent_text.append(m))
    monkeypatch.setattr(runner.wecom, "send_image", lambda p: None)

    # 候选：5 笔，deadline 分别 100h / 50h / 10h / 30h / 80h
    decisions = [
        _mk_b_decision("A", 100),
        _mk_b_decision("B", 50),
        _mk_b_decision("C", 10),
        _mk_b_decision("D", 30),
        _mk_b_decision("E", 80),
    ]

    # 配额 3：今天已推 20-3=17 条
    with db.get_conn(fresh_db) as c:
        for i in range(17):
            c.execute(
                "INSERT INTO pushed_records(refund_id, scenario, pushed_at, message_text) "
                "VALUES (?,?,?,?)",
                (f"SEED_{i}", "B", NOW.replace(hour=9).isoformat(), "x"),
            )
    runner._push_b(decisions, NOW, dry_run=False)

    # 应当按 deadline 升序取前 3：C(10), D(30), B(50)
    # 所有归为"无供货商"组 → 1 条组头 markdown + 1 条多行 text（3 笔合并）
    assert len(sent_md) == 1, f"应有 1 条组头 markdown，实际 {len(sent_md)}"
    assert "无供货商" in sent_md[0] and "3 笔" in sent_md[0]
    assert len(sent_text) == 1, f"应有 1 条多行 text（组内合并），实际 {len(sent_text)}"
    # 多行 text 里顺序按 deadline 升序：C → D → B
    body = sent_text[0]
    pos_c = body.find("TNC")
    pos_d = body.find("TND")
    pos_b = body.find("TNB")
    assert pos_c >= 0 and pos_d >= 0 and pos_b >= 0
    assert pos_c < pos_d < pos_b, f"顺序错: TNC={pos_c} TND={pos_d} TNB={pos_b}"
    # 不应包含 A/E（被裁掉）
    assert "TNA" not in body and "TNE" not in body


def test_b_push_quota_exhausted(fresh_db, monkeypatch):
    monkeypatch.setattr(runner.card_render, "render_many", lambda specs: [None] * len(specs))
    monkeypatch.setattr(runner.weidian_supplier, "fetch_suppliers", lambda ids: {})
    monkeypatch.setattr(runner.wecom, "send_markdown", lambda m: None)
    monkeypatch.setattr(runner.wecom, "send_text", lambda m: None)
    monkeypatch.setattr(runner.wecom, "send_image", lambda p: None)

    with db.get_conn(fresh_db) as c:
        for i in range(20):
            c.execute(
                "INSERT INTO pushed_records(refund_id, scenario, pushed_at, message_text) "
                "VALUES (?,?,?,?)",
                (f"SEED_{i}", "B", NOW.replace(hour=9).isoformat(), "x"),
            )

    decisions = [_mk_b_decision("R0", 50)]
    assert runner._push_b(decisions, NOW, dry_run=False) == 0
