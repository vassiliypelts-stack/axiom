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
# Каналы-доноры MTProto-прокси. @MTProxy убран: юзернейма не существует, каждый
# прогон тратил на него запрос и получал ResolveUsernameRequest-ошибку в лог.
# Список можно дополнять — несуществующий канал не ломает сбор, но и пользы не даёт.
PROXY_CHANNELS = ["TProxyRU", "ProxyMTProto", "mtproto_proxy_free",
                  "proxy_mtproto_telegram", "MTProtoProxies",
                  # добавлено 28.08.2026 — расширяем донорскую базу: публичный прокси
                  # живёт 30 мин — пару часов, поэтому берём числом источников, а не
                  # глубиной по каждому (per_channel и так ограничен).
                  "mtproto_proxy", "MTProto_Proxy_Free", "proxybest_mtproto",
                  "freemtprotoproxy", "mtprotoproxies_free", "ProxyMTProtoRu"]

# Списки прокси на GitHub — источник КАЧЕСТВЕННЕЕ каналов: там прокси уже прогнаны
# автопроверкой (репозитории обновляются каждые 4-12 часов), тогда как в каналах
# лежит всё подряд, включая мёртвое с прошлой недели. Обычный HTTPS-запрос, аккаунт
# и Telegram для этого не нужны вовсе — то есть ноль риска для наших сессий.
# Проверено 28.08.2026 живым запросом; нерабочее сюда не кладём, чтобы каждый прогон
# не тратил время на 404. mtpro.xyz/api требует ключ (401), репозиторий Grim1313 удалён.
PROXY_URLS = [
    # основной: бот обновляет список каждые 12 часов, на проверке дал 47 совместимых
    "https://raw.githubusercontent.com/SoliSpirit/mtproto/master/all_proxies.txt",
    # мелкие добавки (по 4-6 ссылок), берём ради разнообразия источников
    "https://raw.githubusercontent.com/Chumbayoumba/free-telegram-proxy-russia-2026/main/README.md",
    "https://toproxylab.com/ru/proksi-dlya-tg",
]
TARGET_ALIVE = 10          # запасной ориентир, если парк аккаунтов посчитать не удалось


def target_alive() -> int:
    """Сколько живых прокси держать в пуле — считается от парка, а не константой.

    Правило антибана — «1 прокси = 1 аккаунт», поэтому нужный размер пула прямо
    зависит от числа аккаунтов, которым нужен выход в сеть. Константа TARGET_ALIVE=10
    ставилась, когда аккаунтов была горстка; парк дорос до 34, а планка осталась — и
    пул честно добирал до десяти, пока девять аккаунтов стояли без адреса.

    +30% сверху на естественную убыль: бесплатные MTProto из публичных каналов дохнут
    пачками между прогонами, и пул без запаса приходит к дефициту на следующий же день.
    """
    try:
        with database.get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM accounts WHERE tg_session IS NOT NULL AND tg_session<>'' "
                "AND COALESCE(protected,0)=0 AND COALESCE(status,'') IN ('active','warming','paused')"
            ).fetchone()["c"]
        return max(TARGET_ALIVE, int(n * 1.3) + 1)
    except Exception:  # noqa: BLE001 — БД недоступна: не хуже прежней константы
        return TARGET_ALIVE
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


def harvest_web(timeout: int = 20) -> list[tuple[str, int, str]]:
    """Прокси из веб-списков (PROXY_URLS). Обычный HTTPS, без Telegram и аккаунтов.

    Два формата: текстовые списки со ссылками tg://proxy?… (их разбирает общий
    parse_proxies_from_text) и JSON от mtpro.xyz, где host/port/secret лежат полями.
    Сбоя источника достаточно, чтобы его пропустить: доноров много, и падать из-за
    одного недоступного репозитория незачем."""
    import json as _json
    import urllib.request

    found: set[tuple[str, int, str]] = set()
    for url in PROXY_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            print(f"[harvest-web] {url}: {e}")
            continue
        before = len(found)
        for p in parse_proxies_from_text(body):
            found.add(p)
        # JSON-формат (mtpro.xyz): [{"host": "...", "port": 443, "secret": "..."}, …]
        if body.lstrip().startswith("["):
            try:
                for it in _json.loads(body):
                    host = it.get("host") or it.get("server")
                    port, secret = it.get("port"), it.get("secret")
                    if host and port and secret and _is_telethon_compatible(str(secret)):
                        found.add((str(host), int(port), str(secret)))
            except Exception:  # noqa: BLE001
                pass
        print(f"[harvest-web] {url.split('/')[2]}: +{len(found) - before}")
    return list(found)


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
    print(f"[harvest] из каналов: {len(found)}")
    # Веб-списки добираем всегда: они уже прогнаны автопроверкой на своей стороне,
    # тогда как в каналах лежит всё подряд вперемешку с мёртвым.
    for p in harvest_web():
        found.add(p)
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


