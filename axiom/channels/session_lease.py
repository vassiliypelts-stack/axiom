"""Бронь сессии аккаунта: ОДНО подключение одним ключом на весь сервер.

ЗАЧЕМ. Ключ сессии Telegram сгорает навсегда (AuthKeyDuplicatedError), если он в эфире
одновременно с двух IP. Бесплатные MTProto-прокси — это домены с несколькими серверами
за одним именем, так что даже «тот же прокси» у двух подключений не гарантирует один IP.
Значит, правило одно: в каждый момент сессию аккаунта держит РОВНО ОДИН клиент.

Раньше это пытались обеспечить общим тумблером слушателя (settings.listener_enabled):
рассылка, выпуск запаски, пробив номеров, служебная пауза пульта — каждый выключал его
и включал обратно, не зная о других. 23.09.2026 выпуск запаски выключил слушатель в
11:32:57, автозаход рассылки в 11:33:00 увидел «уже выключен», не стал ждать отключения
и сразу поднял 12 сессий, а в 11:33:13 запаска включила слушатель обратно — прямо под
идущей рассылкой. Сгорели Егор466, Игорь838, Василий5353. А прогрев, обслуживание
боевых, вступления в чаты и ещё два десятка модулей вообще подключались мимо тумблера.

КАК ТЕПЕРЬ. Любое подключение к сессии аккаунта (channels.telegram.build_client) сначала
бронирует аккаунт — строка в session_leases, account_id — первичный ключ, то есть бронь
у аккаунта может быть только одна. Слушатель броню уважает: забронированный аккаунт он
отпускает и не подключает, пока бронь не снята, а остальные аккаунты слушает дальше.
Бронирующий ждёт, пока слушатель ПОДТВЕРДИТ, что отпустил аккаунт (он публикует список
своих подключений в settings.listener_clients), и только потом подключается сам.
Отключение клиента снимает бронь. Процесс умер, не сняв её, — бронь протухает по pid.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import time

from db import database

HOST = socket.gethostname()
# Сколько ждать, пока слушатель отпустит аккаунт. Он проверяет брони каждые POLL_SEC
# (5 с), но в фазе переподключения пачки (до ~минуты) отчитывается реже.
WAIT_LISTENER_SEC = 120
# Сколько ждать, пока аккаунт освободит ДРУГОЙ модуль (не слушатель). Прогрев держит
# аккаунт минутами, поэтому ждём дольше, но не бесконечно: лучше пропустить аккаунт в
# этом заходе, чем повиснуть.
WAIT_OTHER_SEC = 300
# Потолок жизни брони, чей процесс проверить нельзя (тот же pid, что у пульта: клиент
# в самом пульте забыли отключить). Дольше этого слушатель без аккаунта не остаётся.
MAX_AGE_SEC = 2 * 3600
# Отчёт слушателя старше этого — слушатель не работает, ждать его подтверждения незачем.
LISTENER_STALE_SEC = 45

_SCHEMA = ("CREATE TABLE IF NOT EXISTS session_leases ("
           "account_id INTEGER PRIMARY KEY, owner TEXT, pid INTEGER, host TEXT, "
           "created_at REAL)")


class LeaseBusy(RuntimeError):
    """Аккаунт занят другим подключением — подключаться сейчас нельзя."""


def _ensure(conn) -> None:
    conn.execute(_SCHEMA)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # На Windows os.kill(pid, 0) — это CTRL_C_EVENT, а не проверка. Там бронь
        # протухает только по MAX_AGE_SEC (пульт работает на Linux-сервере).
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _stale(row) -> bool:
    if time.time() - float(row["created_at"] or 0) > MAX_AGE_SEC:
        return True
    if row["host"] == HOST and row["pid"] != os.getpid() and not _pid_alive(int(row["pid"])):
        return True
    return False


def purge_stale(conn) -> int:
    _ensure(conn)
    n = 0
    for r in conn.execute("SELECT * FROM session_leases").fetchall():
        if _stale(r):
            conn.execute("DELETE FROM session_leases WHERE account_id=?", (r["account_id"],))
            n += 1
    return n


def leased_ids() -> set[int]:
    """Забронированные сейчас аккаунты — их слушатель держать не должен."""
    with database.get_conn() as conn:
        purge_stale(conn)
        return {r["account_id"] for r in conn.execute("SELECT account_id FROM session_leases")}


# ─────────────────────────── отчёт слушателя ───────────────────────────

def publish_listener(ids) -> None:
    """Слушатель: какие аккаунты он сейчас держит (подключены или подключаются)."""
    payload = json.dumps({"ts": time.time(), "pid": os.getpid(), "host": HOST,
                          "ids": sorted(int(i) for i in ids)})
    with database.get_conn() as conn:
        database.set_setting(conn, "listener_clients", payload)


def _listener_state() -> tuple[float, set[int]]:
    with database.get_conn() as conn:
        raw = database.get_setting(conn, "listener_clients", "") or ""
    try:
        d = json.loads(raw)
        return float(d.get("ts") or 0), {int(i) for i in d.get("ids") or []}
    except (ValueError, TypeError):
        return 0.0, set()


# ─────────────────────────── бронь ───────────────────────────

def _try_insert(acc_id: int, owner: str) -> tuple[bool, str]:
    with database.get_conn() as conn:
        _ensure(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM session_leases WHERE account_id=?",
                               (acc_id,)).fetchone()
            if row and _stale(row):
                conn.execute("DELETE FROM session_leases WHERE account_id=?", (acc_id,))
                row = None
            if row:
                conn.execute("COMMIT")
                return False, f"{row['owner']} (pid {row['pid']})"
            conn.execute("INSERT INTO session_leases (account_id, owner, pid, host, created_at) "
                         "VALUES (?,?,?,?,?)", (acc_id, owner, os.getpid(), HOST, time.time()))
            conn.execute("COMMIT")
            return True, ""
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def release(acc_id: int | None) -> None:
    if not acc_id:
        return
    try:
        with database.get_conn() as conn:
            _ensure(conn)
            conn.execute("DELETE FROM session_leases WHERE account_id=? AND pid=? AND host=?",
                         (acc_id, os.getpid(), HOST))
    except Exception:  # noqa: BLE001 — снятие брони не должно ронять вызывающего
        pass


async def acquire(acc_id: int, owner: str) -> None:
    """Забронировать аккаунт и дождаться, пока слушатель его отпустит. LeaseBusy — нельзя."""
    t0 = time.time()
    who = ""
    while True:
        ok, who = await asyncio.to_thread(_try_insert, acc_id, owner)
        if ok:
            break
        if time.time() - t0 > WAIT_OTHER_SEC:
            raise LeaseBusy(f"аккаунт #{acc_id} занят другим подключением: {who}")
        await asyncio.sleep(2)
    created = time.time()
    import threading
    if threading.current_thread().name == "tg-listener":
        # Бронь берёт сам поток слушателя (heal проверяет прокси аккаунта, который
        # слушатель только что НЕ смог подключить). Ждать его же отчёта — ждать самого
        # себя: он стоит на этом await. Аккаунт он в этот момент заведомо не держит.
        return
    while True:
        ts, ids = await asyncio.to_thread(_listener_state)
        if time.time() - ts > LISTENER_STALE_SEC:
            return            # слушатель не работает — держать сессию некому
        if ts >= created and acc_id not in ids:
            return            # слушатель увидел бронь и отпустил аккаунт
        if time.time() - created > WAIT_LISTENER_SEC:
            release(acc_id)
            raise LeaseBusy(f"слушатель не отпустил аккаунт #{acc_id} за {WAIT_LISTENER_SEC}с — "
                            f"не подключаюсь, иначе ключ окажется в эфире дважды")
        await asyncio.sleep(1)


# ─────────────────────────── какой это аккаунт ───────────────────────────

_KEY_INDEX: dict[str, int] = {}


def _key_sha(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()


def account_for_session(session) -> int | None:
    """id аккаунта по ключу сессии (основной или запасной). None — сессия не из базы
    (пустая при первом входе, импорт файла) — такую бронировать незачем."""
    key = getattr(getattr(session, "auth_key", None), "key", None)
    if not key:
        return None
    sha = _key_sha(key)
    if sha in _KEY_INDEX:
        return _KEY_INDEX[sha]
    from telethon.sessions import StringSession
    with database.get_conn() as conn:
        rows = conn.execute("SELECT id, tg_session, tg_session_spare FROM accounts").fetchall()
    for r in rows:
        for s in (r["tg_session"], r["tg_session_spare"]):
            if not s:
                continue
            try:
                k = StringSession(s).auth_key
            except Exception:  # noqa: BLE001
                continue
            if k and k.key:
                _KEY_INDEX[_key_sha(k.key)] = r["id"]
    return _KEY_INDEX.get(sha)
