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
    "niche": "TEXT",            # чем занимается / род деятельности (по bio + канал)
    "offer": "TEXT",            # что ПРОДАЁТ/предлагает — оффер (из bio/канала)
    "web_note": "TEXT",         # обогащение из соцсетей/веба с пометкой «не подтверждено»
    # Ссылка на первоисточник: профиль в каталоге (hrtime/vsetreningi/…), сайт, соцсеть.
    # Держим отдельно от web_note (там ИИ-догадки) и от notes (там текст свободной формы):
    # по этой ссылке человека можно открыть и дообогатить руками или скрапером, когда
    # телефона в выгрузке не было вовсе — а таких выгрузок большинство.
    "site": "TEXT",
    # --- Обогащение ИЗ TELEGRAM: bio профиля + личный канал человека ---
    # Отдельный слой поверх обычного обогащения (agent/enrich.py читает сайт/2ГИС).
    # Здесь источник — сам Telegram: описание профиля и, если в нём есть ссылка на
    # свой канал, закреп и несколько последних постов. Именно там человек своими
    # словами говорит, ЧТО продаёт, — сайт и справочники этого не дают.
    "tg_channel": "TEXT",             # ссылка на личный канал, найденная в bio
    # Позиция в социуме: предприниматель | эксперт/тренер | наёмный | госслужащий |
    # инвестор | студент… Заводится отдельно от person_role (там должность из
    # справочника) и от segment (там сфера): для захода важно не «директор ООО», а
    # «сам себе хозяин» против «работает на дядю» — это разные разговоры.
    "social_role": "TEXT",
    "tg_enriched_at": "TEXT",         # когда прошло обогащение из Telegram (NULL = не проходило)
    "tg_enrich_note": "TEXT",         # что именно удалось прочитать (bio / канал / сколько постов)
    "email": "TEXT",                  # email из импорта/обогащения
    "is_test": "INTEGER DEFAULT 0",  # свой тестовый номер — встаёт первым в очереди кампании
    "test_campaign_id": "INTEGER",   # для какой кампании он тестовый (NULL = легаси, для любой)
    # --- карточка человека (bэклог P0): идентификация ---
    "gender": "TEXT",           # male|female — угадан по имени (channels/ru_names.gender_of)
    "is_premium": "INTEGER",    # Telegram Premium: 1/0/NULL=неизвестно (виден при парсинге User-сущности)
    "has_photo": "INTEGER DEFAULT 0",  # 1 = аватар скачан в data/avatars/{tg_user_id}.jpg (для карточки)
    # Горячий лид: готов созвониться ПРЯМО СЕЙЧАС, не в назначенное время (см. agent.Reply.hot).
    # NOT NULL = ждём, что оператор возьмёт трубку/напишет сам; если за HOT_LEAD_TIMEOUT_MIN
    # (см. web/app.py) ничего не изменилось — бот сам мягко закрывает разговор и чистит поле.
    "hot_since": "TEXT",
    # Корзина: удаление из UI больше не стирает карточку сразу — ставит эту метку.
    # NULL = контакт активен (виден везде, участвует в рассылке/пробиве). Не NULL —
    # в корзине: пропадает из обычных списков и явно исключён из _audience()
    # (channels/campaign_send.py) и _targets() (channels/phone_resolve.py), поэтому
    # выделенный по ошибке человек не получит сообщение, пока висит в корзине.
    # Настоящее удаление — только /api/contacts/purge, и только уже для тех, кто здесь.
    "deleted_at": "TEXT",
}

# Поля компаний, добавляемые миграцией
_EXTRA_COMPANY_COLS = {
    "source": "TEXT DEFAULT 'import'",  # откуда импортирована компания
    "director_inn": "TEXT",            # ИНН руководителя (отдельное поле в выгрузках)
    "director_name": "TEXT",     # ФИО руководителя
    "director_phone": "TEXT",    # Телефон директора
    "director_email": "TEXT",    # Email директора
    "director_role": "TEXT",     # Должность руководителя
    "kpp": "TEXT",               # КПП
    "registration_date": "TEXT", # Дата регистрации
    "employee_count": "INTEGER", # Количество сотрудников
    "revenue": "REAL",           # Выручка, тыс. руб
    "profit": "REAL",            # Чистая прибыль/убыток, тыс. руб
    "balance": "REAL",           # Баланс, тыс. руб
    "arbitration": "REAL",       # Арбитраж (ответчик), тыс. руб
    "licenses": "TEXT",          # Полученные лицензии
    "main_activity": "TEXT",     # Основной вид деятельности
    "other_activities": "TEXT",  # Другие виды деятельности
    "procurement_codes": "TEXT", # Предметы закупок (ОКПД2)
    "region": "TEXT",            # Регион регистрации
    "sme_category": "TEXT",      # Категория МСП
    "lessee": "INTEGER DEFAULT 0", # Лизингополучатель
}


