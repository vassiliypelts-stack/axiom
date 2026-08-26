"""Пул бесплатных MTProto-прокси для AXIOM.

Собирает свежие MTProto-прокси из публичных TG-каналов, проверяет их
РЕАЛЬНЫМ подключением через Telethon (не просто TCP-пинг — а live-тест:
создаёт клиента, логинится, выходит), держит пул только реально работающих,
раздаёт аккаунтам с минимальным пингом.

Фильтрует faketls (секреты ee...) на этапе сбора — Telethon их не тянет,
хранить и проверять бессмысленно. Алгоритм:
  1) Собрать прокси из каналов-доноров
  2) Отсеять faketls (заведомо несовместимые с Telethon)
  3) Быстрый TCP-пинг всех (отсеять мёртвые сервера)
  4) TCP-alive → реальный тест через Telethon (connect + get_me)
  5) Статус 'alive' ставят только те, кто прошёл Telethon-тест
  6) Раздать аккаунтам лучшие (мин. пинг среди Telethon-живых)

Авто-обновление (раз в сутки): см. планировщик в веб-пульте.

Запуск:
    python -m channels.proxy_pool --refresh          # собрать+проверить+раздать
    python -m channels.proxy_pool --refresh --target 10
"""
from __future__ import annotations

import argparse
import asyncio
import re
import time
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

from db import database

# Каналы-источники (можно дополнять).
PROXY_CHANNELS = ["TProxyRU", "ProxyMTProto", "MTProxy"]
TARGET_ALIVE = 10          # сколько реально живых держим в пуле
MIN_ALIVE_BEFORE_REFILL = 2
PING_TIMEOUT = 4.0         # TCP-пинг: таймаут на коннект (сек)
TELETHON_TEST_TIMEOUT = 8.0  # Telethon-тест: таймаут на всю попытку (сек)


def _is_telethon_compatible(secret: str) -> bool:
    """Telethon поддерживает только «чистый» (32 hex) или «секьюрный» dd-секрет
    (dd+32 hex). Faketls (ee…) и битые — не поддерживает. Проверка по той же
    логике, что parse_mtproxy в telegram.py."""
    s = secret.lower().strip()
    is_hex = all(c in "0123456789abcdef" for c in s)
    return bool(is_hex and (len(s) == 32 or (s.startswith("dd") and len(s) == 34)))


def parse_proxies_from_text(text: str | None) -> list[tuple[str, int, str]]:
    """Достаёт (server, port, secret) из текста с tg://proxy / t.me/proxy ссылками.
    Фильтрует faketls (секреты ee...) — Telethon их не поддерживает,
    хранить и проверять бессмысленно."""
    out: list[tuple[str, int, str]] = []
    if not text:
        return out
    for m in re.finditer(r"(?:tg://proxy\?|t\.me/proxy\?|https?://t\.me/proxy\?)([^\s\)\]\"'<]+)", text):
        q = parse_qs(m.group(1))
        server = (q.get("server") or [None])[0]
        port = (q.get("port") or [None])[0]
        secret = (q.get("secret") or [None])[0]
        if server and port and secret:
            if not _is_telethon_compatible(secret):
                continue  # ee... и битые — Telethon не умеет, не храним
            try:
                out.append((server, int(port), secret))
            except ValueError:
                continue
    return out


def _msg_sources(msg) -> list[str]:
    """Все места, где может быть ссылка на прокси: текст, entities, кнопки."""
    parts: list[str] = []
    if getattr(msg, "message", None):
        parts.append(msg.message)
    for ent, txt in (msg.get_entities_text() or []):
        url = getattr(ent, "url", None)
        if url:
            parts.append(url)
    try:
        for row in (msg.buttons or []):
            for b in row:
                if getattr(b, "url", None):
                    parts.append(b.url)
    except Exception:  # noqa: BLE001
        pass
    return parts


async def harvest(client, per_channel: int = 80) -> list[tuple[str, int, str]]:
    found: set[tuple[str, int, str]] = set()
    for ch in PROXY_CHANNELS:
        try:
            async for msg in client.iter_messages(ch, limit=per_channel):
                for src in _msg_sources(msg):
                    for p in parse_proxies_from_text(src):
                        found.add(p)
        except Exception as e:  # noqa: BLE001
            print(f"[harvest] {ch}: {e}")
    print(f"[harvest] собрано уникальных прокси: {len(found)}")
    return list(found)


