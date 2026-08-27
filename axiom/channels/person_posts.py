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


def _live_accounts(exclude_protected: bool = True) -> list[dict]:
    """Кандидаты на заход: живая сессия + СВОЙ прокси. Список, а не один.

    Прокси обязателен: без него build_client (справедливо) откажется собирать клиент —
    подключение живой сессии через общий IP сжигает ключ. Родные (protected) номера
    для чтения чужих чатов не берём: это работа для расходных.

    Почему список. session_state в БД отстаёт от реальности: аккаунт может быть
    помечен 'alive', а Telegram уже отозвал ключ (слушатель это видит, но узнать об
    этом карточка могла и не успеть). Один такой в начале списка не должен обрывать
    всю операцию — берём следующего.
    """
    where = ("WHERE tg_session IS NOT NULL AND tg_session<>'' "
             "AND (session_state='alive' OR session_alive=1) "
             "AND COALESCE(status,'')<>'banned' "
             "AND proxy IS NOT NULL AND proxy<>'' AND COALESCE(proxy_alive,1)<>0")
    if exclude_protected:
        where += " AND COALESCE(protected,0)=0"
    with database.get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, label, tg_session, proxy, api_id, api_hash FROM accounts {where} "
            f"ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _mark_revoked(acc_id: int, err: Exception) -> None:
    """Ключ отозван Telegram — записать в карточку, чтобы следующий модуль не
    выяснял это заново собственным падением."""
    try:
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE accounts SET session_alive=0, session_state='revoked', "
                "session_reason=?, session_checked_at=datetime('now') WHERE id=?",
                (f"{type(err).__name__}: {str(err)[:150]}", acc_id))
    except Exception:  # noqa: BLE001
        pass


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
                f"SELECT id, title, username, link, tg_chat_id, tg_access_hash, joined_by FROM chats "
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
                "SELECT id, title, username, link, tg_chat_id, tg_access_hash, joined_by FROM chats "
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


async def _resolve_user(client, c) -> object:
    """Пользователь как его поймёт Telethon: сначала @username, потом числовой id.

    Голый tg_user_id рабочий только там, где у аккаунта уже есть access_hash этого
    человека (видел его в общем чате в ЭТОЙ сессии). У свежего аккаунта его нет, и
    get_entity(id) падает «Cannot find any entity corresponding to ...» — именно на
    этом спотыкались 4 аккаунта подряд. @username резолвится всегда.
    """
    uname = (c["username"] or "").strip().lstrip("@")
    if uname:
        try:
            return await client.get_entity(uname)
        except Exception:  # noqa: BLE001 — ник сменили/занят: пробуем по id
            pass
    return await client.get_entity(int(c["tg_user_id"]))


async def collect(contact_id: int, per_chat: int = PER_CHAT, total_cap: int = TOTAL_CAP) -> dict:
    """Собрать сообщения человека по его чатам. Возвращает сводку для веб-ручки."""
    database.init_db()
    with database.get_conn() as conn:
        c = conn.execute("SELECT id, tg_user_id, username, name FROM contacts WHERE id=?",
                         (contact_id,)).fetchone()
    if not c:
        return {"ok": False, "error": "контакт не найден"}
    if not c["tg_user_id"]:
        return {"ok": False, "error": "у контакта нет tg_user_id — сначала пробей его в Telegram"}
    chats = _chats_of(contact_id)
    if not chats:
        return {"ok": False, "error": "не знаем ни одного чата этого человека: карточка пришла "
                                      "не из парсинга чатов, либо у чата не сохранён его id"}

    # КАЖДЫЙ чат читаем ТЕМ аккаунтом, который в нём состоит (chats.joined_by).
    # access_hash Telegram выдаёт индивидуально каждому аккаунту: чужой ключ к тому же
    # чату не подходит и даёт ChannelInvalidError. Плюс в приватную группу посторонний
    # аккаунт всё равно не попадёт — читать может только участник.
    by_id = {x["id"]: x for x in _live_accounts(exclude_protected=False)}
    saved_total = 0
    looked: list[str] = []
    used: list[str] = []
    joinable: list[dict] = []
    clients: dict = {}          # acc_id -> (client, user) — переиспользуем на все его чаты

    async def _client_for(acc_id: int):
        """Поднять аккаунт и резолвить им человека. Кэшируем: у одного аккаунта может
        быть несколько чатов из списка, второй раз подключаться незачем."""
        if acc_id in clients:
            return clients[acc_id]
        cand = by_id.get(acc_id)
        if cand is None:
            raise RuntimeError("аккаунт чата недоступен (мёртвая сессия или нет прокси)")
        cl = build_client(StringSession(cand["tg_session"]), cand["proxy"],
                          cand["api_id"], cand["api_hash"])
        try:
            await cl.connect()
            if not await cl.is_user_authorized():
                raise RuntimeError("сессия не авторизована")
            u = await _resolve_user(cl, c)
        except Exception:
            try:
                await cl.disconnect()
            except Exception:  # noqa: BLE001
                pass
            raise
        clients[acc_id] = (cl, u)
        used.append(cand["label"])
        return clients[acc_id]

    try:
        for ch in chats:
            if saved_total >= total_cap:
                break
            peer = _peer(ch)
            if peer is None:
                looked.append(f"{ch['title']}: нечем адресовать (нет @username и ключа доступа)")
                continue
            if not ch.get("joined_by"):
                # В чате нет ни одного нашего аккаунта — читать физически некому.
                # Не вступаем молча: вступление видно в списке участников чата и при
                # частых входах ведёт к бану, поэтому решение оставляем оператору —
                # отдаём чат наверх как «можно войти» (см. joinable в ответе).
                looked.append(f"{ch['title']}: никто из наших не состоит")
                if (ch.get("username") or "").strip() or (ch.get("link") or "").strip():
                    joinable.append({"id": ch["id"], "title": ch["title"]})
                continue
            try:
                client, user = await _client_for(int(ch["joined_by"]))
            except Exception as e:  # noqa: BLE001
                name = type(e).__name__
                if name in ("AuthKeyDuplicatedError", "AuthKeyUnregisteredError",
                            "SessionRevokedError", "SessionExpiredError", "AuthKeyInvalidError"):
                    _mark_revoked(int(ch["joined_by"]), e)
                looked.append(f"{ch['title']}: аккаунт не поднялся ({name})")
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
            except Exception as e:  # noqa: BLE001 — нет доступа к чату: идём дальше
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
        for cl, _u in clients.values():
            try:
                await cl.disconnect()
            except Exception:  # noqa: BLE001
                pass

    with database.get_conn() as conn:
        have = conn.execute("SELECT COUNT(*) c FROM tg_user_posts WHERE tg_user_id=?",
                            (c["tg_user_id"],)).fetchone()["c"]
    return {"ok": True, "saved": saved_total, "total_posts": have,
            "chats": len(chats), "detail": "; ".join(looked), "joinable": joinable,
            "account": ", ".join(dict.fromkeys(used)) or "—"}


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
