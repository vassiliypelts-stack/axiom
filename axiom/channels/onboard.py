"""Массовое оформление купленных аккаунтов: bio + аватар + приватность (спрятать
номер, защита от репортов) — одним прогоном по списку id, а не по одному вручную.

Переиспользует ту же логику, что кнопка «✨ Оформить профиль» в карточке аккаунта
(_setup_profile из warmup.py) — просто прогоняет её по многим аккаунтам подряд, с
человеческой паузой между ними (не долбим Telegram полусотней коннектов разом).

    python -m channels.onboard --ids 12,13,14,20
"""
from __future__ import annotations

import argparse
import asyncio
import random

from telethon.sessions import StringSession

from channels.telegram import build_client
from channels.warmup import _setup_profile
from db import database


async def _onboard_one(acc: dict) -> tuple[bool, str]:
    label = acc.get("label") or acc.get("phone") or f"#{acc['id']}"
    if not acc.get("tg_session"):
        return False, f"{label}: нет сессии — сначала подключи (кнопка «Подключить»)"
    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
    try:
        await client.start()
        done = await _setup_profile(client, acc, force=True)
        me = await client.get_me()   # синхронизируем реальный ник обратно в нашу БД
        if me.username:
            with database.get_conn() as conn:
                conn.execute("UPDATE accounts SET username=? WHERE id=?", (me.username, acc["id"]))
        return True, f"{label}: {', '.join(done) if done else 'нечего было ставить (пустая карточка)'}"
    except Exception as e:  # noqa: BLE001
        return False, f"{label}: ошибка — {e}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def run(ids: list[int]) -> None:
    database.init_db()
    ok = 0
    for i, acc_id in enumerate(ids):
        with database.get_conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if not row:
            print(f"[skip] аккаунт #{acc_id} не найден")
            continue
        success, msg = await _onboard_one(dict(row))
        print(("[ok] " if success else "[skip] ") + msg)
        ok += int(success)
        if i < len(ids) - 1:
            await asyncio.sleep(random.uniform(2.0, 5.0))  # не долбим разом
    with database.get_conn() as conn:
        database.add_event(
            conn, "info", f"✨ Массовое оформление: готово {ok} из {len(ids)}",
            "bio + аватар + приватность (спрятан номер) — там, где карточка/сессия позволили",
            level="good" if ok else "warn",
        )
    print(f"\nИтого оформлено: {ok} из {len(ids)}")


def main() -> None:
    p = argparse.ArgumentParser(description="Массовое оформление профиля + приватность")
    p.add_argument("--ids", required=True, help="через запятую: 1,2,3")
    args = p.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()]
    if not ids:
        p.error("пустой список --ids")
    asyncio.run(run(ids))


if __name__ == "__main__":
    main()
