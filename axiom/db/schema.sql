-- AXIOM — схема книжки (пилот). SQLite.

-- Риелтор из твоей базы
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT DEFAULT 'import',     -- import / chat
    phone       TEXT,
    username    TEXT,
    tg_user_id  INTEGER,
    name        TEXT,
    city        TEXT,
    agency      TEXT,
    tags        TEXT,
    notes       TEXT,
    wa_phone    TEXT,                       -- номер для WhatsApp (если отличается)
    preferred_channel TEXT DEFAULT 'telegram', -- telegram | whatsapp
    -- результат чекера наличия мессенджера: yes | no | unknown
    has_tg      TEXT DEFAULT 'unknown',
    has_wa      TEXT DEFAULT 'unknown',
    has_max     TEXT DEFAULT 'unknown',
    checked_at  TEXT,
    status      TEXT DEFAULT 'new',        -- new|messaged|in_dialog|meeting_set|met|won|lost|nurture|stop
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(phone),
    UNIQUE(username)
);

-- Реплики диалога (входящие/исходящие)
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id),
    channel     TEXT DEFAULT 'telegram',   -- telegram | whatsapp
    direction   TEXT NOT NULL,             -- in | out
    text        TEXT NOT NULL,
    intent      TEXT,                      -- positive|objection|later|not_interested|question|agreed
    ts          TEXT DEFAULT (datetime('now'))
);

-- Воронка / встречи
CREATE TABLE IF NOT EXISTS deals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id        INTEGER NOT NULL REFERENCES contacts(id),
    stage             TEXT DEFAULT 'new',  -- new|meeting_set|met|won|lost
    zoom_link         TEXT,
    meeting_at        TEXT,
    calendar_event_id TEXT,
    reminder_sent     INTEGER DEFAULT 0,
    next_action_at    TEXT,
    outcome           TEXT,
    notes             TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
