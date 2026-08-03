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
from telethon.errors import FloodWaitError
from telethon.tl.types import (InputPeerChannel, InputPeerChat, PeerChannel, User)

from channels.telegram import _build_client, build_client, client_for_account
from channels import hit_intent
from db import database


def _load_niches(conn) -> list[tuple[int, list[str], str]]:
    rows = conn.execute(
        "SELECT id, keywords, COALESCE(hunt_mode,'clients') AS hunt_mode "
        "FROM niches WHERE active=1").fetchall()
    out = []
    for r in rows:
        kws = [k.strip().lower() for k in (r["keywords"] or "").split(",") if k.strip()]
        if kws:
            out.append((r["id"], kws, r["hunt_mode"]))
    return out


def _match(text: str, niches: list[tuple[int, list[str], str]]):
    low = text.lower()
    for nid, kws, mode in niches:
        for kw in kws:
            if kw in low:
                return nid, kw, mode
    return None


def _display_name(u: User) -> str:
    name = " ".join(x for x in [u.first_name, u.last_name] if x).strip()
    return name or (u.username and f"@{u.username}") or str(u.id)


def _mark_scanned(chat_id: int) -> None:
    """Отметка «заходили» — двигает чат в конец ротации. Ставится ВСЕГДА: и когда новых
    сообщений не было, и когда чат упал с ошибкой, и когда его нечем читать. Иначе такой
    чат навечно остаётся первым в очереди и заслоняет весь остальной каталог.

    Ошибку глушим намеренно: вызов стоит в finally, а «database is locked» оттуда выбросил
    бы исключение мимо цикла — прогон оборвался бы без disconnect'а клиентов и без отчёта.
    Не проставленная отметка стоит лишнего захода, необорванный прогон — висящих сессий.
    """
    try:
        with database.get_conn() as conn:
            conn.execute("UPDATE chats SET kw_scanned_at=datetime('now') WHERE id=?", (chat_id,))
    except Exception as e:  # noqa: BLE001
        print(f"[kw] не отметил чат {chat_id} как просканированный: {str(e)[:80]}")


# Сколько чатов за один прогон разрешено «знакомить» с аккаунтом (спросить у Telegram
# номер и ключ доступа). Именно эти запросы — ResolveUsernameRequest — и сожгли основной
# аккаунт на 13.8 часа, когда код резолвил все 2.4 тысячи чатов каждый обход. Теперь
# знакомство разовое: узнали — записали в базу — больше никогда не спрашиваем. Но первый
# круг всё равно требует по запросу на чат, поэтому размазываем его по прогонам.
# 40 за прогон при почасовом запуске — весь каталог за пару суток и без ограничений.
RESOLVE_BUDGET = 40


def _is_channel(ch) -> bool:
    """Супергруппа/канал (адресуются с ключом доступа) против обычной группы (без него)."""
    return (ch["kind"] or "") in ("супергруппа", "канал") or bool(ch["username"])


def _cached_peer(ch):
    """Обращение к чату, не стоящее ни одного сетевого запроса, — или None, если нечем.

    ЗАЧЕМ. Строка «@name» заставляет Telethon сходить в Telegram за ResolveUsernameRequest:
    отдельный запрос на КАЖДЫЙ чат в КАЖДЫЙ обход. На каталоге в 2.4 тысячи чатов это
    тысячи резолвов в сутки, и Telegram ответил ограничением на 49684 секунды (13.8 часа)
    — по основному аккаунту, то есть по личному номеру.

    Одного номера чата для этого мало: к супергруппе и каналу Telegram пускает только с
    парой «номер + ключ доступа» (access_hash), выданной конкретному аккаунту. Голый
    PeerChannel(id) работает лишь пока сущность лежит в кэше Telethon, а строковая сессия
    свой кэш между запусками не хранит — отсюда «Could not find the input entity» на
    ровном месте. Поэтому ключ храним у себя (chats.tg_access_hash) и собираем
    InputPeerChannel сами: он самодостаточен и резолва не требует вовсе.

    Обычной группе (не супергруппе) ключ не нужен — там хватает номера."""
    cid = ch["tg_chat_id"]
    if not cid:
        return None
    if not _is_channel(ch):
        return InputPeerChat(int(cid))
    ah = ch["tg_access_hash"] if "tg_access_hash" in ch.keys() else None
    if ah:
        return InputPeerChannel(int(cid), int(ah))
    # Ключа нет — вдруг сущность всё же осела в кэше сессии за этот же прогон.
    # Не сработает — вызывающий откатится на «@username».
    return PeerChannel(int(cid)) if not ch["username"] else None


def _target(ch) -> object:
    """Чем адресовать чат: сначала бесплатный путь, иначе «@username» с резолвом."""
    peer = _cached_peer(ch)
    if peer is not None:
        return peer
    if ch["username"]:
        return "@" + ch["username"]
    return PeerChannel(int(ch["tg_chat_id"])) if _is_channel(ch) else InputPeerChat(int(ch["tg_chat_id"]))