# Поля проектов: с кем именно идёт работа (компания ИЛИ человек — не оба сразу) и
# что считается успехом. Без этого карточка проекта — просто название с описанием,
# а проект заводят ровно затем, чтобы он стал кейсом.
_EXTRA_PROJECT_COLS = {
    "client_company_id": "INTEGER",  # клиент — юрлицо (companies.id)
    "client_contact_id": "INTEGER",  # клиент — физлицо (contacts.id)
    "goal": "TEXT",                  # цель проекта
    "ideal_result": "TEXT",          # как выглядит идеальный результат / будущий кейс
    "deadline": "TEXT",              # к какому сроку (YYYY-MM-DD)
}


# Поля сделок (deals как воронка Битрикс, а не только встречи).
_EXTRA_DEAL_COLS = {
    "title": "TEXT",            # название сделки
    "pipeline_id": "INTEGER",   # воронка (NULL = дефолтная)
    "company_id": "INTEGER",    # юрлицо сделки
    "product": "TEXT",          # продукт/услуга
    "amount": "REAL",           # сумма сделки
    "updated_at": "TEXT",
    "archived": "INTEGER DEFAULT 0",  # флаг, а не стадия — воронка настраивается
                                       # пользователем, архив от её стадий не зависит
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
    "archived": "INTEGER DEFAULT 0",  # в архиве — не мешает в основном списке, но не удалена
    # 1 — слать ТОЛЬКО тем, кого достанем без ImportContacts (см. TG_REACHABLE_SQL).
    # По умолчанию включено: непробитый номер в рассылке = ImportContacts в момент
    # выстрела, а промах по нему («номера нет в TG») — самый явный признак спамера.
    "tg_verified_only": "INTEGER DEFAULT 1",
    # --- своя «переговорка» и свой ответственный у КАЖДОЙ кампании ---
    # Кампании ведут разные продукты и разных людей: у одной созвон в Телемосте и
    # уведомления Василию, у другой — Zoom и другой менеджер. Общие настройки пульта
    # остаются запасным вариантом: пусто в кампании → берём общие (см. meetings и notify).
    "agent_model": "TEXT",             # какая модель ведёт диалоги ЭТОЙ кампании
    "meeting_url": "TEXT",             # постоянная комната именно этой кампании
    "notify_target": "TEXT",           # кому в личку падает «договорились о встрече»
    "notify_account_id": "INTEGER",    # с какого аккаунта уходит это уведомление
    # Доп. шаг дожима молчунов ИМЕННО этой кампании (см. scheduler.FOLLOWUP_TEMPLATES).
    # Общий список шаблонов один на все кампании — оффер ИЖС-застройщикам и оффер
    # нетворкинга с экспертами звучат по-разному, а дожим один и тот же был бы для
    # обоих. Пусто → кампания дожимается только общими шаблонами, как раньше.
    "extra_followup_template": "TEXT",
    # --- рабочие часы кампании: когда боту можно писать/отвечать живым людям ---
    # Ночная рассылка и полуночный ответ — то, что моментально выдаёт бота и пугает
    # людей. Пусто во всех трёх = ограничений нет (как раньше, ничего не ломаем).
    "work_hours_tz": "TEXT",       # IANA-зона, напр. Europe/Moscow; пусто = UTC
    "work_hours_start": "TEXT",    # "09:00"
    "work_hours_end": "TEXT",      # "20:00"
}


