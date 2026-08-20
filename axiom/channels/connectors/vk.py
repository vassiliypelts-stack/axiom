"""VK — Bot API / Callback API для сообщений сообщества (vk.com/dev/bots_docs).

Токен сообщества выдаётся без верификации юрлица (Управление → Работа с API → Ключи
доступа). Отправка: POST https://api.vk.com/method/messages.send с access_token,
peer_id, message, random_id, v=5.199. Приём — Callback API (вебхук на message_new)
или Bots Long Poll API.

TODO: получить токен сообщества, реализовать messages.send + подписку Callback API.
"""
from __future__ import annotations

from typing import Any

from .base import Connector


class VkConnector(Connector):
    id = "vk"

    def test_connection(self) -> dict[str, Any]:
        raise NotImplementedError

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        raise NotImplementedError
