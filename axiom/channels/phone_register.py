"""Покупка номера через hero-sms + создание аккаунта в БД.

Финансовая граница: ДЕНЬГИ ТРАТИТ только get_number(). Всё остальное (balance,
countries) — read-only. Вызывать лишь по явному действию оператора в пульте.

После покупки номера создаётся запись в accounts (status=warming) — оператор
дальше логинится через веб-логин как обычно.
"""
from __future__ import annotations

import config
from channels.sms_hero import SmsHeroError, get_number, cancel, finish
from db import database


def buy_and_save(country: int, qty: int = 1, label: str = "") -> list[dict]:
    """Купить N номеров в стране, создать аккаунты в БД.

    Возвращает список созданных аккаунтов: [{id, phone, activation_id}].
    ТРАТИТ ДЕНЬГИ: get_number() × qty.
    """
    from channels.sms_hero import COUNTRY_RU

    if not config.HERO_SMS_API_KEY:
        raise SmsHeroError("HERO_SMS_API_KEY не задан в .env — заведи ключ в кабинете hero-sms.com")

    country_name = COUNTRY_RU.get(country, f"страна {country}")
    created = []

    database.init_db()
    with database.get_conn() as conn:
        for i in range(qty):
            activation_id, phone = get_number(country)
            # убираем + в начале если есть
            phone_clean = phone.lstrip("+")
            cur = conn.execute(
                "INSERT INTO accounts (label, phone, country, kind, status, daily_limit, notes, bought_at) "
                "VALUES (?, ?, ?, 'bought', 'warming', 10, ?, datetime('now'))",
                (f"{label or country_name} #{phone_clean[-4:]}",
                 f"+{phone_clean}",
                 str(country),
                 f"Куплен через hero-sms, активация {activation_id}"),
            )
            created.append({
                "id": cur.lastrowid,
                "phone": f"+{phone_clean}",
                "activation_id": activation_id,
            })

    return created


def cancel_activation(activation_id: str) -> None:
    """Отменить активацию — вернуть деньги (если код ещё не пришёл)."""
    cancel(activation_id)


def confirm_activation(activation_id: str) -> None:
    """Подтвердить успешную активацию."""
    finish(activation_id)
