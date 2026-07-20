"""Проверка ЖИВОСТИ TG-сессий аккаунтов: реально ли ими ещё можно работать.

Зачем отдельно от health.py (@SpamBot) и proxy_check.py (прокси):
  • proxy_check  — «доедет ли трафик до Telegram» (канал), про сессию ничего не знает;
  • health.py    — «нет ли ограничений» у ЖИВОГО аккаунта (спрашивает @SpamBot);
  • session_check — «а аккаунт вообще ещё наш?» (сессия не отозвана, номер не забанен).
Раньше разлогиненная сессия схлопывалась в spam_status='unknown' и была неотличима
от «SpamBot не ответил», а колонка «TG ✓» означала лишь «строка сессии в БД непуста».

Вердикт (accounts.session_state):
    alive   — get_me() ответил, аккаунт наш и рабочий
    revoked — сессия отозвана/разлогинена (в TG: «Устройства» → завершили сеанс)
    banned  — номер забанен/удалён Telegram'ом
    noconn  — не достучались (мёртвый прокси / нет интернета) — про сессию НЕ судим
    nosess  — строки сессии в БД нет (аккаунт не подключён)

Ключевое: мёртвый прокси ≠ мёртвая сессия. Если через прокси не вышло — пробуем
напрямую, и только молчание обоих даёт 'noconn' (alive=NULL, не 0): лучше «не знаю»,
чем ложно похоронить рабочий аккаунт и выбросить его из рассылки.

Запуск:
    python -m channels.session_check                # все аккаунты с сессией
    python -m channels.session_check --ids 8,11,22  # только эти
"""
from __future__ import annotations

import argparse
import asyncio
import json

from telethon.sessions import StringSession

from channels.telegram import build_client
from db import database

# Сколько сессий проверяем одновременно (у каждой свой прокси, но не устраиваем залп).
_CONCURRENCY = 8
_TIMEOUT = 25

# Ошибки, по которым Telegram прямо говорит «этого аккаунта больше нет».
_BAN_ERRORS = ("UserDeactivatedBanError", "UserDeactivatedError", "PhoneNumberBannedError")
# Ошибки «сессия больше не действительна», но сам номер жив.
_REVOKE_ERRORS = ("AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError",
                  "AuthKeyDuplicatedError", "UserMigrateError", "AuthKeyInvalidError",
                  "UnauthorizedError")

_LABEL = {
    "alive": "🟢 жив",
    "revoked": "🔴 сессия отозвана",
    "banned": "⛔ бан/удалён",
    "noconn": "⚪ нет связи",
    "nosess": "⚪ нет сессии",
}


def _targets(ids: list[int] | None) -> list[dict]:
    with database.get_conn() as conn:
        sql = ("SELECT id, label, phone, tg_session, proxy, api_id, api_hash FROM accounts "
               "WHERE tg_session IS NOT NULL AND tg_session<>''")
        if ids:
            qm = ",".join("?" * len(ids))
            rows = conn.execute(f"{sql} AND id IN ({qm})", ids).fetchall()
        else:
            rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def _classify(exc: Exception) -> tuple[str, str] | None:
    """Ошибка → (state, причина). None — ошибка не про сессию (связь), решаем выше."""
    name = type(exc).__name__
    if name in _BAN_ERRORS:
        return "banned", f"Telegram забанил/удалил номер ({name})"
    if name in _REVOKE_ERRORS:
        return "revoked", f"сессия недействительна ({name})"
    return None


def note_failure(acc_id: int, exc: Exception) -> str | None:
    """Любой воркер напоролся на ошибку у аккаунта → зафиксировать, если это ПРО СЕССИЮ.

    Зачем публично: массовые модули (phone_resolve, chat_scan_all) первыми узнают, что
    сессия отозвана или номер забанен — они ходят аккаунтами каждый день, а полный
    session_check гоняется редко. Без этого аккаунт остаётся session_alive=1, снова и
    снова выбирается в работу и снова падает (реальный случай: AuthKeyDuplicatedError
    у #12 при живом session_alive=1).

    Возвращает вердикт ('revoked'/'banned') или None, если ошибка про связь, а не про
    сессию — тогда НЕ трогаем: мёртвый прокси не повод хоронить аккаунт.
    """
    v = _classify(exc)
    if not v:
        return None
    state, reason = v
    _save(acc_id, state, reason)
    return state


