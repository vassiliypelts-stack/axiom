"""Авито — Messenger API (developers.avito.ru/api-catalog/messenger).

Доступ по заявке в партнёрском кабинете Авито (не самостоятельная регистрация).
Нужны client_id/client_secret (OAuth2 client_credentials), user_id аккаунта Авито,
и вебхук-эндпоинт на входящие сообщения (подпись в заголовке x-avito-messenger-signature).

TODO: подать заявку на доступ к Messenger API, получить client_id/secret,
реализовать OAuth-обмен токена и POST /messenger/v1/accounts/{user_id}/chats/{chat_id}/messages.
"""
from __future__ import annotations

from typing import Any

from .base import Connector


class AvitoConnector(Connector):
    id = "avito"

    def test_connection(self) -> dict[str, Any]:
        raise NotImplementedError("Avito Messenger API: нужен доступ по заявке в партнёрском кабинете")

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        raise NotImplementedError
