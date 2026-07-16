"""Размножение каталога по ПОХОЖИМ чатам (модуль Дениса «находит 1000 похожих»).

Телеграм сам знает, какие каналы похожи друг на друга (та же выдача, что в приложении
показывается блоком «Похожие каналы») — берём её через GetChannelRecommendations и
заносим находки в каталог `chats`. В отличие от `chat_discover` (глобальный поиск по
словам) здесь мы растём ОТ УЖЕ НАЙДЕННОГО: каждый хороший чат приводит соседей.

«Размножение» = snowball по кругам (--depth): круг 1 — похожие на затравку, круг 2 —
похожие на находки круга 1, и т.д. Рост лавинообразный, поэтому есть жёсткий потолок
--max-new и антибан-паузы.

Затравка (что берём за основу):
    --chat <id>      один каталожный чат
    --favorites      все избранные (⭐) — обычно самое осмысленное
    --niche <id>     все чаты с topic = названию ниши
    (без флагов)     все проанализированные/присоединённые чаты каталога

Только поиск и запись в каталог — без вступления и рассылки.

Запуск:
    python -m channels.chat_similar --favorites
    python -m channels.chat_similar --chat 42 --depth 2 --min-members 500
    python -m channels.chat_similar --niche 3 --groups-only --max-new 300
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random

from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import GetChannelRecommendationsRequest
from telethon.tl.types import Channel

from channels.chat_keywords import _target
from channels.telegram import _build_client
from db import database

SEED_PAUSE = (2.0, 4.5)   # антибан-пауза между затравками
MAX_NEW = 500             # потолок новых чатов за прогон (snowball растёт лавиной)
MAX_SEEDS = 200           # потолок затравок за прогон


def _kind(c: Channel) -> str:
    return "супергруппа" if c.megagroup else ("канал" if c.broadcast else "группа")


def _seed_chats(chat_id: int | None, favorites: bool, niche_id: int | None) -> list[dict]:
    """Затравки из каталога: только с tg_chat_id или @username (иначе не резолвятся)."""
    where = "(tg_chat_id IS NOT NULL OR (username IS NOT NULL AND username<>''))"
    params: list = []
    if chat_id:
        where += " AND id=?"
        params.append(chat_id)
    elif favorites:
        where += " AND COALESCE(favorite,0)=1"
    elif niche_id:
        with database.get_conn() as conn:
            row = conn.execute("SELECT name FROM niches WHERE id=?", (niche_id,)).fetchone()
        if not row:
            return []
        where += " AND topic=?"
        params.append(row["name"])
    else:
        where += " AND status IN ('analyzed','joined')"
    with database.get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, title, username, tg_chat_id, kind, topic FROM chats WHERE {where} "
            f"AND status NOT IN ('skip','banned') ORDER BY COALESCE(favorite,0) DESC, "
            f"COALESCE(members_count,0) DESC LIMIT ?", (*params, MAX_SEEDS)
        ).fetchall()
    return [dict(r) for r in rows]


async def _recommend(client, seed: dict) -> list[Channel]:
    """Похожие на затравку. FloodWait — ждём и повторяем один раз.

    Резолвим через общий `chat_keywords._target`: голый int Telethon'у давать нельзя —
    он гадает тип и принимает id супергруппы за PeerUser, из-за чего ВСЕ затравки без
    @username молча уходили в [skip]."""
    ent = await client.get_entity(_target(seed))
    if not isinstance(ent, Channel):
        return []   # рекомендации есть только у каналов/супергрупп
    try:
        res = await client(GetChannelRecommendationsRequest(channel=ent))
    except FloodWaitError as e:
        print(f"[floodwait] жду {e.seconds}с на «{seed.get('title')}»")
        await asyncio.sleep(e.seconds + 5)
        res = await client(GetChannelRecommendationsRequest(channel=ent))
    return [c for c in res.chats if isinstance(c, Channel)]


def _upsert(c: Channel, topic: str | None, seed_title: str) -> tuple[str, int | None]:
    """Чат в каталог. Возвращает ('added'|'updated', id новой записи). Дедуп: tg_chat_id → username."""
    link = f"https://t.me/{c.username}" if c.username else None
    members = getattr(c, "participants_count", None)
    with database.get_conn() as conn:
        ex = conn.execute("SELECT id FROM chats WHERE tg_chat_id=?", (c.id,)).fetchone()
        if not ex and c.username:
            ex = conn.execute("SELECT id FROM chats WHERE username=?", (c.username,)).fetchone()
        if ex:
            conn.execute(
                "UPDATE chats SET title=?, kind=?, members_count=COALESCE(?,members_count), "
                "tg_chat_id=COALESCE(?,tg_chat_id), link=COALESCE(link,?), topic=COALESCE(topic,?) "
                "WHERE id=?",
                (c.title, _kind(c), members, c.id, link, topic, ex["id"]),
            )
            return "updated", None
        cur = conn.execute(
            "INSERT INTO chats (title, username, link, kind, members_count, tg_chat_id, topic, "
            "status, notes) VALUES (?,?,?,?,?,?,?, 'new', ?)",
            (c.title, c.username, link, _kind(c), members, c.id, topic,
             f"похож на «{seed_title}» (рекомендации TG)"),
        )
        return "added", cur.lastrowid


async def run(chat_id: int | None, favorites: bool, niche_id: int | None, depth: int,
              min_members: int, groups_only: bool, max_new: int = MAX_NEW, join: int = 0) -> None:
    database.init_db()
    seeds = _seed_chats(chat_id, favorites, niche_id)
    if not seeds:
        print(json.dumps({"ok": False, "error": "нет затравок: в каталоге нет подходящих чатов "
                                                "(нужен tg_chat_id или @username)"}, ensure_ascii=False))
        return

    client = _build_client()
    await client.start()

    # затравки помечаем виденными и по tg_chat_id, и по @username: у части чатов
    # tg_chat_id ещё не заполнен (см. channels/backfill.py), и без username-ключа
    # Telegram вернул бы их же в рекомендациях соседа как «находку»
    seen: set[int] = {s["tg_chat_id"] for s in seeds if s.get("tg_chat_id")}
    seen_names: set[str] = {s["username"].lower() for s in seeds if s.get("username")}
    new_ids: list[int] = []
    found = added = updated = skipped = 0
    circles: list[dict] = []
    # круг = список затравок; находки круга становятся затравками следующего
    wave = seeds
    try:
        for lvl in range(1, depth + 1):
            next_wave: list[dict] = []
            c_added = c_found = 0
            for i, seed in enumerate(wave):
                if added >= max_new:
                    break
                try:
                    sims = await _recommend(client, seed)
                except Exception as e:  # noqa: BLE001
                    print(f"[skip] «{seed.get('title')}»: {e}")
                    continue
                for c in sims:
                    uname = (c.username or "").lower()
                    if c.id in seen or (uname and uname in seen_names):
                        continue
                    seen.add(c.id)
                    if uname:
                        seen_names.add(uname)
                    found += 1
                    c_found += 1
                    members = getattr(c, "participants_count", None)
                    if groups_only and not c.megagroup:
                        skipped += 1
                        continue
                    # ВАЖНО: у Channel из рекомендаций participants_count обычно None
                    # (счётчик живёт в ChannelFull). Считать None за 0 нельзя — тогда
                    # --min-members отсекает ВСЁ подряд. Неизвестно ≠ мало: пропускаем.
                    if min_members and members is not None and members < min_members:
                        skipped += 1
                        continue
                    res, new_id = _upsert(c, seed.get("topic"), seed.get("title") or "?")
                    if res == "added":
                        added += 1
                        c_added += 1
                        if new_id:
                            new_ids.append(new_id)
                    else:
                        updated += 1
                    # kind обязателен: по нему _target выбирает PeerChannel vs PeerChat
                    next_wave.append({"title": c.title, "username": c.username,
                                      "tg_chat_id": c.id, "kind": _kind(c),
                                      "topic": seed.get("topic")})
                    if added >= max_new:
                        print(f"[стоп] потолок {max_new} новых чатов за прогон")
                        break
                if i < len(wave) - 1:
                    await asyncio.sleep(random.uniform(*SEED_PAUSE))
            circles.append({"depth": lvl, "seeds": len(wave), "found": c_found, "added": c_added})
            print(f"[круг {lvl}] затравок {len(wave)} → найдено {c_found}, новых {c_added}")
            if not next_wave or added >= max_new:
                break
            wave = next_wave[:MAX_SEEDS]
    finally:
        await client.disconnect()

    summary = {
        "ok": True, "seeds": len(seeds), "depth": depth, "found": found,
        "added": added, "updated": updated, "skipped": skipped, "circles": circles,
    }
    if join and new_ids:
        from channels import chat_join
        print(f"[join] вступаю в {len(new_ids)} новых чат(ов), до {join} на аккаунт…")
        summary["join"] = await chat_join.run(per=join, favorites=False, only_id=None,
                                              chat_ids=new_ids)
    print(json.dumps(summary, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: размножение каталога по похожим чатам")
    p.add_argument("--chat", type=int, default=None, help="id каталожного чата — искать похожие на него")
    p.add_argument("--favorites", action="store_true", help="затравка — все избранные (⭐) чаты")
    p.add_argument("--niche", type=int, default=None, help="затравка — чаты с темой этой ниши")
    p.add_argument("--depth", type=int, default=1, help="кругов размножения (1 = только соседи затравок)")
    p.add_argument("--min-members", type=int, default=0, help="отсекать чаты меньше N участников")
    p.add_argument("--groups-only", action="store_true", help="только супергруппы, без каналов")
    p.add_argument("--max-new", type=int, default=MAX_NEW, help="потолок новых чатов за прогон")
    p.add_argument("--join", type=int, default=0, metavar="N",
                   help="сразу вступить армией в найденное: до N новых чатов на аккаунт (0 = не вступать)")
    args = p.parse_args()
    depth = max(1, min(args.depth, 4))
    asyncio.run(run(args.chat, args.favorites, args.niche, depth,
                    args.min_members, args.groups_only, args.max_new, join=args.join))


if __name__ == "__main__":
    main()
