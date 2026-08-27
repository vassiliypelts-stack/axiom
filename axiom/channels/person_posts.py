"""Сообщения КОНКРЕТНОГО человека во всех чатах, где мы его видели (сырьё для досье).

ЗАЧЕМ ОТДЕЛЬНО от tg_parser --harvest. Тот собирает тексты попутно, когда идёт по
чату режимом «активные комментаторы»: сначала считает топ авторов, у них и забирает
сообщения. Человек, попавший в базу через «всех участников» (а это большинство —
список участников отдаёт всех разом), остаётся без единого сообщения, и досье по
нему строить не из чего: enrich_person читает ровно tg_user_posts. В карточке это
выглядит как «Ещё не обогащён» без единого способа что-то с этим сделать, если в
профиле пусто (ни bio, ни канала) — как у большинства участников групп.

ЧТО ДЕЛАЕТ. Берёт чаты, где человек замечен (chats из его тегов TG-парсинга +
чат-источник карточки), заходит в каждый и просит у Telegram сообщения ИМЕННО
этого автора — iter_messages(chat, from_user=...). Telegram фильтрует на своей
стороне: не нужно вычитывать всю историю чата ради одного человека, поэтому это
дёшево и по времени, и по нагрузке на аккаунт.

ДАЛЬШЕ. Тексты ложатся в tg_user_posts (дедуп по msg_id), откуда их берёт
agent/enrich_person.py и сводит в боли/страхи/желания. То есть этот модуль
закрывает ровно дыру «нечего анализировать», а сам портрет строит он же, что и
раньше — второй логики портрета не заводим.

АНТИБАН. Заход в чат = запросы с боевого аккаунта, поэтому: пауза между чатами,
лимит сообщений на чат и на человека, при FloodWait — выходим, а не ждём часами.

Запуск:
    python -m channels.person_posts --contact 123          # один человек
    python -m channels.person_posts --contact 123 --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random

from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from channels.telegram import build_client
from db import database

# Сколько сообщений человека берём максимум из ОДНОГО чата и всего.
PER_CHAT = 30
TOTAL_CAP = 60
# Пауза между чатами — заход в каждый это отдельная порция запросов.
PAUSE = (1.5, 4.0)
# Больше скольки чатов одного человека не обходим за раз: у активного участника их
# могут быть десятки, а ценность падает — портрет строится и по первым.
MAX_CHATS = 6


def _pick_live_account(exclude_protected: bool = True) -> tuple[int | None, str | None]:
    """Аккаунт для захода: живая сессия + СВОЙ прокси.

    Прокси обязателен: без него build_client (справедливо) откажется собирать клиент —
    подключение живой сессии через общий IP сжигает ключ. Родные (protected) номера
    для чтения чужих чатов не берём: это работа для расходных.
    """
    where = ("WHERE tg_session IS NOT NULL AND tg_session<>'' "
             "AND (session_state='alive' OR session_alive=1) "
             "AND COALESCE(status,'')<>'banned' "
             "AND proxy IS NOT NULL AND proxy<>'' AND COALESCE(proxy_alive,1)<>0")
    if exclude_protected:
        where += " AND COALESCE(protected,0)=0"
    with database.get_conn() as conn:
        a = conn.execute(f"SELECT id, label FROM accounts {where} ORDER BY id LIMIT 1").fetchone()
    if not a:
        return None, ("нет подходящего аккаунта: нужна живая сессия И свой живой прокси "
                      "(общий IP из .env сжигает сессию). Раздай прокси в «Аккаунтах»")
    return a["id"], None


def _chats_of(contact_id: int) -> list[dict]:
    """Где мы видели этого человека: чат-источник карточки + чаты из его сообщений.

    Ходим только по тем, у кого известен tg_chat_id (иначе резолвить чат = лишний
    ResolveUsername, самый лимитируемый вызов) и есть access_hash либо @username.
    """
    with database.get_conn() as conn:
        row = conn.execute("SELECT tg_user_id, tags FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if not row or not row["tg_user_id"]:
            return []
        uid = row["tg_user_id"]
        # 1) чаты, где его сообщения уже находили раньше (parser --harvest / прошлый заход)
        seen = [r["chat_id"] for r in conn.execute(
            "SELECT DISTINCT chat_id FROM tg_user_posts WHERE tg_user_id=? AND chat_id IS NOT NULL",
            (uid,)).fetchall()]
        # 2) чат, из которого лид приехал (chat_hits — «найден в чате»)
        hit = conn.execute(
            "SELECT chat_id FROM chat_hits WHERE tg_user_id=? AND chat_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if hit and hit["chat_id"] not in seen:
            seen.append(hit["chat_id"])
        rows: list = []
        if seen:
            qm = ",".join("?" * len(seen))
            rows = list(conn.execute(
                f"SELECT id, title, username, tg_chat_id, tg_access_hash FROM chats "
                f"WHERE id IN ({qm}) AND (tg_chat_id IS NOT NULL OR (username IS NOT NULL AND username<>''))",
                seen).fetchall())
        # 3) ГЛАВНЫЙ случай для участников: у них нет ни постов, ни chat_hits — их
        # единственный след это тег «TG-парсинг: <название чата> / участник», который
        # ставит tg_parser._save_lead. Название совпадает с chats.title, потому что
        # парсер берёт его оттуда же (entity.title).
        have = {r["id"] for r in rows}
        for part in (row["tags"] or "").split(","):
            part = part.strip()
            if not part.startswith("TG-парсинг:"):
                continue
            title = part.split(":", 1)[1].split("/")[0].strip()
            if not title:
                continue
            found = conn.execute(
                "SELECT id, title, username, tg_chat_id, tg_access_hash FROM chats "
                "WHERE title=? AND (tg_chat_id IS NOT NULL OR (username IS NOT NULL AND username<>'')) "
                "LIMIT 1", (title,)).fetchone()
            if found and found["id"] not in have:
                rows.append(found)
                have.add(found["id"])
    return [dict(r) for r in rows][:MAX_CHATS]


def _peer(ch: dict):
    """Адрес чата без лишнего резолва: id+access_hash, иначе @username."""
    from telethon.tl.types import InputPeerChannel
    if ch.get("tg_chat_id") and ch.get("tg_access_hash"):
        try:
            return InputPeerChannel(int(ch["tg_chat_id"]), int(ch["tg_access_hash"]))
        except (TypeError, ValueError):
            pass
    return ("@" + ch["username"]) if ch.get("username") else None


async def collect(contact_id: int, per_chat: int = PER_CHAT, total_cap: int = TOTAL_CAP) -> dict:
    """Собрать сообщения человека по его чатам. Возвращает сводку для веб-ручки."""
    database.init_db()
    with database.get_conn() as conn:
        c = conn.execute("SELECT id, tg_user_id, name FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if not c:
        return {"ok": False, "error": "контакт не найден"}
    if not c["tg_user_id"]:
        return {"ok": False, "error": "у контакта нет tg_user_id — сначала пробей его в Telegram"}
    chats = _chats_of(contact_id)
    if not chats:
        return {"ok": False, "error": "не знаем ни одного чата этого человека: карточка пришла "
                                      "не из парсинга чатов, либо у чата не сохранён его id"}

    acc_id, err = _pick_live_account()
    if err:
        return {"ok": False, "error": err}
    with database.get_conn() as conn:
        a = conn.execute("SELECT tg_session, proxy, api_id, api_hash, label FROM accounts WHERE id=?",
                         (acc_id,)).fetchone()
    client = build_client(StringSession(a["tg_session"]), a["proxy"], a["api_id"], a["api_hash"])

    saved_total = 0
    looked: list[str] = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {"ok": False, "error": f"сессия аккаунта «{a['label']}» не авторизована"}
        user = await client.get_entity(int(c["tg_user_id"]))
        for ch in chats:
            if saved_total >= total_cap:
                break
            peer = _peer(ch)
            if peer is None:
                continue
            posts: list[tuple] = []
            try:
                # from_user — Telegram сам отдаёт сообщения ТОЛЬКО этого автора:
                # не вычитываем всю историю чата ради одного человека.
                async for m in client.iter_messages(peer, from_user=user, limit=per_chat):
                    if m.message and m.message.strip():
                        posts.append((m.id, str(m.date), m.message.strip()[:1000]))
            except FloodWaitError as e:
                looked.append(f"{ch['title']}: флуд-лимит {e.seconds}с — остановился")
                break
            except Exception as e:  # noqa: BLE001 — не состоим в чате/нет доступа: идём дальше
                looked.append(f"{ch['title']}: {type(e).__name__}")
                continue
            if posts:
                with database.get_conn() as conn:
                    n = database.save_user_posts(conn, int(c["tg_user_id"]), ch["tg_chat_id"],
                                                 ch["title"], posts)
                saved_total += n
                looked.append(f"{ch['title']}: +{n}")
            else:
                looked.append(f"{ch['title']}: не писал")
            await asyncio.sleep(random.uniform(*PAUSE))
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    with database.get_conn() as conn:
        have = conn.execute("SELECT COUNT(*) c FROM tg_user_posts WHERE tg_user_id=?",
                            (c["tg_user_id"],)).fetchone()["c"]
    return {"ok": True, "saved": saved_total, "total_posts": have,
            "chats": len(chats), "detail": "; ".join(looked), "account": a["label"]}


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: сообщения человека из его чатов (сырьё для досье)")
    p.add_argument("--contact", type=int, required=True, help="contacts.id")
    p.add_argument("--limit", type=int, default=PER_CHAT, help="сообщений на чат")
    args = p.parse_args()
    # Пульт ждёт JSON последней строкой. Без этого любое падение (мёртвый прокси,
    # обрыв связи) прилетало бы оператору сырым трейсбеком в alert'е — ровно та же
    # болезнь, что чинили в channels/enrich_tg.
    try:
        res = asyncio.run(collect(args.contact, per_chat=args.limit))
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
