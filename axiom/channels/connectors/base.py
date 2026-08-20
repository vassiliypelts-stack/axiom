"""Общий интерфейс коннектора канала."""
from __future__ import annotations

from typing import Any


class Connector:
    """Каждый канал реализует эти два метода поверх своего API."""

    id: str = ""

    def __init__(self, config: dict[str, Any]):
        self.fields = config.get("fields") or {}
        self.note = config.get("note") or ""

    def test_connection(self) -> dict[str, Any]:
        """Дешёвый вызов API (кто я / список чатов), чтобы проверить токен живой."""
        raise NotImplementedError

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        raise NotImplementedError
