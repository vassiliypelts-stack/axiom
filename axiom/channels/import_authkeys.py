"""Массовый завод аккаунтов из построчного дампа магазина: каждая строка —
«authkey_hex:dc_id» (без телефона — в отличие от account_add_fields, тут
телефон не в файле, поэтому получаем его сами живым подключением, как и при
проверке живости).

    python -m channels.import_authkeys путь/к/файлу.txt [--status warming]

Файл с ключами — секрет (полный доступ к аккаунту), после успешного завода
удали его. Ничего не коммить в git.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from channels.account_add_fields import build_session, save_to_db, verify


async def run(path: str, status: str) -> None:
    lines = [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    ok = 0
    for i, line in enumerate(lines, 1):
        if ":" not in line:
            print(f"[{i}] пропуск — нет «:dc_id» в строке")
            continue
        authkey, _, dc_s = line.rpartition(":")
        try:
            dc = int(dc_s)
            session_str = build_session(authkey, dc)
        except Exception as e:  # noqa: BLE001
            print(f"[{i}] ошибка сборки сессии: {e}")
            continue
        alive, info = await verify(session_str)
        if not alive:
            print(f"[{i}] МЁРТВЫЙ — {info.get('reason')}")
            continue
        phone = (info.get("phone") or "").lstrip("+")
        if not phone:
            print(f"[{i}] живой, но телефон не отдался — заведи вручную через account_add_fields --phone")
            continue
        msg = save_to_db(phone, session_str, info, twofa="", label="", status=status)
        print(f"[{i}] {msg}")
        ok += 1
        if i < len(lines):
            await asyncio.sleep(1.5)  # не долбим Telegram коннектами подряд
    print(f"\nИтого заведено: {ok} из {len(lines)}")


def main() -> None:
    p = argparse.ArgumentParser(description="Массовый завод аккаунтов из дампа authkey:dc_id")
    p.add_argument("file")
    p.add_argument("--status", default="warming", choices=["warming", "active", "paused"])
    args = p.parse_args()
    asyncio.run(run(args.file, args.status))


if __name__ == "__main__":
    main()