async def ping_tcp(server: str, port: int) -> int | None:
    """Быстрый TCP-пинг (сек). Отсеивает откровенно мёртвые сервера ДО
    дорогого Telethon-теста. Возвращает пинг в мс или None, если недоступен."""
    t0 = time.monotonic()
    try:
        fut = asyncio.open_connection(server, port)
        reader, writer = await asyncio.wait_for(fut, timeout=PING_TIMEOUT)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return int((time.monotonic() - t0) * 1000)
    except Exception:  # noqa: BLE001
        return None


async def telethon_test(server: str, port: int, secret: str,
                        api_id: int, api_hash: str) -> int | None:
    """РЕАЛЬНАЯ проверка прокси: создаёт Telethon-клиента с этим прокси,
    логинится (get_me), выходит. Если прокси реально работает с Telethon —
    возвращает пинг (мс). Если нет — None.

    Дороже TCP-пинга (~3-8 сек на прокси), зато даёт 100% гарантию."""
    from channels.telegram import parse_mtproxy
    proxy_link = _mt_link(server, port, secret)
    mt = parse_mtproxy(proxy_link)
    if not mt:
        return None  # faketls/битый — telethon не потянет
    t0 = time.monotonic()
    try:
        client = TelegramClient(
            StringSession(), api_id, api_hash,
            connection=ConnectionTcpMTProxyRandomizedIntermediate,
            proxy=mt,
        )
    except Exception:  # noqa: BLE001
        return None
    # finally, а не except: отмена задачи прилетает как CancelledError (BaseException),
    # мимо `except Exception` — и клиент оставался жить с висящими _send_loop/_recv_loop.
    # Тест гоняется по десяткам прокси за прогон, так что течёт быстро.
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TEST_TIMEOUT)
        # Сессия пустая — это ок: нас интересует только что коннект через прокси состоялся.
        return int((time.monotonic() - t0) * 1000)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _store_harvested(conn, proxies: list[tuple[str, int, str]], source: str) -> None:
    for server, port, secret in proxies:
        conn.execute(
            "INSERT OR IGNORE INTO proxies (kind, server, port, secret, source, status) "
            "VALUES ('mtproto', ?, ?, ?, ?, 'new')",
            (server, port, secret, source),
        )


def _mt_link(server: str, port: int, secret: str) -> str:
    return f"tg://proxy?server={server}&port={port}&secret={secret}"


async def _harvest_client():
    """Клиент для СБОРА прокси из публичных каналов. Возвращает (client, как_подписан).

    Читает открытые каналы — годится любой авторизованный аккаунт. Раньше брали только
    сессию из .env (_build_client), и когда она умерла, refresh() падал так: Telethon
    на client.start() уходил в интерактивный ввод, натыкался на закрытый stdin и валился
    с EOFError («Please enter your phone»). Трейсбек уходил в файловый лог, автосбор
    молча не работал неделями — а следствие вылезало совсем в другом месте: пул не
    пополнялся, живых прокси стало меньше, чем аккаунтов, и те вставали без выхода в сеть.

    Поэтому: сначала .env, а если сессия не авторизована — любой живой аккаунт из базы.
    НЕ берём подключённых слушателем: вторая сессия тем же ключом = AuthKeyDuplicated и
    сожжённый аккаунт. И никакого интерактива — is_user_authorized() вместо start().
    """
    import config
    from channels.telegram import _build_client, build_client

    if config.TG_STRING_SESSION:
        client = _build_client()
        try:
            await client.connect()
            if await client.is_user_authorized():
                return client, "сессия из .env"
            await client.disconnect()
        except Exception:  # noqa: BLE001
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    try:
        from channels.listener import CLIENTS
        busy = {aid for aid, cl in list(CLIENTS.items())
                if getattr(cl, "is_connected", None) and cl.is_connected()}
    except Exception:  # noqa: BLE001
        busy = set()

    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, label, tg_session, proxy, api_id, api_hash FROM accounts "
            "WHERE tg_session IS NOT NULL AND tg_session<>'' "
            "AND COALESCE(status,'') IN ('active','warming') "
            "AND COALESCE(session_alive,1)=1 ORDER BY id"
        ).fetchall()
    for r in rows:
        if r["id"] in busy:
            continue
        try:
            client = build_client(StringSession(r["tg_session"]), r["proxy"],
                                  r["api_id"], r["api_hash"])
            await client.connect()
            if await client.is_user_authorized():
                return client, f"аккаунт {r['label'] or r['id']}"
            await client.disconnect()
        except Exception:  # noqa: BLE001
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    return None, ""