# ЕДИНСТВЕННЫЙ источник правды «этого контакта достанем БЕЗ ImportContacts».
# Два способа: @username (get_entity, ни одного добавления в книгу) или уже известный
# tg_user_id (номер пробит заранее дозированным phone_resolve).
#
# Важно, чего здесь НЕТ: has_tg='yes'. Этот флаг импортёр 2ГИС ставит по одному лишь
# наличию ссылки t.me в карточке — это ДОГАДКА, без единого запроса в Telegram. У 14
# контактов в базе has_tg='yes' при пустых username и tg_user_id: поверив флагу, мы
# пошли бы их резолвить прямо во время рассылки — ровно то, от чего защищаемся.
TG_REACHABLE_SQL = "((username IS NOT NULL AND username<>'') OR tg_user_id IS NOT NULL)"


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
    # Саморегистрация через SMS-сервис (channels/phone_register.py, ТЗ hero-sms).
    "reg_source": "TEXT",                 # откуда аккаунт: hero-sms | bought | own
    "reg_activation_id": "TEXT",          # id активации у SMS-сервиса (для разбора/возвратов)
    "reg_cost": "REAL",                   # сколько стоила активация, $ (для экономики/ROI)
    # Защита купленных аккаунтов от реклейма (channels/account_protect.py):
    # 2FA закрывает вход по SMS сразу; смена номера на свой — единственное, что
    # полностью отвязывает продавца (см. память account-reclaim-2fa).
    # Своя страница КП у КАЖДОГО аккаунта (channels/kp_pages.py). Одна общая ссылка на всю
    # армию — готовый общий след: по нему аккаунты связываются в одну группу, и бан одного
    # тянет остальных. Плюс единая точка отказа: страницу репортят — умирают все разом.
    "kp_link": "TEXT",                    # https://telegra.ph/... персональная страница аккаунта
    "kp_token": "TEXT",                   # токен Telegraph для правки ЭТОЙ страницы (без него не отредактировать)
    "protect_stage": "TEXT",              # NULL | phone_pending | phone_ok
    "protect_phone_activation_id": "TEXT",  # id активации hero-sms на НОВЫЙ номер, пока смена не завершена
    "protect_last_try_at": "TEXT",        # когда последний раз пробовали сменить номер (троттлинг retry)
    "protect_note": "TEXT",               # человекочитаемый статус/причина последнего исхода
    # Запасная (холодная) сессия — channels/session_spare.py. СЕКРЕТ, как tg_session.
    # Это ВТОРАЯ независимая авторизация Telegram (свой ключ), а НЕ копия основной:
    # копия — тот же ключ, и поднятая параллельно она сжигает аккаунт, а не страхует.
    # Смысл: купленный аккаунт разовый, SMS нам не придёт, и потеря ключа = списание
    # в убыток (так сгорело 6 лотов). Запаска переживает смерть основного ключа.
    # Держим ХОЛОДНОЙ: не подключать, пока жива основная (см. шапку session_spare.py).
    "tg_session_spare": "TEXT",
    "spare_made_at": "TEXT",              # когда выпущена (NULL = запаски нет)
    "spare_used_at": "TEXT",              # когда её подняли как основную (запаска израсходована)
    "spare_note": "TEXT",                 # человекочитаемый статус последнего исхода
}


# Связь кампания↔контакт: с какого аккаунта отправлено (для прогресса по номерам).
_EXTRA_CAMPAIGN_CONTACT_COLS = {
    "account_id": "INTEGER",
}


# Поля каталога чатов (добавляются миграцией к уже созданной таблице chats).
# Кто написал найденное сообщение: клиент (ищет услугу) или исполнитель (рекламирует
# себя). Одно и то же ключевое слово стоит и в запросе, и в объявлении, поэтому без
# этой пометки «Запросы» забиваются рекламой конкурентов — см. channels/hit_intent.py.
_EXTRA_HIT_COLS = {
    "intent": "TEXT",        # client | vendor | unknown
    "intent_why": "TEXT",    # причина решения — оператор должен видеть, почему так
}

# Что ниша складывает в «Запросы»: clients (по умолчанию) | vendors | all.
_EXTRA_NICHE_COLS = {
    "hunt_mode": "TEXT DEFAULT 'clients'",
}

