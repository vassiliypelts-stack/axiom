"""Веб-логин аккаунта в Telegram — 2 шага: запросить код → ввести код.

В отличие от CLI (`account_login.py`), здесь логин делается из пульта без консоли:
1) start_login — Telegram присылает код (в приложение/SMS), временная сессия
   сохраняется в памяти процесса пульта;
2) submit_code — вводим код (и пароль 2FA, если включён) → сессия пишется в БД.

Состояние между двумя HTTP-запросами держим в _PENDING (память процесса). Этого
достаточно для single-process uvicorn. Сессия (полный доступ) ложится в БД —
секрет, БД не публикуй.

КУДА УХОДИТ КОД (10.11.2026, по следам #5 Василий988). Telegram сам выбирает
способ доставки и возвращает его в sent.type. По умолчанию это `app` — код
летит в САМО приложение Telegram, на другое активное устройство аккаунта. Если
единственная сессия сгорела (AuthKeyDuplicated), активных устройств нет,
доставлять некуда — код уходит в никуда, а пульт раньше рапортовал «ok» и
человек ждал SMS, которой не будет.

Поэтому: start_login возвращает `sent_to` (куда ушёл) и `can_sms` (можно ли
попросить SMS), а resend_sms() повторяет запрос с force_sms=True. force_sms
работает ТОЛЬКО как повторная отправка — поверх уже выданного phone_code_hash,
на свежей сессии Telegram ответит SendCodeUnavailableError. Поэтому resend
переиспользует сессию и хэш из _PENDING, а не поднимает новых.
"""
from __future__ import annotations

import asyncio

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

# acc_id -> {"client": TelegramClient, "qr": QRLogin, "proxy": str|None}
# Здесь, в отличие от _PENDING, держим ЖИВОЙ клиент: токен QR действителен только
# внутри той сессии, где он выдан, пересоздать её между запросами нельзя.
_PENDING_QR: dict[int, dict] = {}


def _describe_sent(sent) -> tuple[str, bool]:
    """По ответу Telegram понять, КУДА ушёл код и можно ли попросить SMS.

    Возвращает (человеческое описание, можно_ли_sms). Класс типа смотрим по
    имени, а не через isinstance: набор SentCodeType* разнится по версиям
    Telethon, и неизвестный тип не должен ронять логин."""
    name = type(getattr(sent, "type", None)).__name__
    nxt = type(getattr(sent, "next_type", None)).__name__
    can_sms = "Sms" in nxt
    if "App" in name:
        return ("в приложение Telegram (на другое устройство этого аккаунта)", can_sms)
    if "Sms" in name:
        return ("SMS-кой на номер", False)
    if "Call" in name or "Flash" in name:
        return ("звонком на номер", can_sms)
    if "Email" in name:
        return ("на привязанную почту", can_sms)
    if "Missed" in name:
        return ("звонком-сбросом (код — последние цифры номера)", can_sms)
    return (f"способом {name or 'неизвестно'}", can_sms)


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
    client = build_client(StringSession(), acc["proxy"], allow_shared_ip=True)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        _PENDING[acc_id] = {
            "session": client.session.save(),
            "hash": sent.phone_code_hash,
            "phone": phone,
            "proxy": acc["proxy"],
        }
        sent_to, can_sms = _describe_sent(sent)
        return {"ok": True, "phone": phone, "sent_to": sent_to, "can_sms": can_sms}
    except Exception as e:  # noqa: BLE001
        return {"error": f"не удалось отправить код: {e}"}
    finally:
        await client.disconnect()


async def resend_sms(acc_id: int) -> dict:
    """Повторная отправка кода — «Прислать по SMS».

    Нужна, когда первый код ушёл в приложение Telegram, а живых устройств у
    аккаунта нет (сгоревшая сессия): ждать там нечего. Telegram разрешает
    пересылку только поверх выданного phone_code_hash, поэтому работаем на
    сохранённой сессии из _PENDING — новый клиент получил бы отказ."""
    st = _PENDING.get(acc_id)
    if not st:
        return {"error": "сначала запроси код (шаг 1)"}
    client = build_client(StringSession(st["session"]), st["proxy"], allow_shared_ip=True)
    try:
        await client.connect()
        sent = await client.send_code_request(st["phone"], force_sms=True)
        # хэш после пересылки меняется — иначе следующий sign_in отдаст
        # PhoneCodeInvalid на верный код из SMS
        st["hash"] = sent.phone_code_hash
        st["session"] = client.session.save()
        sent_to, can_sms = _describe_sent(sent)
        return {"ok": True, "phone": st["phone"], "sent_to": sent_to, "can_sms": can_sms}
    except Exception as e:  # noqa: BLE001
        return {"error": f"не удалось отправить SMS: {e}"}
    finally:
        await client.disconnect()


