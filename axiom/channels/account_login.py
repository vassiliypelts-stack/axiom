"""Логин аккаунта из «Мои агенты» в Telegram → сохранить сессию в БД.

Разовая операция на каждый номер (Telegram пришлёт код в приложение/SMS — вводишь
руками). После этого аккаунт можно прогревать и слать с него headless.

    python -m channels.account_login --id 4      # id из раздела «Мои агенты»

Сессия (полный доступ к аккаунту) ложится в accounts.tg_session — это секрет, БД
не публикуй.
"""
from __future__ import annotations

import argparse
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
from channels.telegram import parse_proxy_str
from db import database


async def _login(acc_id: int) -> None:
    if not config.TG_API_ID or not config.TG_API_HASH:
        print("Заполни TG_API_ID и TG_API_HASH в .env")
        return
    database.init_db()
    with database.get_conn() as conn:
        acc = database.get_account(conn, acc_id)
    if not acc:
        print(f"аккаунта #{acc_id} нет в «Мои агенты»")
        return
    phone = (acc["phone"] or "").strip()
    if not phone:
        print(f"у аккаунта #{acc_id} не указан телефон")
        return
    print(f"Логиню #{acc_id} «{acc['label'] or ''}» {phone}.")
    print("Telegram пришлёт код — введи его здесь (и пароль 2FA, если включён).")
    proxy = parse_proxy_str(acc["proxy"])
    client = TelegramClient(StringSession(), int(config.TG_API_ID), config.TG_API_HASH, proxy=proxy)
    await client.start(phone=phone)
    me = await client.get_me()
    with database.get_conn() as conn:
        database.save_account_session(conn, acc_id, client.session.save(), me.username)
    print(f"\nOK: вошёл как @{me.username or me.id}. Сессия сохранена (accounts.tg_session #{acc_id}).")
    print("Теперь можно прогревать: python -m channels.warmup --run")
    await client.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description="Логин аккаунта AXIOM в Telegram (сессия в БД)")
    p.add_argument("--id", type=int, required=True, help="id аккаунта из «Мои агенты»")
    args = p.parse_args()
    asyncio.run(_login(args.id))


if __name__ == "__main__":
    main()