async def _remember_peer(client, target, ch) -> None:
    """Сохранить номер и ключ доступа чата, чтобы следующий обход обошёлся без резолва.

    Вызывается только после успешного чтения: Telethon к этому моменту уже разрешил
    сущность и отдаёт её из своей памяти, так что лишнего запроса в Telegram здесь нет.

    Ключ пишем строкой: access_hash — 64-битное знаковое число, и в SQLite INTEGER
    часть значений легла бы с потерей."""
    if ch["tg_chat_id"] and (not _is_channel(ch) or (
            "tg_access_hash" in ch.keys() and ch["tg_access_hash"])):
        return
    try:
        ent = await client.get_input_entity(target)
    except Exception:  # noqa: BLE001 — не вышло, попробуем в следующий обход
        return
    cid = getattr(ent, "channel_id", None) or getattr(ent, "chat_id", None)
    if not cid:
        return
    ah = getattr(ent, "access_hash", None)
    try:
        with database.get_conn() as conn:
            conn.execute("UPDATE chats SET tg_chat_id=?, tg_access_hash=? WHERE id=?",
                         (int(cid), str(ah) if ah is not None else None, ch["id"]))
    except Exception as e:  # noqa: BLE001
        print(f"[kw] не сохранил ключ доступа к чату {ch['id']}: {str(e)[:80]}")


LISTEN_ACCOUNT_SETTING = "listen_account_id"  # какой аккаунт опрашивает публичные чаты


