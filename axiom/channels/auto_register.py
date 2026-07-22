"""Авто-регистрация TG-номера: купить → дождаться SMS → зарегистрировать → упаковать.

Полный конвейер (запускается фоном, может идти до 3 мин):
1. Покупка номера через hero-sms (getNumber)
2. Отправка запроса кода Telegram (send_code_request)
3. Ожидание SMS-кода через hero-sms (poll_code)
4. Регистрация аккаунта в Telegram (sign_up/sign_in)
5. Сохранение сессии в БД
6. Подтверждение активации hero-sms (finish)
7. Покупка SOCKS5-прокси той же страны через Proxy6
8. Настройка профиля: приватность, имя, аватар (позже)

Запуск: python3 -m channels.auto_register --country 6
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import time

import config
from channels.sms_hero import (SmsHeroError, get_number, get_status,
                                poll_code, cancel, finish, countries)
from db import database
from telethon.sessions import StringSession
from telethon import TelegramClient
from telethon.errors import PhoneCodeInvalidError, PhoneCodeExpiredError
from telethon.tl.functions.account import SetPrivacyRequest
from telethon.tl.types import InputPrivacyKeyPhoneNumber, InputPrivacyValueAllowAll

# Имена для свежих аккаунтов (чередуем)
FIRST_NAMES = ["Алексей", "Дмитрий", "Максим", "Сергей", "Антон",
               "Елена", "Ольга", "Анна", "Наталья", "Ирина",
               "Артём", "Павел", "Роман", "Денис", "Кирилл",
               "Екатерина", "Мария", "Светлана", "Татьяна", "Юлия"]


async def _register_number(country: int, proxy_period: int = 7,
                           proxy_version: int = 4) -> dict:
    """Полный цикл: купить номер → SMS → регистрация → прокси.
    Возвращает dict с результатом."""
    import phone_geo
    from channels.sms_hero import country_label

    result = {"ok": False, "steps": []}

    def _log(step: str, msg: str):
        print(f"[{step}] {msg}")
        result["steps"].append({"step": step, "msg": msg})

    # --- Шаг 1: Купить номер через hero-sms ---
    _log("buy", "Покупаю номер...")
    activation_id, phone = get_number(country)
    phone_clean = phone.lstrip("+")
    phone_full = f"+{phone_clean}"
    country_name = country_label(country)
    _log("buy", f"Номер {phone_full} куплен (активация {activation_id})")

    user_label = f"{country_name} #{phone_clean[-4:]}"
    result["phone"] = phone_full
    result["activation_id"] = activation_id

    # --- Шаг 2: Отправить запрос кода Telegram ---
    _log("code_request", "Запрашиваю код у Telegram...")
    client = TelegramClient(StringSession(), config.TG_API_ID, config.TG_API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone_full)
        hash_code = sent.phone_code_hash
        _log("code_request", "Код отправлен Telegram на номер")
    except Exception as e:
        _log("code_request", f"Ошибка: {e}")
        cancel(activation_id)
        await client.disconnect()
        return result

    # --- Шаг 3: Ждать SMS-код от hero-sms ---
    _log("sms_wait", "Ожидаю SMS-код от hero-sms...")
    code = await poll_code(activation_id, timeout=120, interval=3)
    if not code:
        _log("sms_wait", "Код не пришёл за 2 мин — отмена")
        cancel(activation_id)
        await client.disconnect()
        return result
    _log("sms_wait", f"Получен код: {code}")

    # --- Шаг 4: Зарегистрировать аккаунт в Telegram ---
    name = random.choice(FIRST_NAMES)
    _log("register", f"Регистрирую как «{name}»...")
    try:
        me = await client.sign_up(code=code, first_name=name, last_name="")
        _log("register", f"Зарегистрирован: @{me.username or me.id}")
    except Exception as e:
        err_str = str(e)
        # Может быть уже зарегистрирован (попробуем sign_in)
        if "PHONE_NUMBER_OCCUPIED" in err_str or "AUTH_KEY_UNREGISTERED" not in err_str:
            try:
                await client.sign_in(phone=phone_full, code=code,
                                     phone_code_hash=hash_code)
                me = await client.get_me()
                _log("register", f"Вошёл (уже был зарегистрирован): @{me.username or me.id}")
            except Exception as e2:
                _log("register", f"Ошибка входа: {e2}")
                cancel(activation_id)
                await client.disconnect()
                return result
        else:
            _log("register", f"Ошибка регистрации: {err_str}")
            cancel(activation_id)
            await client.disconnect()
            return result

    session_str = client.session.save()
    await client.disconnect()

    # --- Шаг 5: Сохранить сессию + подтвердить hero-sms ---
    _log("save", "Сохраняю сессию в БД...")
    phone_iso2 = phone_geo.detect(phone_full)
    database.init_db()
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO accounts (label, phone, country, kind, status, daily_limit, "
            "notes, bought_at, tg_session) "
            "VALUES (?, ?, ?, 'bought', 'warming', 10, ?, datetime('now'), ?)",
            (user_label, phone_full, phone_iso2 or str(country),
             f"Авто-регистрация, активация {activation_id}",
             session_str),
        )
        acc_id = cur.lastrowid
        conn.execute(
            "UPDATE accounts SET tg_session=?, session_alive=1, username=? WHERE id=?",
            (session_str, str(me.username or ""), acc_id),
        )
    result["account_id"] = acc_id
    _log("save", f"Аккаунт #{acc_id} сохранён")

    # Подтверждаем hero-sms (деньги списаны окончательно)
    try:
        finish(activation_id)
        _log("save", "Активация hero-sms подтверждена")
    except Exception as e:
        _log("save", f"Ошибка подтверждения hero-sms: {e}")

    # --- Шаг 6: Прокси — Proxy6 (платно) той же страны, иначе бесплатный MTProto ---
    proxy_url = None
    if proxy_period and phone_iso2 and config.PROXY6_API_KEY:
        try:
            from channels.proxy6 import buy as p6_buy, to_socks_url, Proxy6Error
            p6_list = p6_buy(country=phone_iso2, count=1,
                             period=proxy_period, version=proxy_version)
            if p6_list:
                proxy_url = to_socks_url(p6_list[0])
                _log("proxy", f"Прокси Proxy6 куплен: {proxy_url}")
        except Proxy6Error as e:
            _log("proxy", f"Proxy6 не куплен: {e}")
    if not proxy_url:
        try:
            from channels.proxy_pool import pick_free_mt
            proxy_url = pick_free_mt()
            if proxy_url:
                _log("proxy", "Назначен бесплатный MTProto из пула")
        except Exception as e:
            _log("proxy", f"Пул MTProto недоступен: {e}")
    if proxy_url:
        with database.get_conn() as conn:
            conn.execute("UPDATE accounts SET proxy=?, proxy_alive=1 WHERE id=?",
                         (proxy_url, acc_id))
        result["proxy"] = proxy_url

    # --- Шаг 7: Спрятать номер (приватность) ---
    try:
        client2 = TelegramClient(StringSession(session_str),
                                  config.TG_API_ID, config.TG_API_HASH)
        await client2.connect()
        await client2(SetPrivacyRequest(
            key=InputPrivacyKeyPhoneNumber(),
            rules=[InputPrivacyValueAllowAll()]  # кто может видеть номер — никто
        ))
        await client2.disconnect()
        _log("privacy", "Номер скрыт (приватность)")
    except Exception as e:
        _log("privacy", f"Приватность: {e}")

    result["ok"] = True
    return result


async def register_batch(country: int, qty: int = 1,
                         proxy_period: int = 7, proxy_version: int = 4,
                         parallel: bool = False) -> list[dict]:
    """Зарегистрировать batch номеров."""
    results = []
    if parallel:
        coros = [_register_number(country, proxy_period, proxy_version)
                 for _ in range(qty)]
        results = await asyncio.gather(*coros)
    else:
        for i in range(qty):
            res = await _register_number(country, proxy_period, proxy_version)
            results.append(res)
            if i < qty - 1:
                await asyncio.sleep(5)  # пауза между
    return results


def main():
    """CLI: python3 -m channels.auto_register --country 6 --qty 1"""
    import argparse
    p = argparse.ArgumentParser(description="Авто-регистрация TG-аккаунтов")
    p.add_argument("--country", type=int, required=True, help="Код страны hero-sms")
    p.add_argument("--qty", type=int, default=1, help="Сколько номеров")
    p.add_argument("--proxy-period", type=int, default=7, help="Дней прокси (0=без)")
    p.add_argument("--proxy-version", type=int, default=4, help="4=IPv4, 3=Shared")
    args = p.parse_args()

    result = asyncio.run(register_batch(
        args.country, args.qty, args.proxy_period, args.proxy_version
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
