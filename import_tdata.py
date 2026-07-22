"""Конвертация Telegram Desktop tdata → Telethon-сессия (opentele) и импорт в AXIOM.

tdata часто переживает то, что убивает .session. Структура: <base>/<id>/tdata/.
По умолчанию DRY-RUN (только показать живых). С --write — писать сессии в БД.

Запуск на сервере (QT offscreen):
    QT_QPA_PLATFORM=offscreen python import_tdata.py /tmp/tdata_all           # dry-run
    QT_QPA_PLATFORM=offscreen python import_tdata.py /tmp/tdata_all --write   # запись
"""
import asyncio
import os
import sqlite3
import sys

sys.path.insert(0, ".")
from telethon.sessions import StringSession
from vendor.opentele.td import TDesktop
from vendor.opentele.api import API, UseCurrentSession

DB = "/home/vassiliy_pelts/axiom-repo/axiom/data/axiom.db"


def find_tdata_dirs(base: str) -> list[str]:
    """Пути к папкам tdata: <base>/<id>/tdata или <base>/tdata напрямую."""
    out = []
    for root, dirs, files in os.walk(base):
        if os.path.basename(root) == "tdata" and (
            "key_datas" in files or any(f.startswith("D877F783") for f in files)
        ):
            out.append(root)
    return sorted(set(out))


async def one(tdata_path: str, write: bool, conn) -> str:
    try:
        tdesk = TDesktop(tdata_path)
        if not tdesk.isLoaded():
            return f"  [пусто] {tdata_path} — не загрузилось"
        client = await tdesk.ToTelethon(session=StringSession(),
                                        flag=UseCurrentSession, api=API.TelegramDesktop)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return f"  [мёртв] {os.path.basename(os.path.dirname(tdata_path))} — не авторизован"
            me = await client.get_me()
            phone = "+" + (me.phone or "").lstrip("+")
            uname = me.username or ""
            sess_str = StringSession.save(client.session)
            tag = ""
            if write and me.phone:
                c = conn.cursor()
                c.execute("SELECT id,status FROM accounts WHERE phone=?", (phone,))
                row = c.fetchone()
                if row and row[1] != "banned":
                    c.execute(
                        "UPDATE accounts SET tg_session=?, session_state='alive', "
                        "status=CASE WHEN status='banned' THEN status ELSE 'warming' END, "
                        "username=COALESCE(NULLIF(?,''),username) WHERE id=?",
                        (sess_str, uname, row[0]),
                    )
                    conn.commit()
                    tag = f" → записан в БД (id={row[0]})"
                elif not row:
                    tag = " (нет в БД — не записан)"
                else:
                    tag = " (в бане — не записан)"
            return f"  [ЖИВОЙ] {phone} @{uname} uid={me.id}{tag}"
        finally:
            await client.disconnect()
    except Exception as e:  # noqa: BLE001
        return f"  [ошибка] {os.path.basename(os.path.dirname(tdata_path))} — {type(e).__name__}: {str(e)[:80]}"


async def main(base: str, write: bool):
    dirs = find_tdata_dirs(base)
    print(f"Найдено tdata-папок: {len(dirs)}  (режим: {'ЗАПИСЬ' if write else 'DRY-RUN'})\n")
    conn = sqlite3.connect(DB) if write else None
    alive = 0
    for d in dirs:
        line = await one(d, write, conn)
        print(line, flush=True)
        if "[ЖИВОЙ]" in line:
            alive += 1
    if conn:
        conn.close()
    print(f"\n=== ИТОГ: живых {alive} из {len(dirs)} ===")


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tdata_all"
    write = "--write" in sys.argv
    asyncio.run(main(base, write))
