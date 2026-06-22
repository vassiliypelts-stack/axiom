"""Проверка здоровья аккаунтов AXIOM через @SpamBot (ограничения/бан).

Каждый аккаунт сам спрашивает у официального @SpamBot, нет ли на нём ограничений,
и пишет вердикт в карточку (accounts.spam_status). Так видно, какой номер «подсел»,
до того как он сожжёт кампанию.

Проверяет основной аккаунт (TG_STRING_SESSION из .env) + все аккаунты, залогиненные
через `python -m channels.account_login` (у кого есть tg_session в БД).

    python -m channels.health            # проверить все
    python -m channels.health --id 4     # один аккаунт
"""
from __future__ import annotations

import argparse
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
from channels.telegram import _build_client, build_client
from db import database

# Маркеры «всё чисто» в ответе @SpamBot (он отвечает на языке аккаунта).
_OK_MARKERS = ("no limits", "good news", "свободен", "нет ограничен", "ваш аккаунт свободен")
_BAN_MARKERS = ("banned", "deleted", "заблокирован", "удал")


async def _check(client) -> tuple[str, str]:
    """Возвращает (status, текст ответа). status: ok|limited|banned|unknown."""
    try:
        await client.send_message("SpamBot", "/start")
        await asyncio.sleep(4)
        msgs = await client.get_messages("SpamBot", limit=1)
        text = (msgs[0].message if msgs else "") or ""
    except Exception as e:  # noqa: BLE001
        return "banned", f"ошибка обращения к @SpamBot: {e}"
    low = text.lower()
    if any(m in low for m in _OK_MARKERS):
        return "ok", text[:300]
    if any(m in low for m in _BAN_MARKERS):
        return "banned", text[:300]
    if not text:
        return "unknown", "пустой ответ"
    return "limited", text[:300]


def _save(acc_id: int, status: str) -> None:
    with database.get_conn() as conn:
        conn.execute(
            "UPDATE accounts SET spam_status=?, spam_checked_at=datetime('now') WHERE id=?",
            (status, acc_id),
        )
        if status == "banned":
            conn.execute("UPDATE accounts SET status='banned' WHERE id=?", (acc_id,))


async def _check_account(acc: dict) -> None:
    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"))
    await client.connect()
    if not await client.is_user_authorized():
        _save(acc["id"], "unknown")
        print(f"[#{acc['id']}] сессия не авторизована")
        await client.disconnect()
        return
    status, text = await _check(client)
    _save(acc["id"], status)
    print(f"[#{acc['id']} {acc.get('label') or acc.get('phone')}] {status} — {text[:80]}")
    await client.disconnect()


async def _check_main() -> None:
    """Основной аккаунт из .env (TG_STRING_SESSION) — обновляем его строку в БД по username."""
    if not config.TG_STRING_SESSION:
        return
    try:
        client = _build_client()
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return
        me = await client.get_me()
        status, text = await _check(client)
        await client.disconnect()
    except Exception as e:  # noqa: BLE001
        print(f"[main] ошибка: {e}")
        return
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM accounts WHERE username=? OR phone=?",
            (me.username, f"+{me.phone}" if me.phone else None),
        ).fetchone()
    if row:
        _save(row["id"], status)
        print(f"[main #{row['id']} @{me.username}] {status} — {text[:80]}")
    else:
        print(f"[main @{me.username}] {status} (нет в «Команде»)")


async def run(only_id: int | None) -> None:
    database.init_db()
    with database.get_conn() as conn:
        if only_id:
            accs = [dict(a) for a in conn.execute(
                "SELECT * FROM accounts WHERE id=? AND tg_session IS NOT NULL AND tg_session<>''", (only_id,))]
        else:
            accs = [dict(a) for a in conn.execute(
                "SELECT * FROM accounts WHERE tg_session IS NOT NULL AND tg_session<>''")]
    if not only_id:
        await _check_main()
    for acc in accs:
        try:
            await _check_account(acc)
        except Exception as e:  # noqa: BLE001
            print(f"[fail #{acc['id']}] {e}")
        await asyncio.sleep(2)
    print("проверка @SpamBot завершена")


def main() -> None:
    p = argparse.ArgumentParser(description="Проверка аккаунтов через @SpamBot")
    p.add_argument("--id", type=int, default=None, help="проверить один аккаунт по id")
    args = p.parse_args()
    asyncio.run(run(args.id))


if __name__ == "__main__":
    main()
