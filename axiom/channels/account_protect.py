"""Защита купленных аккаунтов от реклейма продавцом — 2 слоя (recovery-почта НЕ
входит, см. низ файла).

СЛОЙ 1 — 2FA-пароль (переиспользуем channels/twofa.set_one). Продавец шлёт себе SMS-код,
жмёт «выйти со всех устройств» — теперь у него спросят пароль, которого он не знает.
Работает СРАЗУ после покупки.

СЛОЙ 2 — смена номера аккаунта на свой (снятый через hero-sms). Единственное, что
ПОЛНОСТЬЮ отвязывает продавца: после смены его SIM для входа не годится вообще —
недоступен даже официальный 7-дневный сброс пароля Telegram (тот требует владения
номером, который аккаунту больше не принадлежит). Не работает сразу после свежего
логина — Telegram отвечает FreshChangePhoneForbiddenError, официальный текст ошибки:
«Recently logged-in users cannot use this request... wait for a few hours»
(https://translations.telegram.org/zh-hans/android/login/FreshChangePhoneForbidden).
Берём запас (PHONE_CHANGE_MIN_AGE_HOURS) вместо угадывания точного порога — лишний
час ожидания дешевле, чем сожжённая аренда номера на Fresh-отказе.

Порядок важен: слой 2 пробуем ТОЛЬКО если слой 1 уже стоит — если смена номера
затянется (fresh-лок, ретраи), аккаунт всё это время защищён хотя бы паролем.

Recovery-почта НЕ автоматизирована. Telegram присылает код подтверждения В САМО
письмо — чтобы ввести его автоматически, нужен IMAP-доступ к почтовому ящику,
которого в проекте пока нет (.env). Как достроить: держать реальный ящик, читать
код через imaplib по теме письма, передать в edit_2fa(email=..., email_code_callback=...)
(Telethon это уже поддерживает — проверено). Отдельная задача, здесь не делаем.

Запуск:
    python -m channels.account_protect --dry                      # кто под защиту, без денег
    python -m channels.account_protect --ids 9,10                 # только 2FA (без --country)
    python -m channels.account_protect --ids 9,10 --country 36    # 2FA + смена номера (36=Канада)
    python -m channels.account_protect --country 36               # все подходящие
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json

from telethon.errors import FreshChangePhoneForbiddenError
from telethon.sessions import StringSession
from telethon.tl.functions.account import ChangePhoneRequest, SendChangePhoneCodeRequest
from telethon.tl.types import CodeSettings

from channels import sms_hero
from channels.telegram import build_client
from channels.twofa import set_one as twofa_set_one
from db import database

PHONE_CHANGE_MIN_AGE_HOURS = 24     # см. шапку файла — официально «несколько часов», берём с запасом
RETRY_BACKOFF_HOURS = 12            # после Fresh-отказа не пробуем чаще этого (не жечь деньги на ретраях)
_PARALLEL = 3                       # смена номера — тяжёлая операция (сеть + hero-sms), не гнать пачкой


def _targets(ids: list[int] | None) -> list[dict]:
    where = ("session_alive=1 AND tg_session IS NOT NULL AND tg_session<>'' "
             "AND COALESCE(protected,0)=0")
    with database.get_conn() as conn:
        if ids:
            qm = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT * FROM accounts WHERE id IN ({qm}) AND {where}", ids).fetchall()
        else:
            rows = conn.execute(f"SELECT * FROM accounts WHERE {where} ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _hours_since(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("T", " ").split(".")[0])
    except (ValueError, TypeError):
        return None
    # dt naive (из SQLite datetime('now'), без tzinfo) — now() тоже наивный, той же зоны (UTC).
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    return (now - dt).total_seconds() / 3600


def _phone_change_due(acc: dict) -> bool:
    """Слой 2 включаем, только когда: 2FA уже стоит (слой 1 важнее и должен встать первым),
    номер ещё не сменён, аккаунт не совсем свежий (см. PHONE_CHANGE_MIN_AGE_HOURS), и не
    долбим Telegram/hero-sms чаще backoff-паузы после предыдущего отказа."""
    if acc.get("protect_stage") == "phone_ok":
        return False
    if not (acc.get("tg_2fa") or "").strip():
        return False
    age = _hours_since(acc.get("bought_at")) if acc.get("bought_at") else None
    if age is not None and age < PHONE_CHANGE_MIN_AGE_HOURS:
        return False
    last_try = _hours_since(acc.get("protect_last_try_at"))
    if last_try is not None and last_try < RETRY_BACKOFF_HOURS:
        return False
    return True


async def change_phone(acc: dict, country: int) -> tuple[bool, str]:
    """Слой 2: снимает номер у hero-sms, переключает на него аккаунт. Отмена активации
    (деньги возвращаются) при fresh-локе Telegram или если код не пришёл."""
    aid = acc["id"]
    try:
        client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                              acc.get("api_id"), acc.get("api_hash"))
    except Exception as e:  # noqa: BLE001
        return False, f"клиент не собрался: {str(e)[:60]}"
    activation_id = None
    try:
        await asyncio.wait_for(client.connect(), timeout=20)
        if not await client.is_user_authorized():
            return False, "сессия не авторизована — защищать нечего"

        activation_id, new_phone = sms_hero.get_number(country, "tg")
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE accounts SET protect_phone_activation_id=?, protect_last_try_at=datetime('now') "
                "WHERE id=?", (activation_id, aid))

        try:
            sent = await client(SendChangePhoneCodeRequest(
                phone_number=new_phone, settings=CodeSettings()))
        except FreshChangePhoneForbiddenError:
            sms_hero.cancel(activation_id)   # SMS ещё не уходил — отмена полностью возвращает деньги
            with database.get_conn() as conn:
                conn.execute(
                    "UPDATE accounts SET protect_stage='phone_pending', "
                    "protect_note='Telegram: fresh-лок, попробуем позже' WHERE id=?", (aid,))
            return False, "fresh-лок (аккаунт ещё «свежий» для Telegram) — отложено, номер отменён"

        code = await sms_hero.poll_code(activation_id, timeout=180)
        if not code:
            sms_hero.cancel(activation_id)
            with database.get_conn() as conn:
                conn.execute(
                    "UPDATE accounts SET protect_stage='phone_pending', "
                    "protect_note='код на новый номер не пришёл за 180с' WHERE id=?", (aid,))
            return False, "код не пришёл за 180с — отменено, деньги возвращены"

        await client(ChangePhoneRequest(
            phone_number=new_phone, phone_code_hash=sent.phone_code_hash, phone_code=code))
        sms_hero.finish(activation_id)
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE accounts SET phone=?, protect_stage='phone_ok', "
                "protect_note='номер сменён, продавец отвязан', protect_phone_activation_id=NULL "
                "WHERE id=?", (new_phone, aid))
            database.add_event(
                conn, "account_protected",
                f"🔐 «{acc.get('label') or aid}»: номер сменён на свой",
                f"Старый номер продавца больше не привязан к аккаунту (новый: {new_phone}).",
                level="good", account_id=aid)
        return True, f"номер сменён на {new_phone}"
    except Exception as e:  # noqa: BLE001
        if activation_id:
            try:
                sms_hero.cancel(activation_id)
            except Exception:  # noqa: BLE001
                pass
        with database.get_conn() as conn:
            conn.execute("UPDATE accounts SET protect_note=? WHERE id=?", (str(e)[:200], aid))
        return False, f"ошибка: {str(e)[:100]}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def protect_one(acc: dict, country: int | None) -> dict:
    out = {"id": acc["id"], "label": acc.get("label"), "twofa": None, "phone": None}
    if not (acc.get("tg_2fa") or "").strip():
        ok, msg = await twofa_set_one(acc)
        out["twofa"] = {"ok": ok, "msg": msg}
        if ok:
            acc["tg_2fa"] = msg   # чтобы _phone_change_due увидел свежий пароль в этом же проходе
    else:
        out["twofa"] = {"ok": True, "msg": "уже стоит"}

    if country is not None and _phone_change_due(acc):
        ok, msg = await change_phone(acc, country)
        out["phone"] = {"ok": ok, "msg": msg}
    return out


async def run(ids: list[int] | None, country: int | None, dry: bool = False) -> None:
    database.init_db()
    accs = _targets(ids)
    if not accs:
        print(json.dumps({"ok": False, "error": "некого защищать: нет живых боевых аккаунтов"},
                         ensure_ascii=False))
        return
    if dry:
        for a in accs:
            has_2fa = bool((a.get("tg_2fa") or "").strip())
            due = _phone_change_due(a)
            phone_plan = ("страна не задана" if country is None
                          else ("сменим номер" if due else "не сейчас (возраст/backoff/уже сменён)"))
            print(f"[dry] #{a['id']} {a.get('label') or ''}: "
                  f"2FA={'есть' if has_2fa else 'ПОСТАВИМ'} · {phone_plan}")
        print(json.dumps({"ok": True, "dry": True, "candidates": len(accs)}, ensure_ascii=False))
        return

    sem = asyncio.Semaphore(_PARALLEL)
    results: list[dict] = []

    async def _one(a: dict) -> None:
        async with sem:
            r = await protect_one(a, country)
        results.append(r)
        t, p = r["twofa"], r["phone"]
        line = f"[#{a['id']}] {a.get('label') or ''}: 2FA {'✓' if t and t['ok'] else '✗ ' + (t['msg'] if t else '')}"
        if p:
            line += f" · номер {'✓ ' + p['msg'] if p['ok'] else '— ' + p['msg']}"
        print(line)

    await asyncio.gather(*[_one(a) for a in accs])
    print(json.dumps({"ok": True, "processed": len(results)}, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: защита купленных аккаунтов (2FA + смена номера)")
    p.add_argument("--ids", help="через запятую id аккаунтов (по умолчанию — все подходящие)")
    p.add_argument("--country", type=int,
                   help="код страны hero-sms для нового номера (см. sms_hero.countries) — "
                        "без него меняем только 2FA, номер не трогаем")
    p.add_argument("--dry", action="store_true", help="показать кандидатов, ничего не менять")
    args = p.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    asyncio.run(run(ids, args.country, args.dry))


if __name__ == "__main__":
    main()
