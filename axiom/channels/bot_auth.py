"""Telegram bot auth — вход в веб-пульт AXIOM через @Jarvisvvp_bot.

Как работает:
1. Пользователь пишет /login боту @Jarvisvvp_bot
2. На странице логина нажимает «Получить код»
3. Сервер забирает апдейты бота → извлекает chat_id → генерирует 6-значный код
4. Бот отправляет код пользователю в Telegram
5. Пользователь вводит код на странице → сессия создана
"""
from __future__ import annotations

import json
import secrets
import time
import hmac
import hashlib
from urllib.request import Request, urlopen

import os as _os
BOT_TOKEN = (_os.environ.get("TG_BOT_TOKEN") or "").strip()
BOT_USERNAME = (_os.environ.get("TG_BOT_USERNAME") or "Jarvisvvp_bot").strip()
if not BOT_TOKEN:
    BOT_USERNAME = "none"
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

# Коды авторизации: code -> {chat_id, expires_at, used}
_codes: dict[str, dict] = {}
# Сессии: session_id -> {chat_id, created_at}
_sessions: dict[str, dict] = {}
# Последний обработанный update_id
_last_update_id: int = 0


def _api(method: str, data: dict | None = None) -> dict:
    """Вызов Telegram Bot API."""
    body = json.dumps(data).encode() if data else None
    headers = {"Content-Type": "application/json"} if data else {}
    try:
        with urlopen(Request(f"{API_BASE}/{method}", data=body, headers=headers), timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_message(chat_id: int, text: str) -> bool:
    """Отправить сообщение через бота (HTML-разметка)."""
    result = _api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    return result.get("ok", False)


def get_updates() -> list[dict]:
    """Забрать новые апдейты. None если ошибка."""
    result = _api("getUpdates", {
        "offset": _last_update_id + 1 if _last_update_id else 0,
        "timeout": 2,
    })
    return result.get("result", []) if result.get("ok") else []


def _consume_updates() -> dict | None:
    """Забрать апдейты и вернуть последнее сообщение (чат, текст)."""
    updates = get_updates()
    if not updates:
        return None
    last = updates[-1]
    global _last_update_id
    _last_update_id = last.get("update_id", 0)
    msg = last.get("message") or {}
    chat = msg.get("chat", {})
    text = (msg.get("text") or "").strip()
    if not chat.get("id"):
        return None
    return {"chat_id": chat["id"], "text": text,
            "username": chat.get("username", ""),
            "first_name": chat.get("first_name", "")}


def request_code() -> dict:
    """Сгенерировать код и отправить пользователю через бота.

    Ищет последнее сообщение в апдейтах — по нему определяет chat_id.
    Если сообщений нет — просит написать боту.
    """
    # Забираем апдейты чтобы получить chat_id
    update = _consume_updates()
    if not update:
        return {"ok": False, "error": f"Напишите /login боту @{BOT_USERNAME}"}

    chat_id = update["chat_id"]
    text = update["text"]
    name = update["first_name"] or update["username"] or str(chat_id)

    # Если сообщение содержит /login — ок, иначе тоже ок (любое сообщение = запрос)
    code = f"{secrets.randbelow(900000) + 100000}"
    _codes[code] = {
        "chat_id": chat_id,
        "expires_at": time.time() + 300,  # 5 минут
        "used": False,
    }

    ok = send_message(chat_id,
        f"🔑 <b>Код для входа в AXIOM</b>\n\n"
        f"<code>{code}</code>\n\n"
        f"Действителен 5 минут. Никому не сообщайте код.")
    if not ok:
        return {"ok": False, "error": "Не удалось отправить код. Попробуйте ещё раз."}

    return {"ok": True, "code_sent": True, "to": name}


def verify_code(code: str) -> dict:
    """Проверить код и создать сессию. Возвращает session_id."""
    entry = _codes.get(code)
    if not entry:
        return {"ok": False, "error": "Неверный код"}
    if entry["used"]:
        return {"ok": False, "error": "Код уже использован"}
    if time.time() > entry["expires_at"]:
        _codes.pop(code, None)
        return {"ok": False, "error": "Код истёк. Запросите новый."}

    entry["used"] = True
    # Создаём сессию
    session_id = secrets.token_hex(20)
    _sessions[session_id] = {
        "chat_id": entry["chat_id"],
        "created_at": time.time(),
    }
    # чистим старые сессии того же chat_id (перезаписываем)
    for sid, sess in list(_sessions.items()):
        if sess["chat_id"] == entry["chat_id"] and sid != session_id:
            _sessions.pop(sid, None)

    _codes.pop(code, None)
    cleanup()
    return {"ok": True, "session_id": session_id}


def check_session(session_id: str) -> bool:
    """Проверить, жива ли сессия."""
    if not session_id:
        return False
    entry = _sessions.get(session_id)
    if not entry:
        return False
    # Сессия живёт 30 дней
    if time.time() - entry["created_at"] > 60 * 60 * 24 * 30:
        _sessions.pop(session_id, None)
        return False
    return True


def cleanup():
    """Удалить протухшие коды и сессии."""
    now = time.time()
    for k, v in list(_codes.items()):
        if now > v["expires_at"]:
            _codes.pop(k, None)
    for sid, sess in list(_sessions.items()):
        if now - sess["created_at"] > 60 * 60 * 24 * 30:
            _sessions.pop(sid, None)
