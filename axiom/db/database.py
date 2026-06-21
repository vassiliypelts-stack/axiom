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


# Поля обогащения добавляем миграцией (ALTER), чтобы не ломать существующую БД.
_EXTRA_CONTACT_COLS = {
    "person_name": "TEXT",      # контактное лицо (для {name} в рассылке)
    "person_role": "TEXT",      # должность
    "specialization": "TEXT",   # на чём специализируется
    "hook": "TEXT",             # персональная зацепка для первого сообщения
    "bio": "TEXT",              # bio из Telegram-профиля (если доставали)
    "inn": "TEXT",              # ИНН юрлица/ИП (из ЕГРЮЛ через DaData)
    "ogrn": "TEXT",             # ОГРН/ОГРНИП
    "founders": "TEXT",         # учредители (ФИО через «; »; на free-тарифе DaData пусто)
    "enriched_at": "TEXT",      # когда обогащён
    "has_wa": "TEXT",           # есть ли WhatsApp ('yes'/'no'/'unknown')
    "wa_jid": "TEXT",           # WhatsApp JID собеседника (например 79991234567@s.whatsapp.net)
    "agent_context": "TEXT",    # ручной контекст для агента (история/нюансы общения с этим лидом)
    "pipeline_id": "INTEGER",   # в какой воронке лид (NULL = дефолтная)
}


# Стадии дефолтной воронки (совпадают со старой единой воронкой — данные не ломаются).
DEFAULT_STAGES = [
    ("new", "Новые"), ("messaged", "Написано"), ("in_dialog", "В диалоге"),
    ("meeting_set", "Встреча назначена"), ("met", "Встреча прошла"), ("won", "Сделка"),
    ("nurture", "Прогрев"), ("lost", "Потеряны"), ("stop", "Стоп"),
]


# Поля кампаний, добавляемые миграцией (промпт ИИ-агента и т.п.).
_EXTRA_CAMPAIGN_COLS = {
    "agent_prompt": "TEXT",
    "project_id": "INTEGER",   # к какому проекту относится кампания
}


# Поля аккаунтов (для прогрева и многоаккаунтной рассылки).
_EXTRA_ACCOUNT_COLS = {
    "tg_session": "TEXT",                 # StringSession аккаунта (Telegram)
    "wa_authed": "TEXT",                  # авторизован ли в WhatsApp ('yes'/'no')
    "proxy": "TEXT",                      # персональный прокси (socks5://user:pass@host:port)
    "warm_stage": "INTEGER DEFAULT 0",    # стадия/день прогрева
    "warm_started_at": "TEXT",
    "last_warm_at": "TEXT",
    "spam_status": "TEXT",                # вердикт @SpamBot: ok|limited|banned|unknown
    "spam_checked_at": "TEXT",
}


# Связь кампания↔контакт: с какого аккаунта отправлено (для прогресса по номерам).
_EXTRA_CAMPAIGN_CONTACT_COLS = {
    "account_id": "INTEGER",
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(contacts)")}
    for col, typ in _EXTRA_CONTACT_COLS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {typ}")
    camp = {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}
    for col, typ in _EXTRA_CAMPAIGN_COLS.items():
        if col not in camp:
            conn.execute(f"ALTER TABLE campaigns ADD COLUMN {col} {typ}")
    acc = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    for col, typ in _EXTRA_ACCOUNT_COLS.items():
        if col not in acc:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} {typ}")
    cc = {r["name"] for r in conn.execute("PRAGMA table_info(campaign_contacts)")}
    for col, typ in _EXTRA_CAMPAIGN_CONTACT_COLS.items():
        if col not in cc:
            conn.execute(f"ALTER TABLE campaign_contacts ADD COLUMN {col} {typ}")


