"""Анти-фрод/анти-бан утилиты для Telegram-аккаунтов AXIOM.

Без сети и без побочных эффектов — чистые функции, которые переиспользуют
рассылка (campaign_send), прогрев (warmup) и пульт (web/app):

  • classify_error(exc)   — что за ошибка Telegram: ban / flood / spam / skip / other
  • is_ban(exc)           — фатально ли (аккаунт мёртв/деактивирован)
  • phone_country(phone)  — страна номера (для проверки «прокси = страна номера»)
  • active_window_ok(...) — внутри ли дневного окна активности (антибан-режим)

Цель — централизовать логику, чтобы реакция на бан/флуд была единой во всём коде.
"""
from __future__ import annotations

import datetime

# Префикс кода страны → (ISO, читаемое имя). Длинные префиксы проверяем первыми,
# поэтому при использовании сортируем по длине убыв. (77 раньше 7 — KZ vs RU).
_COUNTRY_PREFIXES: list[tuple[str, str, str]] = [
    ("77", "KZ", "Казахстан"),
    ("375", "BY", "Беларусь"),
    ("380", "UA", "Украина"),
    ("374", "AM", "Армения"),
    ("994", "AZ", "Азербайджан"),
    ("995", "GE", "Грузия"),
    ("996", "KG", "Киргизия"),
    ("998", "UZ", "Узбекистан"),
    ("992", "TJ", "Таджикистан"),
    ("7", "RU", "Россия"),
    ("1", "US", "США/Канада"),
    ("44", "GB", "Британия"),
    ("49", "DE", "Германия"),
    ("48", "PL", "Польша"),
    ("90", "TR", "Турция"),
]
_SORTED_PREFIXES = sorted(_COUNTRY_PREFIXES, key=lambda x: len(x[0]), reverse=True)

# Имена классов ошибок Telethon — сверяем по имени, чтобы не падать на разных
# версиях библиотеки (где-то класса может не быть).
# ⚠️ РАЗДЕЛЕНО НАРОЧНО (было слито в один _BAN_ERRORS — баг, живые номера
# уходили в status='banned' навсегда из-за обычного конфликта сессий):
#   ban            — Telegram убил САМ НОМЕР/аккаунт. Необратимо, номер сожжён.
#   session_revoked — умерла ЭТА КОНКРЕТНАЯ сессия (как «вышел с одного
#                     устройства»). Номер жив, чинится обычным перелогином
#                     (🔌 Подключить). Частый триггер — наши же параллельные
#                     подключения одной sessions-строки с разных IP/прокси.
_BAN_ERRORS = {
    "UserDeactivatedBanError", "UserDeactivatedError", "PhoneNumberBannedError",
}
_SESSION_REVOKED_ERRORS = {
    "AuthKeyUnregisteredError", "AuthKeyDuplicatedError", "SessionRevokedError",
    "SessionExpiredError", "UnauthorizedError",
}
_FLOOD_ERRORS = {"FloodWaitError", "FloodError", "SlowModeWaitError"}
_SPAM_ERRORS = {"PeerFloodError"}
_BLOCKED_ERRORS = {"UserIsBlockedError"}
_SKIP_ERRORS = {
    "UserPrivacyRestrictedError", "UsernameNotOccupiedError", "UsernameInvalidError",
    "PeerIdInvalidError", "UserIdInvalidError",
}


def classify_error(exc: BaseException) -> str:
    """Категория ошибки: 'ban' | 'session_revoked' | 'flood' | 'spam' | 'blocked' | 'skip' | 'other'.
    'ban'            — НОМЕР мёртв/деактивирован Telegram'ом. Необратимо, статус banned навсегда.
    'session_revoked'— жива только СЕССИЯ отозвана (конфликт подключений, устарел вход) —
                        номер жив, лечится перелогином (🔌 Подключить), НЕ статус banned.
    'flood'  — временный лимит (подождать/вывести из ротации на заход).
    'spam'   — PeerFlood: слишком много ЛС незнакомцам (риск бана, притормозить).
    'blocked'— контакт заблокировал НАШ аккаунт (не «потерян» вообще — просто
               этот отправитель ему больше не пишет; отличать от прочих skip,
               чтобы в CRM было видно причину, а не общее «Потерян»).
    'skip'   — прочая проблема контакта (приватность/нет такого), аккаунт ни при чём.
    """
    name = type(exc).__name__
    msg = str(exc).lower()
    if name in _BAN_ERRORS or "banned" in msg or "deactivated" in msg:
        return "ban"
    if name in _SESSION_REVOKED_ERRORS or "auth key" in msg or "unauthorized" in msg:
        return "session_revoked"
    if name in _FLOOD_ERRORS or "flood" in msg:
        return "flood"
    if name in _SPAM_ERRORS or "too many requests" in msg:
        return "spam"
    if name in _BLOCKED_ERRORS or "blocked" in msg:
        return "blocked"
    if name in _SKIP_ERRORS:
        return "skip"
    return "other"


def is_ban(exc: BaseException) -> bool:
    """True, если НОМЕР мёртв/забанен/деактивирован Telegram'ом (не просто сессия)."""
    return classify_error(exc) == "ban"


def is_session_revoked(exc: BaseException) -> bool:
    """True, если отозвана именно СЕССИЯ (лечится перелогином, номер жив)."""
    return classify_error(exc) == "session_revoked"


def phone_country(phone: str | None) -> tuple[str | None, str | None]:
    """Номер → (ISO-код, имя страны). Нужен для проверки «прокси из страны номера»."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    for pref, code, name in _SORTED_PREFIXES:
        if digits.startswith(pref):
            return code, name
    return None, None


def active_window_ok(start_h: int = 9, end_h: int = 22, tz_offset_h: int = 3) -> bool:
    """Внутри ли дневного окна активности (по умолчанию 09:00–22:00, МСК = UTC+3).
    Антибан: живые люди не пишут пачками ночью. tz_offset_h — под страну аккаунтов."""
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=tz_offset_h)
    return start_h <= now.hour < end_h
