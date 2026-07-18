"""Доступ к книжке (SQLite). Инициализация схемы + базовые операции."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import config


def get_conn() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout=30 + WAL: у AXIOM много параллельных фоновых процессов (прогрев,
    # упаковка, слушатель, веб) пишущих в один файл. Дефолтный sqlite3 (timeout=5с,
    # journal rollback) роняет процесс с «database is locked» уже при небольшой
    # накладке двух писателей — WAL даёт читателям не блокировать писателя и
    # наоборот, а больший busy_timeout просто ждёт своей очереди вместо падения.
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
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
    # Пробив телефона в Telegram (channels/phone_resolve.py) — парная к has_wa.
    # Без tg_checked_at не отличить «в TG нет» от «ещё не пробивали», и номера
    # долбились бы по кругу — а лишний ImportContacts это спам-сигнал.
    "has_tg": "TEXT",           # есть ли Telegram по номеру: yes|no|unknown
    "tg_checked_at": "TEXT",    # когда пробивали номер в TG
    "tg_checked_by": "INTEGER", # каким аккаунтом пробивали (accounts.id) — для разбора банов
    "agent_context": "TEXT",    # ручной контекст для агента (история/нюансы общения с этим лидом)
    "pipeline_id": "INTEGER",   # в какой воронке лид (NULL = дефолтная)
    "company_id": "INTEGER",    # юрлицо, к которому привязан контакт (companies.id)
    # --- H1: AI-досье физлица по сообщениям из чатов (enrich_person.py) ---
    "pains": "TEXT",            # боли (что мешает/беспокоит)
    "fears": "TEXT",            # страхи/риски, которых избегает
    "desires": "TEXT",          # желания/цели
    "interests": "TEXT",        # темы/интересы (через «; »)
    "psychotype": "TEXT",       # психотип/тип принятия решений
    "comm_style": "TEXT",       # стиль общения (как с ним лучше говорить)
    "best_time": "TEXT",        # оптимальное время для контакта
    "score": "REAL",            # AI-скоринг релевантности 0..1 (на скрине Дениса 0.90)
    "segment": "TEXT",          # авто-сфера/сегмент (IT/бизнес/маркетинг/…)
    "quotes": "TEXT",           # 1-3 показательные цитаты из чатов
    "rec_message": "TEXT",      # рекомендуемое первое сообщение (готовый крючок)
    "photo_analysis": "TEXT",   # анализ аватара (дресс-код/возраст/статус) — этап 4
    "confidence": "REAL",       # достоверность портрета 0..1 («85% по профилю»)
    "web_note": "TEXT",         # обогащение из соцсетей/веба с пометкой «не подтверждено»
    "is_test": "INTEGER DEFAULT 0",  # свой тестовый номер — встаёт первым в очереди кампании
    # --- карточка человека (bэклог P0): идентификация ---
    "gender": "TEXT",           # male|female — угадан по имени (channels/ru_names.gender_of)
    "is_premium": "INTEGER",    # Telegram Premium: 1/0/NULL=неизвестно (виден при парсинге User-сущности)
    "has_photo": "INTEGER DEFAULT 0",  # 1 = аватар скачан в data/avatars/{tg_user_id}.jpg (для карточки)
}


# Поля сделок (deals как воронка Битрикс, а не только встречи).
_EXTRA_DEAL_COLS = {
    "title": "TEXT",            # название сделки
    "pipeline_id": "INTEGER",   # воронка (NULL = дефолтная)
    "company_id": "INTEGER",    # юрлицо сделки
    "product": "TEXT",          # продукт/услуга
    "amount": "REAL",           # сумма сделки
    "updated_at": "TEXT",
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
    "kp_file": "TEXT",         # имя прикреплённого файла КП (data/kp/...), агент шлёт файлом
    # --- экономика/ROI кампании ---
    "goal_start": "TEXT",          # цель на старте
    "result_note": "TEXT",         # факт/результат (заметка)
    "cost_proxy": "REAL",          # прокси, ₽/мес
    "cost_accounts": "REAL",       # аккаунты/SIM, ₽/мес
    "cost_ai": "REAL",             # ИИ/Claude, ₽/мес
    "cost_other": "REAL",          # прочее (сервер и т.п.), ₽/мес
    "revenue_per_deal": "REAL",    # доход со сделки, ₽
    "manager_salary": "REAL",      # ЗП живого менеджера, ₽/мес (для сравнения)
    "manager_leads": "REAL",       # сколько лидов даёт живой менеджер, шт/мес
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
    "avatar": "TEXT",                     # имя файла аватара (data/avatars/...)
    "description": "TEXT",                # описание профиля агента (для команды)
    "api_id": "INTEGER",                  # собственные api_id/api_hash аккаунта (для купленных
    "api_hash": "TEXT",                   # сессий — используем их, а не глобальные из .env)
    "protected": "INTEGER DEFAULT 0",     # «родной» личный номер — НЕ трогать автоматикой (прогрев/рассылка)
    "chats_backup": "TEXT",               # резерв чатов аккаунта (JSON: список {title,link,note}) на случай бана
    "kind": "TEXT",                       # происхождение: own (родной) | sim (своя симка) | bought (купленный/расходный)
    "country": "TEXT",                    # страна аккаунта, ISO2 (авто по коду номера: ru|kz|uz|... ) — для гео-прокси
    "bought_at": "TEXT",                  # дата покупки на маркете (для оценки живучести: «жив N дней»)
    "tg_name": "TEXT",                    # чистое «Имя Фамилия» для профиля Telegram (без цифр — не путать
                                           # с label, который может быть внутренним ярлыком вида «Василий928»)
    "proxy_alive": "INTEGER",             # живость ТЕКУЩЕГО proxy: 1=жив, 0=мёртв, NULL=не проверялся
    "proxy_checked_at": "TEXT",           # когда последний раз проверяли живость прокси
    # Живость САМОЙ TG-сессии (channels/session_check.py) — не путать с proxy_alive (канал)
    # и spam_status (ограничения у живого аккаунта). NULL у alive = «не смогли проверить»,
    # это НЕ «мёртв»: не достучались (мёртвый прокси) — про сессию не судим.
    "session_alive": "INTEGER",           # 1=жив, 0=мёртв (отозвана/бан), NULL=не проверялся/нет связи
    "session_state": "TEXT",              # alive|revoked|banned|noconn|nosess
    "session_reason": "TEXT",             # человекочитаемая причина вердикта
    "session_checked_at": "TEXT",         # когда последний раз проверяли живость сессии
    # Облачный пароль (2FA). СЕКРЕТ — как и tg_session, БД не публикуй.
    # Смысл: у купленного аккаунта номер остаётся у продавца, и он в любой момент входит
    # по SMS и сносит наши сессии («разлогинен»). 2FA этому мешает: одного кода мало.
    # Пароль ОБЯЗАН лежать здесь — забыли пароль = потеряли аккаунт при первом же релогине.
    "tg_2fa": "TEXT",
    "tg_2fa_set_at": "TEXT",              # когда поставили (NULL = 2FA не наша/не ставили)
}


# Связь кампания↔контакт: с какого аккаунта отправлено (для прогресса по номерам).
_EXTRA_CAMPAIGN_CONTACT_COLS = {
    "account_id": "INTEGER",
}


# Поля каталога чатов (добавляются миграцией к уже созданной таблице chats).
_EXTRA_CHAT_COLS = {
    "can_write": "TEXT",         # да|только админы|ограничено|заблокирован|не вступил
    "members_visible": "TEXT",   # да|нет
    "in_account": "TEXT",        # yes = чат уже в личном аккаунте
    "city": "TEXT",
    "kw_last_id": "INTEGER",     # watermark: до какого msg_id уже сканировали по ключам
    "favorite": "INTEGER DEFAULT 0",   # ⭐ избранный чат — лучшие, по ним и слушаем в первую очередь
    # СЫРОЙ telegram-id чата (entity.id, БЕЗ приставки -100). Именно его пишет парсер
    # в tg_user_posts.chat_id — по нему и связываем «сырьё досье» с карточкой каталога.
    # NB: chats.id (каталожный) и tg_chat_id — РАЗНЫЕ вещи, не путать при JOIN'ах.
    "tg_chat_id": "INTEGER",
    "members_access": "TEXT",    # открыт|ограничен|скрыт|закрыт|неизвестно (детальнее members_visible)
    "can_export_all": "TEXT",    # да|частично (>10k, TG отдаёт лимит)|нет
    "summary": "TEXT",           # AI-описание: что это за чат, о чём (Claude по выборке сообщений)
    "enriched_at": "TEXT",       # когда чат обогащён ИИ
    # ─── Ось «одобрено» (НЕ путать со status!) ───────────────────────────────────
    # status  = стадия РОБОТА:   new → analyzed → joined (кто где в конвейере);
    # verdict = решение ХОЗЯИНА: годен ли чат для работы. Раньше оси были склеены в
    # status, и «analyzed» читали как «одобрен», хотя это лишь «робот прочитал».
    # Рассылка/прослушка должны брать ТОЛЬКО verdict='годен'.
    "verdict": "TEXT",           # годен|не годен|на проверку|мёртвый (NULL = ещё не решали)
    "verdict_at": "TEXT",        # когда поставлен вердикт
    "verdict_src": "TEXT",       # кто поставил: ai (предварительно) | человек (окончательно)
    "scan_error": "TEXT",        # почему скан не удался (UsernameInvalidError и т.п.) — для verdict='мёртвый'
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
    deal = {r["name"] for r in conn.execute("PRAGMA table_info(deals)")}
    for col, typ in _EXTRA_DEAL_COLS.items():
        if col not in deal:
            conn.execute(f"ALTER TABLE deals ADD COLUMN {col} {typ}")
    _relax_deals_contact_notnull(conn)
    chat = {r["name"] for r in conn.execute("PRAGMA table_info(chats)")}
    if chat:  # таблица существует
        for col, typ in _EXTRA_CHAT_COLS.items():
            if col not in chat:
                conn.execute(f"ALTER TABLE chats ADD COLUMN {col} {typ}")


def _relax_deals_contact_notnull(conn: sqlite3.Connection) -> None:
    """Снимает NOT NULL с deals.contact_id (сделка может быть только на компанию).
    SQLite не умеет ALTER COLUMN — пересобираем таблицу, сохраняя все данные."""
    info = list(conn.execute("PRAGMA table_info(deals)"))
    cn = next((r for r in info if r["name"] == "contact_id"), None)
    if not cn or not cn["notnull"]:
        return
    coldefs = []
    for r in info:
        if r["name"] == "id":
            coldefs.append('"id" INTEGER PRIMARY KEY AUTOINCREMENT')
            continue
        d = f'"{r["name"]}" {r["type"] or ""}'.rstrip()
        if r["name"] != "contact_id" and r["notnull"]:
            d += " NOT NULL"
        if r["dflt_value"] is not None:
            d += f' DEFAULT ({r["dflt_value"]})'
        coldefs.append(d)
    names = ", ".join(f'"{r["name"]}"' for r in info)
    conn.execute(f"CREATE TABLE deals_new ({', '.join(coldefs)})")
    conn.execute(f"INSERT INTO deals_new ({names}) SELECT {names} FROM deals")
    conn.execute("DROP TABLE deals")
    conn.execute("ALTER TABLE deals_new RENAME TO deals")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


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


def get_default_pipeline_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT id FROM pipelines ORDER BY is_default DESC, id LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def _extract(pattern: str, text: str | None) -> str | None:
    import re
    if not text:
        return None
    m = re.search(pattern, text)
    return m.group(0) if m else None


def _migrate_companies(conn: sqlite3.Connection) -> None:
    """Одноразово: из каждого контакта (агентства) создаём Компанию (юрлицо) и
    связываем contacts.company_id. Запускается, только если companies пуста."""
    have = conn.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
    if have:
        return
    rows = conn.execute(
        "SELECT id, name, agency, city, phone, inn, ogrn, founders, tags, notes FROM contacts"
    ).fetchall()
    for r in rows:
        cname = (r["agency"] or r["name"] or "").strip() or "Без названия"
        ctype = "ИП" if "ИП " in (" " + cname) or cname.startswith("ИП") else "ООО"
        notes = r["notes"] or ""
        email = _extract(r"[\w.+-]+@[\w-]+\.[\w.-]+", notes)
        vk = _extract(r"https?://[^\s|]*vk\.com[^\s|]*", notes)
        site = None
        import re
        for u in re.findall(r"https?://[^\s|]+", notes):
            if "vk.com" not in u:
                site = u
                break
        cur = conn.execute(
            "INSERT INTO companies (name, company_type, city, phone, site, email, vk, "
            "inn, ogrn, founders, tags, notes, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active')",
            (cname, ctype, r["city"], r["phone"], site, email, vk,
             r["inn"], r["ogrn"], r["founders"], r["tags"], notes or None),
        )
        conn.execute("UPDATE contacts SET company_id=? WHERE id=?", (cur.lastrowid, r["id"]))


def _migrate_deals(conn: sqlite3.Connection) -> None:
    """Одноразово: для «лидов с интересом» (статус не new) создаём Сделку в воронке.
    Холодная база (new) остаётся справочником — сделка появляется при работе."""
    pid = get_default_pipeline_id(conn)
    rows = conn.execute(
        "SELECT c.id, c.status, c.company_id, COALESCE(co.name, c.agency, c.name) AS title, "
        "co.company_type FROM contacts c LEFT JOIN companies co ON co.id=c.company_id "
        "WHERE c.status IS NOT NULL AND c.status NOT IN ('new') "
        "AND NOT EXISTS (SELECT 1 FROM deals d WHERE d.contact_id=c.id)"
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO deals (contact_id, company_id, pipeline_id, stage, title, "
            "product, created_at, updated_at) VALUES (?,?,?,?,?, 'Фонд доступного жилья', "
            "datetime('now'), datetime('now'))",
            (r["id"], r["company_id"], pid, r["status"], r["title"]),
        )


def _seed_default_niche(conn: sqlite3.Connection) -> None:
    """Если ниш нет — заводим дефолтную (недвижимость/ипотека)."""
    n = conn.execute("SELECT COUNT(*) c FROM niches").fetchone()["c"]
    if n == 0:
        kws = ("ищу риелтора, нужен риелтор, посоветуйте риелтора, куплю квартиру, "
               "продаю квартиру, сниму квартиру, сдаю квартиру, нужен ипотечный, "
               "ищу ипотеку, помогите с ипотекой, новостройк, вторичк, переуступк")
        conn.execute("INSERT INTO niches (name, keywords, active) VALUES (?,?,1)",
                     ("Недвижимость / ипотека", kws))


def _backfill_account_geo(conn: sqlite3.Connection) -> None:
    """Заполняет country (по коду номера) и bought_at (по дате добавления) у аккаунтов,
    где они пусты. Так страна и «жив N дней» появляются и у ранее заведённых номеров."""
    import phone_geo
    rows = conn.execute(
        "SELECT id, phone, created_at FROM accounts "
        "WHERE (country IS NULL OR country='') OR (bought_at IS NULL OR bought_at='')"
    ).fetchall()
    for r in rows:
        code = phone_geo.detect(r["phone"])
        conn.execute(
            "UPDATE accounts SET "
            "country = COALESCE(NULLIF(country,''), ?), "
            "bought_at = COALESCE(NULLIF(bought_at,''), ?) WHERE id=?",
            (code, r["created_at"], r["id"]),
        )


def init_db() -> None:
    schema = Path(config.SCHEMA_PATH).read_text(encoding="utf-8")
    with get_conn() as conn:
        conn.executescript(schema)
        _ensure_columns(conn)
        _seed_default_pipeline(conn)
        _seed_default_niche(conn)
        _migrate_companies(conn)
        _migrate_deals(conn)
        _backfill_account_geo(conn)
        # членство армии в чатах: подтягиваем уже вступленные (chats.joined_by) в
        # account_chats, чтобы отчёт покрытия сразу отражал реальность
        conn.execute(
            "INSERT OR IGNORE INTO account_chats (account_id, chat_id, can_write) "
            "SELECT joined_by, id, can_write FROM chats WHERE joined_by IS NOT NULL"
        )


def upsert_contact(conn: sqlite3.Connection, **fields) -> int:
    """Вставляет или обновляет контакт по phone/username. Возвращает id."""
    phone = fields.get("phone")
    username = fields.get("username")
    row = None
    if phone:
        row = conn.execute("SELECT id FROM contacts WHERE phone = ?", (phone,)).fetchone()
    if row is None and username:
        row = conn.execute("SELECT id FROM contacts WHERE username = ?", (username,)).fetchone()

    cols = ["source", "phone", "username", "tg_user_id", "name", "city", "agency", "tags", "notes",
            "gender", "is_premium"]
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


def resolve_catalog_chat(conn: sqlite3.Connection, tg_chat_id: int | None,
                         title: str | None = None, username: str | None = None) -> int | None:
    """chats.id (КАТАЛОЖНЫЙ) по сырому telegram-id чата. Ищет по tg_chat_id, затем по
    @username; если чата в каталоге ещё нет — заводит его (чат реальный: мы в нём сидим/
    слушаем, ему место в каталоге). Заодно проставляет tg_chat_id старым записям.

    Нужен потому, что chat_hits.chat_id/chats.id — каталожные, а слушатель/парсер знают
    только telegram-id. Без резолва JOIN на chats молча не находит ничего.
    """
    if tg_chat_id is None:
        return None
    row = conn.execute("SELECT id FROM chats WHERE tg_chat_id=?", (tg_chat_id,)).fetchone()
    if row:
        return row["id"]
    if username:
        row = conn.execute("SELECT id FROM chats WHERE username=?", (username,)).fetchone()
        if row:  # чат заводили по @username до появления tg_chat_id — до-заполняем
            conn.execute("UPDATE chats SET tg_chat_id=? WHERE id=?", (tg_chat_id, row["id"]))
            return row["id"]
    cur = conn.execute(
        "INSERT INTO chats (title, username, link, tg_chat_id, status) VALUES (?,?,?,?, 'new')",
        (title or username or str(tg_chat_id), username,
         f"https://t.me/{username}" if username else None, tg_chat_id),
    )
    return cur.lastrowid


def save_user_posts(conn: sqlite3.Connection, tg_user_id: int, chat_id: int | None,
                    chat_title: str | None, posts: list[tuple]) -> int:
    """Кладёт сырьё для досье (H1): сообщения человека из чата. posts: [(msg_id, ts, text), ...].
    Дедуп по (tg_user_id, chat_id, msg_id). Возвращает число новых строк."""
    new = 0
    for msg_id, ts, text in posts:
        cur = conn.execute(
            "INSERT OR IGNORE INTO tg_user_posts (tg_user_id, chat_id, chat_title, text, msg_id, ts) "
            "VALUES (?,?,?,?,?,?)",
            (tg_user_id, chat_id, chat_title, text, msg_id, ts),
        )
        new += cur.rowcount
    return new


def set_bio_by_tg(conn: sqlite3.Connection, tg_user_id: int, bio: str | None) -> None:
    """Записывает bio из TG-профиля в карточку лида (если есть и контакт найден)."""
    if not bio:
        return
    conn.execute(
        "UPDATE contacts SET bio = COALESCE(?, bio), updated_at = datetime('now') WHERE tg_user_id = ?",
        (bio, tg_user_id),
    )


def mark_photos_by_tg(conn: sqlite3.Connection, tg_user_ids) -> None:
    """Ставит has_photo=1 контактам, чей аватар скачан (файл data/avatars/{id}.jpg)."""
    ids = list(tg_user_ids)
    if not ids:
        return
    qm = ",".join("?" * len(ids))
    conn.execute(f"UPDATE contacts SET has_photo=1 WHERE tg_user_id IN ({qm})", ids)


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


def add_event(conn: sqlite3.Connection, type: str, title: str, text: str | None = None,
              level: str = "info", contact_id: int | None = None,
              campaign_id: int | None = None, account_id: int | None = None) -> None:
    """Записать событие в ленту колокольчика (старт/финиш кампании, лид, бан, прогрев)."""
    conn.execute(
        "INSERT INTO events (type, level, title, text, contact_id, campaign_id, account_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (type, level, title, text, contact_id, campaign_id, account_id),
    )


def warming_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Аккаунты в прогреве, у которых есть авторизованная TG-сессия И назначен
    прокси, который не помечен мёртвым. «Родные» (protected) исключаем — их
    автоматика не трогает. Без этого гейта аккаунт без прокси коннектится
    напрямую (или через общий) — сразу несколько «разных» аккаунтов светят
    Telegram один и тот же IP, прямой путь к бану всей пачки."""
    return conn.execute(
        "SELECT * FROM accounts WHERE status='warming' AND tg_session IS NOT NULL AND tg_session<>'' "
        "AND COALESCE(protected,0)=0 AND proxy IS NOT NULL AND proxy<>'' AND COALESCE(proxy_alive,1)<>0"
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
