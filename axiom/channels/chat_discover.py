"""Авто-поиск чатов по нише (модуль №1/№5 Дениса: «система сама находит чаты»).

По ключевым словам ниши (или явному запросу) прогоняет глобальный поиск Telegram
(SearchRequest) и заносит найденные ПУБЛИЧНЫЕ группы/каналы в каталог `chats`
(статус 'new', с tg_chat_id — чтобы дальше их анализировать/вступать/слушать как
обычные каталожные чаты). Дедуп по tg_chat_id/username. Только поиск и запись в
каталог — без вступления и рассылки (это отдельные, осознанные шаги оператора).

Запуск:
    python -m channels.chat_discover --niche 3                 # по ключам ниши #3
    python -m channels.chat_discover --query "ипотека сочи"    # по одному запросу
    python -m channels.chat_discover --niche 3 --min-members 200 --groups-only
"""
from __future__ import annotations

import argparse
import asyncio
import json

from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel

from channels.telegram import _build_client
from db import database

PER_QUERY = 30          # сколько кандидатов брать на один ключ (TG отдаёт максимум ~50)
QUERY_PAUSE = (1.5, 3.0)  # антибан-пауза между поисковыми запросами


def _kind(c: Channel) -> str:
    return "супергруппа" if c.megagroup else ("канал" if c.broadcast else "группа")


async def _search_one(client, query: str, limit: int) -> list[Channel]:
    """Публичные (с @username) группы/каналы по одному запросу. FloodWait — ждём и повторяем."""
    try:
        res = await client(SearchRequest(q=query, limit=min(limit, 50)))
    except FloodWaitError as e:
        print(f"[floodwait] жду {e.seconds}с по «{query}»")
        await asyncio.sleep(e.seconds + 5)
        res = await client(SearchRequest(q=query, limit=min(limit, 50)))
    return [c for c in res.chats if isinstance(c, Channel) and c.username]


def _keywords_for_niche(niche_id: int) -> tuple[str, list[str]]:
    with database.get_conn() as conn:
        row = conn.execute("SELECT name, keywords FROM niches WHERE id=?", (niche_id,)).fetchone()
    if not row:
        return "", []
    keys = [k.strip() for k in (row["keywords"] or "").split(",") if k.strip()]
    return row["name"], keys


async def run(niche_id: int | None, query: str | None, min_members: int,
              groups_only: bool, limit: int = PER_QUERY) -> None:
    import random

    database.init_db()
    niche_name = ""
    if query:
        queries = [query]
    else:
        niche_name, queries = _keywords_for_niche(niche_id)
        if not queries:
            print(json.dumps({"ok": False, "error": "у ниши нет ключевых слов (или ниша не найдена)"},
                             ensure_ascii=False))
            return

    client = _build_client()
    await client.start()

    seen: set[int] = set()
    found = added = updated = skipped = 0
    topic = niche_name or None
    try:
        for i, q in enumerate(queries):
            try:
                chats = await _search_one(client, q, limit)
            except Exception as e:  # noqa: BLE001
                print(f"[skip] «{q}»: {e}")
                continue
            for c in chats:
                if c.id in seen:
                    continue
                seen.add(c.id)
                found += 1
                members = getattr(c, "participants_count", None)
                if groups_only and not c.megagroup:
                    skipped += 1
                    continue
                if min_members and (members or 0) < min_members:
                    skipped += 1
                    continue
                # заносим/обновляем в каталоге (дедуп по tg_chat_id, потом @username)
                with database.get_conn() as conn:
                    ex = conn.execute("SELECT id FROM chats WHERE tg_chat_id=?", (c.id,)).fetchone()
                    if not ex:
                        ex = conn.execute("SELECT id FROM chats WHERE username=?", (c.username,)).fetchone()
                    link = f"https://t.me/{c.username}"
                    if ex:
                        conn.execute(
                            "UPDATE chats SET title=?, kind=?, members_count=COALESCE(?,members_count), "
                            "tg_chat_id=COALESCE(?,tg_chat_id), link=COALESCE(link,?), "
                            "topic=COALESCE(topic,?) WHERE id=?",
                            (c.title, _kind(c), members, c.id, link, topic, ex["id"]),
                        )
                        updated += 1
                    else:
                        conn.execute(
                            "INSERT INTO chats (title, username, link, kind, members_count, "
                            "tg_chat_id, topic, status) VALUES (?,?,?,?,?,?,?, 'new')",
                            (c.title, c.username, link, _kind(c), members, c.id, topic),
                        )
                        added += 1
            if i < len(queries) - 1:
                await asyncio.sleep(random.uniform(*QUERY_PAUSE))
    finally:
        await client.disconnect()

    print(json.dumps({
        "ok": True, "niche": niche_name or query, "queries": len(queries),
        "found": found, "added": added, "updated": updated, "skipped": skipped,
    }, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM авто-поиск чатов по нише")
    p.add_argument("--niche", type=int, default=None, help="id ниши — искать по её ключам")
    p.add_argument("--query", default=None, help="явный поисковый запрос (вместо ниши)")
    p.add_argument("--min-members", type=int, default=0, help="отсекать чаты меньше N участников")
    p.add_argument("--groups-only", action="store_true", help="только супергруппы (где можно писать), без каналов")
    args = p.parse_args()
    if not args.niche and not args.query:
        p.error("нужен --niche <id> или --query <текст>")
    asyncio.run(run(args.niche, args.query, args.min_members, args.groups_only))


if __name__ == "__main__":
    main()