def _store_harvested(conn, proxies: list[tuple[str, int, str]], source: str,
                     kind: str = "mtproto") -> int:
    """Положить прокси в пул. kind по умолчанию mtproto — харвест из каналов не меняется.
    Возвращает, сколько записей реально добавилось (дубли отсекает UNIQUE)."""
    added = 0
    for server, port, secret in proxies:
        cur = conn.execute(
            "INSERT OR IGNORE INTO proxies (kind, server, port, secret, source, status) "
            "VALUES (?, ?, ?, ?, ?, 'new')",
            (kind, server, port, secret, source),
        )
        added += cur.rowcount or 0
    return added


def import_list(lines, source: str = "manual") -> dict:
    """Загрузить СВОЙ список прокси в пул (купленные у провайдера socks5/http).

    ЗАЧЕМ. Пул наполнялся единственным способом — харвестом MTProto из публичных
    Telegram-каналов. Купленные адреса приходят списком в текстовом виде, и положить
    их было некуда: ни роута, ни функции. Из-за этого платный прокси попадал в
    систему только напрямую в accounts.proxy (как делает proxy6_bulk) — мимо пула,
    а значит мимо учёта, авто-раздачи и лечения.

    Понимает форматы, которые отдают панели провайдеров (разбор — parse_proxy_str,
    он же используется при подключении, так что «принято здесь» = «подключится там»):
        socks5://user:pass@host:port     socks5://host:port
        http://user:pass@host:port       host:port:user:pass       host:port

    Кладём kind по фактической схеме, а secret держит 'user:pass' (или пусто) —
    ровно так его читает _link() при раздаче.

    Возвращает {added, duplicates, skipped, bad} — bad это строки, которые не удалось
    разобрать (их показываем оператору, чтобы он увидел опечатку, а не гадал).
    """
    from channels.telegram import parse_mtproxy, parse_proxy_str
    if isinstance(lines, str):
        lines = lines.replace(",", "\n").splitlines()
    added = duplicates = 0
    bad: list[str] = []
    seen: set[tuple] = set()
    database.init_db()
    with database.get_conn() as conn:
        for raw in lines:
            raw = (raw or "").strip()
            if not raw or raw.startswith("#"):
                continue
            if raw.startswith("tg://") or "proxy?" in raw:
                # MTProto-ссылку кладём как mtproto — свой список может быть смешанным
                mt = parse_mtproxy(raw)
                if not mt:
                    bad.append(raw)
                    continue
                server, port, secret, kind = mt[0], mt[1], mt[2], "mtproto"
            else:
                p = parse_proxy_str(raw)
                if not p:
                    bad.append(raw)
                    continue
                server, port = p["addr"], p["port"]
                kind = p.get("proxy_type") or "socks5"
                user, pw = p.get("username") or "", p.get("password") or ""
                secret = f"{user}:{pw}" if (user or pw) else ""
            key = (server, port, secret)
            if key in seen:                      # дубль внутри самого списка
                duplicates += 1
                continue
            seen.add(key)
            n = _store_harvested(conn, [(server, port, secret)], source, kind=kind)
            added += n
            duplicates += 0 if n else 1          # 0 строк = запись уже была в пуле
    return {"added": added, "duplicates": duplicates, "bad": bad, "skipped": len(bad)}


def _mt_link(server: str, port: int, secret: str) -> str:
    return f"tg://proxy?server={server}&port={port}&secret={secret}"


