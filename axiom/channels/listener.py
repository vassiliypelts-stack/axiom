"""Многоаккаунтный слушатель входящих Telegram.

Держит подключёнными СРАЗУ все боевые/прогреваемые аккаунты (а не только главный из
.env) и на каждое входящее сообщение от известного контакта:
  • ВСЕГДА пишет его в книжку → сразу видно в разделе «Диалоги»;
  • авто-отвечает ИИ-агентом ТОЛЬКО с активных аккаунтов (прогреваемые молчат — чтобы
    не спалиться раньше времени и не зациклиться на взаимном прогреве между аккаунтами).

Незнакомые отправители (в т.ч. сообщения прогрева между самими аккаунтами) молча
игнорируются — они не в таблице контактов.

Запуск:
  • автоматически из веб-пульта (фоновый поток при старте сервера, start_in_thread);
  • вручную:  python -m channels.listener
"""
from __future__ import annotations

import asyncio
import datetime
import logging

from telethon import events
from telethon.sessions import StringSession
from telethon.tl.types import User

import config
from channels import antiban
from channels.telegram import _agent_reply, _record_incoming, build_client
from db import database

# Дохлые прокси у прогреваемых аккаунтов заваливают консоль сервера простынёй
# «Attempt N at connecting failed…» (внутренние ретраи Telethon). Сервер от этого не
# падает, но выглядит «сломанным». Глушим этот шум — осмысленные строки пишем сами.
logging.getLogger("telethon").setLevel(logging.CRITICAL)

_LOG = config.DB_PATH.parent / "logs" / "listener.log"

CLIENTS: dict[int, object] = {}                 # acc_id -> подключённый TelegramClient
_LOOP: "asyncio.AbstractEventLoop | None" = None  # event loop потока слушателя (для shutdown)
STATUS: dict = {"started": None, "accounts": {}, "hits": 0, "enabled": True}  # снимок для веб-статуса
# [(niche_id, [ключи], режим охоты), ...] — кэш ниш. Режим лежит рядом с ключами,
# чтобы решение «нужна ли эта находка» принималось без похода в базу на каждое
# сообщение чата (их бывают сотни в минуту).
_NICHES: list[tuple[int | None, list[str], str]] = []
# contact_id -> замок «по этому диалогу уже готовится ответ». Человек часто дробит
# мысль на два-три сообщения подряд, и без замка каждое поднимало свою генерацию:
# вживую пришли два почти одинаковых приветствия. Держим в памяти процесса —
# слушатель один, переживать перезапуск такому состоянию не нужно.
_REPLY_LOCKS: dict[int, "asyncio.Lock"] = {}

CONNECT_TIMEOUT = 15    # сек на подключение одного аккаунта (дохлый прокси не повесит всё)
RECHECK_SEC = 30        # как часто пере-сканировать: новые логины / отвалившиеся / быстрый фейловер прокси
POLL_SEC = 5            # с какой дробностью проверять тумблер «Стоп/Пуск» внутри паузы

# heal() раздаёт прокси из общего пула: два одновременных вызова выберут один и тот же
# свободный адрес. Пускаем строго по одному (см. вызов в _try).
_HEAL_LOCK = asyncio.Lock()


async def _enabled() -> bool:
    """Тумблер «слушать/не слушать» из пульта. Живёт в settings, а не в памяти процесса:
    поток демонский и переживает только рестарт сервера — намерение оператора должно
    пережить его тоже.

    Читаем в отдельном потоке: sqlite3.connect + PRAGMA busy_timeout=30000 — блокирующий
    вызов, а дёргается он раз в POLL_SEC. Прямо в event loop он на время конкуренции с
    писателями пульта морозил ВСЕХ подключённых клиентов (входящие ждали бы до 30 сек).
    """
    def _read() -> bool:
        with database.get_conn() as conn:
            return database.get_setting(conn, "listener_enabled", "on") != "off"
    return await asyncio.to_thread(_read)


