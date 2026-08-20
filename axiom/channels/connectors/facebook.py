"""Facebook — Meta Graph API, Messenger Platform (developers.facebook.com/docs/messenger-platform).

Нужна Facebook-страница + приложение Meta с разрешением pages_messaging (App Review).
Отправка — POST /me/messages с page access token.

TODO: завести приложение Meta на странице AXIOM/клиента, пройти App Review.
"""
from __future__ import annotations

from typing import Any

from .base import Connector


class FacebookConnector(Connector):
    id = "facebook"

    def test_connection(self) -> dict[str, Any]:
        raise NotImplementedError("Messenger Platform: нужен App Review у Meta (pages_messaging)")

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        raise NotImplementedError
