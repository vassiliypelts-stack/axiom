"""Проверка купленных Telegram-аккаунтов на «живость» + завод живых в БД.

Берёт папку, рекурсивно находит аккаунты в двух форматах:
  • Telethon  — файл *.session (рядом опц. <имя>.json с api_id/api_hash/proxy/2FA)
  • TDesktop  — папка tdata/ (формат Telegram Desktop)

Для каждого аккаунта ПЕРЕИСПОЛЬЗУЕТ существующую авторизацию (UseCurrentSession —
без нового логина и без SMS), подключается (через персональный прокси аккаунта,
если он указан в json), и зовёт get_me(). Живой → выводим в отчёт и (с --save)
кладём в accounts со status=warming и его собственными api_id/api_hash/proxy.

    python -m channels.account_check "G:/.../АККАУНТ ТГ 2025"
    python -m channels.account_check "<path>" --save            # живых сразу в БД
    python -m channels.account_check "<path>" --save --status active

Внимание: подключение засветит IP аккаунта. Для купленных сессий прокси берётся
из json автоматически; у чистых tdata прокси нет — идут с локального IP.
Сессии и 2FA — секреты, БД не публикуй.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

logging.getLogger("telethon").setLevel(logging.CRITICAL)
from dataclasses import dataclass, field
from pathlib import Path

# vendor/ — пропатченный под Python 3.14 opentele + tgcrypto-шим (чистый AES)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))

from telethon import TelegramClient  # noqa: E402
from telethon.sessions import SQLiteSession, StringSession  # noqa: E402

from opentele.api import API, UseCurrentSession  # noqa: E402

from db import database  # noqa: E402


@dataclass
class Candidate:
    name: str                       # ярлык (имя папки/файла)
    kind: str                       # "session" | "tdata"
    path: Path                      # путь к .session или папке tdata
    meta: dict = field(default_factory=dict)   # json маркета
    twofa: str = ""                 # пароль 2FA (если найден)


# ─────────────────────────── поиск кандидатов ────────────────────────────

def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _twofa_near(folder: Path, meta: dict) -> str:
    if meta.get("twoFA"):
        return str(meta["twoFA"])
    tf = folder / "twoFA.txt"
    if tf.exists():
        try:
            return tf.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:  # noqa: BLE001
            return ""
    return ""


def discover(root: Path) -> list[Candidate]:
    """Находит .session и tdata, dedupe: если рядом и .session, и tdata — берём .session."""
    cands: list[Candidate] = []
    session_dirs: set[Path] = set()

    for s in sorted(root.rglob("*.session")):
        meta = _read_json(s.with_suffix(".json"))
        cands.append(Candidate(
            name=s.stem, kind="session", path=s, meta=meta,
            twofa=_twofa_near(s.parent, meta),
        ))
        session_dirs.add(s.parent.resolve())

    for td_dir in sorted(root.rglob("tdata")):
        if not td_dir.is_dir():
            continue
        parent = td_dir.parent
        if parent.resolve() in session_dirs:
            continue  # уже есть .session в этой же папке
        # json/2FA могут лежать рядом с tdata
        meta = {}
        for jp in parent.glob("*.json"):
            meta = _read_json(jp)
            if meta:
                break
        cands.append(Candidate(
            name=parent.name, kind="tdata", path=td_dir, meta=meta,
            twofa=_twofa_near(parent, meta),
        ))
    return cands


# ─────────────────────────── прокси ────────────────────────────

def _proxy_dict(meta: dict):
    """proxy из json [type, host, port, ?, user, pass] → telethon python_socks dict (socks5)."""
    p = meta.get("proxy")
    if not isinstance(p, list) or len(p) < 3:
        return None
    host, port = p[1], int(p[2])
    user = p[4] if len(p) > 4 else None
    pwd = p[5] if len(p) > 5 else None
    d = {"proxy_type": "socks5", "addr": host, "port": port, "rdns": True}
    if user and pwd:
        d["username"], d["password"] = user, pwd
    return d


# ─────────────────────────── проверка одного аккаунта ────────────────────────────

@dataclass
class Result:
    cand: Candidate
    alive: bool = False
    reason: str = ""
    user_id: int | None = None
    username: str | None = None
    phone: str | None = None
    first_name: str | None = None
    session_str: str | None = None
    api_id: int | None = None
    api_hash: str | None = None


_CONN = dict(connection_retries=2, retry_delay=1, timeout=10)


def _string_session_from(sess) -> str:
    """Любая telethon-сессия → строка StringSession (dc + auth_key)."""
    ss = StringSession()
    ss.set_dc(sess.dc_id, sess.server_address, sess.port)
    ss.auth_key = sess.auth_key
    return ss.save()


async def _make_client(cand: Candidate, proxy) -> tuple[TelegramClient, int | None, str | None]:
    """Создаёт TelegramClient под кандидата (без нового логина). Не подключает."""
    if cand.kind == "tdata":
        from opentele.td import TDesktop  # нужен только PyQt5-путь tdata; для .session не грузим
        tdesk = TDesktop(str(cand.path))
        if not tdesk.isLoaded():
            raise RuntimeError("tdata не загрузилась (пустая/битая)")
        # session=None → опентелевский баг с StringSession обходим; UseCurrentSession
        # лишь читает ключ из tdata, нового логина/SMS нет. Девайс-параметры из tdata.
        client = await tdesk.ToTelethon(
            session=None, flag=UseCurrentSession, api=API.TelegramDesktop, **_CONN,
        )
        if proxy:
            client.set_proxy(proxy)
        return client, None, None
    # session
    sql = SQLiteSession(str(cand.path.with_suffix("")))
    ss = StringSession()
    ss.set_dc(sql.dc_id, sql.server_address, sql.port)
    ss.auth_key = sql.auth_key
    api_id = cand.meta.get("app_id") or cand.meta.get("api_id") or API.TelegramDesktop.api_id
    api_hash = cand.meta.get("app_hash") or cand.meta.get("api_hash") or API.TelegramDesktop.api_hash
    client = TelegramClient(ss, int(api_id), str(api_hash), proxy=proxy, **_CONN)
    return client, int(api_id), str(api_hash)


async def check_one(cand: Candidate) -> Result:
    r = Result(cand=cand)
    proxy = _proxy_dict(cand.meta)
    # сначала через прокси аккаунта; если он мёртв — пробуем напрямую
    attempts = [proxy, None] if proxy else [None]
    for i, px in enumerate(attempts):
        client = None
        try:
            client, api_id, api_hash = await _make_client(cand, px)
            r.api_id, r.api_hash = api_id, api_hash
            await client.connect()
            if not await client.is_user_authorized():
                r.reason = "не авторизован (сессия мертва/разлогинена)"
                return r
            me = await client.get_me()
            if me is None:
                r.reason = "get_me вернул None"
                return r
            r.alive = True
            r.user_id = me.id
            r.username = me.username
            r.phone = ("+" + me.phone) if me.phone else None
            r.first_name = me.first_name
            r.session_str = _string_session_from(client.session)
            r.reason = "OK (прямое подключение)" if px is None and proxy else "OK"
            return r
        except Exception as e:  # noqa: BLE001
            r.reason = f"{type(e).__name__}: {e}"
            last = (i == len(attempts) - 1)
            if not last:
                r.reason += " → пробую без прокси"
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
    return r


# ─────────────────────────── запись в БД ────────────────────────────

def save_to_db(r: Result, status: str) -> str:
    c = r.cand
    phone = r.phone or ("+" + str(c.meta.get("phone") or c.name).lstrip("+"))
    username = r.username or c.meta.get("username")
    name = r.first_name or c.meta.get("first_name") or username or phone
    proxy = _proxy_dict(c.meta)
    proxy_str = None
    if proxy:
        up = f"{proxy['username']}:{proxy['password']}@" if proxy.get("username") else ""
        proxy_str = f"socks5://{up}{proxy['addr']}:{proxy['port']}"
    notes = "проверен account_check" + (f" · 2FA: {c.twofa}" if c.twofa else "")
    api_id = r.api_id if r.api_id and r.api_id != API.TelegramDesktop.api_id else None
    api_hash = r.api_hash if api_id else None

    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE phone=?", (phone,)).fetchone()
        if row:
            conn.execute(
                "UPDATE accounts SET tg_session=?, username=COALESCE(?,username), "
                "api_id=COALESCE(?,api_id), api_hash=COALESCE(?,api_hash), "
                "proxy=COALESCE(?,proxy), label=COALESCE(label,?), notes=? WHERE id=?",
                (r.session_str, username, api_id, api_hash, proxy_str, name, notes, row["id"]),
            )
            return f"обновлён #{row['id']} {phone} (@{username or '—'})"
        cur = conn.execute(
            "INSERT INTO accounts (label, phone, username, role, status, daily_limit, "
            "notes, tg_session, api_id, api_hash, proxy) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, phone, username, "sdr", status, 15, notes, r.session_str, api_id, api_hash, proxy_str),
        )
        return f"добавлен #{cur.lastrowid} {phone} (@{username or '—'})"


# ─────────────────────────── main ────────────────────────────

async def run(root: Path, save: bool, status: str) -> None:
    cands = discover(root)
    if not cands:
        print("не нашёл ни .session, ни tdata по пути")
        return
    print(f"Найдено кандидатов: {len(cands)}\n")
    alive: list[Result] = []
    for c in cands:
        print(f"… {c.name:<22} [{c.kind}] {'(proxy)' if _proxy_dict(c.meta) else '(no proxy)'}")
        r = await check_one(c)
        if r.alive:
            alive.append(r)
            print(f"   ЖИВОЙ  id={r.user_id}  @{r.username or '—'}  {r.phone or '—'}  {r.first_name or ''}")
        else:
            print(f"   мёртв/ошибка — {r.reason}")
        await asyncio.sleep(2)  # щадящий темп, не палим аккаунты пачкой

    print("\n================= ИТОГ =================")
    print(f"Живых: {len(alive)} из {len(cands)}")
    for r in alive:
        print(f"  • {r.cand.name:<22} @{r.username or '—':<16} {r.phone or '—':<16} "
              f"{'2FA:'+r.cand.twofa if r.cand.twofa else ''}")

    if save and alive:
        print("\n— запись в БД —")
        for r in alive:
            print("  " + save_to_db(r, status))
    elif alive:
        print("\n(для записи в базу повтори с флагом --save)")


def main() -> None:
    p = argparse.ArgumentParser(description="Проверка живости TG-аккаунтов и завод в БД")
    p.add_argument("path", help="папка с аккаунтами (.session/tdata, ищет рекурсивно)")
    p.add_argument("--save", action="store_true", help="живых записать в accounts")
    p.add_argument("--status", default="warming", choices=["warming", "active", "paused"],
                   help="статус для записанных (по умолчанию warming)")
    args = p.parse_args()
    asyncio.run(run(Path(args.path), args.save, args.status))


if __name__ == "__main__":
    main()
