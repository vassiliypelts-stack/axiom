"""MAX (dev.max.ru) — Bot API, REST на platform-api.max.ru, лимит 30 запр/сек.

Токен бота выдаётся в @MasterBot внутри MAX. С 08.2025 бота может создать и
опубликовать только юрлицо РФ — самозанятому/ИП токен не выдадут (нужна компания).

Документация: https://dev.max.ru/docs-api
"""
from __future__ import annotations

from typing import Any

import requests

from .base import Connector

# Current official endpoint.  The old platform-api.max.ru domain was retired.
API_BASE = "https://platform-api2.max.ru"


class MaxConnector(Connector):
    id = "max"

    def _token(self) -> str:
        token = self.fields.get("token")
        if not token:
            raise ValueError("нет токена MAX-бота")
        return token

    def _headers(self) -> dict[str, str]:
        """Do not put a bot token in the URL: URLs commonly reach access logs."""
        return {
            "Authorization": self._token(),
            "Content-Type": "application/json",
        }

    def test_connection(self) -> dict[str, Any]:
        r = requests.get(f"{API_BASE}/me", headers=self._headers(), timeout=15)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json()
        return {"ok": True, "detail": f"бот @{data.get('username', '?')} ({data.get('name', '')})"}

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        r = requests.post(
            f"{API_BASE}/messages",
            params={"chat_id": chat_id},
            headers=self._headers(),
            json={"text": text},
            timeout=15,
        )
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"ok": True, "detail": r.json()}
