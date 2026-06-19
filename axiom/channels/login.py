"""Разовый вход в Telegram → печатает StringSession для сервера.

Запусти ОДИН раз локально (там, где удобно ввести номер и код из Telegram):

    python -m channels.login

Введёшь номер телефона и код подтверждения (и пароль 2FA, если включён).
Скрипт распечатает длинную строку — вставь её в .env как TG_STRING_SESSION
(НЕ публикуй её: это полный доступ к аккаунту). После этого на сервере вход
проходит без кода.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
from channels.telegram import _parse_proxy


async def _main() -> None:
    if not config.TG_API_ID or not config.TG_API_HASH:
        print("Сначала заполни TG_API_ID и TG_API_HASH в .env (my.telegram.org).")
        return
    client = TelegramClient(StringSession(), int(config.TG_API_ID), config.TG_API_HASH, proxy=_parse_proxy())
    await client.start()  # спросит номер + код (+ 2FA-пароль при наличии)
    me = await client.get_me()
    print(f"\nВошёл как @{me.username or me.id}\n")
    print("=== TG_STRING_SESSION (вставь в .env, держи в секрете) ===")
    print(client.session.save())
    print("=== конец ===")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(_main())
