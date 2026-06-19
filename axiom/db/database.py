"""Доступ к книжке (SQLite). Инициализация схемы + базовые операции."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import config


def get_conn() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    schema = Path(config.SCHEMA_PATH).read_text(encoding="utf-8")
    with get_conn() as conn:
        conn.executescript(schema)


def upsert_contact(conn: sqlite3.Connection, **fields) -> int:
    """Вставляет или обновляет контакт по phone/username. Возвращает id."""
    phone = fields.get("phone")
    username = fields.get("username")
    row = None
    if phone:
        row = conn.execute("SELECT id FROM contacts WHERE phone = ?", (phone,)).fetchone()
    if row is None and username:
        row = conn.execute("SELECT id FROM contacts WHERE username = ?", (username,)).fetchone()

    cols = ["source", "phone", "username", "tg_user_id", "name", "city", "agency", "tags", "notes"]
    vals = {c: fields.get(c) for c in cols}

    if row:
        sets = ", ".join(f"{c} = COALESCE(?, {c})" for c in cols)
        conn.execute(
            f"UPDATE contacts SET {sets}, updated_at = datetime('now') WHERE id = ?",
            [*[vals[c] for c in cols], row["id"]],
        )
        return row["id"]

    placeholders = ", ".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO contacts ({', '.join(cols)}) VALUES ({placeholders})",
        [vals[c] for c in cols],
    )
    return cur.lastrowid


def add_message(conn: sqlite3.Connection, contact_id: int, direction: str, text: str, intent: str | None = None) -> None:
    conn.execute(
        "INSERT INTO messages (contact_id, direction, text, intent) VALUES (?, ?, ?, ?)",
        (contact_id, direction, text, intent),
    )


def get_history(conn: sqlite3.Connection, contact_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT direction, text, intent, ts FROM messages WHERE contact_id = ? ORDER BY id",
        (contact_id,),
    ).fetchall()


def set_status(conn: sqlite3.Connection, contact_id: int, status: str) -> None:
    conn.execute(
        "UPDATE contacts SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, contact_id),
    )


def find_contact_by_tg(
    conn: sqlite3.Connection, tg_user_id: int | None = None, username: str | None = None
) -> sqlite3.Row | None:
    """Ищет контакт по tg_user_id (приоритет), затем по username. Для входящих сообщений."""
    if tg_user_id:
        row = conn.execute("SELECT * FROM contacts WHERE tg_user_id = ?", (tg_user_id,)).fetchone()
        if row:
            return row
    if username:
        u = username.lstrip("@")
        return conn.execute("SELECT * FROM contacts WHERE username = ? OR username = ?", (u, "@" + u)).fetchone()
    return None


def set_tg_user_id(conn: sqlite3.Connection, contact_id: int, tg_user_id: int) -> None:
    conn.execute(
        "UPDATE contacts SET tg_user_id = ?, updated_at = datetime('now') WHERE id = ?",
        (tg_user_id, contact_id),
    )


def record_meeting(
    conn: sqlite3.Connection,
    contact_id: int,
    meeting_at: str | None,
    notes: str | None = None,
    zoom_link: str | None = None,
    calendar_event_id: str | None = None,
) -> None:
    """Фиксирует договорённость о встрече: создаёт/обновляет deal и двигает статус контакта.
    zoom_link / calendar_event_id подставляет integrations/ (если доступы есть)."""
    row = conn.execute(
        "SELECT id FROM deals WHERE contact_id = ? ORDER BY id DESC LIMIT 1", (contact_id,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE deals SET stage = 'meeting_set', meeting_at = COALESCE(?, meeting_at), "
            "notes = ?, zoom_link = COALESCE(?, zoom_link), "
            "calendar_event_id = COALESCE(?, calendar_event_id) WHERE id = ?",
            (meeting_at, notes, zoom_link, calendar_event_id, row["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO deals (contact_id, stage, meeting_at, notes, zoom_link, calendar_event_id) "
            "VALUES (?, 'meeting_set', ?, ?, ?, ?)",
            (contact_id, meeting_at, notes, zoom_link, calendar_event_id),
        )
    set_status(conn, contact_id, "meeting_set")


if __name__ == "__main__":
    init_db()
    print(f"БД готова: {config.DB_PATH}")
