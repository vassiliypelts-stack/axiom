"""Проверка живости ТЕКУЩЕГО прокси у аккаунтов (не подбор нового, а именно
«работает ли то, что уже привязано»). Даёт зелёный/красный индикатор в пульте
(колонка «Прокси» в «Мои агенты») — чтобы видеть на глаз, кто реально может
слать/прогреваться, а не гадать по логам после того, как всё зависло.

Реальный MTProto-запрос (help.GetConfig) через прокси аккаунта — как и подбор
free-прокси в proxy_find.py, только тут проверяем уже назначенный прокси, а не
ищем новый. Пишет результат в accounts.proxy_alive / proxy_checked_at.

Запуск:
    python -m channels.proxy_check                # все аккаунты с прокси
    python -m channels.proxy_check --ids 8,11,22   # только эти
"""
from __future__ import annotations

import argparse
import asyncio
import json

from telethon.sessions import StringSession

from channels.proxy_find import _alive
from channels.telegram import build_client
from db import database

_PARALLEL = 8      # сколько прокси проверяем одновременно (см. коммент в run())


def _targets(ids: list[int] | None) -> list[dict]:
    with database.get_conn() as conn:
        if ids:
            qm = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT id, label, proxy, api_id, api_hash FROM accounts "
                f"WHERE id IN ({qm}) AND proxy IS NOT NULL AND proxy<>''", ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, label, proxy, api_id, api_hash FROM accounts "
                "WHERE proxy IS NOT NULL AND proxy<>''"
            ).fetchall()
    return [dict(r) for r in rows]


async def _check_one(acc: dict) -> bool:
    """Живость прокси через собственные api_id/api_hash аккаунта (если есть) —
    ближе к реальным условиям, под которыми он потом будет коннектиться в бою."""
    try:
        client = build_client(StringSession(), acc["proxy"], acc.get("api_id"), acc.get("api_hash"))
    except Exception:  # noqa: BLE001
        return False
    ok = False
    try:
        from telethon import functions
        await asyncio.wait_for(client.connect(), timeout=12)
        await asyncio.wait_for(client(functions.help.GetConfigRequest()), timeout=12)
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return ok


async def run(ids: list[int] | None) -> None:
    database.init_db()
    accs = _targets(ids)
    if not accs:
        print(json.dumps({"ok": False, "error": "нет аккаунтов с назначенным прокси"}, ensure_ascii=False))
        return
    results: dict[int, bool] = {}

    # Пачками, а не все разом: на 38 одновременных коннектов половина живых прокси
    # отваливалась по таймауту и красилась в 🔴 — при перепроверке те же оживали.
    # Ложный «мёртв» хуже медленной проверки: по нему выключают рабочий аккаунт.
    sem = asyncio.Semaphore(_PARALLEL)

    async def _one(a: dict) -> None:
        async with sem:
            alive = await _check_one(a)
            if not alive:            # прежде чем красить в 🔴 — второй шанс, вдруг просто моргнуло
                await asyncio.sleep(1)
                alive = await _check_one(a)
        results[a["id"]] = alive
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE accounts SET proxy_alive=?, proxy_checked_at=datetime('now') WHERE id=?",
                (1 if alive else 0, a["id"]),
            )
        print(f"[#{a['id']}] {a.get('label') or ''}: {'🟢 жив' if alive else '🔴 мёртв'}")

    # параллельно — каждый аккаунт бьётся в свой прокси, друг другу не мешают
    await asyncio.gather(*[_one(a) for a in accs])
    alive_n = sum(1 for v in results.values() if v)
    print(json.dumps({"ok": True, "checked": len(accs), "alive": alive_n,
                      "dead": len(accs) - alive_n}, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM проверка живости назначенных прокси")
    p.add_argument("--ids", help="через запятую id аккаунтов (по умолчанию — все с прокси)")
    args = p.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    asyncio.run(run(ids))


if __name__ == "__main__":
    main()
