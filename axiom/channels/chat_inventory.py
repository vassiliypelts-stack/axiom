"""Инвентаризация чатов личного аккаунта (Волна C).

Проходит по диалогам авторизованного аккаунта и заносит в каталог (chats) все
группы/каналы, где аккаунт уже состоит: название, тип, число участников, ссылка
(если публичный), могу ли писать. Помечает in_account='yes'. Дедуп по @username.

Только чтение своих диалогов — без вступлений и рассылки.

Запуск:
    python -m channels.chat_inventory
    python -m channels.chat_inventory --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import json

from telethon.tl.types import Channel, Chat

from channels.chat_scan import can_write, _kind
from channels.telegram import _build_client
from db import database


async def run(limit: int) -> None:
    database.init_db()
    client = _build_client()
    await client.start()

    found = added = updated = 0
    rows = []
    async for dialog in client.iter_dialogs(limit=limit or None):
        e = dialog.entity
        if not isinstance(e, (Channel, Chat)):
            continue  # пропускаем личные диалоги с людьми
        found += 1
        title = getattr(e, "title", None) or "—"
        username = getattr(e, "username", None)
        kind = _kind(e)
        members = getattr(e, "participants_count", None)
        link = f"https://t.me/{username}" if username else None
        cw = can_write(e)
        rows.append((title, username, link, kind, members, cw))

    with database.get_conn() as conn:
        for title, username, link, kind, members, cw in rows:
            ex = None
            if username:
                ex = conn.execute("SELECT id FROM chats WHERE username=?", (username,)).fetchone()
            if ex:
                conn.execute(
                    "UPDATE chats SET title=?, kind=?, members_count=COALESCE(?,members_count), "
                    "can_write=?, in_account='yes', link=COALESCE(link,?) WHERE id=?",
                    (title, kind, members, cw, link, ex["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO chats (title, username, link, kind, members_count, can_write, "
                    "in_account, status) VALUES (?,?,?,?,?,?, 'yes', 'joined')",
                    (title, username, link, kind, members, cw),
                )
                added += 1

    await client.disconnect()
    print(json.dumps({"ok": True, "found": found, "added": added, "updated": updated},
                     ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM инвентаризация чатов аккаунта")
    p.add_argument("--limit", type=int, default=0, help="макс диалогов (0 = все)")
    args = p.parse_args()
    asyncio.run(run(args.limit))


if __name__ == "__main__":
    main()
