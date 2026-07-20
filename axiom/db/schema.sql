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

-- Несколько КП в одной кампании (под разные типы ЦА: брокеры/застройщики/АН).
-- Агент сам выбирает уместное по полю when_to_use.
CREATE TABLE IF NOT EXISTS campaign_kps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    name        TEXT,                          -- тип/сегмент: «Застройщики», «Брокеры», «АН»
    when_to_use TEXT,                          -- условие выбора для агента: кому это КП подходит
    kp_text     TEXT,                          -- текст КП
    kp_file     TEXT,                          -- имя файла КП (в data/kp), опц.
    created_at  TEXT DEFAULT (datetime('now'))
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
    can_write      TEXT,                      -- могу ли Я слать текст: да|только админы|заблокирован|
                                              -- не вступил|нужно одобрение|неизвестно. «заблокирован» =
                                              -- личный мут (banned_rights), см. chat_scan.can_write
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

-- Членство армии в чатах (many-to-many): какой аккаунт в каком чате состоит.
-- Основа отчёта покрытия «сколько агентов в скольких чатах» и распределения
-- авто-вступления (чтобы не слать один аккаунт в один чат дважды).
CREATE TABLE IF NOT EXISTS account_chats (
    account_id INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    can_write  TEXT,
    joined_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(account_id, chat_id)
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

-- Сырьё для AI-досье (H1): что человек РЕАЛЬНО писал в чатах. Собирает парсер
-- (--harvest), потребляет agent/enrich_person.py для психо-портрета (боли/желания).
CREATE TABLE IF NOT EXISTS tg_user_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id  INTEGER NOT NULL,
    contact_id  INTEGER,                    -- связь с contacts (если лид уже заведён)
    chat_id     INTEGER,
    chat_title  TEXT,
    text        TEXT,
    msg_id      INTEGER,
    ts          TEXT,                        -- дата сообщения (для окна 90 дней)
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(tg_user_id, chat_id, msg_id)      -- дедуп: повторный прогон не плодит
);

-- Простые настройки приложения (ключ-значение): расписание прокси и т.п.
-- ИИ-агенты: роль + тип задачи + промпт + привязка к аккаунту-исполнителю.
-- Один аккаунт может играть разные роли (нетворкинг/лидген/инвайтинг) разными агентами.
CREATE TABLE IF NOT EXISTS ai_agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    task        TEXT,                          -- leadgen|networking|inviting|qualify|support|other
    prompt      TEXT,                          -- характер/инструкция агента
    account_id  INTEGER,                       -- аккаунт-исполнитель (accounts.id), опц.
    active      INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Лента событий для колокольчика: старт/финиш кампании, лиды, баны, прогрев.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT,                          -- campaign_start|campaign_done|lead|meeting|ban|warm_ready|info
    level       TEXT DEFAULT 'info',           -- info|good|warn
    title       TEXT,
    text        TEXT,
    contact_id  INTEGER,
    campaign_id INTEGER,
    account_id  INTEGER,
    ts          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Инбокс: то, что ты надиктовал/написал личному боту, ИИ разобрал (agent/inbox.py).
-- Лиды уходят в contacts, заметки к лиду — в contacts.agent_context; ЗДЕСЬ живут
-- задачи/напоминания и свободные заметки — у них есть срок и признак «сделано»,
-- чего в events (лента колокольчика) нет.
CREATE TABLE IF NOT EXISTS inbox_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT DEFAULT 'task',   -- task | note
    text        TEXT,
    due_at      TEXT,                  -- срок (ISO), если ИИ распознал «завтра в 15:00»
    done        INTEGER DEFAULT 0,
    contact_id  INTEGER,               -- к кому относится, если понятно
    raw         TEXT,                  -- исходное сообщение (на случай разбора вручную)
    created_at  TEXT DEFAULT (datetime('now'))
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

-- Очередь «доотправки» опенера: не шлём все строки пачкой («портянкой»), а по одной,
-- с паузой в несколько минут между строками. Если за это время человек ОТВЕТИЛ (статус
-- контакта уже не 'messaged') — оставшиеся строки НЕ шлём, дальше ведёт живой диалог/агент.
CREATE TABLE IF NOT EXISTS opener_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id   INTEGER NOT NULL REFERENCES contacts(id),
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    campaign_id  INTEGER,
    parts_json   TEXT NOT NULL,               -- JSON-список ЕЩЁ не отправленных строк
    next_at      TEXT NOT NULL,               -- когда слать следующую строку
    created_at   TEXT DEFAULT (datetime('now'))
);

-- Оргструктура (как в Битрикс): отделы + сотрудники внутри — живые люди и
-- виртуальные ИИ-агенты (ссылка на ai_agents). Чисто для наглядности «кто за что
-- отвечает» — не привязана жёстко к projects/campaigns.
CREATE TABLE IF NOT EXISTS departments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,                       -- зона ответственности
    parent_id   INTEGER,                    -- вложенность отделов (NULL = верхний уровень)
    sort_order  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Сотрудник отдела: kind='human' — живой человек (поля ниже свои), kind='agent' —
-- виртуальный, ссылается на ai_agents (имя/промпт/исполнитель берутся оттуда).
CREATE TABLE IF NOT EXISTS org_members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    kind          TEXT DEFAULT 'human',     -- human | agent
    name          TEXT,                     -- для human; для agent можно пусто (берём из ai_agents.name)
    role          TEXT,                     -- должность/роль
    phone         TEXT,
    email         TEXT,
    ai_agent_id   INTEGER REFERENCES ai_agents(id),  -- для kind='agent' (наследие; данные слиты в поля ниже)
    account_id    INTEGER,                  -- аккаунт-исполнитель прямо на должности (слияние со «Структурой»)
    task          TEXT,                     -- задача ИИ-роли (leadgen|networking|inviting|…)
    prompt        TEXT,                     -- характер/инструкция ИИ-роли
    needs_access  INTEGER DEFAULT 0,        -- нужен ли доступ в пульт (пока просто пометка на будущее)
    notes         TEXT,
    sort_order    INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_org_members_dept ON org_members(department_id);
CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
CREATE INDEX IF NOT EXISTS idx_chat_admins_chat ON chat_admins(chat_id);
CREATE INDEX IF NOT EXISTS idx_user_posts_user ON tg_user_posts(tg_user_id);
CREATE INDEX IF NOT EXISTS idx_opener_queue_due ON opener_queue(next_at);
