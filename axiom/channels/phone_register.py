"""Покупка номера через hero-sms + создание аккаунта + привязка Proxy6.

Финансовая граница:
- ДЕНЬГИ ТРАТИТ get_number() (hero-sms) за номер
- ДЕНЬГИ ТРАТИТ proxy6.buy() за прокси
- Всё остальное (balance, countries) — read-only

После покупки:
1. Покупается номер через hero-sms
2. Определяется страна по коду номера
3. Покупается SOCKS5-прокси той же страны через Proxy6 (на N дней)
4. Создаётся аккаунт в accounts (status=warming) с привязанным прокси

Если Proxy6 ключ не задан — прокси не покупается, аккаунт создаётся без прокси.
"""
from __future__ import annotations

import config
from channels.sms_hero import SmsHeroError, get_number, cancel, finish
from db import database


def _iso2_to_proxy6_country(iso2: str | None) -> str | None:
    """ISO2 → код страны для Proxy6 (API Proxy6 понимает ISO2 как есть)."""
    if not iso2 or len(iso2) != 2:
        return None
    return iso2.lower()


def buy_and_save(country: int, qty: int = 1, label: str = "",
                 proxy_period: int = 0, proxy_version: int = 4) -> list[dict]:
    """Купить N номеров в стране, создать аккаунты в БД, привязать прокси.

    proxy_period=0 — не покупать прокси (только номер).
    proxy_period>0 — купить прокси на N дней через Proxy6 той же страны.

    Возвращает список созданных аккаунтов: [{id, phone, activation_id}].
    ТРАТИТ ДЕНЬГИ: get_number() × qty + proxy6.buy() × qty.
    """
    import phone_geo
    from channels.sms_hero import COUNTRY_RU

    if not config.HERO_SMS_API_KEY:
        raise SmsHeroError("HERO_SMS_API_KEY не задан в .env — заведи ключ в кабинете hero-sms.com")

    country_name = COUNTRY_RU.get(country, f"страна {country}")
    use_proxy = proxy_period > 0 and bool(config.PROXY6_API_KEY)
    created = []

    database.init_db()
    with database.get_conn() as conn:
        for i in range(qty):
            # Шаг 1: купить номер
            activation_id, phone = get_number(country)
            phone_clean = phone.lstrip("+")

            # Шаг 2: определить страну номера
            phone_iso2 = phone_geo.detect(f"+{phone_clean}")

            # Шаг 3: купить прокси той же страны (если нужно и есть ключ)
            proxy_url = None
            proxy_bought = False
            if use_proxy and phone_iso2:
                try:
                    from channels.proxy6 import Proxy6Error, buy as p6_buy, to_socks_url
                    p6_list = p6_buy(country=phone_iso2, count=1,
                                     period=proxy_period, version=proxy_version)
                    if p6_list:
                        p = p6_list[0]
                        proxy_url = to_socks_url(p)
                        proxy_bought = True
                except Proxy6Error as e:
                    # Прокси не купился — аккаунт создаём без прокси, не роняем всю пачку
                    proxy_url = None

            # Шаг 4: создать аккаунт
            notes = f"Куплен через hero-sms, активация {activation_id}"
            if proxy_bought:
                notes += f" + Proxy6 ({phone_iso2}) на {proxy_period} дн"

            cur = conn.execute(
                "INSERT INTO accounts (label, phone, country, kind, status, daily_limit, proxy, notes, bought_at) "
                "VALUES (?, ?, ?, 'bought', 'warming', 10, ?, ?, datetime('now'))",
                (f"{label or country_name} #{phone_clean[-4:]}",
                 f"+{phone_clean}",
                 phone_iso2 or str(country),
                 proxy_url,
                 notes),
            )
            created.append({
                "id": cur.lastrowid,
                "phone": f"+{phone_clean}",
                "activation_id": activation_id,
                "proxy": proxy_url,
                "country": phone_iso2,
            })

    return created


def cancel_activation(activation_id: str) -> None:
    """Отменить активацию — вернуть деньги (если код ещё не пришёл)."""
    cancel(activation_id)


def confirm_activation(activation_id: str) -> None:
    """Подтвердить успешную активацию."""
    finish(activation_id)
