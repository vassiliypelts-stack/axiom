"""Правильный импорт Telethon .session файлов из LZT в AXIOM.

Открывает каждый .session НАПРЯМУЮ (это SQLite-сессия Telethon), с РОДНЫМ
app_id/app_hash из парного .json, проверяет get_me() вживую. Живые → StringSession
в БД. Если аккаунт с таким номером уже есть — обновляет; если нет — СОЗДАЁТ
новый (status=warming, kind=bought). Мёртвые/неавторизованные — репорт.

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


def _detect_country(phone: str) -> str | None:
    """Грубое определение страны по коду номера — для метки в карточке аккаунта."""
    d = phone.lstrip("+")
    prefixes = [
        ("77", "kz"), ("375", "by"), ("380", "ua"), ("7", "ru"), ("1", "us"),
        ("44", "gb"), ("49", "de"), ("48", "pl"), ("90", "tr"), ("998", "uz"),
    ]
    for pref, code in sorted(prefixes, key=lambda x: -len(x[0])):
        if d.startswith(pref):
            return code
    return None


async def main(sessions_dir: str):
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    files = sorted(f for f in os.listdir(sessions_dir) if f.endswith(".session"))
    print(f"Найдено .session: {len(files)}\n")

    alive, dead, created, updated = 0, 0, 0, 0
    for fn in files:
        phone = "+" + fn.replace(".session", "").lstrip("+")
        sess_path = os.path.join(sessions_dir, fn[:-8])  # без .session — Telethon добавит

        # найти аккаунт в БД (создадим позже, если живой и не найден)
        c.execute("SELECT id,label,status FROM accounts WHERE phone=?", (phone,))
        acc = c.fetchone()
        if acc and acc[2] == "banned":
            print(f"  [бан, пропуск] {phone} ({acc[1]})")
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
        if not res["ok"]:
            print(f"  [мёртв] {phone} ({acc[1] if acc else '—'}) — {res['reason']}")
            dead += 1
            continue

        if acc:
            aid, label = acc[0], acc[1]
            c.execute(
                "UPDATE accounts SET tg_session=?, api_id=?, api_hash=?, "
                "session_state='alive', session_alive=1, "
                "status=CASE WHEN status='banned' THEN status ELSE 'warming' END, "
                "username=COALESCE(NULLIF(?,''),username) WHERE id=?",
                (res["session_str"], api_id, api_hash, res["username"], aid),
            )
            conn.commit()
            print(f"  [ЖИВОЙ, обновлён] {phone} ({label}) @{res['username']} uid={res['uid']}")
            updated += 1
        else:
            country = _detect_country(phone)
            label = f"LZT #{phone[-4:]}"
            c.execute(
                "INSERT INTO accounts (label, phone, username, api_id, api_hash, tg_session, "
                "kind, status, daily_limit, session_state, session_alive, country, notes, bought_at) "
                "VALUES (?,?,?,?,?,?,'bought','warming',10,'alive',1,?,?,datetime('now'))",
                (label, phone, res["username"] or None, api_id, api_hash,
                 res["session_str"], country, f"Импорт LZT, uid={res['uid']}"),
            )
            conn.commit()
            print(f"  [ЖИВОЙ, СОЗДАН] {phone} ({label}) @{res['username']} uid={res['uid']}")
            created += 1
        alive += 1

    conn.close()
    print(f"\n=== ИТОГ ===")
    print(f"Живых залогинено: {alive} (создано новых: {created}, обновлено: {updated})")
    print(f"Мёртвых/слетевших: {dead}")


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "/tmp/lzt_sessions"
    asyncio.run(main(d))