async def qr_start(acc_id: int) -> dict:
    """Вход по QR — запасной путь, когда код подтверждения получить нельзя.

    Нужен там, где SMS не предлагается вовсе: Telegram отдаёт SentCodeTypeApp с
    next_type=None (так у #5 Василий988 10.11.2026), то есть считает, что у
    аккаунта есть активное устройство, и шлёт код только туда. Если добраться до
    того устройства нельзя, обычный вход заперт — а QR сканируется из Telegram
    на телефоне и кода не требует.

    Ссылку отдаём строкой tg://login?token=… : рисовать картинку на сервере
    незачем, в пульте её покажет JS-библиотека. Сессию держим в _PENDING_QR —
    ждать подтверждения будет qr_wait()."""
    if not config.TG_API_ID or not config.TG_API_HASH:
        return {"error": "Заполни TG_API_ID и TG_API_HASH в .env (my.telegram.org)"}
    database.init_db()
    with database.get_conn() as conn:
        acc = database.get_account(conn, acc_id)
    if not acc:
        return {"error": f"аккаунта #{acc_id} нет в «Мои агенты»"}
    client = build_client(StringSession(), acc["proxy"], allow_shared_ip=True)
    try:
        await client.connect()
        qr = await client.qr_login()
        _PENDING_QR[acc_id] = {"client": client, "qr": qr, "proxy": acc["proxy"]}
        return {"ok": True, "url": qr.url, "expires_in": 30}
    except Exception as e:  # noqa: BLE001
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        _PENDING_QR.pop(acc_id, None)
        return {"error": f"не удалось выдать QR: {e}"}


async def qr_wait(acc_id: int, password: str = "") -> dict:
    """Дождаться скана QR (или сообщить, что ссылка протухла).

    Токен живёт ~30 секунд, поэтому ждём короткими отрезками, а пульт по ответу
    `expired` просто просит новый QR и перерисовывает картинку — так человек
    видит живой код, а не висящую мёртвую ссылку. Клиент НЕ закрываем, пока
    ждём: закрытие убило бы сессию, в которой выдан токен."""
    st = _PENDING_QR.get(acc_id)
    if not st:
        return {"error": "сначала запроси QR"}
    client, qr = st["client"], st["qr"]
    try:
        try:
            await qr.wait(timeout=25)
        except SessionPasswordNeededError:
            if not (password or "").strip():
                return {"need_password": True}
            await client.sign_in(password=password.strip())
        except asyncio.TimeoutError:
            # Токен протух — обновляем его в той же сессии и отдаём новую ссылку.
            try:
                await qr.recreate()
                return {"expired": True, "url": qr.url, "expires_in": 30}
            except Exception as e:  # noqa: BLE001
                return {"error": f"QR истёк и не обновился: {e}"}
        me = await client.get_me()
        with database.get_conn() as conn:
            database.save_account_session(conn, acc_id, client.session.save(), me.username)
            conn.execute(
                "UPDATE accounts SET status='warming' WHERE id=? AND status NOT IN ('active','banned')",
                (acc_id,),
            )
        # Вошли — временный клиент больше не нужен. Сессия уже сохранена в БД,
        # её поднимет слушатель; держать второе подключение тем же ключом нельзя,
        # это ровно «два IP на один ключ».
        await qr_cancel(acc_id)
        return {"ok": True, "username": me.username}
    except Exception as e:  # noqa: BLE001
        await qr_cancel(acc_id)
        return {"error": str(e)}


async def qr_cancel(acc_id: int) -> dict:
    """Закрыть временный клиент QR-входа (закрыли окно, ушли со страницы)."""
    st = _PENDING_QR.pop(acc_id, None)
    if not st:
        return {"ok": True}
    try:
        await st["client"].disconnect()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


async def submit_code(acc_id: int, code: str, password: str = "") -> dict:
    """Шаг 2: ввести код (и пароль 2FA при необходимости) → сохранить сессию."""
    st = _PENDING.get(acc_id)
    if not st:
        return {"error": "сначала запроси код (шаг 1)"}
    code = (code or "").strip()
    if not code:
        return {"error": "введи код из Telegram"}
    client = build_client(StringSession(st["session"]), st["proxy"], allow_shared_ip=True)
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