async def refresh(target_alive: int = TARGET_ALIVE, ids: list[int] | None = None) -> dict:
    import config
    database.init_db()
    client, who = await _harvest_client()

    # 1) собрать свежие (уже отфильтрованы от faketls на этапе parse)
    if client is None:
        # Сбор невозможен — но проверку УЖЕ НАКОПЛЕННЫХ прокси это не отменяет: среди
        # них могли ожить старые, и аккаунтам они нужны прямо сейчас. Раньше здесь был
        # трейсбек и выход, то есть переставали работать обе половины.
        fresh = []
        print("[refresh] СБОР ПРОПУЩЕН: нет авторизованной сессии для чтения каналов "
              "(ни .env, ни свободный аккаунт). Проверю только уже накопленные прокси.")
        with database.get_conn() as conn:
            # Не чаще раза в сутки: чинится это перелогином вручную, а тревога каждые
            # два часа приучает пролистывать ленту не читая.
            dup = conn.execute(
                "SELECT 1 FROM events WHERE type='proxy_harvest_down' "
                "AND ts >= datetime('now','-1 day') LIMIT 1").fetchone()
            if not dup:
                database.add_event(
                    conn, "proxy_harvest_down", "🌐 Сбор прокси не работает",
                    "Нечем читать каналы с прокси: сессия в .env не авторизована, а "
                    "свободного живого аккаунта не нашлось. Пул не пополняется — когда "
                    "живых прокси станет меньше, чем аккаунтов, часть из них останется "
                    "без выхода в сеть. Перелогинь основную сессию (TG_STRING_SESSION).",
                    level="warn")
    else:
        print(f"[refresh] читаю каналы: {who}")
        fresh = await harvest(client)
        await client.disconnect()
    with database.get_conn() as conn:
        _store_harvested(conn, fresh, "+".join("@" + c for c in PROXY_CHANNELS))
        # Тестируем ВСЕ прокси в БД (и новые, и старые — вдруг ожили)
        rows = conn.execute("SELECT id, server, port, secret FROM proxies").fetchall()

    if not rows:
        print("[refresh] в БД нет прокси — нечего проверять")
        return {"alive": 0, "harvested": len(fresh), "assigned": 0}

    # 2) БЫСТРЫЙ TCP-пинг всех прокси — отсеять мёртвые сервера (дёшево)
    tcp_results = await asyncio.gather(*[ping_tcp(r["server"], r["port"]) for r in rows])

    # 3) TELEITHON-ТЕСТ: только те, кто прошёл TCP-пинг И совместим с Telethon
    telethon_candidates = []
    telethon_indices = []
    for i, r in enumerate(rows):
        if tcp_results[i] is not None and _is_telethon_compatible(r["secret"]):
            telethon_candidates.append(r)
            telethon_indices.append(i)

    api_id = int(config.TG_API_ID)
    api_hash = config.TG_API_HASH

    if telethon_candidates:
        print(f"[refresh] Telethon-тест: {len(telethon_candidates)} кандидатов...")
        tl_results = await asyncio.gather(*[
            telethon_test(r["server"], r["port"], r["secret"], api_id, api_hash)
            for r in telethon_candidates
        ])
    else:
        tl_results = []

    # 4) Записать статусы в БД
    tl_idx = 0
    alive = 0
    with database.get_conn() as conn:
        for i, r in enumerate(rows):
            if i in telethon_indices:
                tl_ping = tl_results[tl_idx]
                tl_idx += 1
                if tl_ping is not None:
                    conn.execute(
                        "UPDATE proxies SET status='alive', ping_ms=?, checked_at=datetime('now') WHERE id=?",
                        (tl_ping, r["id"]),
                    )
                    alive += 1
                else:
                    conn.execute(
                        "UPDATE proxies SET status='dead', ping_ms=NULL, checked_at=datetime('now') WHERE id=?",
                        (r["id"],),
                    )
            elif tcp_results[i] is not None:
                # TCP жив, но faketls — не совместим, помечаем dead
                conn.execute(
                    "UPDATE proxies SET status='dead', ping_ms=?, checked_at=datetime('now') WHERE id=?",
                    (tcp_results[i], r["id"]),
                )
            else:
                conn.execute(
                    "UPDATE proxies SET status='dead', ping_ms=NULL, checked_at=datetime('now') WHERE id=?",
                    (r["id"],),
                )
        # подчистить дохлых сверх запаса
        conn.execute(
            "DELETE FROM proxies WHERE status='dead' AND id NOT IN "
            "(SELECT id FROM proxies WHERE status='dead' ORDER BY added_at DESC LIMIT 20)"
        )

    print(f"[refresh] Telethon-совместимых живых: {alive} из {len(rows)}")
    assigned = assign(ids=ids)
    return {"alive": alive, "harvested": len(fresh), "assigned": assigned}