def _chatscan_on() -> bool:
    """Сканировать ли ЧАТЫ по ключам ниш.

    Слушатель делает два разных дела одним подключением: ловит ответы клиентов в
    личке (это переписка кампании) и ищет ключевые слова в群 чатах (это лидген).
    Раньше их глушил один тумблер: выключаешь мониторинг чатов — вместе с ним
    перестают приходить и ответы живых людей, которым мы сами написали. Именно
    так агент и «замолчал»: слушатель стоял остановленным.

    Личка теперь не зависит от этой настройки вообще — ответы клиентов приходят
    всегда, пока слушатель поднят.
    """
    try:
        with database.get_conn() as conn:
            return database.get_setting(conn, "chatscan_enabled", "on") != "off"
    except Exception:  # noqa: BLE001 — настройка не критична, по умолчанию слушаем
        return True


def _load_niches() -> list[tuple[int | None, list[str], str]]:
    """Активные ниши (ключи + режим охоты) из БД. Пусто → слушаем только личку."""
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, keywords, COALESCE(hunt_mode,'clients') AS hunt_mode "
            "FROM niches WHERE active=1").fetchall()
    out: list[tuple[int | None, list[str], str]] = []
    for r in rows:
        kws = [k.strip().lower() for k in (r["keywords"] or "").split(",") if k.strip()]
        if kws:
            out.append((r["id"], kws, r["hunt_mode"]))
    return out


def _hunt_mode(niche_id: int | None) -> str:
    """Кого ловит эта ниша: clients | vendors | all. Берём из кэша ниш, который
    и так обновляется раз в круг supervise — лезть в базу на каждое сообщение чата
    накладно, а меняется настройка редко (галочка в интерфейсе)."""
    for nid, _kws, mode in _NICHES:
        if nid == niche_id:
            return mode or "clients"
    return "clients"


def _match_niche(text: str):
    low = text.lower()
    for nid, kws, _mode in _NICHES:
        for kw in kws:
            if kw in low:
                return nid, kw
    return None


def _display_name(u) -> str:
    name = " ".join(x for x in [getattr(u, "first_name", None),
                                getattr(u, "last_name", None)] if x).strip()
    return name or (getattr(u, "username", None) and f"@{u.username}") or str(getattr(u, "id", "?"))


def _log(msg: str) -> None:
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _should_reply(acc_id: int, contact_id: int | None = None) -> bool:
    """Авто-отвечать ли с этого аккаунта прямо сейчас.

    Правило «отвечают только 'active'» задумывалось против взаимного прогрева:
    аккаунты греются перепиской между собой, и агент не должен вести диалог сам с
    собой. Но рассылка отбирает команду по `status <> 'banned'` (campaign_send._team)
    — то есть ПИШЕТ и с прогреваемых. Человек отвечал на такое письмо и не получал
    ничего: аккаунт, который сам его и позвал, был не вправе ответить. Тишина
    выглядела как поломка агента, а на деле это расхождение двух правил.

    Поэтому: если мы этому человеку УЖЕ писали с этого аккаунта — обязаны и
    отвечать, какой бы ни была ступень прогрева. Незнакомцам и взаимному прогреву
    прежнее ограничение остаётся.

    «Родной» (protected) аккаунт — ОСОБЫЙ случай, а не частность «status='active'».
    Личный номер хозяина слушаем (см. _listenable), но ярлык «active → отвечаем
    всем» тут запрещён категорически: это переписка с мамой, друзьями, партнёрами,
    и агент не имеет права встрять в неё только потому, что случайный собеседник
    когда-то попал в таблицу контактов. Разрешаем ответ ТОЛЬКО если кампания САМА
    написала этому человеку с этого аккаунта первой — остальное чужое."""
    with database.get_conn() as conn:
        if database.get_setting(conn, "tg_auto_reply", "on") != "on":
            return False
        row = conn.execute("SELECT status, COALESCE(protected,0) protected FROM accounts "
                           "WHERE id=?", (acc_id,)).fetchone()
        if not row:
            return False
        if row["status"] == "banned" or not contact_id:
            return False
        if row["status"] == "active" and not row["protected"]:
            return True
        # мы первыми написали этому человеку с этого аккаунта — значит диалог наш,
        # родной аккаунт это условие проходит теми же средствами, без исключения
        started = conn.execute(
            "SELECT 1 FROM messages WHERE contact_id=? AND account_id=? AND direction='out' "
            "LIMIT 1", (contact_id, acc_id)).fetchone()
    return bool(started)


