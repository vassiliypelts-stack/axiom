"""Тонкий клиент API Proxy6.net — автопокупка/подбор резидентных прокси под страну
купленного TG-аккаунта (гео-совпадение номер↔прокси, см. phone_geo.py).

Нужен ключ в .env: PROXY6_API_KEY (личный кабинет proxy6.net → раздел «API»).
Без ключа все функции кидают Proxy6Error с понятным текстом — ничего не падает молча.

⚠️ buy() тратит реальные деньги с баланса Proxy6 — вызывать только явным действием
пользователя (кнопка), не в фоновых авто-тиках без спроса.

Документация провайдера: https://proxy6.net/ru/developers — эндпоинты ниже собраны
по её описанию; если провайдер изменит схему ответа, поправить нужно только здесь.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import config

# Официальный домен API (не proxy6.net!) — см. https://proxy6.net/ru/developers.
# Формат: https://px6.link/api/{ключ}/{метод}/?{параметры}
BASE = "https://px6.link/api"

# «Тип прокси» в терминах Proxy6 — это version (IP-версия/технология), а не протокол
# (протокол http/socks — отдельный параметр type, для нас всегда socks, см. buy()).
VERSIONS: dict[int, str] = {
    4: "IPv4 — индивидуальный (рекомендую для антибана)",
    3: "IPv4 Shared — дешевле, IP делится с другими",
    6: "IPv6 — самый дешёвый, не все сервисы его принимают",
    5: "MTProto — для Telegram MTProxy (не для SOCKS5-подключения аккаунта)",
}


class Proxy6Error(RuntimeError):
    pass


def _call(method: str, **params) -> dict:
    key = (config.PROXY6_API_KEY or "").strip()
    if not key:
        raise Proxy6Error("нет PROXY6_API_KEY в .env — получи ключ в личном кабинете proxy6.net → API")
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE}/{key}/{method}/"
    if qs:
        url += f"?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "AXIOM/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise Proxy6Error(f"нет связи с px6.link: {e}") from e
    if data.get("status") != "yes":
        raise Proxy6Error(data.get("error") or f"proxy6 вернул ошибку на {method}: {data}")
    return data


def whoami() -> dict:
    """Проверка ключа: минимальный запрос без метода (см. документацию) — возвращает
    user_id/баланс/валюту. Удобно для самопроверки сразу после вставки ключа в .env."""
    key = (config.PROXY6_API_KEY or "").strip()
    if not key:
        raise Proxy6Error("нет PROXY6_API_KEY в .env — получи ключ в личном кабинете proxy6.net → API")
    req = urllib.request.Request(f"{BASE}/{key}", headers={"User-Agent": "AXIOM/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise Proxy6Error(f"нет связи с px6.link: {e}") from e
    if data.get("status") != "yes":
        raise Proxy6Error(data.get("error") or f"px6.link вернул ошибку: {data}")
    return {"user_id": data.get("user_id"), "balance": data.get("balance"), "currency": data.get("currency")}


def available(country: str, version: int = 4) -> int:
    """Сколько прокси доступно для покупки в стране (ISO2, напр. 'ru')."""
    data = _call("getcount", country=country, version=version)
    return int(data.get("count") or 0)


def price(count: int, period: int, version: int = 4) -> dict:
    """Сколько БУДЕТ СТОИТЬ покупка — без самой покупки (проверить баланс заранее).
    Возвращает {"price": итого, "price_single": цена за штуку, "period":.., "count":..}."""
    data = _call("getprice", count=count, period=period, version=version)
    return {
        "price": float(data.get("price") or 0),
        "price_single": float(data.get("price_single") or 0),
        "period": int(data.get("period") or period),
        "count": int(data.get("count") or count),
    }


def countries(version: int = 4) -> list[str]:
    """Страны (ISO2), где есть прокси нужной версии/типа, прямо сейчас."""
    data = _call("getcountry", version=version)
    lst = data.get("list") or []
    return list(lst.values()) if isinstance(lst, dict) else list(lst)


def buy(country: str, count: int = 1, period: int = 30, version: int = 4,
       proxy_type: str = "socks") -> list[dict]:
    """Купить count прокси нужной страны на period дней (реальная трата денег).
    Возвращает список словарей прокси, как отдаёт Proxy6 (id/ip/host/port/user/pass/…)."""
    data = _call("buy", count=count, period=period, country=country,
                version=version, type=proxy_type)
    return list((data.get("list") or {}).values())


def my_list(state: str = "active") -> list[dict]:
    """Уже купленные прокси на аккаунте Proxy6 (для повторного использования без новой покупки)."""
    data = _call("getproxy", state=state)
    return list((data.get("list") or {}).values())


def to_socks_url(p: dict) -> str:
    """Прокси Proxy6 (dict) → socks5://user:pass@host:port для карточки аккаунта AXIOM."""
    host = p.get("host") or p.get("ip")
    return f"socks5://{p.get('user')}:{p.get('pass')}@{host}:{p.get('port')}"
