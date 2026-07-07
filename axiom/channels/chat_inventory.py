"""Инвентаризация чатов аккаунта (Волна C).

Проходит по диалогам авторизованного аккаунта и заносит в каталог (chats) все
группы/каналы, где аккаунт уже состоит: название, тип, число участников, ссылка
(если публичный), могу ли писать. Помечает in_account='yes' и joined_by=<account.id>,
чтобы было видно, сколько чатов «слушает»/держит каждый аккаунт.

Только чтение своих диалогов — без вступлений и рассылки.

Запуск:
    python -m channels.chat_inventory                # главный аккаунт из .env
    python -m channels.chat_inventory --id 9         # КОНКРЕТНЫЙ аккаунт из БД (его сессия/прокси/api)
    python -m channels.chat_inventory --id 9 --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import json

from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat

from channels.chat_scan import can_write, _kind
from channels.telegram import _build_client, build_client
from db import database


def _client_for(only_id: int | None):
    """Главный аккаунт из .env (only_id=None) или конкретный аккаунт из БД по его id."""
    if only_id is None:
        return _build_client(), None
    with database.get_conn() as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE id=?", (only_id,)).fetchone()
    if not acc:
        raise SystemExit(json.dumps({"ok": False, "error": f"аккаунт #{only_id} не найден"}, ensure_ascii=False))
    acc = dict(acc)
    if not acc.get("tg_session"):
        raise SystemExit(json.dumps(
            {"ok": False, "error": f"у аккаунта #{only_id} нет TG-сессии — подключи его (🔌 Подключить)"},
            ensure_ascii=False))
    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
    return client, only_id


async def run(limit: int, only_id: int | None = None) -> None:
    database.init_db()
    client, acc_id = _client_for(only_id)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        print(json.dumps({"ok": False, "error": "сессия не авторизована (ключ мёртв/разлогинен)"},
                         ensure_ascii=False))
        return

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
                    "can_write=?, in_account='yes', link=COALESCE(link,?), "
                    "joined_by=COALESCE(?,joined_by) WHERE id=?",
                    (title, kind, members, cw, link, acc_id, ex["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO chats (title, username, link, kind, members_count, can_write, "
                    "in_account, status, joined_by) VALUES (?,?,?,?,?,?, 'yes', 'joined', ?)",
                    (title, username, link, kind, members, cw, acc_id),
                )
                added += 1

    await client.disconnect()
    print(json.dumps({"ok": True, "account_id": acc_id, "found": found,
                      "added": added, "updated": updated}, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM инвентаризация чатов аккаунта")
    p.add_argument("--id", type=int, default=None, dest="acc_id",
                   help="id аккаунта из БД (по умолчанию — главный из .env)")
    p.add_argument("--limit", type=int, default=0, help="макс диалогов (0 = все)")
    args = p.parse_args()
    asyncio.run(run(args.limit, args.acc_id))


if __name__ == "__main__":
    main()
