"""Экспорт TG-аккаунта в портативный Telegram Desktop (зайти в аккаунт руками).

Из сохранённой сессии аккаунта (accounts.tg_session) создаёт НОВУЮ desktop-сессию
(CreateNewSession — как привязка нового устройства «Telegram Desktop», без риска
api-mismatch) и кладёт её в формате tdata рядом с портативным Telegram.exe. После
этого можно запустить Telegram.exe и оказаться внутри аккаунта.

    python -m channels.tg_export --id 9

Требует подключённую живую сессию (TG✓) и рабочий прокси (или его отсутствие).
tdata — секрет, папку не публикуй.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))

from telethon.sessions import StringSession  # noqa: E402

from opentele.api import API, CreateNewSession  # noqa: E402

from channels.telegram import build_client  # telethon-клиент, расширенный opentele (ToTDesktop)  # noqa: E402
from db import database  # noqa: E402

# Откуда взять портативный Telegram.exe + modules (чтобы папка была сразу запускаемой).
PORTABLE_SRC = Path(__file__).resolve().parents[2] / "ТГ Аккуанты(portable)" / "Telegram"
# Куда складывать готовые порталки по аккаунтам.
PORTABLE_ROOT = Path(__file__).resolve().parents[2] / "ТГ Аккуанты(portable)"


def _out_folder(acc: dict) -> Path:
    name = (acc.get("phone") or f"acc{acc['id']}").lstrip("+")
    return PORTABLE_ROOT / name


async def run(acc_id: int) -> None:
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        print(json.dumps({"ok": False, "error": f"аккаунт #{acc_id} не найден"}, ensure_ascii=False)); return
    acc = dict(row)
    if not acc.get("tg_session"):
        print(json.dumps({"ok": False, "error": "у аккаунта нет TG-сессии — сначала «🔌 Подключить»"},
                         ensure_ascii=False)); return

    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(json.dumps({"ok": False, "error": "сессия не авторизована (ключ мёртв/разлогинен)"},
                             ensure_ascii=False)); return
        # CreateNewSession: новый «Telegram Desktop» на этот аккаунт (как новое устройство)
        tdesk = await client.ToTDesktop(flag=CreateNewSession, api=API.TelegramDesktop)
    except Exception as e:  # noqa: BLE001
        hint = ""
        if "MTProxy secret" in str(e):
            hint = " — у аккаунта битый MTProxy (faketls): сними прокси или поставь рабочий SOCKS5"
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}{hint}"}, ensure_ascii=False)); return
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    folder = _out_folder(acc)
    folder.mkdir(parents=True, exist_ok=True)
    # старый tdata убираем, чтобы не смешать аккаунты
    if (folder / "tdata").exists():
        shutil.rmtree(folder / "tdata", ignore_errors=True)
    tdesk.SaveTData(str(folder / "tdata"))

    # делаем папку сразу запускаемой: копируем Telegram.exe + modules из эталонной порталки
    copied_exe = False
    if PORTABLE_SRC.exists():
        for item in ("Telegram.exe", "modules"):
            src = PORTABLE_SRC / item
            dst = folder / item
            if src.exists() and not dst.exists():
                (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
        copied_exe = (folder / "Telegram.exe").exists()

    print(json.dumps({"ok": True, "folder": str(folder), "runnable": copied_exe,
                      "exe": str(folder / "Telegram.exe") if copied_exe else None},
                     ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="Экспорт TG-аккаунта в портативный Telegram Desktop")
    p.add_argument("--id", type=int, required=True, help="id аккаунта из БД")
    args = p.parse_args()
    asyncio.run(run(args.id))


if __name__ == "__main__":
    main()
