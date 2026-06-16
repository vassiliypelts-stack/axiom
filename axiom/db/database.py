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


if __name__ == "__main__":
    init_db()
    print(f"БД готова: {config.DB_PATH}")
