"""Instagram — Meta Graph API, Instagram Messaging (developers.facebook.com/docs/messenger-platform/instagram).

Нужен Instagram Business-аккаунт, привязанный к Facebook-странице, приложение
Meta с прохождением App Review (scope instagram_manage_messages). Отправка —
POST /me/messages через тот же Graph API, что и Messenger.

TODO: завести Business-аккаунт + Facebook-страницу, пройти App Review у Meta.
"""
from __future__ import annotations

from typing import Any

from .base import Connector


class InstagramConnector(Connector):
    id = "instagram"

    def test_connection(self) -> dict[str, Any]:
        raise NotImplementedError("Instagram Messaging: нужен App Review у Meta")

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        raise NotImplementedError