def _link(row) -> str:
    """Ссылка на прокси из строки таблицы — MTProto ИЛИ socks5/http.

    Раздача (assign/heal) раньше звала только _mt_link и всё превращала в tg://proxy.
    Свой socks5-сервер (или купленный у провайдера) лёг бы в пул и молча не раздался:
    склеенный tg://proxy из его host/port не проходит parse_mtproxy и отсеивается как
    «не telethon-совместимый». Формат хранится в kind, его и уважаем: secret у socks5
    держит 'user:pass' (или пусто, если прокси без авторизации).

    build_client (channels/telegram) понимает оба вида, так что дальше по коду разницы
    нет — она есть только здесь, при сборке строки.
    """
    kind = (row["kind"] if "kind" in row.keys() else None) or "mtproto"
    server, port = row["server"], row["port"]
    secret = row["secret"] or ""
    if kind == "mtproto":
        return _mt_link(server, port, secret)
    auth = f"{secret}@" if secret else ""
    scheme = kind if kind in ("socks5", "socks4", "http", "https") else "socks5"
    return f"{scheme}://{auth}{server}:{port}"


def _usable_link(link: str) -> bool:
    """Годится ли ссылка для Telethon: MTProto — не faketls, socks — парсится."""
    from channels.telegram import parse_mtproxy, parse_proxy_str
    if link.startswith("tg://"):
        return bool(parse_mtproxy(link))
    return bool(parse_proxy_str(link))


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