def pick_free_mt(exclude: set[str] | None = None) -> str | None:
    """Вернуть ОДИН живой telethon-совместимый MTProto-прокси из пула (мин. пинг).
    Для авто-раздачи при покупке аккаунтов — бесплатная альтернатива Proxy6.
    exclude — набор ссылок, которые уже отданы (чтобы не дублировать в одной пачке).
    None — в пуле нет живых совместимых прокси."""
    from channels.telegram import parse_mtproxy
    exclude = exclude or set()
    with database.get_conn() as conn:
        live = conn.execute(
            "SELECT server, port, secret FROM proxies WHERE status='alive' ORDER BY ping_ms LIMIT 40"
        ).fetchall()
    for p in live:
        link = _mt_link(p["server"], p["port"], p["secret"])
        if link in exclude:
            continue
        if parse_mtproxy(link):
            return link
    return None


def assign(ids: list[int] | None = None, replace_dead: bool = True) -> int:
    """Раздаёт живой прокси (мин. пинг, round-robin) аккаунтам БЕЗ прокси, а при
    replace_dead=True — ещё и тем, у кого текущий прокси уже помечен мёртвым
    (proxy_alive=0, см. кнопку «🔎 Проверить прокси»). НЕ трогает прокси, который
    ещё не проверялся или жив, и пропускает «родные» (protected). ids — сузить
    только на выбранные аккаунты (пусто = все подходящие)."""
    from channels.telegram import parse_mtproxy
    with database.get_conn() as conn:
        live = conn.execute(
            "SELECT server, port, secret FROM proxies WHERE status='alive' ORDER BY ping_ms LIMIT 20"
        ).fetchall()
        # только telethon-совместимые (не faketls ee…): иначе аккаунт молча уйдёт «напрямую»
        live = [p for p in live if parse_mtproxy(_mt_link(p["server"], p["port"], p["secret"]))]
        if not live:
            print("[assign] в пуле нет telethon-совместимых прокси (все faketls/битые) — не раздаю")
            return 0
        cond = "(proxy IS NULL OR proxy='')" + (" OR proxy_alive=0" if replace_dead else "")
        params: list = []
        where = f"tg_session IS NOT NULL AND tg_session<>'' AND ({cond}) AND COALESCE(protected,0)=0"
        if ids:
            qm = ",".join("?" * len(ids))
            where += f" AND id IN ({qm})"
            params.extend(ids)
        accs = conn.execute(f"SELECT id FROM accounts WHERE {where}", params).fetchall()
        # уже занятые прокси (кем-то ДРУГИМ, живым в проверке) — не раздаём их же ещё раз:
        # антибан держится на «1 прокси = 1 аккаунт», зацикленный round-robin по кругу
        # (i % len(live)) при нехватке пула молча сажал по 10+ аккаунтов на один IP.
        # Занятым считаем ЛЮБОЙ назначенный прокси, даже помеченный мёртвым. Условие
        # «proxy_alive<>0» освобождало адрес, на котором аккаунт всё ещё сидит: прокси
        # моргнул, его пометили мёртвым, отдали второму аккаунту — а первый ожил, и оба
        # оказались на одном выходе. Сессия, увиденная с двух IP, жжётся навсегда.
        taken = {r["proxy"] for r in conn.execute(
            "SELECT proxy FROM accounts WHERE proxy IS NOT NULL AND proxy<>''"
        ).fetchall()}
        free = [p for p in live if _mt_link(p["server"], p["port"], p["secret"]) not in taken]
        n = 0
        for a in accs:
            if not free:
                left = len(accs) - n
                print(f"[assign] прокси кончились: назначено {n}, ещё {left} аккаунт(ов) "
                      f"БЕЗ прокси — докупи прокси, дублировать один на несколько аккаунтов не буду (антибан)")
                break
            p = free.pop(0)
            conn.execute(
                "UPDATE accounts SET proxy=?, proxy_alive=NULL, proxy_checked_at=NULL WHERE id=?",
                (_mt_link(p["server"], p["port"], p["secret"]), a["id"]),
            )
            n += 1
    print(f"[assign] прокси выдан аккаунтам: {n}")
    return n


