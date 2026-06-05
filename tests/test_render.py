"""Render 卡片图的烟雾测试 — 主要确认 Playwright 能跑通且输出尺寸合理。"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.notify.render import render_signed_card

pytestmark = pytest.mark.skipif(
    not Path("/Users/nick/Downloads/售后管理/.venv/bin/python").exists(),
    reason="只在本机有 Playwright 时跑",
)


def test_render_signed_card_returns_small_jpeg(tmp_path, monkeypatch):
    import src.notify.render as r
    monkeypatch.setattr(r, "SCREENSHOTS_DIR", tmp_path)
    p = render_signed_card("圆通速递", "YT1234567890",
                           "2026-06-01 19:29", "您的快件已送达，门口")
    assert p.exists()
    size = p.stat().st_size
    assert 5_000 < size < 200_000, f"卡片图尺寸异常: {size}"
    # 文件头是 JPEG
    head = p.read_bytes()[:3]
    assert head == b"\xff\xd8\xff", f"非 JPEG: {head!r}"


def test_render_unsigned_card(tmp_path, monkeypatch):
    import src.notify.render as r
    monkeypatch.setattr(r, "SCREENSHOTS_DIR", tmp_path)
    p = render_signed_card("京东物流", "JDX12345", None, None)
    assert p.exists() and p.stat().st_size > 1000
