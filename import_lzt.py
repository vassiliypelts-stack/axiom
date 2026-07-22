"""Правильный импорт Telethon .session файлов из LZT в AXIOM.

Открывает каждый .session НАПРЯМУЮ (это SQLite-сессия Telethon), с РОДНЫМ
app_id/app_hash из парного .json, проверяет get_me() вживую. Живые → StringSession
в БД к аккаунту с этим номером (status=warming). Мёртвые/неавторизованные — репорт.

Запуск на сервере (GCP, US — Telegram доступен):
    python import_lzt.py /tmp/lzt_sessions
"""
import asyncio
import json
import os
import sqlite3
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

DB = "/home/vassiliy_pelts/axiom-repo/axiom/data/axiom.db"
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"


async def try_session(sess_path: str, phone: str, api_id: int, api_hash: str,
                      proxy=None) -> dict:
    """Открыть .session, проверить живость. Возвращает {ok, session_str, username, reason}."""
    client = TelegramClient(sess_path, api_id, api_hash, proxy=proxy)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {"ok": False, "reason": "не авторизован (сессия слетела)"}
        me = await client.get_me()
        if not me:
            return {"ok": False, "reason": "get_me пустой"}
        sess_str = StringSession.save(client.session)  # конверт в строковую
        return {"ok": True, "session_str": sess_str,
                "username": me.username or "", "uid": me.id}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def main(sessions_dir: str):
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    files = sorted(f for f in os.listdir(sessions_dir) if f.endswith(".session"))
    print(f"Найдено .session: {len(files)}\n")

    alive, dead, notfound = 0, 0, 0
    for fn in files:
        phone = "+" + fn.replace(".session", "").lstrip("+")
        sess_path = os.path.join(sessions_dir, fn[:-8])  # без .session — Telethon добавит

        # найти аккаунт в БД
        c.execute("SELECT id,label,status FROM accounts WHERE phone=?", (phone,))
        acc = c.fetchone()
        if not acc:
            print(f"  [нет в БД] {phone}")
            notfound += 1
            continue
        aid, label, status = acc
        if status == "banned":
            print(f"  [бан, пропуск] {phone} ({label})")
            continue

        # app_id/app_hash из .json
        api_id, api_hash = DEFAULT_API_ID, DEFAULT_API_HASH
        jf = os.path.join(sessions_dir, fn.replace(".session", ".json"))
        if os.path.exists(jf):
            with open(jf) as f:
                meta = json.load(f)
            api_id = meta.get("app_id") or api_id
            api_hash = meta.get("app_hash") or api_hash

        res = await try_session(sess_path, phone, api_id, api_hash, proxy=None)
        if res["ok"]:
            c.execute(
                "UPDATE accounts SET tg_session=?, api_id=?, api_hash=?, "
                "session_state='alive', status=CASE WHEN status='banned' THEN status ELSE 'warming' END, "
                "username=COALESCE(NULLIF(?,''),username) WHERE id=?",
                (res["session_str"], api_id, api_hash, res["username"], aid),
            )
            conn.commit()
            print(f"  [ЖИВОЙ] {phone} ({label}) @{res['username']} uid={res['uid']}")
            alive += 1
        else:
            print(f"  [мёртв] {phone} ({label}) — {res['reason']}")
            dead += 1

    conn.close()
    print(f"\n=== ИТОГ ===")
    print(f"Живых залогинено: {alive}")
    print(f"Мёртвых/слетевших: {dead}")
    print(f"Нет в БД: {notfound}")


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "/tmp/lzt_sessions"
    asyncio.run(main(d))