async def _main_client():
    """Клиент, которым опрашиваются публичные чаты каталога.

    ПОЧЕМУ НЕ ВСЕГДА .env-аккаунт. Раньше здесь всегда стоял _build_client() — главный
    аккаунт из .env, а это ЛИЧНЫЙ номер владельца. Активный опрос — это запросы истории
    по 2.4 тыс. чатов, и именно на нём поймали FloodWait почти на 14 часов (см. _target).
    Расходовать на это можно рабочий аккаунт из пула — если он забанится, теряется прогретый
    номер, а не личный Telegram. Настройка выбирается в пульте (⚙️ Аккаунты → «слушает чаты»)
    и хранится в app_settings под ключом listen_account_id.

    Без настройки — тот же .env, что и раньше (ничего не ломаем по умолчанию), но с
    явным предупреждением в лог: молчаливый риск хуже явного."""
    with database.get_conn() as conn:
        acc_id = database.get_setting(conn, LISTEN_ACCOUNT_SETTING)
    if not acc_id:
        print("[kw] ⚠ рабочий аккаунт для прослушки не назначен — опрашиваю с личного "
              "(.env). Назначь в пульте: Аккаунты → «слушает чаты», чтобы не рисковать личным номером.")
        return _build_client()
    try:
        client, _ = client_for_account(int(acc_id))
        return client
    except Exception as e:  # noqa: BLE001 — назначенный аккаунт недоступен, не рискуем личным молча
        print(f"[kw] ⚠ назначенный аккаунт #{acc_id} недоступен ({str(e)[:80]}) — "
              f"опрашиваю с личного (.env)")
        return _build_client()


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
        sql = ("SELECT id, title, username, tg_chat_id, tg_access_hash, kind, joined_by, "
               "kw_last_id FROM chats "
               "WHERE ((username IS NOT NULL AND username<>'') "
               "OR (in_account='yes' AND tg_chat_id IS NOT NULL))")
        if only_fav:
            sql += " AND COALESCE(favorite,0)=1"   # слушаем только избранные (лучшие) чаты
        # РОТАЦИЯ: дольше всех не сканированные — первыми (никогда не сканированные вообще
        # впереди). Без ORDER BY порядок был стабильным (по rowid), а вызывающий по
        # расписанию убивает процесс по таймауту — значит хвост каталога не сканировался
        # бы НИКОГДА, сколько раз задачу ни запусти.
        sql += " ORDER BY COALESCE(kw_scanned_at,'') ASC, id"
        chats = conn.execute(sql).fetchall()
    if not niches:
        print(json.dumps({"ok": False, "error": "нет активных ниш"}, ensure_ascii=False)); return
    if not chats:
        msg = ("нет ⭐ избранных чатов — отметь лучшие звёздочкой в каталоге «Чаты»"
               if only_fav else "нет чатов в каталоге для прослушки")
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False)); return

    main_client = await _main_client()
    await main_client.start()
    owned: dict[int, object] = {}      # aid → клиент аккаунта-участника (по одному на аккаунт)
    scanned = hits = 0
    resolves = deferred = 0
    flood_wait = 0
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
                # Отмечаем и пропущенный: иначе чат с мёртвым joined_by навсегда остаётся
                # в голове ротации (kw_scanned_at=NULL) и каждый прогон начинается с
                # бесполезной попытки коннекта по нему же.
                _mark_scanned(ch["id"])
                continue
        target = _target(ch)
        # Чат, который придётся «знакомить» заново, — только в пределах квоты.
        # Не отмечаем просканированным: пусть дождётся своей очереди в следующий прогон,
        # иначе он уедет в хвост ротации и ключ доступа мы не узнаем ещё сутки.
        if isinstance(target, str):
            if resolves >= RESOLVE_BUDGET:
                deferred += 1
                continue
            resolves += 1
        last_id = ch["kw_last_id"] or 0
        max_id = last_id
        try:
            # reverse=True — идём от СТАРЫХ к новым, начиная от watermark. Без него
            # Telethon отдаёт свежие сначала: при >limit новых сообщений мы читали
            # последние 300, но kw_last_id двигали на самый новый id — середина
            # пропускалась безвозвратно. С ротацией чат навещается реже, и такой разрыв
            # из редкого стал бы штатным. Теперь непрочитанное просто доберётся
            # следующим кругом.
            async for msg in client.iter_messages(target, limit=limit, min_id=last_id,
                                                  reverse=True):
                if not (msg.message and msg.sender_id and msg.sender_id > 0):
                    continue
                max_id = max(max_id, msg.id)
                m = _match(msg.message, niches)
                if not m:
                    continue
                nid, kw, hunt = m
                try:
                    sender = await msg.get_sender()
                except Exception:  # noqa: BLE001
                    sender = None
                if not isinstance(sender, User) or sender.bot or sender.deleted:
                    continue
                # Заказчик или конкурент? Ключевое слово одинаково стоит в «ищу сайт»
                # и «делаю сайты», поэтому решает отдельный разбор — иначе «Запросы»
                # забиваются рекламой (см. channels/hit_intent).
                intent, why = hit_intent.classify(msg.message or "")
                if not hit_intent.wanted(intent, hunt):
                    continue
                with database.get_conn() as conn:
                    # Репост того же объявления (новый msg_id, текст слово в слово) —
                    # в очередь не кладём: UNIQUE(chat_id, msg_id) такое не ловит.
                    if database.hit_is_repost(conn, sender.id, msg.message or ""):
                        continue
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO chat_hits (niche_id, chat_id, chat_title, tg_user_id, "
                        "username, name, text, keyword, source_msg_id, ts, status, intent, intent_why) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?, 'new', ?, ?)",
                        (nid, ch["id"], ch["title"], sender.id, sender.username,
                         _display_name(sender), msg.message.strip()[:500], kw, msg.id,
                         str(msg.date) if msg.date else None, intent, why),
                    )
                    if cur.rowcount > 0:
                        hits += 1
            scanned += 1
            if max_id > last_id:
                with database.get_conn() as conn:
                    conn.execute("UPDATE chats SET kw_last_id=? WHERE id=?", (max_id, ch["id"]))
            await _remember_peer(client, target, ch)
        except FloodWaitError as e:
            # Telegram сказал «хватит». Раньше цикл шёл дальше и получал ту же ошибку
            # на каждом оставшемся чате — сотни запросов в стену, что только продлевает
            # ограничение. Выходим сразу и честно сообщаем, сколько ждать.
            flood_wait = int(getattr(e, "seconds", 0) or 0)
            print(f"[kw] Telegram ограничил аккаунт на {flood_wait} с "
                  f"({flood_wait // 3600} ч) — обход остановлен")
            _mark_scanned(ch["id"])
            break
        except Exception as e:  # noqa: BLE001
            print(f"[kw] {(ch['title'] or target)}: {e}")
        finally:
            _mark_scanned(ch["id"])
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
    out = {"ok": True, "scanned_chats": scanned, "hits_new": hits, "skipped": skipped}
    if deferred:
        # Молча урезанный охват читался бы как «обошли всё» — говорим вслух.
        out["deferred_chats"] = deferred
        out["note"] = (f"{deferred} чатов отложены до следующего прогона: за раз знакомимся "
                       f"максимум с {RESOLVE_BUDGET}, чтобы не поймать ограничение Telegram")
    if flood_wait:
        out["flood_wait_sec"] = flood_wait
        out["error"] = (f"Telegram ограничил аккаунт на {flood_wait // 3600} ч "
                        f"— обход остановлен, повтори позже")
    print(json.dumps(out, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM прослушка чатов по ключам ниш")
    p.add_argument("--limit", type=int, default=300, help="глубина сканирования по каждому чату")
    p.add_argument("--favorites", action="store_true", help="слушать только ⭐ избранные чаты")
    args = p.parse_args()
    asyncio.run(run(args.limit, args.favorites))


if __name__ == "__main__":
    main()
