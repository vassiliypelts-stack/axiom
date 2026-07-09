"""Завод аккаунта в БД из «сырых» полей панели магазина (без файла .session).

Магазины (lzt.market и пр.) показывают: Phone, Auth Key (HEX), DC ID, User ID.
Этого достаточно, чтобы собрать Telethon StringSession без логина/SMS. Скрипт
строит сессию, ПОДКЛЮЧАЕТСЯ и проверяет get_me (живость), и кладёт в accounts
(status=warming) с дефолтными api_id/api_hash Telegram Desktop.

    python -m channels.account_add_fields --phone 17018957506 --dc 1 \
        --authkey 7851b6...dd004 [--twofa 1234] [--label "США #1"] [--status warming]
    # или из stdin (4 строки: phone / authkey_hex / dc / user_id) — формат панели:
    python -m channels.account_add_fields < fields.txt

Auth Key и 2FA — секреты, БД не публикуй.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

logging.getLogger("telethon").setLevel(logging.CRITICAL)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))

from telethon import TelegramClient  # noqa: E402
from telethon.crypto import AuthKey  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

from opentele.api import API  # noqa: E402

from db import database  # noqa: E402

# Боевые адреса дата-центров Telegram (telethon) — нужны StringSession.set_dc.
DC_ADDR = {
    1: ("149.154.175.53", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("91.108.56.130", 443),
}


def build_session(authkey_hex: str, dc: int) -> str:
    raw = bytes.fromhex(authkey_hex.strip())
    if len(raw) != 256:
        raise ValueError(f"Auth Key должен быть 256 байт (512 hex), а тут {len(raw)} байт")
    if dc not in DC_ADDR:
        raise ValueError(f"неизвестный DC ID: {dc}")
    ip, port = DC_ADDR[dc]
    ss = StringSession()
    ss.set_dc(dc, ip, port)
    ss.auth_key = AuthKey(raw)
    return ss.save()


async def verify(session_str: str) -> tuple[bool, dict]:
    client = TelegramClient(
        StringSession(session_str), API.TelegramDesktop.api_id, API.TelegramDesktop.api_hash,
        connection_retries=2, retry_delay=1, timeout=15,
    )
    info: dict = {}
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return False, {"reason": "не авторизован — ключ мёртв/отозван"}
        me = await client.get_me()
        info = {
            "user_id": me.id,
            "username": me.username,
            "phone": ("+" + me.phone) if me.phone else None,
            "first_name": me.first_name,
        }
        return True, info
    except Exception as e:  # noqa: BLE001
        return False, {"reason": f"{type(e).__name__}: {e}"}
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def save_to_db(phone: str, session_str: str, info: dict, twofa: str, label: str, status: str) -> str:
    import phone_geo
    phone = "+" + phone.lstrip("+")
    username = info.get("username")
    name = label or info.get("first_name") or username or phone
    notes = "заведён из полей панели" + (f" · 2FA: {twofa}" if twofa else "")
    country = phone_geo.detect(phone)   # страна по коду номера (для гео-прокси)
    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE phone=?", (phone,)).fetchone()
        if row:
            conn.execute(
                "UPDATE accounts SET tg_session=?, username=COALESCE(?,username), "
                "label=COALESCE(label,?), notes=?, country=COALESCE(NULLIF(country,''), ?) WHERE id=?",
                (session_str, username, name, notes, country, row["id"]),
            )
            return f"обновлён #{row['id']} {phone} (@{username or '—'})"
        cur = conn.execute(
            "INSERT INTO accounts (label, phone, username, role, status, daily_limit, notes, "
            "tg_session, country, bought_at, kind) VALUES (?,?,?,?,?,?,?,?,?,datetime('now'),'bought')",
            (name, phone, username, "sdr", status, 15, notes, session_str, country),
        )
        return f"добавлен #{cur.lastrowid} {phone} (@{username or '—'})"


async def run(phone: str, dc: int, authkey: str, twofa: str, label: str, status: str, save: bool) -> None:
    session_str = build_session(authkey, dc)
    print(f"Сессия собрана (DC {dc}). Проверяю живость…")
    alive, info = await verify(session_str)
    if not alive:
        print(f"  МЁРТВЫЙ — {info.get('reason')}")
        return
    print(f"  ЖИВОЙ  id={info['user_id']}  @{info['username'] or '—'}  "
          f"{info['phone'] or '—'}  {info['first_name'] or ''}")
    if save:
        print("— запись в БД —")
        print("  " + save_to_db(phone, session_str, info, twofa, label, status))
    else:
        print("(для записи в базу добавь --save)")


def main() -> None:
    p = argparse.ArgumentParser(description="Завод TG-аккаунта в БД из полей панели")
    p.add_argument("--phone")
    p.add_argument("--dc", type=int)
    p.add_argument("--authkey", help="Auth Key HEX (512 символов)")
    p.add_argument("--twofa", default="")
    p.add_argument("--label", default="")
    p.add_argument("--status", default="warming", choices=["warming", "active", "paused"])
    p.add_argument("--save", action="store_true")
    args = p.parse_args()

    phone, dc, authkey = args.phone, args.dc, args.authkey
    if not (phone and dc and authkey):  # формат панели из stdin: phone / authkey / dc / user_id
        lines = [x.strip() for x in sys.stdin.read().splitlines() if x.strip()]
        if len(lines) >= 3:
            phone = phone or lines[0]
            authkey = authkey or lines[1]
            dc = dc or int(lines[2])
    if not (phone and dc and authkey):
        p.error("нужны --phone, --dc и --authkey (или 3+ строки в stdin: phone/authkey/dc)")
    asyncio.run(run(phone, dc, authkey, args.twofa, args.label, args.status, args.save))


if __name__ == "__main__":
    main()