def _listenable() -> list[dict]:
    """Кого слушаем: есть авторизованная сессия, не забанен.

    «Родные» (protected) аккаунты — тоже здесь: 07-08.08.2026 кампания писала с
    protected-аккаунта Василия, человек отвечал, а слушатель его не подключал вовсе
    — ответ не попадал НИКУДА (ни в «Диалоги», ни агенту), до созвона дойти было
    физически нечем. Личная переписка от этого не страдает: авто-ответ агента для
    protected режется отдельно, в _should_reply, а не здесь на уровне подключения."""
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE tg_session IS NOT NULL AND tg_session<>'' "
            "AND status IN ('active','warming','paused')"
        ).fetchall()
    return [dict(r) for r in rows]


def _unknown_sender_notice(acc_id: int, sender, username: str | None, text: str) -> None:
    """Написал тот, кого нет в книжке. Наши же аккаунты (взаимный прогрев) пропускаем
    молча, живого человека — в колокольчик, не чаще раза в 15 минут на отправителя."""
    import time
    uname = (username or "").lstrip("@").lower()
    phone = (getattr(sender, "phone", "") or "").lstrip("+")
    try:
        with database.get_conn() as conn:
            if uname or phone:
                mine = conn.execute(
                    "SELECT 1 FROM accounts WHERE (LOWER(REPLACE(COALESCE(username,''),'@','')) = ? "
                    "AND ? <> '') OR (REPLACE(COALESCE(phone,''),'+','') = ? AND ? <> '')",
                    (uname, uname, phone, phone)).fetchone()
                if mine:
                    return                       # свои греются друг о друга — это норма
            key = f"unknown_sender_ts_{sender.id}"
            if (time.time() - float(database.get_setting(conn, key, "0") or 0)) < 900:
                return
            database.set_setting(conn, key, str(time.time()))
            who = f"@{username}" if username else f"id{sender.id}"
            database.add_event(
                conn, "agent_error", f"🔇 Написал незнакомец: {who}",
                f"«{text[:160]}»\n\nЭтого человека нет в книжке, поэтому диалог не заведён и агент "
                f"не отвечает. Если это твой тест — добавь номер в контакты кампании; если живой "
                f"лид — заведи карточку и ответь вручную.",
                level="warn", account_id=acc_id)
    except Exception:  # noqa: BLE001 — уведомление не должно ронять слушатель
        pass


