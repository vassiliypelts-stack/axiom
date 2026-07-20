"""Прослушка чатов каталога по ключевым словам (лиды по нишам).

Поллинг-режим (заходит и сканирует, в духе «раз в день»): по каждому чату из
каталога (где аккаунт может читать) проходит новые сообщения, ищет ключи активных
ниш и кладёт находки в очередь chat_hits — НА ОБЗОР ОПЕРАТОРУ (не сразу в лиды).
Оператор в пульте смотрит переписку и кнопкой заносит в CRM.

Watermark: chats.kw_last_id — чтобы не пересканировать старое.

Запуск:
    python -m channels.chat_keywords
    python -m channels.chat_keywords --limit 400   # глубина по каждому чату
"""
from __future__ import annotations

import argparse
import asyncio
import json

from telethon.sessions import StringSession
from telethon.tl.types import PeerChannel, PeerChat, User

from channels.telegram import _build_client, build_client
from db import database


def _load_niches(conn) -> list[tuple[int, list[str]]]:
    rows = conn.execute("SELECT id, keywords FROM niches WHERE active=1").fetchall()
    out = []
    for r in rows:
        kws = [k.strip().lower() for k in (r["keywords"] or "").split(",") if k.strip()]
        if kws:
            out.append((r["id"], kws))
    return out


def _match(text: str, niches: list[tuple[int, list[str]]]):
    low = text.lower()
    for nid, kws in niches:
        for kw in kws:
            if kw in low:
                return nid, kw
    return None


def _display_name(u: User) -> str:
    name = " ".join(x for x in [u.first_name, u.last_name] if x).strip()
    return name or (u.username and f"@{u.username}") or str(u.id)


def _target(ch) -> object:
    """Что скармливать Telethon. Публичный — «@username». Закрытый — PeerChannel(tg_chat_id),
    а НЕ голый int: от голого числа Telethon гадает тип и принимает id супергруппы за
    PeerUser («Could not find the input entity»). И уж точно не ch["id"] — это номер строки
    в нашем каталоге, для Telegram он пустой звук."""
    if ch["username"]:
        return "@" + ch["username"]
    if (ch["kind"] or "") in ("супергруппа", "канал"):
        return PeerChannel(ch["tg_chat_id"])
    return PeerChat(ch["tg_chat_id"])


async def _client_for(acc_id: int | None):
    """Клиент того аккаунта, который вступил в чат. Закрытый чат читается ТОЛЬКО его
    участником — главный аккаунт из .env туда не вхож, сколько id ему ни давай."""
    if not acc_id:
        return None, None
    with database.get_conn() as conn:
        a = conn.execute("SELECT id, label, tg_session, proxy, api_id, api_hash, session_alive "
                         "FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not a or not (a["tg_session"] or "").strip():
        return None, f"#{acc_id}: нет сессии"
    if a["session_alive"] == 0:
        return None, f"#{acc_id} ({a['label']}): сессия слетела — нужен релогин"
    try:
        cl = build_client(StringSession(a["tg_session"]), a["proxy"], a["api_id"], a["api_hash"])
        await cl.connect()
        if not await cl.is_user_authorized():
            await cl.disconnect()
            return None, f"#{acc_id} ({a['label']}): сессия не авторизована"
        return cl, None
    except Exception as e:  # noqa: BLE001
        return None, f"#{acc_id} ({a['label']}): не подключился — {str(e)[:60]}"


async def run(limit: int, only_fav: bool = False) -> None:
    database.init_db()
    with database.get_conn() as conn:
        niches = _load_niches(conn)
        sql = ("SELECT id, title, username, tg_chat_id, kind, joined_by, kw_last_id FROM chats "
               "WHERE ((username IS NOT NULL AND username<>'') "
               "OR (in_account='yes' AND tg_chat_id IS NOT NULL))")
        if only_fav:
            sql += " AND COALESCE(favorite,0)=1"   # слушаем только избранные (лучшие) чаты
        chats = conn.execute(sql).fetchall()
    if not niches:
        print(json.dumps({"ok": False, "error": "нет активных ниш"}, ensure_ascii=False)); return
    if not chats:
        msg = ("нет ⭐ избранных чатов — отметь лучшие звёздочкой в каталоге «Чаты»"
               if only_fav else "нет чатов в каталоге для прослушки")
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False)); return

    main_client = _build_client()
    await main_client.start()
    owned: dict[int, object] = {}      # aid → клиент аккаунта-участника (по одному на аккаунт)
    scanned = hits = 0
    skipped: list[str] = []
    for ch in chats:
        # Публичный читаем главным аккаунтом; закрытый — только тем, кто в нём состоит.
        client = main_client
        if not ch["username"]:
            aid = ch["joined_by"]
            if aid not in owned:
                owned[aid], err = await _client_for(aid)
                if err:
                    skipped.append(f"{(ch['title'] or '')[:24]} — {err}")
            client = owned.get(aid)
            if client is None:
                if not ch["joined_by"]:
                    skipped.append(f"{(ch['title'] or '')[:24]} — не вступил ни один аккаунт")
                continue
        target = _target(ch)
        last_id = ch["kw_last_id"] or 0
        max_id = last_id
        try:
            async for msg in client.iter_messages(target, limit=limit, min_id=last_id):
                if not (msg.message and msg.sender_id and msg.sender_id > 0):
                    continue
                max_id = max(max_id, msg.id)
                m = _match(msg.message, niches)
                if not m:
                    continue
                nid, kw = m
                try:
                    sender = await msg.get_sender()
                except Exception:  # noqa: BLE001
                    sender = None
                if not isinstance(sender, User) or sender.bot or sender.deleted:
                    continue
                with database.get_conn() as conn:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO chat_hits (niche_id, chat_id, chat_title, tg_user_id, "
                        "username, name, text, keyword, source_msg_id, ts, status) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?, 'new')",
                        (nid, ch["id"], ch["title"], sender.id, sender.username,
                         _display_name(sender), msg.message.strip()[:500], kw, msg.id,
                         str(msg.date) if msg.date else None),
                    )
                    if cur.rowcount > 0:
                        hits += 1
            scanned += 1
            if max_id > last_id:
                with database.get_conn() as conn:
                    conn.execute("UPDATE chats SET kw_last_id=? WHERE id=?", (max_id, ch["id"]))
        except Exception as e:  # noqa: BLE001
            print(f"[kw] {(ch['title'] or target)}: {e}")
        await asyncio.sleep(1.5)  # антибан-пауза между чатами

    await main_client.disconnect()
    for cl in owned.values():
        if cl is not None:
            try:
                await cl.disconnect()
            except Exception:  # noqa: BLE001
                pass
    for s in skipped:
        print(f"[kw] пропущен: {s}")
    print(json.dumps({"ok": True, "scanned_chats": scanned, "hits_new": hits,
                      "skipped": skipped}, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM прослушка чатов по ключам ниш")
    p.add_argument("--limit", type=int, default=300, help="глубина сканирования по каждому чату")
    p.add_argument("--favorites", action="store_true", help="слушать только ⭐ избранные чаты")
    args = p.parse_args()
    asyncio.run(run(args.limit, args.favorites))


if __name__ == "__main__":
    main()
