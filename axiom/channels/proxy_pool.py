"""Пул бесплатных MTProto-прокси для AXIOM (времянка).

Собирает свежие MTProto-прокси из публичных TG-каналов, проверяет TCP-пингом,
держит 8-10 живых, раздаёт аккаунтам прокси с минимальным пингом, выкидывает
дохлые и добирает свежие, если живых осталось мало.

⚠️ Бесплатные публичные прокси нестабильны и не идеальны для прогретых
аккаунтов (общий IP, оператор видит метаданные). На боевую — платные (proxy6).
MTProto-прокси работают ТОЛЬКО для Telegram (WhatsApp нужен SOCKS5).

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

from db import database

# Каналы-источники (можно дополнять).
PROXY_CHANNELS = ["TProxyRU", "ProxyMTProto"]
TARGET_ALIVE = 10          # сколько живых держим в пуле
MIN_ALIVE_BEFORE_REFILL = 2
PING_TIMEOUT = 4.0


def parse_proxies_from_text(text: str | None) -> list[tuple[str, int, str]]:
    """Достаёт (server, port, secret) из строки/текста с tg://proxy / t.me/proxy ссылками."""
    out: list[tuple[str, int, str]] = []
    if not text:
        return out
    for m in re.finditer(r"(?:tg://proxy\?|t\.me/proxy\?|https?://t\.me/proxy\?)([^\s\)\]\"'<]+)", text):
        q = parse_qs(m.group(1))
        server = (q.get("server") or [None])[0]
        port = (q.get("port") or [None])[0]
        secret = (q.get("secret") or [None])[0]
        if server and port and secret:
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


async def ping(server: str, port: int) -> int | None:
    """TCP-пинг до сервера в мс (или None, если недоступен)."""
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


def _store_harvested(conn, proxies: list[tuple[str, int, str]], source: str) -> None:
    for server, port, secret in proxies:
        conn.execute(
            "INSERT OR IGNORE INTO proxies (kind, server, port, secret, source, status) "
            "VALUES ('mtproto', ?, ?, ?, ?, 'new')",
            (server, port, secret, source),
        )


def _mt_link(server: str, port: int, secret: str) -> str:
    return f"tg://proxy?server={server}&port={port}&secret={secret}"


async def refresh(target_alive: int = TARGET_ALIVE) -> dict:
    from channels.telegram import _build_client
    database.init_db()
    client = _build_client()
    await client.start()

    # 1) собрать свежие
    fresh = await harvest(client)
    await client.disconnect()
    with database.get_conn() as conn:
        _store_harvested(conn, fresh, "+".join("@" + c for c in PROXY_CHANNELS))
        rows = conn.execute("SELECT id, server, port, secret FROM proxies").fetchall()

    # 2) пропинговать всё (живых сортируем по пингу)
    results = await asyncio.gather(*[ping(r["server"], r["port"]) for r in rows])
    alive = 0
    with database.get_conn() as conn:
        for r, ms in zip(rows, results):
            if ms is None:
                conn.execute("UPDATE proxies SET status='dead', ping_ms=NULL, checked_at=datetime('now') WHERE id=?", (r["id"],))
            else:
                alive += 1
                conn.execute("UPDATE proxies SET status='alive', ping_ms=?, checked_at=datetime('now') WHERE id=?", (ms, r["id"]))
        # подчистить дохлых сверх запаса (оставим последние, чтобы не пухло)
        conn.execute(
            "DELETE FROM proxies WHERE status='dead' AND id NOT IN "
            "(SELECT id FROM proxies WHERE status='dead' ORDER BY added_at DESC LIMIT 20)"
        )
    print(f"[refresh] живых прокси: {alive}")
    assigned = assign()
    return {"alive": alive, "harvested": len(fresh), "assigned": assigned}


def assign() -> int:
    """Раздаёт живой прокси (мин. пинг, round-robin) только аккаунтам БЕЗ прокси.
    НЕ перетирает уже назначенный (рабочий) прокси и пропускает «родные» (protected)."""
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
        accs = conn.execute(
            "SELECT id FROM accounts WHERE tg_session IS NOT NULL AND tg_session<>'' "
            "AND (proxy IS NULL OR proxy='') AND COALESCE(protected,0)=0"
        ).fetchall()
        n = 0
        for i, a in enumerate(accs):
            p = live[i % len(live)]
            conn.execute("UPDATE accounts SET proxy=? WHERE id=?",
                         (_mt_link(p["server"], p["port"], p["secret"]), a["id"]))
            n += 1
    print(f"[assign] прокси выдан аккаунтам: {n}")
    return n


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM пул MTProto-прокси")
    p.add_argument("--refresh", action="store_true", help="собрать+проверить+раздать")
    p.add_argument("--target", type=int, default=TARGET_ALIVE)
    args = p.parse_args()
    if args.refresh:
        import json
        print(json.dumps(asyncio.run(refresh(args.target)), ensure_ascii=False))
    else:
        print(json.dumps({"assigned": assign()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
