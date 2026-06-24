"""Авто-поиск рабочего БЕСПЛАТНОГО SOCKS5 для Telegram.

Тянет свежие публичные списки SOCKS5, проверяет каждый реальным коннектом к
Telegram (Telethon через этот прокси), и отдаёт живые. Может сразу назначить
найденный прокси аккаунту.

⚠️ Бесплатные прокси — IP «грязные»/нестабильные: годятся для ПРОВЕРКИ коннекта и
теста прогрева, для боя бери платный мобильный/резидентский.

    python -m channels.proxy_find                 # просто найти и вывести
    python -m channels.proxy_find --assign 8      # найти и назначить аккаунту #8
"""
from __future__ import annotations

import argparse
import asyncio
import urllib.request

from telethon.sessions import StringSession

from channels.telegram import build_client
from db import database

SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=10000",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
]


def _fetch(limit: int = 150) -> list[str]:
    """Собрать host:port из публичных списков → ['socks5://host:port', ...]."""
    out: list[str] = []
    seen: set[str] = set()
    for url in SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            txt = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            print(f"[источник недоступен] {url[:46]}…: {e}")
            continue
        for line in txt.splitlines():
            line = line.strip().replace("socks5://", "")
            if line.count(".") >= 3 and ":" in line:
                host, _, port = line.partition(":")
                port = port.split()[0] if port else ""
                if port.isdigit() and (host, port) not in seen:
                    seen.add((host, port))
                    out.append(f"socks5://{host}:{port}")
        if len(out) >= limit:
            break
    return out[:limit]


async def _alive(proxy_url: str, timeout: int = 8) -> bool:
    """Живой ли прокси для Telegram: пробуем подключиться к TG через него."""
    client = build_client(StringSession(), proxy_url)
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        ok = client.is_connected()
        await client.disconnect()
        return ok
    except Exception:  # noqa: BLE001
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return False


async def find(need: int = 1, max_test: int = 120) -> list[str]:
    cands = _fetch(max_test)
    if not cands:
        print("не удалось получить списки прокси (нет интернета к github/proxyscrape?)")
        return []
    print(f"проверяю до {len(cands)} бесплатных SOCKS5 на доступ к Telegram…")
    found: list[str] = []
    for i, px in enumerate(cands, 1):
        if await _alive(px):
            found.append(px)
            print(f"[OK] {px}")
            if len(found) >= need:
                break
        if i % 25 == 0:
            print(f"  …проверено {i}, живых {len(found)}")
    return found


def main() -> None:
    p = argparse.ArgumentParser(description="Авто-поиск бесплатного SOCKS5 для Telegram")
    p.add_argument("--need", type=int, default=1, help="сколько живых найти")
    p.add_argument("--max-test", type=int, default=120, help="сколько максимум проверить")
    p.add_argument("--assign", type=int, help="назначить первый найденный аккаунту с этим id")
    args = p.parse_args()
    found = asyncio.run(find(args.need, args.max_test))
    if not found:
        print("\nЖивых бесплатных SOCKS5 сейчас не нашлось — запусти ещё раз или возьми платный.")
        return
    if args.assign:
        database.init_db()
        with database.get_conn() as conn:
            conn.execute("UPDATE accounts SET proxy=? WHERE id=?", (found[0], args.assign))
        print(f"\nНазначен аккаунту #{args.assign}: {found[0]}")
    else:
        print("\nНайдено:\n" + "\n".join(found))


if __name__ == "__main__":
    main()