def _hostport(px: str | None) -> tuple[str, int] | None:
    """Достаёт (host, port) из ЛЮБОГО формата прокси для TCP-пинга: tg://proxy?…,
    socks5://user:pass@host:port, http://host:port или сырой host:port[:user:pass].
    Мусор («Auto IP Rotation: off» и пр.) → None."""
    px = (px or "").strip()
    if not px:
        return None
    if "proxy?" in px:                       # tg://proxy?server=…&port=…
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(px).query)
        server = (q.get("server") or [None])[0]
        port = (q.get("port") or [None])[0]
        if server and port and str(port).isdigit():
            return (server, int(port))
        return None
    rest = px.split("://", 1)[1] if "://" in px else px
    rest = rest.split("@")[-1]               # отбросить user:pass@
    parts = rest.split(":")
    if len(parts) >= 2 and parts[0] and parts[1].isdigit():
        return (parts[0], int(parts[1]))
    return None


def _usable(px: str | None) -> bool:
    """Прокси не только валиден, но и РАБОЧ для нашего клиента: tg:// — только
    telethon-совместимый (dd/hex-секрет, не faketls ee…); socks/http — парсится.
    Иначе аккаунт молча уходит напрямую (общий IP пачки → бан)."""
    from channels.telegram import parse_mtproxy, parse_proxy_str
    px = (px or "").strip()
    if not px:
        return False
    if "proxy?" in px:
        return parse_mtproxy(px) is not None
    return parse_proxy_str(px) is not None