async def _handle_private(event, acc_id: int) -> None:
    """Личка: ответ известного контакта → в «Диалоги» (+ авто-ответ с активных)."""
    sender = await event.get_sender()
    username = getattr(sender, "username", None)
    text_in = (event.raw_text or "").strip()
    if not text_in:
        return
    with database.get_conn() as conn:
        contact = database.find_contact_by_tg(
            conn, tg_user_id=int(sender.id), username=username)
    if contact is None:
        # Незнакомец. Обычно это взаимный прогрев (наши же аккаунты пишут друг другу) —
        # такое молчим. А вот живой человек, которого нет в книжке, — это потерянный
        # ответ: он писал НАМ, а мы не показали его нигде. Раньше обе ситуации молча
        # выходили через один return, и «мне ответили, а в пульте пусто» было
        # невозможно отличить от поломки.
        _unknown_sender_notice(acc_id, sender, username, text_in)
        return
    _record_incoming(contact["id"], text_in, username, account_id=acc_id)
    _log(f"[#{acc_id}] ← {username or sender.id}: {text_in[:60]!r} (сохранено в Диалоги)")
    # НОЧЬЮ ЖИВЫМ ЛЮДЯМ НЕ ПИШЕМ (09:00–21:30 МСК). Ответ в три часа ночи — это и
    # потерянный лид (утром прочитают вполуха), и явный признак автоматики для
    # Telegram. Сообщение уже сохранено, ответ уйдёт утром: его подберёт планировщик
    # (scheduler._night_replies). Исключения — свои тест-номера и «родные» (protected)
    # аккаунты: и то и другое личная переписка/проверка, а не холодная рассылка, риск
    # для антибана и потери лида тут другой. «Родные тоже тестовые» — прямая просьба
    # оператора не душить ночным окном личные чаты.
    is_test = bool(dict(contact).get("is_test") or 0)
    with database.get_conn() as conn:
        acc_row = conn.execute("SELECT COALESCE(protected,0) protected FROM accounts "
                               "WHERE id=?", (acc_id,)).fetchone()
    is_protected = bool(acc_row and acc_row["protected"])
    if not is_test and not is_protected and not antiban.within_work_hours():
        wake = antiban.next_work_start()
        with database.get_conn() as conn:
            database.add_event(
                conn, "agent_paused", "🌙 Ответ отложен до утра",
                f"Контакт #{contact['id']} написал в нерабочее время "
                f"({antiban.msk_now():%H:%M} МСК). Агент ответит после "
                f"{wake:%d.%m %H:%M} МСК — ночью живым людям не пишем.",
                level="info", contact_id=contact["id"], account_id=acc_id)
        _log(f"[#{acc_id}] 🌙 контакт {contact['id']}: ночь, ответ отложен до {wake:%d.%m %H:%M} МСК")
        return
    if _should_reply(acc_id, contact["id"]):
        # ОДИН ОТВЕТ НА ОЧЕРЕДЬ СООБЩЕНИЙ. Человек редко пишет одной фразой: «здрась,
        # это я» и следом «что хотели?» — два апдейта Telegram, и раньше каждый
        # запускал свою генерацию. Вживую пришло два почти одинаковых приветствия
        # подряд, вдобавок с разным содержимым: параллельные запросы читали карточку
        # в разные моменты. Лок на контакт: пока один ответ готовится, остальные
        # апдейты только сохраняются в «Диалоги» — их текст агент всё равно увидит,
        # потому что историю он читает уже после паузы, целиком из базы.
        lock = _REPLY_LOCKS.setdefault(contact["id"], asyncio.Lock())
        if lock.locked():
            _log(f"[#{acc_id}] контакт {contact['id']}: ответ уже готовится — "
                 f"это сообщение войдёт в него, второй раз не отвечаем")
            return
        async with lock:
            await _agent_reply(event, contact["id"], username, account_id=acc_id)
        _log(f"[#{acc_id}] → авто-ответ контакту {contact['id']}")
    else:
        # Молчание агента раньше было неотличимо от поломки. Пишем причину в лог и
        # в колокольчик: человек ответил, а мы не отвечаем — это всегда потеря лида.
        with database.get_conn() as conn:
            why = ("выключен тумблер «авто-ответ ИИ»"
                   if database.get_setting(conn, "tg_auto_reply", "on") != "on"
                   else "аккаунт не в статусе «активен» и раньше этому контакту не писал")
            database.add_event(
                conn, "agent_error", f"🔇 Ответ клиента без ответа агента",
                f"Контакт #{contact['id']} написал, но авто-ответ не сработал: {why}. "
                f"Ответь вручную в «Диалогах».", level="warn",
                contact_id=contact["id"], account_id=acc_id)
        _log(f"[#{acc_id}] ⚠ НЕ отвечаем контакту {contact['id']}: {why}")


