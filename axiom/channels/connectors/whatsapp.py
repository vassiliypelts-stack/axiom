"""WhatsApp — мост уже есть отдельно (axiom/whatsapp, Baileys-бридж на Node),
аккаунт id=6 в системе. Здесь — только обёртка, чтобы канал появился в общем
реестре коннекторов и статус «подключён» был виден в «Ресурсы → Коннекторы».

TODO: довести axiom/whatsapp/index.js до состояния «подключён к кампаниям»
(см. TODO.md, пункт «WhatsApp-канал (Baileys)»), затем прокинуть его статус сюда
вместо отдельного токена — Baileys авторизуется QR, не токеном.
"""
from __future__ import annotations

from typing import Any

from .base import Connector


class WhatsAppConnector(Connector):
    id = "whatsapp"

    def test_connection(self) -> dict[str, Any]:
        raise NotImplementedError("см. axiom/whatsapp — мост поднимается отдельно, не через токен")

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        raise NotImplementedError