def get_contact_campaign(conn: sqlite3.Connection, contact_id: int) -> sqlite3.Row | None:
    """Кампания, к которой привязан контакт (последняя по отправке). Для промпта агента."""
    return conn.execute(
        "SELECT c.* FROM campaigns c JOIN campaign_contacts cc ON cc.campaign_id = c.id "
        "WHERE cc.contact_id = ? ORDER BY cc.sent_at DESC LIMIT 1",
        (contact_id,),
    ).fetchone()


def _seed_default_pipeline(conn: sqlite3.Connection) -> None:
    """Если воронок нет — заводим дефолтную со старыми стадиями (данные сохраняются)."""
    import json
    n = conn.execute("SELECT COUNT(*) c FROM pipelines").fetchone()["c"]
    if n == 0:
        stages = [{"key": k, "label": l} for k, l in DEFAULT_STAGES]
        conn.execute(
            "INSERT INTO pipelines (name, product, stages, is_default) VALUES (?,?,?,1)",
            ("Основная", "Общая", json.dumps(stages, ensure_ascii=False)),
        )


def init_db() -> None:
    schema = Path(config.SCHEMA_PATH).read_text(encoding="utf-8")
    with get_conn() as conn:
        conn.executescript(schema)
        _ensure_columns(conn)
        _seed_default_pipeline(conn)


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


def find_contact_by_wa(
    conn: sqlite3.Connection, jid: str | None = None, phone: str | None = None
) -> sqlite3.Row | None:
    """Ищет контакт по wa_jid (приоритет), затем по последним 10 цифрам телефона.
    Телефон в книжке хранится по-разному (+7…, 8…, с пробелами) — матчим хвост."""
    if jid:
        row = conn.execute("SELECT * FROM contacts WHERE wa_jid = ?", (jid,)).fetchone()
        if row:
            return row
    digits = "".join(ch for ch in (phone or jid or "") if ch.isdigit())
    if len(digits) >= 10:
        tail = digits[-10:]
        return conn.execute(
            "SELECT * FROM contacts WHERE phone IS NOT NULL AND "
            "replace(replace(replace(replace(phone,'+',''),' ',''),'-',''),'(','') LIKE ?",
            ("%" + tail,),
        ).fetchone()
    return None


def set_wa_jid(conn: sqlite3.Connection, contact_id: int, jid: str) -> None:
    conn.execute(
        "UPDATE contacts SET wa_jid = ?, has_wa = 'yes', updated_at = datetime('now') WHERE id = ?",
        (jid, contact_id),
    )


def get_account(conn: sqlite3.Connection, acc_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()


def save_account_session(conn: sqlite3.Connection, acc_id: int, session: str, username: str | None = None) -> None:
    conn.execute(
        "UPDATE accounts SET tg_session=?, username=COALESCE(?,username) WHERE id=?",
        (session, username, acc_id),
    )


def warming_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Аккаунты в прогреве, у которых есть авторизованная TG-сессия."""
    return conn.execute(
        "SELECT * FROM accounts WHERE status='warming' AND tg_session IS NOT NULL AND tg_session<>''"
    ).fetchall()


def warm_anchors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """«Якоря» — активные аккаунты (твои основные номера), которым шлёт прогрев,
    чтобы ты видел активность. Плюс к взаимному прогреву между аккаунтами."""
    return conn.execute(
        "SELECT * FROM accounts WHERE status='active' AND (username IS NOT NULL OR phone IS NOT NULL)"
    ).fetchall()


def bump_warm(conn: sqlite3.Connection, acc_id: int, new_stage: int, activate: bool = False) -> None:
    if activate:
        conn.execute(
            "UPDATE accounts SET warm_stage=?, last_warm_at=datetime('now'), status='active' WHERE id=?",
            (new_stage, acc_id),
        )
    else:
        conn.execute(
            "UPDATE accounts SET warm_stage=?, last_warm_at=datetime('now'), "
            "warm_started_at=COALESCE(warm_started_at, datetime('now')) WHERE id=?",
            (new_stage, acc_id),
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
