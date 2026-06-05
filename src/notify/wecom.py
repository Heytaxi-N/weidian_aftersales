from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from pathlib import Path

import httpx

from src.config import WECOM_WEBHOOK_URL

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 企业微信图片消息上限 2MB
MIN_INTERVAL_SECONDS = 3.2          # 企业微信群机器人限频 20 条/分钟，3.2s 间隔安全

_last_send_at: float = 0.0
_send_lock = threading.Lock()


def _throttle() -> None:
    """单进程内的发送节流，避免 errcode=45009。"""
    global _last_send_at
    with _send_lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_send_at)
        if wait > 0:
            time.sleep(wait)
        _last_send_at = time.monotonic()


class WeComError(RuntimeError):
    pass


def _post(payload: dict) -> dict:
    if not WECOM_WEBHOOK_URL:
        raise WeComError("WECOM_WEBHOOK_URL is empty — set it in .env")
    _throttle()
    r = httpx.post(WECOM_WEBHOOK_URL, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("errcode") != 0:
        raise WeComError(f"WeCom returned errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
    return data


def send_text(content: str, mentioned_mobile_list: list[str] | None = None) -> None:
    payload: dict = {"msgtype": "text", "text": {"content": content}}
    if mentioned_mobile_list:
        payload["text"]["mentioned_mobile_list"] = mentioned_mobile_list
    _post(payload)


def send_markdown(content: str) -> None:
    _post({"msgtype": "markdown", "markdown": {"content": content}})


def send_image(path: Path | str) -> None:
    """企业微信图片消息：base64 + md5，单图 ≤ 2MB。"""
    p = Path(path)
    raw = p.read_bytes()
    if len(raw) > MAX_IMAGE_BYTES:
        raise WeComError(f"image {p} too large: {len(raw)} bytes (max {MAX_IMAGE_BYTES})")
    payload = {
        "msgtype": "image",
        "image": {
            "base64": base64.b64encode(raw).decode("ascii"),
            "md5": hashlib.md5(raw).hexdigest(),
        },
    }
    _post(payload)


def send_alert(content: str) -> None:
    """轻量告警，失败仅 log，不抛出（避免主流程崩在告警上）。"""
    try:
        send_text(f"⚠️ 售后系统告警\n{content}")
    except Exception as e:
        log.exception("failed to send alert: %s", e)
