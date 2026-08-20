"""Единая точка входа для web/app.py — по id коннектора даёт готовый экземпляр."""
from __future__ import annotations

from typing import Any

from .avito import AvitoConnector
from .facebook import FacebookConnector
from .instagram import InstagramConnector
from .max_messenger import MaxConnector
from .threads import ThreadsConnector
from .vk import VkConnector
from .whatsapp import WhatsAppConnector

CONNECTORS = {
    "max": MaxConnector,
    "avito": AvitoConnector,
    "threads": ThreadsConnector,
    "vk": VkConnector,
    "whatsapp": WhatsAppConnector,
    "instagram": InstagramConnector,
    "facebook": FacebookConnector,
}


def test_connection(cid: str, config: dict[str, Any]) -> dict[str, Any]:
    cls = CONNECTORS.get(cid)
    if not cls:
        return {"ok": False, "error": "неизвестный коннектор"}
    return cls(config).test_connection()