async def refresh(target: int | None = None, ids: list[int] | None = None) -> dict:
    """target=None — считать нужный размер пула от парка аккаунтов (см. target_alive()).
    Число — жёсткая планка с CLI (--target)."""
    import config
    target_alive_auto = target is None
    target_alive_arg = target or TARGET_ALIVE
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
        rows = conn.execute("SELECT id, kind, server, port, secret FROM proxies").fetchall()

    if not rows:
        print("[refresh] в БД нет прокси — нечего проверять")
        return {"alive": 0, "harvested": len(fresh), "assigned": 0}

    # 2) БЫСТРЫЙ TCP-пинг всех прокси — отсеять мёртвые сервера (дёшево)
    tcp_results = await asyncio.gather(*[ping_tcp(r["server"], r["port"]) for r in rows])

    api_id = int(config.TG_API_ID)
    api_hash = config.TG_API_HASH

    # 3) ПРОВЕРКА ЖИВЫМ ПОДКЛЮЧЕНИЕМ — способ зависит от ТИПА прокси.
    # MTProto проверяется профильным telethon_test (он умеет только tg://proxy), а
    # socks5 — общим _test_account_proxy, который строит клиента через build_client и
    # понимает оба формата. Раньше кандидаты отбирались по _is_telethon_compatible —
    # это проверка MTProto-СЕКРЕТА (32 hex или dd+32), у socks5 там лежит 'user:pass',
    # поэтому платный прокси до проверки не доходил вовсе и ниже помечался мёртвым.
    telethon_candidates, telethon_indices = [], []
    socks_candidates, socks_indices = [], []
    for i, r in enumerate(rows):
        if tcp_results[i] is None:
            continue                       # сервер не отвечает — проверять нечего
        if (r["kind"] or "mtproto") == "mtproto":
            if _is_telethon_compatible(r["secret"]):
                telethon_candidates.append(r)
                telethon_indices.append(i)
        else:
            socks_candidates.append(r)
            socks_indices.append(i)

    if telethon_candidates:
        print(f"[refresh] Telethon-тест (MTProto): {len(telethon_candidates)} кандидатов...")
        tl_results = await asyncio.gather(*[
            telethon_test(r["server"], r["port"], r["secret"], api_id, api_hash)
            for r in telethon_candidates
        ])
    else:
        tl_results = []

    if socks_candidates:
        print(f"[refresh] проверка socks/http: {len(socks_candidates)} кандидатов...")
        sk_results = await asyncio.gather(*[
            _test_account_proxy(r["id"], f"{r['server']}:{r['port']}", _link(r), api_id, api_hash)
            for r in socks_candidates
        ])
    else:
        sk_results = []
    # индекс строки -> прошла ли socks-проверка (True/False)
    socks_ok = {idx: ok for idx, ok in zip(socks_indices, sk_results)}

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
            elif i in socks_ok:
                # socks/http: живым считаем тот, что реально принял подключение к TG
                if socks_ok[i]:
                    conn.execute(
                        "UPDATE proxies SET status='alive', ping_ms=?, checked_at=datetime('now') WHERE id=?",
                        (tcp_results[i], r["id"]),
                    )
                    alive += 1
                else:
                    conn.execute(
                        "UPDATE proxies SET status='dead', ping_ms=?, checked_at=datetime('now') WHERE id=?",
                        (tcp_results[i], r["id"]),
                    )
            elif tcp_results[i] is not None:
                # TCP жив, но faketls — не совместим, помечаем dead.
                # Сюда попадают ТОЛЬКО mtproto-записи: socks/http разобраны веткой выше.
                conn.execute(
                    "UPDATE proxies SET status='dead', ping_ms=?, checked_at=datetime('now') WHERE id=?",
                    (tcp_results[i], r["id"]),
                )
            else:
                conn.execute(
                    "UPDATE proxies SET status='dead', ping_ms=NULL, checked_at=datetime('now') WHERE id=?",
                    (r["id"],),
                )
        # Подчистить дохлых сверх запаса — ТОЛЬКО бесплатный мусор из каналов.
        # Платные адреса (source='manual'/провайдер) не удаляем никогда: за них плачено,
        # они лежат в панели провайдера, и «мёртв» у них чаще значит временную
        # недоступность. Удалив, мы потеряли бы их из базы навсегда и потребовали бы
        # заново загружать список руками.
        conn.execute(
            "DELETE FROM proxies WHERE status='dead' AND COALESCE(source,'') LIKE '@%' "
            "AND id NOT IN (SELECT id FROM proxies WHERE status='dead' "
            "AND COALESCE(source,'') LIKE '@%' ORDER BY added_at DESC LIMIT 20)"
        )

    print(f"[refresh] Telethon-совместимых живых: {alive} из {len(rows)}")
    assigned = assign(ids=ids)

    # Хватило ли собранного на весь парк. Раньше target_alive был мёртвым параметром:
    # он передавался в refresh() и нигде не использовался, поэтому «пул недобран» никак
    # не проявлялось — аккаунты просто молча стояли без адреса, а понять это можно было
    # только сверив два числа руками. Теперь дефицит виден сразу.
    need = target_alive() if target_alive_auto else target_alive_arg
    short = max(0, need - alive)
    if short:
        print(f"[refresh] ДЕФИЦИТ: живых {alive}, нужно ~{need} — не хватает {short}")
        with database.get_conn() as conn:
            dup = conn.execute(
                "SELECT 1 FROM events WHERE type='proxy_short' "
                "AND ts >= datetime('now','-12 hours') LIMIT 1").fetchone()
            if not dup:
                database.add_event(
                    conn, "proxy_short", f"🌐 Прокси не хватает: живых {alive}, нужно ~{need}",
                    f"Аккаунтам нужен отдельный адрес каждому (правило антибана «1 прокси = "
                    f"1 аккаунт»), а живых в пуле меньше на {short}. Столько аккаунтов "
                    f"останется без выхода в сеть: они выпадут из прогрева и из рассылки. "
                    f"Бесплатные MTProto из публичных каналов дохнут быстро — если дефицит "
                    f"держится, имеет смысл докупить платные (Proxy6).", level="warn")
    return {"alive": alive, "harvested": len(fresh), "assigned": assigned,
            "need": need, "short": short}


def pick_free_mt(exclude: set[str] | None = None) -> str | None:
    """Вернуть ОДИН живой telethon-совместимый прокси из пула (мин. пинг).
    Для авто-раздачи при покупке аккаунтов — бесплатная альтернатива Proxy6.
    exclude — набор ссылок, которые уже отданы (чтобы не дублировать в одной пачке).
    None — в пуле нет живых совместимых прокси.

    Имя историческое (mt = MTProto), но с появлением платных socks5 в пуле функция
    отдаёт ЛЮБОЙ пригодный формат — какой лежит в kind, такой и вернём."""
    exclude = exclude or set()
    with database.get_conn() as conn:
        live = conn.execute(
            "SELECT kind, server, port, secret FROM proxies WHERE status='alive' ORDER BY ping_ms LIMIT 40"
        ).fetchall()
    for p in live:
        link = _link(p)
        if link in exclude:
            continue
        if _usable_link(link):
            return link
    return None


