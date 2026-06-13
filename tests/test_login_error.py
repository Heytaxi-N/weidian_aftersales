"""锁住登录失效识别：微店会返回大写 'LOGIN ERROR'，必须大小写不敏感匹配。"""
from __future__ import annotations

from src.weidian.client import _is_login_error


def test_uppercase_login_error_detected():
    # 这正是 2026-06-13 21:00 漏报的真实报文
    assert _is_login_error("LOGIN ERROR") is True


def test_lowercase_and_chinese():
    assert _is_login_error("login expired") is True
    assert _is_login_error("登录已失效") is True
    assert _is_login_error("未授权") is True
    assert _is_login_error("token invalid") is True


def test_non_login_errors_not_flagged():
    assert _is_login_error("系统开小差") is False
    assert _is_login_error("参数错误") is False
    assert _is_login_error("") is False
    assert _is_login_error(None) is False
