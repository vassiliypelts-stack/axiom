-- AXIOM — схема книжки (пилот). SQLite.

-- Компании (юрлица) — как в Битрикс. Агентство/организация. Контакты (физлица)
-- ссылаются на компанию через contacts.company_id; сделки — через deals.company_id.
CREATE TABLE IF NOT EXISTS companies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT,
    company_type TEXT DEFAULT 'ООО',   -- ООО | ИП | АО | Физлицо | Самозанятый
    city         TEXT,
    phone        TEXT,
    site         TEXT,
    email        TEXT,
    vk           TEXT,
    address      TEXT,
    inn          TEXT,
    ogrn         TEXT,
    founders     TEXT,
    tags         TEXT,
    notes        TEXT,
    status       TEXT DEFAULT 'active',
    created_at   TEXT DEFAULT (datetime('now'))
);

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

-- Мои аккаунты (нейрокоманда / пул отправителей). «Кто есть кто».
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT,                      -- ярлык: «Вася SDR», «Аккаунт #2»
    phone       TEXT,                      -- номер телефона аккаунта
    username    TEXT,                      -- @username
    role        TEXT,                      -- роль в команде: sdr|qualifier|closer|scheduler
    status      TEXT DEFAULT 'active',     -- active|warming|paused|banned
    daily_limit INTEGER DEFAULT 15,        -- лимит первых сообщений в день
    notes       TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(phone)
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

-- Проекты: верхний уровень. В одном проекте несколько маркетинговых кампаний.
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    entity      TEXT,                       -- юр. сущность / бренд проекта
    description TEXT,
    status      TEXT DEFAULT 'active',      -- active|paused|done
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Воронки (как в Битрикс): несколько воронок, каждая = продукт. У воронки свои стадии.
CREATE TABLE IF NOT EXISTS pipelines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    product     TEXT,
    project_id  INTEGER,
    stages      TEXT,                       -- JSON: [{"key":"new","label":"Новые"}, ...]
    is_default  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Команда кампании: какие агенты (аккаунты) работают эту кампанию.
CREATE TABLE IF NOT EXISTS campaign_accounts (
    campaign_id INTEGER NOT NULL,
    account_id  INTEGER NOT NULL,
    daily_limit INTEGER,                    -- персональный лимит агента (пусто = лимит кампании)
    UNIQUE(campaign_id, account_id)
);

-- Кампании (задания на обзвон/рассылку)
CREATE TABLE IF NOT EXISTS campaigns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT,
    product      TEXT,                       -- что продаём
    audience_tag TEXT,                       -- фильтр аудитории по тегу (пусто = вся база)
    channel      TEXT DEFAULT 'telegram',    -- telegram | whatsapp
    account_id   INTEGER,                    -- с какого аккаунта слать (accounts.id)
    daily_limit  INTEGER DEFAULT 15,
    message_template TEXT,                    -- первое сообщение; каждая строка = отдельное сообщение; {name} подставляется
    agent_prompt TEXT,                         -- промпт общения ИИ-агента в диалоге (как ведёт, отрабатывает возражения)
    kp_text      TEXT,                        -- коммерческое предложение
    status       TEXT DEFAULT 'draft',        -- draft|running|paused|done
    created_at   TEXT DEFAULT (datetime('now'))
);

-- Связь кампания ↔ контакт (кому в рамках кампании уже отправлено)
CREATE TABLE IF NOT EXISTS campaign_contacts (
    campaign_id INTEGER NOT NULL,
    contact_id  INTEGER NOT NULL,
    sent_at     TEXT DEFAULT (datetime('now')),
    UNIQUE(campaign_id, contact_id)
);

-- Воронка / встречи
CREATE TABLE IF NOT EXISTS deals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id        INTEGER REFERENCES contacts(id),  -- может быть NULL (сделка только на компанию)
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

-- Каталог чатов/каналов (отдельная база для лидгена, не мешается с CRM).
CREATE TABLE IF NOT EXISTS chats (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT,
    username       TEXT,                      -- @username (если публичный)
    link           TEXT,                      -- ссылка/инвайт
    kind           TEXT,                      -- группа|супергруппа|канал
    members_count  INTEGER,
    activity       TEXT,                      -- оценка активности (сообщений/день и т.п.)
    status         TEXT DEFAULT 'new',        -- new|analyzed|queued|joined|skip
    joined_by      INTEGER,                   -- accounts.id, кто вступил
    can_write      TEXT,                      -- да|только админы|ограничено|заблокирован|не вступил|неизвестно
    members_visible TEXT,                     -- да|нет (виден ли список участников)
    in_account     TEXT,                      -- yes = чат уже есть в личном аккаунте (инвентаризация)
    city           TEXT,                      -- город (для фильтра)
    topic          TEXT,                      -- тема/ниша (для группировки)
    notes          TEXT,
    last_scanned_at TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    UNIQUE(username)
);

-- Админы чата (часто это ЛПР). Несколько на чат.
CREATE TABLE IF NOT EXISTS chat_admins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    tg_user_id  INTEGER,
    username    TEXT,
    name        TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(chat_id, tg_user_id)
);

-- Ниши лидгена: наборы ключевых слов для прослушки чатов (запросы людей).
CREATE TABLE IF NOT EXISTS niches (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT,
    keywords  TEXT,                          -- ключи через запятую
    active    INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Очередь находок прослушки (на обзор оператору перед заносом в лиды).
CREATE TABLE IF NOT EXISTS chat_hits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    niche_id      INTEGER,
    chat_id       INTEGER,
    chat_title    TEXT,
    tg_user_id    INTEGER,
    username      TEXT,
    name          TEXT,
    text          TEXT,
    keyword       TEXT,
    source_msg_id INTEGER,
    status        TEXT DEFAULT 'new',         -- new | lead | ignored
    contact_id    INTEGER,
    ts            TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(chat_id, source_msg_id)
);

-- Простые настройки приложения (ключ-значение): расписание прокси и т.п.
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Пул бесплатных MTProto-прокси (собираются из TG-каналов, авто-замена дохлых).
CREATE TABLE IF NOT EXISTS proxies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT DEFAULT 'mtproto',     -- mtproto | socks5
    server      TEXT,
    port        INTEGER,
    secret      TEXT,
    ping_ms     INTEGER,                    -- TCP-пинг до сервера (мс), NULL = не проверен/мёртв
    status      TEXT DEFAULT 'new',         -- new | alive | dead
    source      TEXT,                       -- откуда (@канал)
    assigned_to INTEGER,                    -- accounts.id, если выдан аккаунту
    checked_at  TEXT,
    added_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(server, port, secret)
);

CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
CREATE INDEX IF NOT EXISTS idx_chat_admins_chat ON chat_admins(chat_id);
