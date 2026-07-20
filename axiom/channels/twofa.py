"""Облачный пароль (2FA) на аккаунты — защита от реклейма продавцом.

ЗАЧЕМ. У купленного на маркете аккаунта НОМЕР остаётся у продавца. Пока 2FA нет, он в
любой момент входит по SMS-коду и жмёт «завершить все другие сеансы» — наша сессия
умирает («разлогинен»), а вернуть её нечем: кода у нас нет. Ровно так мы уже потеряли
лоты +1 740…/+998 95… целиком. С 2FA одного SMS-кода для входа мало.

ЧЕСТНАЯ ГРАНИЦА. Это не броня, а замедлитель. Без recovery-email владелец номера может
запросить сброс пароля — Telegram отдаст аккаунт через 7 дней ожидания. Итог: вместо
мгновенного увода — неделя форы и заметный след. Полная защита = 2FA + recovery-email,
но email требует ввода кода из почты, т.е. живого человека (см. --email в планах).

БЕЗОПАСНОСТЬ. Пароль пишем в БД ДО установки. Иначе возможен сценарий: пароль в Telegram
поставлен, а до записи в БД что-то упало — аккаунт заперт навсегда (при следующем входе
спросят пароль, которого никто не знает). Лучше лишняя запись в БД, чем кирпич.

Родные (protected) аккаунты не трогаем — это личные номера хозяина.

Запуск:
    python -m channels.twofa --dry            # показать, кому поставили бы, ничего не делая
    python -m channels.twofa --ids 9,10       # только этим
    python -m channels.twofa                  # всем живым боевым без 2FA
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import string

from telethon.sessions import StringSession
from telethon.tl.functions.account import GetPasswordRequest

from channels.telegram import build_client
from db import database

_ALPHABET = string.ascii_letters + string.digits    # без спецсимволов: их ломают панели/копипаста
_PWD_LEN = 20
_HINT = "axiom"          # подсказка видна ЛЮБОМУ, кто открыл вход — смысла в ней нет, но пустую TG не любит
_PARALLEL = 5


def _gen_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_PWD_LEN))


def _targets(ids: list[int] | None) -> list[dict]:
    """Кому ставим: живая сессия + не родной. Аккаунт с уже известным нам 2FA пропускаем —
    он уже защищён, а трогать пароль без нужды = рисковать зря."""
    where = ("session_alive=1 AND tg_session IS NOT NULL AND tg_session<>'' "
             "AND COALESCE(protected,0)=0 AND (tg_2fa IS NULL OR tg_2fa='')")
    with database.get_conn() as conn:
        if ids:
            qm = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT id, label, phone, tg_session, proxy, api_id, api_hash, tg_2fa "
                f"FROM accounts WHERE id IN ({qm}) AND {where}", ids).fetchall()
        else:
            rows = conn.execute(
                f"SELECT id, label, phone, tg_session, proxy, api_id, api_hash, tg_2fa "
                f"FROM accounts WHERE {where} ORDER BY id").fetchall()
    return [dict(r) for r in rows]


async def _set_one(acc: dict) -> tuple[bool, str]:
    aid = acc["id"]
    try:
        client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                              acc.get("api_id"), acc.get("api_hash"))
    except Exception as e:  # noqa: BLE001
        return False, f"клиент не собрался: {str(e)[:60]}"
    try:
        await asyncio.wait_for(client.connect(), timeout=20)
        if not await client.is_user_authorized():
            return False, "сессия слетела (разлогинен) — 2FA ставить не через что"
        # Уже есть чужой пароль? Значит его знает продавец, а мы — нет: сменить не можем
        # (нужен текущий), и молча «поставить свой» тоже нельзя. Это к оператору.
        pwd_info = await client(GetPasswordRequest())
        if pwd_info.has_password:
            return False, "2FA уже стоит, но пароль не наш — сменить нечем (нужен текущий)"

        new_pwd = _gen_password()
        # СНАЧАЛА в БД — см. шапку модуля. Ставим до вызова Telegram, а не после.
        with database.get_conn() as conn:
            conn.execute("UPDATE accounts SET tg_2fa=? WHERE id=?", (new_pwd, aid))

        await client.edit_2fa(new_password=new_pwd, hint=_HINT)

        # Проверяем не «нет исключения», а что Telegram реально считает пароль установленным.
        check = await client(GetPasswordRequest())
        if not check.has_password:
            with database.get_conn() as conn:   # не встал — не врём в БД, что защищено
                conn.execute("UPDATE accounts SET tg_2fa=NULL WHERE id=?", (aid,))
            return False, "Telegram не подтвердил установку пароля"
        with database.get_conn() as conn:
            conn.execute("UPDATE accounts SET tg_2fa_set_at=datetime('now') WHERE id=?", (aid,))
        return True, new_pwd
    except Exception as e:  # noqa: BLE001
        # Пароль мог успеть встать до ошибки — НЕ чистим tg_2fa: если он в Telegram есть,
        # а мы забудем, аккаунт потерян. Лишняя запись безобидна, потерянный пароль — нет.
        return False, f"ошибка: {str(e)[:70]}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


# Публичное имя для переиспользования из account_protect.py (слой 1 защиты купленных
# аккаунтов) — _set_one исторически приватный, но логика ровно та же, дублировать вредно.
set_one = _set_one


async def run(ids: list[int] | None, dry: bool) -> None:
    database.init_db()
    accs = _targets(ids)
    if not accs:
        print(json.dumps({"ok": False, "error": "некому ставить: нет живых боевых аккаунтов без 2FA"},
                         ensure_ascii=False))
        return
    if dry:
        for a in accs:
            print(f"[dry] #{a['id']} {a.get('label') or ''} ({a.get('phone')}) — поставили бы 2FA")
        print(json.dumps({"ok": True, "dry": True, "would_set": len(accs)}, ensure_ascii=False))
        return

    sem = asyncio.Semaphore(_PARALLEL)
    done: list[int] = []
    failed: list[dict] = []

    async def _one(a: dict) -> None:
        async with sem:
            ok, msg = await _set_one(a)
        if ok:
            done.append(a["id"])
            print(f"[#{a['id']}] {a.get('label') or ''}: 🔐 2FA поставлена")
        else:
            failed.append({"id": a["id"], "label": a.get("label"), "err": msg})
            print(f"[#{a['id']}] {a.get('label') or ''}: ✗ {msg}")

    await asyncio.gather(*[_one(a) for a in accs])
    print(json.dumps({"ok": True, "set": len(done), "failed": failed}, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: облачный пароль (2FA) на аккаунты")
    p.add_argument("--ids", help="через запятую id аккаунтов (по умолчанию — все живые боевые без 2FA)")
    p.add_argument("--dry", action="store_true", help="показать кандидатов, ничего не менять")
    args = p.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    asyncio.run(run(ids, args.dry))


if __name__ == "__main__":
    main()
