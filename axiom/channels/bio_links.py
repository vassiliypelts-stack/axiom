"""Bio-скан ссылок (модуль №5 Дениса: «сканирует био → достаёт ссылки, в т.ч.
спрятанные на закрытую группу»).

Люди часто оставляют в bio ссылки на свои/смежные чаты и каналы — это готовые
НОВЫЕ цели для каталога, которых нет в глобальном поиске (особенно приватные +hash).
Модуль проходит по bio уже собранных лидов (`contacts.bio`), достаёт t.me-ссылки и
заносит найденные чаты в каталог:
  • публичный @username / t.me/name → резолвим сущность (тип, участники, tg_id) →
    в каталог как обычный чат (status 'new');
  • приватный t.me/+hash или /joinchat/ → в каталог как ссылка со статусом 'new'
    и пометкой «приватный — требует вступления» (резолвить без вступления нельзя).

Только чтение bio + запись в каталог. Без вступления и рассылки.

Запуск:
    python -m channels.bio_links                 # по всем контактам с bio
    python -m channels.bio_links --limit 200      # ограничить
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re

from telethon.tl.types import Channel, Chat

from channels.telegram import _build_client
from db import database

# t.me-ссылки: приватный инвайт (t.me/+hash, t.me/joinchat/hash) и публичный (@name / t.me/name).
_RE_INVITE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]{10,})", re.I)
_RE_PUBLIC = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,32})\b", re.I)
_RE_AT = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{4,32})\b")

# не чаты — личные боты/сервисы/сам телеграм: не тащим в каталог
_SKIP = {"joinchat", "share", "addstickers", "proxy", "socks", "telegram", "durov", "s"}


def extract_links(text: str | None) -> tuple[set[str], set[str]]:
    """Из текста → ({публичные @username}, {приватные инвайт-ссылки целиком})."""
    if not text:
        return set(), set()
    invites = {f"https://t.me/+{m}" for m in _RE_INVITE.findall(text)}
    publics: set[str] = set()
    tmp = _RE_INVITE.sub(" ", text)   # вырезаем инвайты, чтобы их хвост не попал в public
    for m in _RE_PUBLIC.findall(tmp):
        if m.lower() not in _SKIP:
            publics.add(m.lower())
    for m in _RE_AT.findall(tmp):
        if m.lower() not in _SKIP:
            publics.add(m.lower())
    return publics, invites


def _kind(e) -> str:
    if isinstance(e, Channel):
        return "супергруппа" if e.megagroup else ("канал" if e.broadcast else "группа")
    return "группа"


async def run(limit: int) -> None:
    database.init_db()
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, bio FROM contacts WHERE bio IS NOT NULL AND bio<>'' "
            "ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
    if not rows:
        print(json.dumps({"ok": True, "scanned": 0, "added": 0,
                          "note": "нет контактов с bio — прогони парсер с --harvest"}, ensure_ascii=False))
        return

    publics: set[str] = set()
    invites: set[str] = set()
    for r in rows:
        p, i = extract_links(r["bio"])
        publics |= p
        invites |= i
    print(f"[bio-scan] из {len(rows)} bio: публичных {len(publics)}, приватных {len(invites)}")

    added = updated = priv_added = 0

    # приватные инвайты — резолвить без вступления нельзя, кладём как ссылку на обзор
    with database.get_conn() as conn:
        for link in invites:
            ex = conn.execute("SELECT id FROM chats WHERE link=?", (link,)).fetchone()
            if ex:
                continue
            conn.execute(
                "INSERT INTO chats (title, link, kind, status, notes) "
                "VALUES (?,?,?, 'new', 'из bio-скана: приватный, требует вступления')",
                (link, link, "приватный"),
            )
            priv_added += 1

    # публичные — резолвим (это чат/канал?), тянем участников/tg_id → в каталог
    client = _build_client()
    await client.start()
    try:
        for uname in publics:
            with database.get_conn() as conn:
                if conn.execute("SELECT 1 FROM chats WHERE username=?", (uname,)).fetchone():
                    continue
            try:
                e = await client.get_entity(uname)
            except Exception:  # noqa: BLE001
                continue  # занят пользователем/ботом/несуществует
            if not isinstance(e, (Channel, Chat)):
                continue
            members = getattr(e, "participants_count", None)
            tg_id = getattr(e, "id", None)
            with database.get_conn() as conn:
                ex = None
                if tg_id:
                    ex = conn.execute("SELECT id FROM chats WHERE tg_chat_id=?", (tg_id,)).fetchone()
                if ex:
                    conn.execute(
                        "UPDATE chats SET title=?, username=COALESCE(username,?), kind=?, "
                        "members_count=COALESCE(?,members_count), tg_chat_id=COALESCE(?,tg_chat_id) WHERE id=?",
                        (getattr(e, "title", None) or uname, uname, _kind(e), members, tg_id, ex["id"]),
                    )
                    updated += 1
                else:
                    conn.execute(
                        "INSERT INTO chats (title, username, link, kind, members_count, tg_chat_id, "
                        "status, notes) VALUES (?,?,?,?,?,?, 'new', 'из bio-скана')",
                        (getattr(e, "title", None) or uname, uname, f"https://t.me/{uname}",
                         _kind(e), members, tg_id),
                    )
                    added += 1
            await asyncio.sleep(0.4)  # антибан: дозируем резолвы
    finally:
        await client.disconnect()

    print(json.dumps({
        "ok": True, "scanned": len(rows), "public_found": len(publics), "private_found": len(invites),
        "added": added, "updated": updated, "private_added": priv_added,
    }, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM bio-скан ссылок → каталог чатов")
    p.add_argument("--limit", type=int, default=500, help="сколько контактов с bio просканировать")
    args = p.parse_args()
    asyncio.run(run(args.limit))


if __name__ == "__main__":
    main()