_EXTRA_CHAT_COLS = {
    "can_write": "TEXT",         # да|только админы|ограничено|заблокирован|не вступил
    "members_visible": "TEXT",   # да|нет
    "in_account": "TEXT",        # yes = чат уже в личном аккаунте
    "city": "TEXT",
    "kw_last_id": "INTEGER",     # watermark: до какого msg_id уже сканировали по ключам
    "kw_scanned_at": "TEXT",     # когда последний раз проходили по ключам (ротация очереди)
    # Ключ доступа к чату от Telegram. Без него обратиться к каналу по id нельзя, и
    # Telethon каждый раз резолвит @username заново — отдельный сетевой запрос на чат.
    # На каталоге в 2.4 тыс. чатов это и привело к FloodWait на 13.8 часа для основного
    # аккаунта. Строковая сессия свой кэш сущностей между запусками не хранит, поэтому
    # держим ключ у себя: с ним обращение к чату не стоит ни одного лишнего запроса.
    "tg_access_hash": "TEXT",    # храним строкой: значения не влезают в SQLite INTEGER
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
    # ─── Происхождение записи: откуда чат вообще взялся ──────────────────────────
    # Раньше это писалось прозой в notes («похож на «X» (рекомендации TG)») — прочитать
    # глазами можно, отфильтровать нельзя. А вопрос «покажи все каналы, приехавшие из
    # похожих на Хартманна» — рабочий: у такой пачки одна природа и работать с ней надо
    # пачкой (вступить, разобрать, выкинуть целиком).
    "source": "TEXT",            # similar|discover|tgstat|inventory|keyword|manual
    "parent_chat_id": "INTEGER", # каталожный chats.id того, от кого приплыл (для similar)
    # Разбор качества чата (channels/chat_quality): доли рекламы и повторов, сколько
    # живых реплик и прямых запросов, монополия авторов. Держим сырые цифры, а не
    # только вердикт: оператор должен видеть, ПОЧЕМУ чат назван мусорным, иначе
    # доверия к автоматике не будет и он всё равно пойдёт смотреть руками.
    "quality_json": "TEXT",
}


# Слияние «Структуры» и «ИИ-агентов» (2026-07): должность теперь сама несёт аккаунт-
# исполнителя + (для ИИ) задачу и промпт — раньше это жило на отдельной сущности ai_agents,
# из-за чего приходилось прыгать между двумя разделами.
_EXTRA_ORG_MEMBER_COLS = {
    "account_id": "INTEGER",   # аккаунт-исполнитель (accounts.id) прямо на должности
    "task": "TEXT",            # задача ИИ-роли (leadgen|networking|inviting|…) — было в ai_agents
    "prompt": "TEXT",          # характер/инструкция ИИ-роли — было в ai_agents
}

