"""Импорт купленных Telegram-аккаунтов (.session + .json) в AXIOM — офлайн, без кода.

Берёт папку, рекурсивно ищет *.session (формат Telethon SQLite), к каждой —
соседний <имя>.json (метаданные с маркета: phone, username, api_id/api_hash,
proxy, 2FA). Конвертит сессию в StringSession БЕЗ подключения к сети и кладёт
аккаунт в БД (accounts) с его СОБСТВЕННЫМИ api_id/api_hash и прокси — это важно,
чтобы не спалить купленную сессию чужим api_id.

    python -m channels.import_session "G:/.../ТГ Аккуанты(portable)"
    python -m channels.import_session <path> --status active   # сразу в боевые

Сессии и 2FA — секреты, БД не публикуй.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from telethon.sessions import SQLiteSession, StringSession

import phone_geo
from db import database


def _string_session(session_path: Path) -> str:
    """Telethon .session (SQLite) → StringSession, без сети."""
    sql = SQLiteSession(str(session_path.with_suffix("")))  # Telethon добавит .session сам
    ss = StringSession()
    ss.set_dc(sql.dc_id, sql.server_address, sql.port)
    ss.auth_key = sql.auth_key
    return ss.save()


def _proxy_from_json(meta: dict) -> str | None:
    """proxy из json маркета [type, host, port, ?, user, pass] → socks5://user:pass@host:port."""
    p = meta.get("proxy")
    if not isinstance(p, list) or len(p) < 3:
        return None
    host, port = p[1], p[2]
    user = p[4] if len(p) > 4 else None
    pwd = p[5] if len(p) > 5 else None
    if user and pwd:
        return f"socks5://{user}:{pwd}@{host}:{port}"
    return f"socks5://{host}:{port}"


def _import_one(session_path: Path, status: str) -> tuple[bool, str]:
    meta = {}
    jpath = session_path.with_suffix(".json")
    if jpath.exists():
        try:
            meta = json.loads(jpath.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    try:
        session_str = _string_session(session_path)
    except Exception as e:  # noqa: BLE001
        return False, f"{session_path.name}: не прочитал сессию — {e}"

    phone = str(meta.get("phone") or session_path.stem).lstrip("+")
    phone = "+" + phone if not phone.startswith("+") else phone
    username = meta.get("username") or None
    name = " ".join(x for x in [meta.get("first_name"), meta.get("last_name")] if x) or username or phone
    api_id = meta.get("app_id") or meta.get("api_id")
    api_hash = meta.get("app_hash") or meta.get("api_hash")
    proxy = _proxy_from_json(meta)
    twofa = meta.get("twoFA") or ""
    notes = f"импорт с маркета{' · 2FA: ' + str(twofa) if twofa else ''}"
    country = phone_geo.detect(phone)   # страна по коду номера (для гео-прокси)

    database.init_db()
    with database.get_conn() as conn:
        row = conn.execute("SELECT id FROM accounts WHERE phone=?", (phone,)).fetchone()
        if row:
            conn.execute(
                "UPDATE accounts SET tg_session=?, username=COALESCE(?,username), api_id=?, "
                "api_hash=?, proxy=COALESCE(?,proxy), label=COALESCE(label,?), notes=?, "
                "country=COALESCE(NULLIF(country,''), ?) WHERE id=?",
                (session_str, username, api_id, api_hash, proxy, name, notes, country, row["id"]),
            )
            return True, f"обновлён #{row['id']} {phone} (@{username or '—'})"
        cur = conn.execute(
            "INSERT INTO accounts (label, phone, username, role, status, daily_limit, notes, "
            "tg_session, api_id, api_hash, proxy, country, bought_at, kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),'bought')",
            (name, phone, username, "sdr", status, 15, notes, session_str, api_id, api_hash, proxy, country),
        )
        return True, f"добавлен #{cur.lastrowid} {phone} (@{username or '—'})"


def main() -> None:
    p = argparse.ArgumentParser(description="Импорт .session аккаунтов в AXIOM")
    p.add_argument("path", help="папка с .session/.json (ищет рекурсивно) или один .session")
    p.add_argument("--status", default="warming", choices=["warming", "active", "paused"],
                   help="стартовый статус импортированных (по умолчанию warming)")
    args = p.parse_args()
    root = Path(args.path)
    if root.is_file() and root.suffix == ".session":
        sessions = [root]
    else:
        sessions = sorted(root.rglob("*.session"))
    if not sessions:
        print("не нашёл ни одного .session по пути")
        return
    ok = 0
    for s in sessions:
        success, msg = _import_one(s, args.status)
        print(("[ok] " if success else "[skip] ") + msg)
        ok += int(success)
    print(f"\nИтого импортировано/обновлено: {ok} из {len(sessions)}")


if __name__ == "__main__":
    main()
