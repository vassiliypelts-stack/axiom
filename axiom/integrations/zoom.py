"""Zoom: создание запланированной встречи через Server-to-Server OAuth.

Нужны (в .env): ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET
(создаётся как app типа «Server-to-Server OAuth» в Zoom Marketplace, scope meeting:write).

Нет ключей → enabled()=False, create_meeting() вернёт None (пилот продолжит без ссылки).
"""
from __future__ import annotations

from base64 import b64encode
from datetime import datetime

import requests

import config

_TOKEN_URL = "https://zoom.us/oauth/token"
_API = "https://api.zoom.us/v2"


def enabled() -> bool:
    return bool(config.ZOOM_ACCOUNT_ID and config.ZOOM_CLIENT_ID and config.ZOOM_CLIENT_SECRET)


def _access_token() -> str:
    basic = b64encode(f"{config.ZOOM_CLIENT_ID}:{config.ZOOM_CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        _TOKEN_URL,
        params={"grant_type": "account_credentials", "account_id": config.ZOOM_ACCOUNT_ID},
        headers={"Authorization": f"Basic {basic}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def create_meeting(topic: str, start: datetime, duration_min: int, tz: str) -> dict | None:
    """Создаёт встречу. start — datetime (локальное время в зоне tz).
    Возвращает {'join_url', 'id', 'start_time'} или None, если Zoom не настроен/ошибка."""
    if not enabled():
        return None
    try:
        token = _access_token()
        body = {
            "topic": topic,
            "type": 2,  # scheduled
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration": duration_min,
            "timezone": tz,
            "settings": {"join_before_host": True, "waiting_room": False},
        }
        r = requests.post(
            f"{_API}/users/me/meetings",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        return {"join_url": data["join_url"], "id": data["id"], "start_time": data.get("start_time")}
    except Exception as e:
        print(f"[zoom error] {e}")
        return None