# Отдел на схеме: у него есть руководитель (выделяется короной и поднимается наверх
# карточки) и цвет ветки — чтобы визуально разделять направления бизнеса.
_EXTRA_DEPT_COLS = {
    "head_member_id": "INTEGER",  # org_members.id руководителя отдела
    "color": "TEXT",              # hex-цвет ветки на схеме (NULL = цвет по умолчанию)
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
    co = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
    for col, typ in _EXTRA_COMPANY_COLS.items():
        if col not in co:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {typ}")
    proj = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
    for col, typ in _EXTRA_PROJECT_COLS.items():
        if col not in proj:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {typ}")
    deal = {r["name"] for r in conn.execute("PRAGMA table_info(deals)")}
    for col, typ in _EXTRA_DEAL_COLS.items():
        if col not in deal:
            conn.execute(f"ALTER TABLE deals ADD COLUMN {col} {typ}")
    _relax_deals_contact_notnull(conn)
    _repair_unverified_has_tg(conn)
    chat = {r["name"] for r in conn.execute("PRAGMA table_info(chats)")}
    if chat:  # таблица существует
        for col, typ in _EXTRA_CHAT_COLS.items():
            if col not in chat:
                conn.execute(f"ALTER TABLE chats ADD COLUMN {col} {typ}")
    hit = {r["name"] for r in conn.execute("PRAGMA table_info(chat_hits)")}
    if hit:
        for col, typ in _EXTRA_HIT_COLS.items():
            if col not in hit:
                conn.execute(f"ALTER TABLE chat_hits ADD COLUMN {col} {typ}")
    nich = {r["name"] for r in conn.execute("PRAGMA table_info(niches)")}
    if nich:
        for col, typ in _EXTRA_NICHE_COLS.items():
            if col not in nich:
                conn.execute(f"ALTER TABLE niches ADD COLUMN {col} {typ}")
    om = {r["name"] for r in conn.execute("PRAGMA table_info(org_members)")}
    if om:
        for col, typ in _EXTRA_ORG_MEMBER_COLS.items():
            if col not in om:
                conn.execute(f"ALTER TABLE org_members ADD COLUMN {col} {typ}")
    dep = {r["name"] for r in conn.execute("PRAGMA table_info(departments)")}
    if dep:
        for col, typ in _EXTRA_DEPT_COLS.items():
            if col not in dep:
                conn.execute(f"ALTER TABLE departments ADD COLUMN {col} {typ}")
    # С какого аккаунта ушло/пришло сообщение. Аккаунт был известен и раньше (слушатель
    # передаёт его в _record_incoming), но терялся при записи — и в карточке события
    # нельзя было ответить на вопрос «кто именно это написал».
    msg = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if msg and "account_id" not in msg:
        conn.execute("ALTER TABLE messages ADD COLUMN account_id INTEGER")
    # ID реального сообщения в Telegram (через запятую, если один блок в БД — это
    # несколько раздельных сообщений в живом чате). Нужен, чтобы оператор мог удалить
    # ошибочную реплику бота «для всех» кнопкой из «Диалогов» — без него Telegram
    # нечем адресовать, какое именно сообщение стирать.
    if msg and "tg_msg_id" not in msg:
        conn.execute("ALTER TABLE messages ADD COLUMN tg_msg_id TEXT")


def _repair_unverified_has_tg(conn: sqlite3.Connection) -> None:
    """has_tg='no' без отметки о проверке — не вердикт, а мусор. Возвращаем в 'unknown'.

    Инвариант: «нет в Telegram» имеет право поставить ТОЛЬКО channels/phone_resolve,
    реально спросив Telegram, и он всегда пишет вместе с вердиктом tg_checked_at
    (см. _save_absent). Значит has_tg='no' при пустом tg_checked_at физически не может
    быть результатом проверки — это след старой версии кода, писавшей вердикт вслепую.

    Цена ошибки высокая: 'no' исключает контакт из рассылки навсегда и не перепроверяется
    (_targets берёт только tg_checked_at IS NULL). 19.08.2026 на боевой базе таких
    оказалось 637 из 637 — то есть КАЖДЫЙ «нет в Telegram» был ложным. Среди них личный
    номер владельца и номера, покорёженные Excel в «3,75298E+11». Люди в Telegram есть,
    проверено вручную (@dina_dusova, @Usrist_administrator), а рассылка их не видела.

    Идемпотентно: после починки под условие не попадает ни одна строка."""
    n = conn.execute(
        "UPDATE contacts SET has_tg='unknown' "
        "WHERE has_tg='no' AND tg_checked_at IS NULL"
    ).rowcount
    if n:
        print(f"[db] снят непроверенный вердикт «нет в Telegram» с {n} контактов — "
              f"уйдут в обычную очередь пробива")


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


def in_work_hours(camp_row) -> bool:
    """Сейчас можно писать/отвечать по этой кампании? Пусто в start/end = ограничений
    нет (старое поведение). Часовой пояс — IANA-имя (Europe/Moscow); пусто = UTC.

    Окно, переходящее через полночь (start > end, напр. 20:00-02:00), тоже
    поддержано — просто two-side сравнение вместо одностороннего."""
    start = camp_row["work_hours_start"] if "work_hours_start" in camp_row.keys() else None
    end = camp_row["work_hours_end"] if "work_hours_end" in camp_row.keys() else None
    if not start or not end:
        return True
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        tz_name = camp_row["work_hours_tz"] if "work_hours_tz" in camp_row.keys() else None
        tz = ZoneInfo(tz_name) if tz_name else _dt.timezone.utc
        now = _dt.datetime.now(tz).time()
        t_start = _dt.time.fromisoformat(start)
        t_end = _dt.time.fromisoformat(end)
    except (ValueError, KeyError):
        return True  # битые настройки — не блокируем рассылку молча
    if t_start <= t_end:
        return t_start <= now <= t_end
    return now >= t_start or now <= t_end   # окно через полночь


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
    """Одноразово: из каждого контакта-АГЕНТСТВА создаём Компанию (юрлицо) и связываем
    contacts.company_id. Запускается, только если companies пуста.

    Берём лишь тех, у кого заполнено agency — это и есть форма старой базы (импорт 2ГИС
    писал туда название организации). Контакт без agency — живой человек из списка
    клиентов или из чата, и юрлицо «ООО Иванов Пётр Сергеевич» для него не заводим:
    на свежей установке (companies ещё пуста) миграция иначе размножала карточки-мусор
    после первого же импорта списка людей."""
    have = conn.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
    if have:
        return
    rows = conn.execute(
        "SELECT id, name, agency, city, phone, inn, ogrn, founders, tags, notes FROM contacts "
        "WHERE agency IS NOT NULL AND TRIM(agency) <> ''"
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


def _migrate_org_agents(conn: sqlite3.Connection) -> None:
    """Слияние «ИИ-агентов» в оргструктуру: должность сама несёт аккаунт/задачу/промпт.
    Идемпотентно — гоняется при каждом старте, повторно ничего не создаёт (маркер —
    ссылка org_members.ai_agent_id)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(org_members)")}
    if "account_id" not in cols:
        return
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_agents'").fetchone():
        return
    # 1) должностям-агентам дозаполняем аккаунт/задачу/промпт из связанного ai_agent
    conn.execute(
        "UPDATE org_members SET "
        "account_id=COALESCE(account_id,(SELECT account_id FROM ai_agents WHERE id=org_members.ai_agent_id)), "
        "task=COALESCE(task,(SELECT task FROM ai_agents WHERE id=org_members.ai_agent_id)), "
        "prompt=COALESCE(prompt,(SELECT prompt FROM ai_agents WHERE id=org_members.ai_agent_id)) "
        "WHERE kind='agent' AND ai_agent_id IS NOT NULL"
    )
    # 2) осиротевшие ИИ-агенты (не привязаны ни к одной должности) → заводим должность,
    #    чтобы при слиянии ничего не потерялось
    orphans = conn.execute(
        "SELECT * FROM ai_agents WHERE COALESCE(active,1)=1 AND id NOT IN "
        "(SELECT ai_agent_id FROM org_members WHERE ai_agent_id IS NOT NULL)"
    ).fetchall()
    if orphans:
        dept = conn.execute("SELECT id FROM departments ORDER BY sort_order, id LIMIT 1").fetchone()
        dept_id = dept["id"] if dept else conn.execute(
            "INSERT INTO departments (name, description) VALUES ('ИИ-агенты','Виртуальные роли')"
        ).lastrowid
        for a in orphans:
            conn.execute(
                "INSERT INTO org_members (department_id, kind, name, role, ai_agent_id, "
                "account_id, task, prompt) VALUES (?, 'agent', ?, ?, ?, ?, ?, ?)",
                (dept_id, a["name"], a["task"] or "", a["id"], a["account_id"], a["task"], a["prompt"]),
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
        _migrate_org_agents(conn)
        _backfill_account_geo(conn)
        # членство армии в чатах: подтягиваем уже вступленные (chats.joined_by) в
        # account_chats, чтобы отчёт покрытия сразу отражал реальность
        conn.execute(
            "INSERT OR IGNORE INTO account_chats (account_id, chat_id, can_write) "
            "SELECT joined_by, id, can_write FROM chats WHERE joined_by IS NOT NULL"
        )


def _merge_tags(old: str | None, new: str | None) -> str | None:
    """Дописывает новые теги к старым, без повторов. None (нечего добавить) — чтобы
    COALESCE в UPDATE оставил старое значение нетронутым."""
    if not new:
        return None
    have = [t.strip() for t in (old or "").split(",") if t.strip()]
    for t in (x.strip() for x in new.split(",")):
        if t and t not in have:
            have.append(t)
    return ", ".join(have)


def upsert_contact(conn: sqlite3.Connection, **fields) -> int:
    """Вставляет или обновляет контакт. Возвращает id.

    Ключ дедупа — tg_user_id (стабильный, человек его не меняет), затем phone, затем
    username (с «@» и без). tg_user_id ищется ПЕРВЫМ: у человека из чата часто нет ни
    телефона, ни @username — раньше обе ветки поиска пропускались и каждая новая находка
    по тому же человеку молча заводила ещё одну карточку.
    """
    phone = fields.get("phone")
    username = fields.get("username")
    tg_user_id = fields.get("tg_user_id")
    row = None
    if tg_user_id:
        row = conn.execute("SELECT id, tags FROM contacts WHERE tg_user_id = ?",
                           (tg_user_id,)).fetchone()
    if row is None and phone:
        row = conn.execute("SELECT id, tags FROM contacts WHERE phone = ?", (phone,)).fetchone()
    if row is None and username:
        u = username.lstrip("@")
        row = conn.execute("SELECT id, tags FROM contacts WHERE username = ? OR username = ?",
                           (u, "@" + u)).fetchone()
    # Последняя попытка — по имени, и ТОЛЬКО если вызывающий явно попросил (match_name=True;
    # это делают импортёры файлов, но не парсеры чатов, где тёзки — норма).
    #
    # Зачем: в выгрузке людей телефона в строке может не быть вовсе, и тогда у карточки нет
    # ни одного ключа дедупа. Каждый повторный импорт того же файла заводил человеку новую
    # карточку (так в базе накопилось 129 имён-дублей), а телефон из следующей выгрузки
    # ложился на НОВУЮ карточку — старая, уже привязанная к кампании, навсегда оставалась
    # пустой и считалась «недостижимой».
    #
    # Сливаем только с «пустышкой» — карточкой без телефона и без @ника. Полную карточку
    # тёзки не трогаем: там имя уже подтверждено контактными данными, и склейка двух разных
    # людей была бы хуже дубля.
    if row is None and fields.get("match_name"):
        nm = (fields.get("name") or "").strip()
        if nm:
            row = conn.execute(
                "SELECT id, tags FROM contacts WHERE lower(trim(name)) = lower(trim(?)) "
                "AND COALESCE(phone,'') = '' AND COALESCE(username,'') = '' "
                "ORDER BY id LIMIT 1", (nm,)).fetchone()

    # specialization — чем человек занимается. Импортёр и парсер это поле передавали,
    # а сюда оно не попадало: белого списка колонок оно не проходило и молча терялось.
    # Без него не работает главный ход холодного захода («вы тоже по этой теме?» →
    # «тогда мы коллеги») — агент читает деятельность именно отсюда.
    # niche — развёрнутое «чем занимается» (колонка «Описание» в выгрузках). Ровно та же
    # история, что была со specialization: импортёр это поле передавал, а белый список его
    # не пропускал, и текст терялся. Агент читает niche из карточки
    # (channels/telegram._contact_dict), а meetings.arrange подставляет его в приглашение,
    # когда короткой специализации нет.
    cols = ["source", "phone", "username", "tg_user_id", "name", "city", "agency", "tags", "notes",
            "gender", "is_premium", "email", "person_name", "person_role", "specialization",
            "niche", "site"]
    vals = {c: fields.get(c) for c in cols}
    # username пишем ВСЕГДА без «@»: раз поиск умеет оба вида, значит легаси-строки с
    # «@vasya» в базе есть. Записав «vasya» рядом, мы получили бы две неотличимые для
    # UNIQUE строки на одного человека — то есть вечный дубль вместо дедупа.
    if vals["username"]:
        vals["username"] = vals["username"].lstrip("@")

    if row:
        # «где замечен» накапливаем: один человек приходит из нескольких чатов/источников,
        # а COALESCE(?, tags) затирал прошлый след последним вызовом.
        vals["tags"] = _merge_tags(row["tags"], vals["tags"])
        # source — откуда карточка появилась ПЕРВЫЙ раз, не откуда её видели в последний
        # раз. На обновлении не трогаем: без этого повторный импорт другого файла, где
        # тот же человек совпал по имени/телефону, тихо переписывал историю — так 49
        # компаний из старой выгрузки vsetreningi получили source «190826hrtime» только
        # потому, что попали в новый файл под тем же именем без единого нового поля.
        vals["source"] = None
        # phone/username — UNIQUE. Карточка, найденная по tg_user_id, может быть не той,
        # что держит этот номер/@ — тогда запись упала бы на IntegrityError. Чужое не трогаем.
        # Для username проверяем оба написания — иначе «@vasya» у соседа проскочит мимо.
        if vals["phone"] and conn.execute(
                "SELECT 1 FROM contacts WHERE phone = ? AND id <> ?",
                (vals["phone"], row["id"])).fetchone():
            vals["phone"] = None
        if vals["username"] and conn.execute(
                "SELECT 1 FROM contacts WHERE (username = ? OR username = ?) AND id <> ?",
                (vals["username"], "@" + vals["username"], row["id"])).fetchone():
            vals["username"] = None
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
    # status='auto' — чат завёлся САМ (слушатель поймал в нём ключ), человек его не выбирал.
    # 'new' здесь нельзя: chat_join берёт в кандидаты всё, кроме skip/banned, и армия
    # начала бы вступать в случайные группы, куда наши аккаунты добавили без спроса.
    cur = conn.execute(
        "INSERT INTO chats (title, username, link, tg_chat_id, status) VALUES (?,?,?,?, 'auto')",
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


def add_message(conn: sqlite3.Connection, contact_id: int, direction: str, text: str,
                intent: str | None = None, account_id: int | None = None,
                tg_msg_ids: list[int] | None = None) -> None:
    tg_msg_id = ",".join(str(i) for i in tg_msg_ids) if tg_msg_ids else None
    conn.execute(
        "INSERT INTO messages (contact_id, direction, text, intent, account_id, tg_msg_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (contact_id, direction, text, intent, account_id, tg_msg_id),
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


def pause_campaign_contacts(conn: sqlite3.Connection, campaign_id: int, contact_ids: list[int]) -> None:
    """Поставить контакты на паузу ИМЕННО в этой кампании — рассылка их пропустит,
    остальные из очереди продолжат слаться (частичная пауза, без общего Стопа)."""
    conn.executemany(
        "INSERT OR IGNORE INTO campaign_paused_contacts (campaign_id, contact_id) VALUES (?,?)",
        [(campaign_id, cid) for cid in contact_ids],
    )


def unpause_campaign_contacts(conn: sqlite3.Connection, campaign_id: int, contact_ids: list[int]) -> None:
    conn.executemany(
        "DELETE FROM campaign_paused_contacts WHERE campaign_id=? AND contact_id=?",
        [(campaign_id, cid) for cid in contact_ids],
    )


def paused_contact_ids(conn: sqlite3.Connection, campaign_id: int) -> set[int]:
    return {r["contact_id"] for r in conn.execute(
        "SELECT contact_id FROM campaign_paused_contacts WHERE campaign_id=?", (campaign_id,)
    ).fetchall()}


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


def hit_is_repost(conn: sqlite3.Connection, tg_user_id: int | None, text: str | None,
                  days: int = 30) -> bool:
    """Этот же автор уже постил ровно этот текст за последние `days`?

    Уникальность chat_hits стоит по (chat_id, source_msg_id), а рассыльщик публикует
    один и тот же прайс по кругу — каждый раз с новым msg_id. На живой базе это дало
    очередь из 252 «находок», где восемь подряд оказались одним и тем же объявлением
    RAVEN STUDIO. Оператору такое показывать нечего: первый пост он уже видел.

    Сравниваем по тексту, а не по хэшу картинки: репост приходит слово в слово, а
    границы обрезки (500 символов) у обоих путей записи одинаковые.
    """
    t = (text or "").strip()
    if not tg_user_id or not t:
        return False
    row = conn.execute(
        "SELECT 1 FROM chat_hits WHERE tg_user_id=? AND text=? "
        f"AND created_at >= datetime('now','-{int(days)} days') LIMIT 1",
        (tg_user_id, t[:500]),
    ).fetchone()
    return row is not None


def add_campaign_log(conn: sqlite3.Connection, campaign_id: int, status: str,
                     contact_id: int | None = None, account_id: int | None = None,
                     detail: str | None = None) -> None:
    """Записать строку лога отправки кампании (видно в карточке кампании)."""
    conn.execute(
        "INSERT INTO campaign_logs (campaign_id, contact_id, account_id, status, detail) "
        "VALUES (?,?,?,?,?)",
        (campaign_id, contact_id, account_id, status, detail),
    )


def campaign_log_summary(conn: sqlite3.Connection, campaign_id: int) -> dict:
    """Сводка лога кампании: сколько отправлено, пропущено, ошибок."""
    total = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM campaign_logs WHERE campaign_id=? GROUP BY status",
        (campaign_id,),
    ).fetchall()
    return {r["status"]: r["cnt"] for r in total}


def get_campaign_logs(conn: sqlite3.Connection, campaign_id: int, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT l.*, c.name AS contact_name, c.phone AS contact_phone, a.label AS account_label "
        "FROM campaign_logs l "
        "LEFT JOIN contacts c ON c.id = l.contact_id "
        "LEFT JOIN accounts a ON a.id = l.account_id "
        "WHERE l.campaign_id=? ORDER BY l.id DESC LIMIT ?",
        (campaign_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


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
