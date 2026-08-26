"""Веб-логин аккаунта в Telegram — 2 шага: запросить код → ввести код.

В отличие от CLI (`account_login.py`), здесь логин делается из пульта без консоли:
1) start_login — Telegram присылает код (в приложение/SMS), временная сессия
   сохраняется в памяти процесса пульта;
2) submit_code — вводим код (и пароль 2FA, если включён) → сессия пишется в БД.

Состояние между двумя HTTP-запросами держим в _PENDING (память процесса). Этого
достаточно для single-process uvicorn. Сессия (полный доступ) ложится в БД —
секрет, БД не публикуй.
"""
from __future__ import annotations

from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

import config
from channels.telegram import build_client
from db import database

# acc_id -> {"session": str, "hash": str, "phone": str, "proxy": str|None}
_PENDING: dict[int, dict] = {}


async def start_login(acc_id: int) -> dict:
    """Шаг 1: запросить код подтверждения у Telegram."""
    if not config.TG_API_ID or not config.TG_API_HASH:
        return {"error": "Заполни TG_API_ID и TG_API_HASH в .env (my.telegram.org)"}
    database.init_db()
    with database.get_conn() as conn:
        acc = database.get_account(conn, acc_id)
    if not acc:
        return {"error": f"аккаунта #{acc_id} нет в «Мои агенты»"}
    phone = (acc["phone"] or "").strip()
    if not phone:
        return {"error": "у аккаунта не указан телефон"}
    client = build_client(StringSession(), acc["proxy"])
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        _PENDING[acc_id] = {
            "session": client.session.save(),
            "hash": sent.phone_code_hash,
            "phone": phone,
            "proxy": acc["proxy"],
        }
        return {"ok": True, "phone": phone}
    except Exception as e:  # noqa: BLE001
        return {"error": f"не удалось отправить код: {e}"}
    finally:
        await client.disconnect()


async def submit_code(acc_id: int, code: str, password: str = "") -> dict:
    """Шаг 2: ввести код (и пароль 2FA при необходимости) → сохранить сессию."""
    st = _PENDING.get(acc_id)
    if not st:
        return {"error": "сначала запроси код (шаг 1)"}
    code = (code or "").strip()
    if not code:
        return {"error": "введи код из Telegram"}
    client = build_client(StringSession(st["session"]), st["proxy"])
    try:
        await client.connect()
        try:
            await client.sign_in(phone=st["phone"], code=code, phone_code_hash=st["hash"])
        except SessionPasswordNeededError:
            if not (password or "").strip():
                return {"need_password": True}
            await client.sign_in(password=password.strip())
        me = await client.get_me()
        with database.get_conn() as conn:
            database.save_account_session(conn, acc_id, client.session.save(), me.username)
            conn.execute(
                "UPDATE accounts SET status='warming' WHERE id=? AND status NOT IN ('active','banned')",
                (acc_id,),
            )
        _PENDING.pop(acc_id, None)
        return {"ok": True, "username": me.username}
    except PhoneCodeInvalidError:
        return {"error": "неверный код — попробуй ещё раз"}
    except PhoneCodeExpiredError:
        _PENDING.pop(acc_id, None)
        return {"error": "код истёк — запроси заново"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    finally:
        await client.disconnect()
