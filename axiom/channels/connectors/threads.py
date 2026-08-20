"""Threads — Graph API Threads (graph.threads.net), только публикация постов/ответов,
личных сообщений нет (в отличие от остальных каналов тут не лидген-переписка, а контент-канал).

Нужна Tech Provider Verification в Meta for Developers, OAuth-приложение со scope
threads_content_publish, лимит 250 постов/24ч. Публикация — двухшаговая: POST /threads
(создать контейнер) → POST /threads_publish (опубликовать по creation_id).

TODO: пройти верификацию у Meta, завести приложение, получить long-lived access token.
"""
from __future__ import annotations

from typing import Any

from .base import Connector


class ThreadsConnector(Connector):
    id = "threads"

    def test_connection(self) -> dict[str, Any]:
        raise NotImplementedError("Threads API: нужна Tech Provider Verification у Meta")

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        raise NotImplementedError("Threads — это публикация постов, не ЛС; используй publish_post()")

    def publish_post(self, text: str) -> dict[str, Any]:
        raise NotImplementedError