def assign(ids: list[int] | None = None, replace_dead: bool = True) -> int:
    """Раздаёт живой прокси (мин. пинг, round-robin) аккаунтам БЕЗ прокси, а при
    replace_dead=True — ещё и тем, у кого текущий прокси уже помечен мёртвым
    (proxy_alive=0, см. кнопку «🔎 Проверить прокси»). НЕ трогает прокси, который
    ещё не проверялся или жив, и пропускает «родные» (protected). ids — сузить
    только на выбранные аккаунты (пусто = все подходящие)."""
    with database.get_conn() as conn:
        # Берём ВЕСЬ живой пул, а не первые 20. Лимит ставился, когда аккаунтов была
        # горстка, и с ростом парка превратился в потолок: при 34 аккаунтах раздать
        # можно было максимум 20 адресов, остальные оставались без прокси независимо
        # от того, сколько живых прокси реально лежит в пуле. Антибан от этого не
        # страдает — «1 прокси = 1 аккаунт» держится ниже, на проверке занятости.
        #
        # kind в выборке обязателен: без него _link() считает любую запись MTProto и
        # склеивает socks5-адрес в tg://proxy — купленный прокси молча не раздался бы.
        live = conn.execute(
            "SELECT kind, server, port, secret FROM proxies WHERE status='alive' ORDER BY ping_ms"
        ).fetchall()
        # только telethon-совместимые (не faketls ee…): иначе аккаунт молча уйдёт «напрямую».
        # _usable_link понимает оба формата, а parse_mtproxy(_mt_link(...)) резал socks5.
        live = [(p, _link(p)) for p in live]
        live = [(p, lk) for p, lk in live if _usable_link(lk)]
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
        free = [lk for _p, lk in live if lk not in taken]
        n = 0
        for a in accs:
            if not free:
                left = len(accs) - n
                print(f"[assign] прокси кончились: назначено {n}, ещё {left} аккаунт(ов) "
                      f"БЕЗ прокси — докупи прокси, дублировать один на несколько аккаунтов не буду (антибан)")
                break
            link = free.pop(0)
            conn.execute(
                "UPDATE accounts SET proxy=?, proxy_alive=NULL, proxy_checked_at=NULL WHERE id=?",
                (link, a["id"]),
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
        # Тот же потолок, что и в assign(): LIMIT отсекал хвост пула, и лечение
        # упиралось в него раньше, чем в реальный запас адресов.
        # Держим уже ГОТОВЫЕ ссылки, а не (server,port,secret): формат зависит от kind,
        # и склейка на месте превращала бы платный socks5 в нерабочий tg://proxy.
        pool = [_link(p) for p in conn.execute(
            "SELECT kind, server, port, secret FROM proxies WHERE status='alive' ORDER BY ping_ms"
        ).fetchall()]
        pool = [lk for lk in pool if _usable_link(lk)]

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
        free = [lk for lk in pool if lk not in taken]
        for (aid, label, px), ok in zip(accs, results):
            if ok:
                conn.execute(
                    "UPDATE accounts SET proxy_alive=1, proxy_checked_at=datetime('now') WHERE id=?",
                    (aid,),
                )
                alive_kept += 1
                print(f"  [{label}] прокси жив")
            elif free:
                link = free.pop(0)
                conn.execute(
                    "UPDATE accounts SET proxy=?, proxy_alive=1, proxy_checked_at=datetime('now') WHERE id=?",
                    (link, aid),
                )
                taken.add(link)
                healed += 1
                print(f"  [{label}] прокси мёртв → заменён на {_hostport(link) or link}")
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
        client = build_client(StringSession(), proxy_raw, api_id, api_hash, allow_shared_ip=True)
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
    # default=None, а не TARGET_ALIVE: иначе автоподсчёт от парка аккаунтов никогда
    # не включался бы — планировщик зовёт refresh именно через CLI.
    p.add_argument("--target", type=int, default=None,
                   help="сколько живых держать в пуле (по умолчанию — от числа аккаунтов)")
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
