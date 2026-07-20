"""Тонкий клиент API hero-sms.com — SMS-активация для саморегистрации TG-номеров.

Зачем: перестать зависеть от покупных аккаунтов, которые дохнут лотами (см. память
lzt-arbitrage-luna). Номер и 2FA — под нашим контролем с нулевой секунды. Полное ТЗ:
axiom/ТЗ_саморегистрация_TG_hero-sms.md.

Протокол — SMS-Activate handler_api (hero-sms совместим). Часть ответов — сырой текст
(`ACCESS_BALANCE:0.93`), часть — JSON (`getPrices`). Ключ в .env → HERO_SMS_API_KEY,
в логи/ответы НЕ печатаем.

⚠️ ГРАНИЦА ДЕНЕГ: платит и резервирует номер только get_number(). Всё остальное
(balance/prices) — read-only, денег не тратит. get_number() вызывать лишь на реальном
шаге регистрации по явному действию пользователя, НИКОГДА в тестах/проверках.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request

import config

BASE = "https://hero-sms.com/stubs/handler_api.php"
SERVICE_TG = "tg"

# Коды стран SMS-Activate (подмножество популярных + те, что реально в наличии у tg).
# 0 = Россия (сейчас 0 номеров для tg). Незнакомый код → «страна N».
COUNTRY_RU: dict[int, str] = {
    0: "Россия", 1: "Украина", 2: "Казахстан", 3: "Китай", 4: "Филиппины",
    6: "Индонезия", 7: "Малайзия", 10: "Вьетнам", 11: "Кыргызстан", 12: "США (вирт.)",
    13: "Израиль", 15: "Польша", 16: "Англия", 22: "Индия", 31: "ЮАР", 32: "Румыния",
    33: "Колумбия", 34: "Эстония", 36: "Канада", 43: "Германия", 44: "Литва",
    46: "Швеция", 48: "Нидерланды", 52: "Таиланд", 54: "Аргентина", 56: "Испания",
    73: "Бразилия", 78: "Франция", 82: "Бельгия", 187: "США (реал.)",
}

# Текстовые коды ошибок SMS-Activate → человеку. Всё, что начинается на эти токены,
# считаем ошибкой, а не данными.
_ERRORS = {
    "BAD_KEY": "неверный HERO_SMS_API_KEY",
    "ERROR_SQL": "внутренняя ошибка сервиса (ERROR_SQL)",
    "NO_BALANCE": "недостаточно баланса на hero-sms",
    "NO_NUMBERS": "нет свободных номеров для этой страны",
    "BAD_ACTION": "неизвестный метод API",
    "BAD_SERVICE": "неизвестный сервис",
    "BANNED": "аккаунт hero-sms заблокирован",
    "WRONG_MAX_PRICE": "неверная максимальная цена",
}


class SmsHeroError(RuntimeError):
    pass


def country_label(code: int | str) -> str:
    try:
        code = int(code)
    except (ValueError, TypeError):
        return str(code)
    return COUNTRY_RU.get(code, f"страна {code}")


def _get(action: str, **params) -> str:
    """Сырой запрос к handler_api. Возвращает текст ответа. Ключ подставляем сами;
    в исключения его не пускаем."""
    key = (config.HERO_SMS_API_KEY or "").strip()
    if not key:
        raise SmsHeroError("нет HERO_SMS_API_KEY в .env — получи ключ в личном кабинете hero-sms.com")
    q = {"api_key": key, "action": action}
    q.update({k: v for k, v in params.items() if v is not None})
    url = f"{BASE}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": "AXIOM/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            text = resp.read().decode("utf-8", "replace").strip()
    except urllib.error.URLError as e:
        raise SmsHeroError(f"нет связи с hero-sms: {e}") from e
    head = text.split(":", 1)[0]
    if head in _ERRORS:
        raise SmsHeroError(_ERRORS[head])
    return text


def balance() -> float:
    """Остаток на счёте hero-sms. Read-only. `ACCESS_BALANCE:0.93` → 0.93."""
    text = _get("getBalance")
    if not text.startswith("ACCESS_BALANCE"):
        raise SmsHeroError(f"неожиданный ответ getBalance: {text[:60]}")
    try:
        return float(text.split(":", 1)[1])
    except (IndexError, ValueError) as e:
        raise SmsHeroError(f"не разобрал баланс: {text[:60]}") from e


def prices(service: str = SERVICE_TG, country: int | None = None) -> dict:
    """Цены/наличие. Read-only. Ответ getPrices — JSON {страна: {сервис: {...}}}."""
    text = _get("getPrices", service=service, country=country)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise SmsHeroError(f"getPrices вернул не-JSON: {text[:80]}") from e


def countries(service: str = SERVICE_TG) -> list[dict]:
    """Плоский список стран, где ЕСТЬ номера сервиса, для выпадашки в UI:
    [{code, label, cost, count}], отсортирован по наличию (сначала где есть номера)."""
    raw = prices(service)
    out = []
    for code, svc in (raw or {}).items():
        info = (svc or {}).get(service) or {}
        cost = info.get("cost")
        count = info.get("count") or info.get("physicalCount") or 0
        if cost is None:
            continue
        try:
            code_i = int(code)
        except (ValueError, TypeError):
            continue
        out.append({"code": code_i, "label": country_label(code_i),
                    "cost": float(cost), "count": int(count)})
    out.sort(key=lambda x: (x["count"] == 0, x["cost"]))   # сначала где есть, потом дешевле
    return out


# --- ПЛАТНЫЕ методы (деньги!) — не вызывать в read-only/тестах -------------- #

def get_number(country: int, service: str = SERVICE_TG) -> tuple[str, str]:
    """⚠️ ПЛАТНО: арендует номер и резервирует его. `ACCESS_NUMBER:{id}:{phone}`."""
    text = _get("getNumber", service=service, country=country)
    if not text.startswith("ACCESS_NUMBER"):
        raise SmsHeroError(f"getNumber: {text[:60]}")
    parts = text.split(":")
    if len(parts) < 3:
        raise SmsHeroError(f"getNumber: не разобрал ответ {text[:60]}")
    return parts[1], parts[2]     # (activation_id, phone)


def get_status(activation_id: str) -> str:
    return _get("getStatus", id=activation_id)


async def poll_code(activation_id: str, timeout: int = 180, interval: float = 5.0) -> str | None:
    """Опрашивает getStatus, пока не придёт код или не выйдет время. `STATUS_OK:1234` → '1234'.
    Отмена активации ('STATUS_CANCEL') или таймаут → None (деньги за номер НЕ списаны,
    отменять/подтверждать — обязанность вызывающего кода)."""
    import time
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        status = get_status(activation_id)
        if status.startswith("STATUS_OK"):
            return status.split(":", 1)[1]
        if status.startswith("STATUS_CANCEL"):
            return None
        await asyncio.sleep(interval)
    return None


def cancel(activation_id: str) -> None:
    """Отменить активацию (деньги возвращаются, если код не пришёл). status=8."""
    _get("setStatus", id=activation_id, status=8)


def finish(activation_id: str) -> None:
    """Подтвердить успешную активацию. status=6."""
    _get("setStatus", id=activation_id, status=6)