async def _probe(acc: dict, proxy: str | None) -> tuple[str, str]:
    """Одна попытка коннекта. (state, причина). Клиент всегда закрываем."""
    try:
        session = StringSession(acc["tg_session"])
    except Exception as e:  # noqa: BLE001 — битая строка сессии: это точно про сессию
        return "revoked", f"строка сессии не читается: {e}"
    try:
        client = build_client(session, proxy, acc.get("api_id"), acc.get("api_hash"))
    except Exception as e:  # noqa: BLE001
        # Развалилась сборка клиента (обычно кривая строка прокси) — это НЕ приговор
        # сессии: отдаём noconn, и _check_one перепроверит напрямую, без прокси.
        return "noconn", f"не собрать клиент: {e}"
    try:
        try:
            await asyncio.wait_for(client.connect(), timeout=_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            verdict = _classify(e)
            return verdict or ("noconn", f"не подключиться: {type(e).__name__}")
        try:
            me = await asyncio.wait_for(client.get_me(), timeout=_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            verdict = _classify(e)
            return verdict or ("noconn", f"get_me не ответил: {type(e).__name__}")
        if me is None:
            return "revoked", "сессия не авторизована (разлогинена)"
        who = f"@{me.username}" if me.username else (me.first_name or "").strip()
        return "alive", f"ответил {who or 'без имени'}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def _check_one(acc: dict) -> tuple[str, str]:
    """Через прокси аккаунта; если не достучались — пробуем напрямую (прокси мог сдохнуть,
    а сессия при этом жива — не хороним её из-за канала)."""
    state, reason = await _probe(acc, acc.get("proxy"))
    if state == "noconn" and acc.get("proxy"):
        d_state, d_reason = await _probe(acc, None)
        if d_state != "noconn":
            return d_state, f"{d_reason} (напрямую — прокси мёртв)"
    return state, reason


def _save(acc_id: int, state: str, reason: str) -> None:
    alive = {"alive": 1, "revoked": 0, "banned": 0}.get(state)   # noconn/nosess → NULL: «не знаю»
    with database.get_conn() as conn:
        prev = conn.execute("SELECT session_state, label, phone FROM accounts WHERE id=?",
                            (acc_id,)).fetchone()
        conn.execute(
            "UPDATE accounts SET session_alive=?, session_state=?, session_reason=?, "
            "session_checked_at=datetime('now') WHERE id=?",
            (alive, state, reason[:200], acc_id),
        )
        if state == "banned":
            conn.execute("UPDATE accounts SET status='banned' WHERE id=?", (acc_id,))
        # Колокольчик — только на переходе в плохое (не спамим при каждой перепроверке).
        if state in ("banned", "revoked") and (not prev or prev["session_state"] != state):
            who = (prev["label"] or prev["phone"] or f"#{acc_id}") if prev else f"#{acc_id}"
            if state == "banned":
                ttl, hint = "⛔ Аккаунт забанен", "номер сгорел — выведи из рассылки"
            else:
                ttl, hint = "🔴 Сессия отозвана", "переподключи аккаунт (🔌 Подключить)"
            database.add_event(conn, "ban", f"{ttl}: {who}", hint, level="warn", account_id=acc_id)


async def run(ids: list[int] | None) -> None:
    database.init_db()
    accs = _targets(ids)
    if not accs:
        print(json.dumps({"ok": False, "error": "нет аккаунтов с сессией"}, ensure_ascii=False))
        return
    sem = asyncio.Semaphore(_CONCURRENCY)
    results: dict[int, str] = {}

    async def _one(a: dict) -> None:
        async with sem:
            try:
                state, reason = await _check_one(a)
            except Exception as e:  # noqa: BLE001 — не роняем всю пачку из-за одного
                state, reason = "noconn", f"сбой проверки: {type(e).__name__}: {e}"
            results[a["id"]] = state
            _save(a["id"], state, reason)
            print(f"[#{a['id']}] {a.get('label') or a.get('phone') or ''}: "
                  f"{_LABEL.get(state, state)} — {reason}")

    await asyncio.gather(*[_one(a) for a in accs])
    tally = {k: sum(1 for v in results.values() if v == k) for k in _LABEL}
    print(json.dumps({"ok": True, "checked": len(accs), "alive": tally["alive"],
                      "revoked": tally["revoked"], "banned": tally["banned"],
                      "noconn": tally["noconn"]}, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: проверка живости TG-сессий аккаунтов")
    p.add_argument("--ids", help="через запятую id аккаунтов (по умолчанию — все с сессией)")
    args = p.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    asyncio.run(run(ids))


if __name__ == "__main__":
    main()