async def _scan_group(event, acc_id: int) -> None:
    """Группа/чат: ищем в сообщении ключи активных ниш → находка в «Запросы»
    (chat_hits). Дедуп по (chat_id, msg_id) — если в чате несколько наших аккаунтов,
    запрос попадёт один раз. Это и есть лидген «поймать того, кто пишет прямо сейчас»."""
    if not _NICHES:
        return
    text = (event.raw_text or "").strip()
    if not text:
        return
    m = _match_niche(text)
    if not m:
        return
    nid, kw = m
    sender = await event.get_sender()
    if not isinstance(sender, User) or sender.bot or sender.deleted:
        return
    chat = await event.get_chat()
    title = getattr(chat, "title", None) or "чат"
    name = _display_name(sender)
    # Кто это написал — заказчик или конкурент, рекламирующий себя. Ключевое слово
    # само по себе не отличает «ищу сайт» от «делаю сайты», поэтому решает отдельный
    # разбор (channels/hit_intent). Классифицируем ДО записи: находку, которая нише
    # не нужна, лучше не заводить вовсе, чем прятать фильтром в интерфейсе.
    from channels import hit_intent
    intent, why = await asyncio.to_thread(hit_intent.classify, text)
    if not hit_intent.wanted(intent, _hunt_mode(nid)):
        return

    with database.get_conn() as conn:
        # chat_hits.chat_id — КАТАЛОЖНЫЙ chats.id (так же пишет chat_keywords). Сюда клался
        # сырой event.chat_id (помеченный, вида -100123…) — JOIN на chats не находил чат,
        # и в «Запросах» у находок слушателя пропадала ссылка на чат/сообщение. Резолвим
        # по chat.id (без -100) — в каталоге tg_chat_id хранится именно в этом виде.
        cat_id = database.resolve_catalog_chat(
            conn, getattr(chat, "id", None), title, getattr(chat, "username", None))
        # Репост того же объявления (новый msg_id, текст слово в слово) в очередь не
        # кладём — UNIQUE(chat_id, msg_id) такое не ловит, см. database.hit_is_repost.
        if database.hit_is_repost(conn, sender.id, text):
            _log(f"[#{acc_id}] ↩ репост от {name} в «{title}» — уже есть в Запросах, пропуск")
            return
        cur = conn.execute(
            "INSERT OR IGNORE INTO chat_hits (niche_id, chat_id, chat_title, tg_user_id, "
            "username, name, text, keyword, source_msg_id, ts, status, intent, intent_why) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'new', ?, ?)",
            (nid, cat_id, title, sender.id, sender.username, name,
             text[:500], kw, event.message.id,
             str(getattr(event.message, "date", None)) if getattr(event.message, "date", None) else None,
             intent, why),
        )
        if cur.rowcount > 0:
            STATUS["hits"] = STATUS.get("hits", 0) + 1
            # Сам запрос сохраняем ВСЕГДА (в «Запросах» видно всё), а колокольчик
            # бережём: рассыльщики постят один и тот же прайс в чат каждые несколько
            # минут, и лента превращалась в пять одинаковых «🎯 Запрос в …: ORDISON»
            # за полчаса. Первое сообщение автора в этом чате показываем, повторы в
            # течение часа — молча копим в «Запросы».
            repeat = conn.execute(
                "SELECT COUNT(*) FROM chat_hits WHERE tg_user_id=? "
                "AND IFNULL(chat_id,-1)=IFNULL(?,-1) AND id<>? "
                "AND created_at >= datetime('now','-1 hour')",
                (sender.id, cat_id, cur.lastrowid),
            ).fetchone()[0]
            if repeat:
                _log(f"[#{acc_id}] 🎯 повтор от {name} в «{title}» ({repeat + 1}-й за час) "
                     f"→ только в Запросы, колокольчик не трогаем")
            else:
                database.add_event(conn, "hit", f"🎯 Запрос в «{title}»: {name}",
                                   f"«{kw}» — {text[:140]}", level="good")
                _log(f"[#{acc_id}] 🎯 запрос «{kw}» от {name} в «{title}» → Запросы")


def _make_handler(acc_id: int):
    async def handler(event) -> None:
        try:
            if event.is_private:
                await _handle_private(event, acc_id)
            elif (event.is_group or event.is_channel) and _chatscan_on():
                await _scan_group(event, acc_id)
        except Exception as e:  # noqa: BLE001
            _log(f"[#{acc_id}] ошибка обработки сообщения: {e}")
    return handler