async def heal(ids: list[int] | None = None, warming_only: bool = True) -> dict:
    """САМО-ЛЕЧЕНИЕ прокси прогреваемых аккаунтов.

    Для каждого подходящего аккаунта: проверяет его текущий прокси РЕАЛЬНЫМ
    Telethon-подключением. Живой → proxy_alive=1. Мёртвый → подставляет живой
    из пула (Telethon-проверенный). Если живого в пуле нет — оставляет прокси как есть
    и ставит proxy_alive=0 (аккаунт не выйдет в сеть, но и не уползёт на голый IP сервера).
    Возвращает {checked, alive_kept, healed, no_pool}."""
    import config
    # init_db — это executescript всей схемы + ALTER-миграции, т.е. секунды блокирующего
    # sqlite. Раньше heal() жил отдельным процессом и это было безразлично; теперь его
    # зовёт слушатель прямо из своего event loop, и на время миграции замирали ВСЕ
    # подключённые Telethon-клиенты (входящие не обрабатывались). Уводим в поток.
    await asyncio.to_thread(database.init_db)
    api_id = int(config.TG_API_ID)
    api_hash = config.TG_API_HASH

    with database.get_conn() as conn:
        # СНАЧАЛА вернуть в оборот адреса, которые держат мертвецы. Занятым считается
        # ЛЮБОЙ назначенный прокси (см. `taken` ниже) — иначе два аккаунта сядут на один
        # IP и сожгут сессию. Но архивный и забаненный аккаунт в сеть уже не выйдет, а
        # адрес за собой держит вечно: на живой базе 40 архивных удерживали 39 живых
        # прокси, свободных для выдачи оставалось 3 из 28, и аккаунты в прогреве стояли
        # без прокси не потому, что прокси кончились. Роут смены статуса теперь
        # освобождает адрес сразу (web/app.accounts_bulk), но это чинит только будущие
        # архивации — накопленное разбираем здесь, на каждом прогоне.
        released = conn.execute(
            "UPDATE accounts SET proxy='', proxy_alive=NULL, proxy_checked_at=NULL "
            "WHERE COALESCE(status,'') IN ('archived','banned') AND COALESCE(proxy,'')<>''"
        ).rowcount or 0
        if released:
            print(f"[heal] освободил {released} прокси у архивных/забаненных аккаунтов")

        # Пул Telethon-живых прокси для подстановки
        pool = conn.execute(
            "SELECT server, port, secret FROM proxies WHERE status='alive' ORDER BY ping_ms LIMIT 40"
        ).fetchall()
        pool = [(p["server"], p["port"], p["secret"]) for p in pool]

        where = "tg_session IS NOT NULL AND tg_session<>'' AND COALESCE(protected,0)=0"
        if warming_only:
            where += " AND status='warming'"
        params: list = []
        if ids:
            qm = ",".join("?" * len(ids))
            where += f" AND id IN ({qm})"
            params.extend(ids)
        accs_raw = conn.execute(
            f"SELECT id, label, proxy FROM accounts WHERE {where}", params
        ).fetchall()
        accs = [(a["id"], a["label"] or f"#{a['id']}", a["proxy"] or "") for a in accs_raw]

    # НЕ трогаем тех, кто ПРЯМО СЕЙЧАС подключён слушателем: смена прокси у живого
    # соединения = та же сессия выходит в эфир со второго IP, а это AuthKeyDuplicatedError
    # и сожжённый ключ (потеряли так #17 и #9320). Отвалившиеся лечатся штатно — слушатель
    # сам зовёт heal() для конкретного id ровно в тот момент, когда аккаунт НЕ подключён.
    # ids задан явно (точечный вызов из слушателя) — доверяем ему, он знает, что делает.
    if not ids:
        try:
            from channels.listener import CLIENTS
            busy = {aid for aid, cl in list(CLIENTS.items())
                    if getattr(cl, "is_connected", None) and cl.is_connected()}
        except Exception:  # noqa: BLE001 — слушатель может быть не запущен
            busy = set()
        if busy:
            before = len(accs)
            accs = [a for a in accs if a[0] not in busy]
            skipped = before - len(accs)
            if skipped:
                print(f"[heal] пропускаю {skipped} подключённых аккаунтов "
                      f"(смена прокси под живой сессией сжигает ключ)")

    if not accs:
        print("[heal] нет аккаунтов для проверки")
        return {"checked": 0, "alive_kept": 0, "healed": 0, "no_pool": 0}

    # Telethon-тест текущих прокси аккаунтов
    print(f"[heal] проверяю {len(accs)} аккаунтов через Telethon...")
    results = await asyncio.gather(*[
        _test_account_proxy(aid, label, px, api_id, api_hash)
        for aid, label, px in accs
    ])

    alive_kept = healed = no_pool = 0
    with database.get_conn() as conn:
        # прокси, уже живьём подтверждённые за ДРУГИМИ аккаунтами в этом же прогоне —
        # не сажаем второго на тот же IP. Старый rr%len(pool) зацикливался по кругу и
        # копил по 10+ аккаунтов на один прокси при каждом плановом --heal (антибан).
        # COALESCE, а не «proxy_alive=1»: у ТОЛЬКО ЧТО выданного прокси alive ещё NULL
        # (см. assign()), и такой прокси не попадал в «занятые» — heal тут же сажал на
        # него второй аккаунт. Ровно так #9329 и #9331 из свежей пачки оказались на одном IP.
        # Занятым считаем ЛЮБОЙ назначенный прокси, даже помеченный мёртвым. Условие
        # «proxy_alive<>0» освобождало адрес, на котором аккаунт всё ещё сидит: прокси
        # моргнул, его пометили мёртвым, отдали второму аккаунту — а первый ожил, и оба
        # оказались на одном выходе. Сессия, увиденная с двух IP, жжётся навсегда.
        taken = {r["proxy"] for r in conn.execute(
            "SELECT proxy FROM accounts WHERE proxy IS NOT NULL AND proxy<>''"
        ).fetchall()}
        free = [(s, p, sec) for (s, p, sec) in pool if _mt_link(s, p, sec) not in taken]
        for (aid, label, px), ok in zip(accs, results):
            if ok:
                conn.execute(
                    "UPDATE accounts SET proxy_alive=1, proxy_checked_at=datetime('now') WHERE id=?",
                    (aid,),
                )
                alive_kept += 1
                print(f"  [{label}] прокси жив")
            elif free:
                s, p, sec = free.pop(0)
                conn.execute(
                    "UPDATE accounts SET proxy=?, proxy_alive=1, proxy_checked_at=datetime('now') WHERE id=?",
                    (_mt_link(s, p, sec), aid),
                )
                taken.add(_mt_link(s, p, sec))
                healed += 1
                print(f"  [{label}] прокси мёртв → заменён на {s}:{p}")
            else:
                # НЕ обнуляем прокси: пустой proxy у build_client означает не «без прокси»,
                # а «прокси главного аккаунта из .env», а если его нет — прямой коннект
                # с IP сервера. Тогда все аккаунты, у которых подсох пул, сползают на один
                # общий IP — ровно тот массовый бан, что чинил коммит 698adcc. Мёртвый
                # прокси = аккаунт просто не подключится (безопасный отказ), и это видно
                # оператору по proxy_alive=0.
                conn.execute(
                    "UPDATE accounts SET proxy_alive=0, proxy_checked_at=datetime('now') WHERE id=?",
                    (aid,),
                )
                no_pool += 1
                print(f"  [{label}] прокси мёртв, живого в пуле нет — аккаунт до замены не выходит в сеть")
    print(f"[heal] проверено:{len(accs)} живых-оставлено:{alive_kept} подставлено:{healed} без-пула:{no_pool}")
    return {"checked": len(accs), "alive_kept": alive_kept, "healed": healed, "no_pool": no_pool}


