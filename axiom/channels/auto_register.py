"""Авто-регистрация TG-номера: купить → дождаться SMS → зарегистрировать → упаковать.

Полный конвейер (запускается фоном, может идти до 3 мин):
1. Покупка номера через hero-sms (getNumber)
2. Прокси СНАЧАЛА (Proxy6 той же страны, иначе бесплатный MTProto) — ключевой шаг:
   hero-sms поддержка прямо предупреждает, что SMS не доходит, если запрос кода
   идёт с IP страны, не совпадающей со страной номера (наш сервер — GCP, обычно
   не та гео, что купленный номер). Поэтому прокси нужен ДО send_code_request,
   не после регистрации, как было раньше.
3. Отправка запроса кода Telegram (send_code_request) — уже через прокси
4. Ожидание SMS-кода через hero-sms (poll_code)
5. Регистрация аккаунта в Telegram (sign_up/sign_in)
6. Сохранение сессии + прокси в БД
7. Подтверждение активации hero-sms (finish)
8. Настройка профиля: приватность (позже — имя, аватар)

Запуск: python3 -m channels.auto_register --country 6
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import time

import config
from channels.sms_hero import (SmsHeroError, get_number, poll_code, cancel, finish,
                               mark_ready)
from channels.telegram import build_client
from channels.privacy import apply_privacy
from db import database
from telethon.sessions import StringSession

# Имена для свежих аккаунтов (чередуем)
def _safe_cancel(activation_id: str) -> None:
    """cancel(), которая не роняет весь процесс.

    ЗАЧЕМ. 29.08 на живом тесте (Бразилия) send_code_request упал, код полез
    отменять активацию — а сам cancel() получил HTTP 409 Conflict от hero-sms
    (сервис уже сам перевёл активацию в другой статус) и это исключение никто не
    ловил. Процесс упал целиком: клиент Telegram остался не отключён, деньги за уже
    купленный прокси (33 руб) пропали без всякого отчёта — задача просто зависла
    с трейсбеком в логе вместо понятного "не получилось, вот почему".

    Отмена — это ЛУЧШАЯ ПОПЫТКА вернуть деньги за номер, а не критичный шаг:
    если hero-sms уже сам всё решил (409 значит именно это), настаивать незачем."""
    try:
        cancel(activation_id)
    except SmsHeroError as e:
        print(f"[cancel] не отменил активацию {activation_id} ({e}) — "
              f"похоже, hero-sms уже сам сменил её статус")


def _safe_mark_ready(activation_id: str) -> None:
    """mark_ready() уже не бросает SmsHeroError сама (см. sms_hero.py), но подстрахуем
    на случай других исключений — шаг вспомогательный, ронять регистрацию не должен."""
    try:
        mark_ready(activation_id)
    except Exception as e:  # noqa: BLE001
        print(f"[mark_ready] пропущено ({e}) — жду код без подтверждения готовности")


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

    # --- Шаг 2: Прокси ДО запроса кода — иначе SMS не дойдёт (см. docstring модуля) ---
    phone_iso2 = phone_geo.detect(phone_full)
    proxy_url = None
    if proxy_period and phone_iso2 and config.PROXY6_API_KEY:
        try:
            from channels.proxy6 import buy as p6_buy, to_socks_url, Proxy6Error
            p6_list = p6_buy(country=phone_iso2, count=1,
                             period=proxy_period, version=proxy_version)
            if p6_list:
                proxy_url = to_socks_url(p6_list[0])
                _log("proxy", f"Прокси Proxy6 ({phone_iso2}) куплен: {proxy_url}")
        except Proxy6Error as e:
            _log("proxy", f"Proxy6 не куплен: {e}")
    if not proxy_url:
        try:
            from channels.proxy_pool import pick_free_mt
            proxy_url = pick_free_mt()
            if proxy_url:
                _log("proxy", "Назначен бесплатный MTProto из пула (нет гео-совпадения — риск недоставки SMS)")
        except Exception as e:
            _log("proxy", f"Пул MTProto недоступен: {e}")
    if not proxy_url:
        _log("proxy", "Без прокси — код пойдёт с IP сервера, SMS может не дойти (см. предупреждение hero-sms)")

    # --- Шаг 3: Отправить запрос кода Telegram — через прокси, если он есть ---
    _log("code_request", "Запрашиваю код у Telegram...")
    client = build_client(StringSession(), proxy_url, allow_shared_ip=True)
    try:
        await client.connect()
        sent = await client.send_code_request(phone_full)
        hash_code = sent.phone_code_hash
        _log("code_request", "Код отправлен Telegram на номер")
    except Exception as e:
        _log("code_request", f"Ошибка: {e}")
        _safe_cancel(activation_id)
        await client.disconnect()
        return result

    # --- Шаг 4: Ждать SMS-код от hero-sms ---
    # Сначала подтверждаем готовность (status=1): по протоколу SMS-Activate активация
    # переходит в ожидание SMS только после этого шага. Мы его не делали вовсе — сразу
    # опрашивали getStatus, — и активация могла висеть «номер выдан», пока не истечёт
    # время. Отсюда «деньги списались, код не пришёл».
    _safe_mark_ready(activation_id)
    _log("sms_wait", "Ожидаю SMS-код от hero-sms (до 5 мин)...")
    # 5 минут вместо 2: в дешёвых странах SMS идёт дольше, а отмена раньше времени
    # означает потраченный номер и повторную попытку с новыми деньгами.
    code = await poll_code(activation_id, timeout=300, interval=3)
    if not code:
        _log("sms_wait", "Код не пришёл за 5 мин — отмена (деньги за номер возвращаются)")
        _safe_cancel(activation_id)
        await client.disconnect()
        return result
    _log("sms_wait", f"Получен код: {code}")

    # --- Шаг 5: Зарегистрировать аккаунт в Telegram ---
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
                _safe_cancel(activation_id)
                await client.disconnect()
                return result
        else:
            _log("register", f"Ошибка регистрации: {err_str}")
            _safe_cancel(activation_id)
            await client.disconnect()
            return result

    session_str = client.session.save()
    await client.disconnect()

    # --- Шаг 6: Сохранить сессию + прокси в БД, подтвердить hero-sms ---
    _log("save", "Сохраняю сессию в БД...")
    database.init_db()
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO accounts (label, phone, country, kind, status, daily_limit, "
            "notes, bought_at, tg_session, proxy) "
            "VALUES (?, ?, ?, 'bought', 'warming', 10, ?, datetime('now'), ?, ?)",
            (user_label, phone_full, phone_iso2 or str(country),
             f"Авто-регистрация, активация {activation_id}",
             session_str, proxy_url),
        )
        acc_id = cur.lastrowid
        conn.execute(
            "UPDATE accounts SET tg_session=?, session_alive=1, username=?, "
            "proxy_alive=? WHERE id=?",
            (session_str, str(me.username or ""), 1 if proxy_url else None, acc_id),
        )
    result["account_id"] = acc_id
    result["proxy"] = proxy_url
    _log("save", f"Аккаунт #{acc_id} сохранён")

    # Подтверждаем hero-sms (деньги списаны окончательно)
    try:
        finish(activation_id)
        _log("save", "Активация hero-sms подтверждена")
    except Exception as e:
        _log("save", f"Ошибка подтверждения hero-sms: {e}")

    # --- Шаг 7: Приватность (номер спрятан + рекомендованный набор) — тем же прокси ---
    try:
        client2 = build_client(StringSession(session_str), proxy_url, allow_shared_ip=True)
        await client2.connect()
        done = await apply_privacy(client2)
        await client2.disconnect()
        _log("privacy", f"Приватность: {', '.join(done) if done else 'не применилась'}")
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
    # Одной строкой, БЕЗ indent: web/app.py:_last_json (тот же приём, что у всех
    # остальных модулей — campaign_send, proxy_pool и т.д.) ищет строку, начинающуюся
    # с '{', и многострочный json.dumps(indent=2) под это не подходил — итоговый
    # результат регистрации не распознавался, /api/auto/status показывал "done: false"
    # даже когда процесс уже завершился и деньги были возвращены/списаны.
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