async def _connect(acc: dict):
    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
    # Таймаут/ошибка коннекта — клиент ОБЯЗАН быть закрыт: Telethon к этому моменту уже
    # поднял свои _send_loop/_recv_loop, и брошенный на полпути клиент оставляет их
    # висеть навсегда («Task was destroyed but it is pending»). На дохлом аккаунте это
    # повторяется каждый цикл supervise и течёт памятью, пока сервер не начнёт задыхаться.
    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
        if not await client.is_user_authorized():
            raise RuntimeError("сессия не авторизована — нужен повторный вход")
        client.add_event_handler(_make_handler(acc["id"]),
                                 events.NewMessage(incoming=True, forwards=False))
        # Что пришло, пока клиент был отключён (рестарт сервиса деплоем, обрыв сети,
        # supervise переподключал аккаунт) — Telethon САМ не доливает, событие теряется
        # НАВСЕГДА и без единой ошибки в логе. Со стороны это неотличимо от «агент
        # проигнорировал ответ»: живой тестовый лид написал ровно в секунды рестарта,
        # реплика осела в Telegram, а в книжку так и не попала. catch_up() просит у
        # Telegram разницу (get_difference) и прогоняет пропущенное через уже
        # зарегистрированный handler — поэтому регистрируем его СТРОКОЙ ВЫШЕ, а не после.
        # Не даём зависшему catch_up сорвать подключение целиком (аккаунт и так
        # авторизован и рабочий) — на этот случай отдельный try, не общий выше.
        try:
            await asyncio.wait_for(client.catch_up(), timeout=CONNECT_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            _log(f"[#{acc['id']}] catch_up не удался (не критично): {e}")
    except BaseException:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        raise
    return client


async def _disconnect_all() -> None:
    for acc_id, client in list(CLIENTS.items()):
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        CLIENTS.pop(acc_id, None)
    STATUS["accounts"].clear()


async def _nap(total: int, was_enabled: bool) -> None:
    """Спим порциями и просыпаемся сразу, как только тумблер переключили в пульте —
    иначе «Стоп» отрабатывал бы только на следующем круге, до полминуты молчком."""
    slept = 0
    while slept < total:
        await asyncio.sleep(min(POLL_SEC, total - slept))
        slept += POLL_SEC
        if await _enabled() != was_enabled:
            return


async def _supervise() -> None:
    global _NICHES
    STATUS["started"] = datetime.datetime.now().isoformat()
    while True:
        on = await _enabled()
        STATUS["enabled"] = on
        # отметка живости круга: без неё упавший поток _supervise выглядел бы в пульте
        # как исправно работающий — со старым снимком STATUS и бодрым «слушаю N из N»
        STATUS["tick"] = datetime.datetime.now().isoformat()
        if not on:
            if CLIENTS:
                await _disconnect_all()
                _log("⏸ слушатель остановлен из пульта — все аккаунты отключены")
            await _nap(RECHECK_SEC, on)
            continue
        _NICHES = _load_niches()   # свежие ключи ниш (можно править в пульте на лету)
        want = {a["id"]: a for a in _listenable()}
        # 1) отключаем выбывших / отвалившихся (переподключим на следующем круге)
        for acc_id, client in list(CLIENTS.items()):
            if acc_id not in want or not client.is_connected():
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                CLIENTS.pop(acc_id, None)
                STATUS["accounts"].pop(acc_id, None)
                _log(f"[#{acc_id}] отключён — переподключусь при след. проверке")
        # 2) подключаем новых параллельно (у каждого свой таймаут)
        to_add = [a for aid, a in want.items() if aid not in CLIENTS]

        async def _try(a: dict) -> None:
            try:
                CLIENTS[a["id"]] = await _connect(a)
                STATUS["accounts"][a["id"]] = {"label": a.get("label"), "ok": True}
                _log(f"[#{a['id']}] {a.get('label') or ''} — слушаю ✓")
            except Exception as e:  # noqa: BLE001
                STATUS["accounts"][a["id"]] = {"label": a.get("label"), "ok": False,
                                               "err": str(e)[:120]}
                _log(f"[#{a['id']}] не подключился: {str(e)[:120]}")
                # быстрый фейловер (как автосвитч прокси в TG-клиенте): не ждём
                # отдельного планировщика — сразу тестируем ТЕКУЩИЙ прокси аккаунта
                # и, если сдох, подставляем живой с мин. пингом из пула. Если дело
                # не в прокси (напр. сессия слетела) — heal() его не трогает.
                #
                # Под замком и строго по одному: _try идёт через gather, и параллельные
                # heal() читают список свободных прокси ДО того, как соседний успеет
                # пометить свой занятым — оба берут один и тот же (минимальный пинг) и
                # сажают два аккаунта на один IP. Это тот самый баг, что чинил 698adcc.
                try:
                    from channels.proxy_pool import heal
                    async with _HEAL_LOCK:
                        res = await heal(ids=[a["id"]], warming_only=False)
                    if res.get("healed"):
                        _log(f"[#{a['id']}] прокси заменён на живой — переподключусь через {RECHECK_SEC} сек")
                except Exception:  # noqa: BLE001
                    pass

        if to_add:
            await asyncio.gather(*[_try(a) for a in to_add])
        ok = sum(1 for v in STATUS["accounts"].values() if v.get("ok"))
        kw = sum(len(k) for _, k, _m in _NICHES)
        _log(f"итог: слушаю {ok} из {len(want)} аккаунтов · ниш {len(_NICHES)}/ключей {kw} · найдено запросов {STATUS.get('hits',0)}")
        await _nap(RECHECK_SEC, on)


async def run() -> None:
    database.init_db()
    _log("=== старт многоаккаунтного слушателя входящих ===")
    await _supervise()


def send_via_listener(acc_id: int, tg_user_id: int, parts: list[str], timeout: float = 120.0) -> bool:
    """Отправить сообщение аккаунтом, который УЖЕ подключён слушателем.

    Планировщику (напоминания, дожим, «не дошёл») тоже нужно писать в Telegram. Своего
    клиента ему заводить нельзя: одна сессия в двух процессах — это AuthKeyDuplicated,
    так уже сгорели аккаунты при перезапусках, а вживую 11.08 отправка теста выбила
    слушатель, и ответ клиента не поймали вовсе. Поэтому переиспользуем соединение
    слушателя: он и так держит все боевые аккаунты подключёнными.

    Вызывается из ОБЫЧНОГО потока (фоновый тик пульта), а клиент живёт в event loop
    слушателя — отсюда run_coroutine_threadsafe.
    """
    loop = _LOOP
    client = CLIENTS.get(acc_id)
    if loop is None or client is None:
        _log(f"[sched] аккаунт #{acc_id} не подключён слушателем — отправку пропускаю")
        return False
    from channels.telegram import _send_parts

    fut = asyncio.run_coroutine_threadsafe(_send_parts(client, tg_user_id, parts), loop)
    try:
        fut.result(timeout=timeout)
        return True
    except Exception as e:  # noqa: BLE001 — сеть/флуд: пусть решает вызывающий
        _log(f"[sched] не отправилось аккаунтом #{acc_id}: {e}")
        return False


def start_in_thread() -> None:
    """Запуск в отдельном демон-потоке со своим event loop — для веб-пульта."""
    import threading

    def _runner() -> None:
        global _LOOP
        try:
            _LOOP = asyncio.new_event_loop()
            asyncio.set_event_loop(_LOOP)
            _LOOP.run_until_complete(run())
        except Exception as e:  # noqa: BLE001
            _log(f"слушатель аварийно остановлен: {e}")
        finally:
            _LOOP = None

    threading.Thread(target=_runner, name="tg-listener", daemon=True).start()


def shutdown(timeout: float = 10.0) -> None:
    """Корректно попрощаться с Telegram перед остановкой сервера.

    ЗАЧЕМ. Поток демонский: при рестарте сервиса его просто убивают, и сокеты умирают
    молча. Telegram ещё какое-то время считает сессию подключённой, а поднявшийся процесс
    logs in снова — и, если фейловер за это время подставил другой прокси, ключ уходит
    в эфир с ДВУХ IP. Telegram на это отвечает AuthKeyDuplicatedError и сжигает сессию
    навсегда (так сгорели #17 и #9320 — 12 рестартов за день).

    Вызывается из web.app на события остановки. Мы в другом потоке, поэтому работу
    планируем в event loop слушателя и ждём результат.
    """
    loop = _LOOP
    if loop is None or loop.is_closed():
        return
    try:
        fut = asyncio.run_coroutine_threadsafe(_disconnect_all(), loop)
        fut.result(timeout=timeout)
        _log("слушатель: все аккаунты отключены штатно (graceful shutdown)")
    except Exception as e:  # noqa: BLE001 — на остановке уже ничего не спасаем
        _log(f"слушатель: не удалось отключить всех штатно: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(run())
