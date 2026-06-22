"""Анализ чата/канала для каталога AXIOM (Волна C, фаза 1 — только чтение).

По цели (@username/ссылка/id) аккаунт ЧИТАЕТ чат (НЕ вступает):
  • название, тип, число участников;
  • лёгкая оценка активности (сообщений/день по последним сообщениям);
  • список админов (часто это ЛПР) — пишется в таблицу chat_admins.

Результат сохраняется в таблицы chats / chat_admins и печатается JSON-строкой
(для веб-пульта). Дедуп чата по @username; админы перезаписываются.

⚠️ Только чтение, без вступления и без рассылки. Вступление (фаза 2) — отдельно.

Запуск:
    python -m channels.chat_scan --target @somechat
    python -m channels.chat_scan --target @somechat --id 5   # обновить чат №5
"""
from __future__ import annotations

import argparse
import asyncio
import json

from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel

from channels.telegram import _build_client
from channels.tg_parser import _display_name, _resolve_scan_chat, collect_admins
from db import database


def _kind(entity) -> str:
    if isinstance(entity, Channel):
        if entity.megagroup:
            return "супергруппа"
        if entity.broadcast:
            return "канал"
    return "группа"


async def _activity(client, entity) -> str | None:
    """Грубая оценка: сообщений/день по последним ~80 сообщениям чата/обсуждения."""
    try:
        chat = await _resolve_scan_chat(client, entity)
        if chat is None:
            return None
        dates = []
        async for m in client.iter_messages(chat, limit=80):
            if m.date:
                dates.append(m.date)
        if len(dates) < 2:
            return None
        span_days = max((dates[0] - dates[-1]).total_seconds() / 86400, 0.04)
        per_day = round(len(dates) / span_days)
        return f"~{per_day} сообщений/день"
    except Exception:  # noqa: BLE001
        return None


async def run(target: str, chat_id: int | None) -> None:
    database.init_db()
    client = _build_client()
    await client.start()
    entity = await client.get_entity(target)

    title = getattr(entity, "title", None) or target
    username = getattr(entity, "username", None)
    kind = _kind(entity)
    members = getattr(entity, "participants_count", None)
    if not members and isinstance(entity, Channel):
        try:
            full = await client(GetFullChannelRequest(entity))
            members = getattr(full.full_chat, "participants_count", None)
        except Exception:  # noqa: BLE001
            pass

    admins = await collect_admins(client, entity)
    activity = await _activity(client, entity)
    await client.disconnect()

    link = target if (target.startswith("http") or target.startswith("t.me")) else None
    with database.get_conn() as conn:
        cid = chat_id
        if cid:
            conn.execute(
                "UPDATE chats SET title=?, username=COALESCE(?,username), kind=?, members_count=?, "
                "activity=?, status='analyzed', last_scanned_at=datetime('now') WHERE id=?",
                (title, username, kind, members, activity, cid),
            )
        else:
            row = None
            if username:
                row = conn.execute("SELECT id FROM chats WHERE username=?", (username,)).fetchone()
            if row:
                cid = row["id"]
                conn.execute(
                    "UPDATE chats SET title=?, kind=?, members_count=?, activity=?, "
                    "status='analyzed', last_scanned_at=datetime('now') WHERE id=?",
                    (title, kind, members, activity, cid),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO chats (title, username, link, kind, members_count, activity, "
                    "status, last_scanned_at) VALUES (?,?,?,?,?,?, 'analyzed', datetime('now'))",
                    (title, username, link, kind, members, activity),
                )
                cid = cur.lastrowid
        conn.execute("DELETE FROM chat_admins WHERE chat_id=?", (cid,))
        for u in admins:
            conn.execute(
                "INSERT OR IGNORE INTO chat_admins (chat_id, tg_user_id, username, name) VALUES (?,?,?,?)",
                (cid, u.id, u.username, _display_name(u)),
            )

    print(json.dumps({
        "ok": True, "chat_id": cid, "title": title, "username": username,
        "kind": kind, "members": members, "activity": activity,
        "admins": [{"username": u.username, "name": _display_name(u)} for u in admins],
    }, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM анализ чата (только чтение)")
    p.add_argument("--target", required=True, help="@username / ссылка / id чата")
    p.add_argument("--id", type=int, default=None, help="id строки в chats для обновления")
    args = p.parse_args()
    asyncio.run(run(args.target, args.id))


if __name__ == "__main__":
    main()
