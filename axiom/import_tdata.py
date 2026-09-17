"""Импорт купленных Telegram-аккаунтов из формата tdata (Telegram Desktop) в AXIOM.

Парная к import_lzt.py (тот читает .session). Здесь на вход идёт папка, внутри
которой лежат каталоги по номеру телефона, а в каждом — свой tdata:

    лзт170926/
      +14722798621/tdata/...
      +14844926574/tdata/...

Каждый tdata конвертируется в Telethon StringSession через opentele и
проверяется живым get_me(). Живые → в БД (создаём или обновляем аккаунт),
мёртвые → в отчёт.

ВАЖНО, почему запускать только на сервере и только один раз:
AuthKeyDuplicatedError сжигает сессию НЕОБРАТИМО, если тот же ключ уходит в
Telegram с двух IP. Поэтому UseCurrentSession (переиспользуем ключ из tdata,
не создавая новый), один коннект на аккаунт и никаких повторных прогонов
«на всякий случай». 2026-07-22 так было потеряно больше половины партии.

Запуск на сервере:
    cd ~/axiom-repo/axiom
    QT_QPA_PLATFORM=offscreen .venv/bin/python import_tdata.py /tmp/tdata_import          # разведка
    QT_QPA_PLATFORM=offscreen .venv/bin/python import_tdata.py /tmp/tdata_import --write  # запись в БД
"""
import asyncio
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

from telethon.sessions import StringSession  # noqa: E402

from opentele.td import TDesktop  # noqa: E402
from opentele.api import API, UseCurrentSession  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "axiom.db")


def _detect_country(phone: str) -> str | None:
    d = phone.lstrip("+")
    prefixes = [
        ("77", "kz"), ("375", "by"), ("380", "ua"), ("7", "ru"), ("1", "us"),
        ("44", "gb"), ("49", "de"), ("48", "pl"), ("90", "tr"), ("998", "uz"),
    ]
    for pref, code in sorted(prefixes, key=lambda x: -len(x[0])):
        if d.startswith(pref):
            return code
    return None


def _find_tdata_dirs(root: str) -> list[tuple[str, str]]:
    """[(номер, путь к tdata)] — номер берём из имени папки."""
    out = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        tdata = os.path.join(path, "tdata")
        if os.path.isdir(tdata):
            out.append(("+" + name.lstrip("+"), tdata))
        elif name == "tdata":
            out.append(("", path))
    return out


async def try_tdata(tdata_path: str) -> dict:
    """tdata → StringSession + живая проверка. Один коннект, без повторов."""
    client = None
    try:
        tdesk = TDesktop(tdata_path)
        if not tdesk.isLoaded():
            return {"ok": False, "reason": "tdata не читается (пустой или битый)"}
        # UseCurrentSession — переиспользуем ключ из tdata. CreateNewSession дал бы
        # второй ключ и лишний вход в «Устройствах», а при сбое — потерю доступа.
        client = await tdesk.ToTelethon(StringSession(), UseCurrentSession,
                                        api=API.TelegramDesktop)
        await client.connect()
        if not await client.is_user_authorized():
            return {"ok": False, "reason": "не авторизован (сессия слетела)"}
        me = await client.get_me()
        if not me:
            return {"ok": False, "reason": "get_me пустой"}
        return {"ok": True,
                "session_str": StringSession.save(client.session),
                "username": me.username or "",
                "phone": "+" + me.phone if me.phone else "",
                "uid": me.id,
                "name": " ".join(filter(None, [me.first_name, me.last_name]))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass


async def main(root: str, write: bool) -> None:
    items = _find_tdata_dirs(root)
    print(f"Найдено tdata: {len(items)}   режим: {'ЗАПИСЬ В БД' if write else 'разведка (--write для записи)'}\n")
    if not items:
        print("Внутри должны лежать папки вида +14722798621/tdata/")
        return

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    alive = dead = created = updated = 0

    for phone_dir, tdata in items:
        res = await try_tdata(tdata)
        phone = res.get("phone") or phone_dir
        if not res["ok"]:
            print(f"  [мёртв]  {phone_dir} — {res['reason']}")
            dead += 1
            continue

        alive += 1
        c.execute("SELECT id,label,status FROM accounts WHERE phone=?", (phone,))
        acc = c.fetchone()
        if acc and acc[2] == "banned":
            print(f"  [бан, пропуск] {phone} ({acc[1]})")
            continue

        if not write:
            where = f"обновит #{acc[0]} ({acc[1]})" if acc else "создаст новый"
            print(f"  [ЖИВОЙ]  {phone} @{res['username'] or '—'} {res['name']} → {where}")
            continue

        if acc:
            c.execute(
                "UPDATE accounts SET tg_session=?, session_state='alive', session_alive=1, "
                "session_checked_at=CURRENT_TIMESTAMP, "
                "status=CASE WHEN status='banned' THEN status ELSE 'warming' END, "
                "username=COALESCE(NULLIF(?,''),username) WHERE id=?",
                (res["session_str"], res["username"], acc[0]),
            )
            conn.commit()
            print(f"  [ЖИВОЙ, обновлён] {phone} ({acc[1]}) @{res['username']} uid={res['uid']}")
            updated += 1
        else:
            label = f"LZT #{phone[-4:]}"
            c.execute(
                "INSERT INTO accounts (label, phone, username, tg_session, kind, status, "
                "daily_limit, session_state, session_alive, session_checked_at, acc_role, "
                "country, notes, bought_at) "
                "VALUES (?,?,?,?,'bought','warming',10,'alive',1,CURRENT_TIMESTAMP,'combat',?,?,datetime('now'))",
                (label, phone, res["username"] or None, res["session_str"],
                 _detect_country(phone), f"Импорт tdata, uid={res['uid']}"),
            )
            conn.commit()
            print(f"  [ЖИВОЙ, СОЗДАН]  {phone} ({label}) @{res['username']} uid={res['uid']}")
            created += 1

    conn.close()
    print("\n=== ИТОГ ===")
    print(f"Живых: {alive} из {len(items)}   мёртвых: {dead}")
    if write:
        print(f"Создано: {created}, обновлено: {updated}")
    else:
        print("Это была разведка — в БД ничего не записано. Повтор с --write.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    asyncio.run(main(args[0] if args else "/tmp/tdata_import", "--write" in sys.argv))