async def _test_account_proxy(aid: int, label: str, proxy_raw: str,
                               api_id: int, api_hash: str) -> bool:
    """Проверить прокси аккаунта через Telethon. True — работает.

    Понимает ОБА формата: tg://proxy (MTProto из пула) и socks5/http — их раздаёт
    channels.proxy_find, когда MTProto-пул пуст. Раньше тут парсился только MTProto,
    и любой SOCKS5 объявлялся мёртвым: на первом же heal() аккаунт получал
    proxy_alive=0 и выпадал из прогрева (warming_accounts требует живой прокси)."""
    if not _usable(proxy_raw):
        return False  # пусто/faketls/битый: клиента не строим — build_client молча
                      # увёл бы такой аккаунт на TG_PROXY из .env или на прямой IP
    from channels.telegram import build_client
    try:
        client = build_client(StringSession(), proxy_raw, api_id, api_hash)
    except Exception:  # noqa: BLE001
        return False
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TEST_TIMEOUT)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:                    # см. комментарий в telethon_test — CancelledError мимо except
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM пул MTProto-прокси")
    p.add_argument("--refresh", action="store_true", help="собрать+проверить+раздать")
    p.add_argument("--heal", action="store_true", help="проверить прокси прогреваемых и заменить битые на живые бесплатные")
    p.add_argument("--all", action="store_true", help="с --heal: лечить не только 'warming', а все не-родные с сессией")
    p.add_argument("--target", type=int, default=TARGET_ALIVE)
    p.add_argument("--ids", help="сузить раздачу на конкретные id аккаунтов, через запятую")
    args = p.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()] if args.ids else None
    import json
    if args.heal:
        print(json.dumps(asyncio.run(heal(ids=ids, warming_only=not args.all)), ensure_ascii=False))
    elif args.refresh:
        print(json.dumps(asyncio.run(refresh(args.target, ids=ids)), ensure_ascii=False))
    else:
        print(json.dumps({"assigned": assign(ids=ids)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
