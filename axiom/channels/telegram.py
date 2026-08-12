"""Telegram-адаптер AXIOM (Telethon / MTProto). Основной канал гибрида.

Делает три вещи:
  • outreach — рассылает персональное первое сообщение по новым контактам (антибан-лимит);
  • listen   — ловит входящие ответы, отдаёт их ИИ-агенту, отвечает и пишет в книжку;
  • run      — то и другое вместе (боевой режим).

Браузер НЕ используется: Telethon говорит с Telegram по родному протоколу MTProto.

Запуск (нужны TG_API_ID/TG_API_HASH в .env + прогретый аккаунт; при первом старте
Telethon спросит номер и код подтверждения в консоли — это нормально):

    python -m channels.telegram --outreach 10   # разослать до 10 первых сообщений
    python -m channels.telegram --listen        # только слушать и отвечать
    python -m channels.telegram --run 10         # разослать 10 и остаться слушать
"""
from __future__ import annotations

import argparse
import asyncio
import random
from datetime import datetime, timedelta

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import ImportContactsRequest, AddContactRequest
from telethon.tl.types import InputPhoneContact, InputUser, PeerUser

import config
from agent.agent import generate_reply
from channels import antiban
from db import database
from integrations import meetings

# Папка с файлами КП (коммерческих предложений), прикреплёнными к кампаниям.
KP_DIR = config.DB_PATH.parent / "kp"


def _kp_path(kp_file: str | None):
    """Путь к существующему файлу КП кампании или None."""
    if not kp_file:
        return None
    p = KP_DIR / kp_file
    return p if p.exists() else None

# ⚠️ ПРАВЬ ПОД СЕБЯ. Первое сообщение знакомому (тема: разработка ИИ / автоматизация).
# Уходит несколькими сообщениями по очереди (как живая личка). Цель — мягко вывести на созвон.
def _first_message_parts(row) -> list[str]:
    name = (row["name"] or "").strip()
    return [
        f"привет, {name})" if name else "привет)",
        "слушай, я тут плотно занялся разработкой ии и автоматизацией для бизнеса. боты, приложения, снятие рутины",
        "хотел посоветоваться: есть задачи, которые каждый день руками делаешь? накидаешь штуки 3? или может знаешь кого, кому такое зайдёт",
    ]

# Антибан: паузы между ПЕРВЫМИ сообщениями (сек). Рандомизируем «человеческий» темп.
OUTREACH_PAUSE = (40, 130)
# Задержка перед ответом в диалоге (сек) — будто человек печатает, а не бот-молния.
REPLY_DELAY = (4, 18)
# Человекоподобная отправка по частям (B1):
TYPING_CPS = (12, 22)     # «скорость печати» — знаков/сек, время набора ∝ длине сообщения
MAX_TYPING_SEC = 9.0      # потолок имитации набора одного сообщения
PART_PAUSE = (1.2, 3.5)   # пауза между соседними сообщениями
# Человекоподобная РЕАКЦИЯ на входящее (как реальный человек, не бот-молния):
# диапазон настраивается в пульте (Аккаунты → «⏱ скорость ответа»), см. _reply_delay_range().
# По умолчанию 30-60с — быстро для лида (не теряет теплоту диалога), но не мгновенно.
REPLY_DELAY_DEFAULT = (30.0, 60.0)


# Что сказать, если модель трижды не смогла собрать ответ (см. _agent_reply).
# Не признание в поломке — так люди сами иногда пишут, когда отвлеклись.
_STALL_REPLIES = [
    "сорри, отвлекся) допиши ещё раз, что спросил?",
    "упс, что-то связь моргнула у меня) повтори последнее?",
    "секунду, отвечу — тут звонок словил) что писал?",
]


def _default_slots() -> list[str]:
    """Слоты для встречи. Заглушка под пилот — позже возьмём из Google Calendar.
    Возвращает пару ближайших удобных вариантов в человеческом виде."""
    base = datetime.now()
    d1 = base + timedelta(days=1)
    d2 = base + timedelta(days=2)
    return [f"{d1:%d.%m} в 11:00", f"{d1:%d.%m} в 16:00", f"{d2:%d.%m} в 12:00"]


def _parse_raw_hostport(raw: str) -> dict | None:
    """Сырой прокси БЕЗ scheme:// — так отдают многие панели (lzt.market и т.п.):
    «host:port:user:pass», «host:port» или «user:pass@host:port». Раньше такое тихо
    ломало addr/port в None (urlparse считает часть до первого «:» схемой) — аккаунт
    физически не мог подключиться к Telegram, без явной ошибки в интерфейсе."""
    s = raw
    user = password = None
    if "@" in s:                              # user:pass@host:port
        creds, _, hostport = s.rpartition("@")
        s = hostport
        if ":" in creds:
            user, _, password = creds.partition(":")
    parts = s.split(":")
    if len(parts) == 4:                       # host:port:user:pass (частый формат панелей)
        host, port_s, user, password = parts
    elif len(parts) == 2:                     # host:port
        host, port_s = parts
    else:
        return None
    if not host or not port_s.isdigit():
        return None
    proxy = {"proxy_type": "socks5", "addr": host, "port": int(port_s), "rdns": True}
    if user:
        proxy["username"] = user
    if password:
        proxy["password"] = password
    return proxy


def parse_proxy_str(raw: str | None) -> dict | None:
    """socks5://user:pass@host:port → dict для python-socks. Пусто → None.
    tg:// (MTProto) и любые не-socks/http схемы → None (их обрабатывает parse_mtproxy,
    а как socks их совать нельзя — иначе ValueError: Unknown proxy protocol type).
    Без scheme:// (сырой «host:port[:user:pass]» из панелей) — см. _parse_raw_hostport."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        return _parse_raw_hostport(raw)
    from urllib.parse import urlparse

    p = urlparse(raw)
    scheme = (p.scheme or "socks5").lower()
    if scheme not in ("socks5", "socks4", "http", "https"):
        return None
    if not p.hostname or not p.port:          # битый URL (напр. лишние «:» в netloc) — не отдаём мусор
        return None
    proxy = {"proxy_type": scheme, "addr": p.hostname, "port": p.port, "rdns": True}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


def _parse_proxy() -> dict | None:
    """Прокси основного аккаунта из TG_PROXY (.env)."""
    return parse_proxy_str(config.TG_PROXY)


def parse_mtproxy(raw: str | None):
    """tg://proxy?server=&port=&secret= → (server, port, secret) для Telethon MTProxy.
    Telethon умеет только «чистый» (32 hex) или «секьюрный» dd-секрет (dd+32 hex). Faketls
    (ee…) и битые секреты telethon не поддерживает — для них возвращаем None, чтобы клиент
    шёл напрямую, а не падал с «MTProxy secret must be 16 bytes»."""
    raw = (raw or "").strip()
    if "proxy?" not in raw or "secret=" not in raw:
        return None
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(raw).query)
    server = (q.get("server") or [None])[0]
    port = (q.get("port") or [None])[0]
    secret = (q.get("secret") or [None])[0]
    if not (server and port and secret):
        return None
    try:
        port = int(port)
    except ValueError:
        return None
    s = secret.lower()
    is_hex = all(c in "0123456789abcdef" for c in s)
    if is_hex and (len(s) == 32 or (s.startswith("dd") and len(s) == 34)):
        return (server, port, secret)
    return None   # faketls (ee…) / нестандартный — telethon не потянет, идём напрямую


def build_client(session, proxy_raw: str | None = None,
                 api_id: int | None = None, api_hash: str | None = None) -> TelegramClient:
    """Единая сборка клиента: MTProto-прокси (tg://proxy) или SOCKS5. proxy_raw
    пуст → прокси основного аккаунта из .env. Используется аккаунтами команды.
    api_id/api_hash — собственные креды аккаунта (для купленных сессий обязательно
    использовать те, под которыми сессия создана); иначе берём глобальные из .env."""
    mt = parse_mtproxy(proxy_raw)
    kwargs: dict = {}
    if mt:
        from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["connection"] = ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["proxy"] = mt
    else:
        kwargs["proxy"] = parse_proxy_str(proxy_raw) or _parse_proxy()
    aid = int(api_id) if api_id else int(config.TG_API_ID)
    ahash = api_hash or config.TG_API_HASH
    return TelegramClient(session, aid, ahash, **kwargs)


def client_for_account(acc_id: int | None):
    """(клиент, acc_id) для аккаунта из БД; acc_id=None → главный аккаунт из .env.

    Общий помощник: раньше каждый модуль строил клиента сам и по умолчанию лез в
    главный аккаунт (_build_client), из-за чего массовые задачи гоняли весь трафик
    через личный номер и рисковали поймать на него FloodWait. Через этот вход работу
    можно раскидать по рабочим аккаунтам.
    """
    if acc_id is None:
        return _build_client(), None
    from db import database
    with database.get_conn() as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not acc:
        raise RuntimeError(f"аккаунт #{acc_id} не найден")
    acc = dict(acc)
    if not acc.get("tg_session"):
        raise RuntimeError(f"у аккаунта #{acc_id} нет TG-сессии — подключи его (🔌 Подключить)")
    from telethon.sessions import StringSession
    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
    return client, acc_id


def _build_client() -> TelegramClient:
    if not config.TG_API_ID or not config.TG_API_HASH:
        raise RuntimeError("Заполни TG_API_ID и TG_API_HASH в .env (получить на my.telegram.org)")
    # На сервере — StringSession из .env (без ввода кода); локально — файловая сессия.
    if config.TG_STRING_SESSION:
        from telethon.sessions import StringSession
        session = StringSession(config.TG_STRING_SESSION)
    else:
        session = config.TG_SESSION
    return TelegramClient(
        session,
        int(config.TG_API_ID),
        config.TG_API_HASH,
        proxy=_parse_proxy(),
    )


def _history_for_agent(rows) -> tuple[str | None, list[dict]]:
    """Книжка → (opener, messages). Лидирующие исходящие (наше первое сообщение)
    выносим в opener, т.к. history для Claude обязана начинаться с реплики собеседника."""
    opener_parts: list[str] = []
    i = 0
    while i < len(rows) and rows[i]["direction"] == "out":
        opener_parts.append(rows[i]["text"])
        i += 1
    messages = [
        {"role": "user" if r["direction"] == "in" else "assistant", "content": r["text"]}
        for r in rows[i:]
    ]
    opener = "\n".join(opener_parts) if opener_parts else None
    return opener, messages


def _contact_dict(row) -> dict:
    """Что агент знает о собеседнике.

    Раньше сюда шли только имя, город и агентство — и агент физически не мог
    опереться на то, ЧЕМ человек занимается, хотя при импорте (ВсеТренинги, 2ГИС)
    и обогащении это поле заполнено. Без него не работает главный ход холодного
    захода — «мы с вами коллеги, поэтому и написал»: у агента нет предмета, о
    котором говорить. Отдаём всё, что описывает деятельность человека.
    """
    keys = ("name", "city", "agency",
            "specialization",   # чем занимается (из импорта/обогащения)
            "niche",            # род деятельности (по bio + каналу)
            "offer",            # что он сам продаёт
            "bio",              # bio из Telegram-профиля
            "hook",             # персональная зацепка
            "person_role")      # должность
    out = {}
    for k in keys:
        try:
            v = row[k]
        except (KeyError, IndexError):
            continue
        if v:
            out[k] = v
    return out


def _reply_delay_range() -> tuple[float, float]:
    """Диапазон паузы «увидел → ответил», настраивается в пульте (app_settings
    reply_delay_min_sec/max_sec). Не настроено — берём REPLY_DELAY_DEFAULT (30-60с)."""
    with database.get_conn() as conn:
        lo_raw = database.get_setting(conn, "reply_delay_min_sec")
        hi_raw = database.get_setting(conn, "reply_delay_max_sec")
    try:
        lo = float(lo_raw) if lo_raw else REPLY_DELAY_DEFAULT[0]
        hi = float(hi_raw) if hi_raw else REPLY_DELAY_DEFAULT[1]
    except ValueError:
        lo, hi = REPLY_DELAY_DEFAULT
    return (lo, hi) if hi >= lo > 0 else REPLY_DELAY_DEFAULT


async def _humanize_before_reply(client, peer) -> None:
    """Ведёт себя как живой человек ПЕРЕД ответом на входящее:
    1) отмечает сообщение прочитанным (собеседник видит галочки «прочитано»);
    2) выдерживает паузу в настроенном диапазоне (см. _reply_delay_range) — у
    собеседника складывается картина живого человека, который увидел, прочитал и
    через некоторое время ответил, а не бота, отвечающего мгновенно или спящего часами."""
    try:
        await client.send_read_acknowledge(peer)
    except Exception:
        pass
    await asyncio.sleep(random.uniform(*_reply_delay_range()))


async def _send_parts(client, peer, parts: list[str]) -> None:
    """Шлёт сообщения по очереди как живой человек: показывает «печатает…»,
    держит паузу пропорционально длине текста, паузит между сообщениями."""
    clean = [p.strip() for p in parts if p and p.strip()]
    for i, part in enumerate(clean):
        typing = min(len(part) / random.uniform(*TYPING_CPS), MAX_TYPING_SEC)
        async with client.action(peer, "typing"):
            await asyncio.sleep(max(1.2, typing))
        await client.send_message(peer, part)
        if i < len(clean) - 1:
            await asyncio.sleep(random.uniform(*PART_PAUSE))


def _col(row, name: str):
    """Значение колонки, которой в этой выборке может и не быть. Сюда приходят и
    sqlite3.Row из «SELECT *», и урезанные dict'ы из прогрева/моста — у первого
    отсутствующая колонка бросает IndexError, у второго это просто None."""
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return None


async def _resolve_entity(client: TelegramClient, row):
    """Находит TG-сущность контакта: @username → известный tg_user_id → телефон.
    Если username протух (переименован/удалён) — не падаем, а откатываемся дальше
    по цепочке (жёсткий отказ только когда вообще нечем резолвить)."""
    if row["username"]:
        try:
            entity = await client.get_entity(row["username"].lstrip("@"))
            # Добавляем в записную книжку аккаунта (антибан)
            if hasattr(entity, "id"):
                try:
                    await client(AddContactRequest(
                        id=InputUser(entity.id, entity.access_hash or 0),
                        first_name=(row["name"] or entity.first_name or "").split()[0] if (row["name"] or entity.first_name or "") else "lead",
                        last_name=" ".join((row["name"] or "").split()[1:]) if row["name"] and len(row["name"].split()) > 1 else (entity.last_name or ""),
                        phone=entity.phone or "",
                        add_phone_privacy_exception=False,
                    ))
                except Exception:  # noqa: BLE001
                    pass  # не критично если не добавилось
            return entity
        except Exception:  # noqa: BLE001
            if not row["phone"] and not _col(row, "tg_user_id"):
                raise
    # Контакт уже пробит: id в Telegram известен (пробив/парсер/прошлая переписка).
    # Пробуем его ДО телефона — ImportContacts по номеру человека, которого мы и так
    # умеем достать, это лишний запрос ровно того вида, по которому Telegram считает
    # спамеров. Сюда же попадают контакты с протухшим @ником: id живёт дольше ника.
    uid = _col(row, "tg_user_id")
    if uid:
        try:
            return await client.get_entity(PeerUser(int(uid)))
        except Exception:  # noqa: BLE001
            pass          # нет в кэше сессии этого аккаунта — идём дальше по телефону
    if row["phone"]:
        # Номер нормализуем ТЕМ ЖЕ способом, что и массовый пробив: Telegram капризен к
        # формату, и «8 (988) 111-22-33» из 2ГИС он просто не найдёт. Без этого мы бы
        # записали живому человеку has_tg='no' из-за скобок в номере.
        from channels.phone_resolve import _norm
        phone = _norm(row["phone"]) or row["phone"]
        res = await client(
            ImportContactsRequest(
                [InputPhoneContact(client_id=0, phone=phone, first_name=row["name"] or "lead", last_name="")]
            )
        )
        if res.users:
            return res.users[0]
        # Промах фиксируем в карточке. Иначе номер остаётся has_tg='unknown' и его снова
        # потянет и эта кампания, и следующая, и массовый пробив — каждый раз новый
        # ImportContacts по номеру, которого в Telegram нет. Именно повторяющиеся
        # промахи и читаются как спам.
        try:
            with database.get_conn() as conn:
                conn.execute(
                    "UPDATE contacts SET has_tg='no', tg_checked_at=datetime('now') WHERE id=?",
                    (row["id"],))
        except Exception:  # noqa: BLE001
            pass          # пометка — не повод ронять отправку
        raise ValueError(f"номер {phone} не найден в Telegram")
    raise ValueError("контакт не резолвится: ни рабочего @ника, ни номера телефона")


# --------------------------------------------------------------------------- #
#  OUTREACH — первые сообщения                                                 #
# --------------------------------------------------------------------------- #
async def run_outreach(client: TelegramClient, limit: int | None = None) -> int:
    """Шлёт первое сообщение новым контактам (status='new'), у кого вероятно есть TG.
    Соблюдает дневной лимит и рандомные паузы. Возвращает число отправленных."""
    cap = min(limit or config.DAILY_FIRST_MESSAGES, config.DAILY_FIRST_MESSAGES)
    sent = 0
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM contacts "
            "WHERE status = 'new' AND has_tg IN ('yes','unknown') "
            "AND (username IS NOT NULL OR phone IS NOT NULL) "
            "ORDER BY id LIMIT ?",
            (cap,),
        ).fetchall()

    for row in rows:
        if sent >= cap:
            break
        try:
            entity = await _resolve_entity(client, row)
            parts = _first_message_parts(row)
            await _send_parts(client, entity, parts)
            text = "\n".join(parts)
        except FloodWaitError as e:
            print(f"[floodwait] ждём {e.seconds}с (Telegram попросил притормозить)")
            await asyncio.sleep(e.seconds + 5)
            continue
        except Exception as e:  # контакт не нашёлся / приватность — помечаем и идём дальше
            print(f"[skip] contact {row['id']} ({row['username'] or row['phone']}): {e}")
            with database.get_conn() as conn:
                database.set_status(conn, row["id"], "lost")
            continue

        with database.get_conn() as conn:
            database.set_tg_user_id(conn, row["id"], int(entity.id))
            database.add_message(conn, row["id"], "out", text, intent=None)
            database.set_status(conn, row["id"], "messaged")
        sent += 1
        print(f"[sent {sent}/{cap}] -> {row['name'] or row['username'] or row['phone']}")
        if sent < cap:
            await asyncio.sleep(random.uniform(*OUTREACH_PAUSE))
    print(f"Готово: отправлено {sent} первых сообщений.")
    return sent


# --------------------------------------------------------------------------- #
#  LISTEN — входящие ответы → ИИ-агент → ответ                                 #
# --------------------------------------------------------------------------- #
def _record_incoming(contact_id: int, text_in: str, username: str | None,
                     account_id: int | None = None) -> str:
    """ВСЕГДА сохраняем входящее сообщение в книжку сразу — чтобы ответ появился
    в разделе «Диалоги» даже если ИИ-агент упадёт или авто-ответ выключен.
    Раньше входящее писалось только вместе с успешным ответом агента — из-за этого
    ответы клиентов терялись. Возвращает «кто» (для событий колокольчика)."""
    with database.get_conn() as conn:
        row = conn.execute("SELECT status, name, person_name FROM contacts WHERE id=?",
                           (contact_id,)).fetchone()
        database.add_message(conn, contact_id, "in", text_in, account_id=account_id)
        # не сбиваем терминальные статусы (встреча/сделка) назад в «диалог»
        if row and (row["status"] or "") in ("new", "messaged", "nurture", "in_dialog", ""):
            database.set_status(conn, contact_id, "in_dialog")
        who = (row["name"] if row else None) or (row["person_name"] if row else None) \
            or (f"@{username}" if username else str(contact_id))
        database.add_event(conn, "reply", f"💬 Новый ответ: {who}",
                           (text_in or "").strip()[:160], level="good",
                           contact_id=contact_id, account_id=account_id)
    return who


def _notify_agent_down(contact_id: int, exc: Exception) -> None:
    """Агент не смог ответить живому человеку — сказать оператору в колокольчик.

    Раньше это была одна строчка в консоли сервера: лид пишет, ответа нет, и об этом
    никто не знает, пока вручную не залезешь в логи. Самый частый случай — кончились
    кредиты API (тогда молчат ВСЕ диалоги сразу).

    Дедуп 15 минут по тексту ошибки: при обвале API падает каждое сообщение, и без
    этого лента колокольчика превратилась бы в простыню из одинаковых записей.
    """
    import time
    msg = f"{type(exc).__name__}: {exc}"
    low = msg.lower()
    if "credit balance" in low or "insufficient" in low or "quota" in low:
        title, hint = "🔴 ИИ-агент не отвечает: кончились кредиты API", \
            "Пополни баланс на console.anthropic.com → Plans & Billing. Пока диалоги молчат."
    else:
        title, hint = "🔴 ИИ-агент не смог ответить", msg[:180]
    try:
        with database.get_conn() as conn:
            last = database.get_setting(conn, "agent_error_ts", "0")
            prev = database.get_setting(conn, "agent_error_sig", "")
            sig = title
            if sig == prev and (time.time() - float(last or 0)) < 900:
                return
            database.set_setting(conn, "agent_error_ts", str(time.time()))
            database.set_setting(conn, "agent_error_sig", sig)
            database.add_event(conn, "agent_error", title, hint, level="warn",
                               contact_id=contact_id)
    except Exception:  # noqa: BLE001 — уведомление не должно ронять обработку сообщения
        pass


async def _agent_reply(event, contact_id: int, username: str | None,
                       account_id: int | None = None) -> None:
    """Генерит ответ ИИ-агентом и шлёт его ЧЕРЕЗ аккаунт, получивший сообщение
    (event.client). Входящее уже сохранено вызывающим — здесь только исходящее и
    события (встреча/тёплый лид). Историю берём из книжки — она уже содержит
    только что записанное входящее последней репликой."""
    with database.get_conn() as conn:
        contact = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        opener, messages = _history_for_agent(database.get_history(conn, contact_id))
        contact_info = _contact_dict(contact)
        camp = database.get_contact_campaign(conn, contact_id)
        campaign_prompt = camp["agent_prompt"] if camp else None
        kp_file = (camp["kp_file"] if camp and "kp_file" in camp.keys() else None)
        extra_context = contact["agent_context"] if "agent_context" in contact.keys() else None
        kps = []
        if camp:
            kps = [dict(r) for r in conn.execute(
                "SELECT id, name, when_to_use, kp_text, kp_file FROM campaign_kps "
                "WHERE campaign_id=? ORDER BY id", (camp["id"],),
            ).fetchall()]

    kp_path = _kp_path(kp_file)
    if not messages or messages[-1]["role"] != "user":
        return  # нечего отвечать (нет реплики собеседника)

    try:
        reply = await asyncio.to_thread(
            generate_reply, messages, _default_slots(), contact_info, opener, campaign_prompt,
            extra_context, bool(kp_path), kps, camp["id"] if camp else None,
        )
    except Exception as e:
        print(f"[agent error] contact {contact_id}: {e}")
        _notify_agent_down(contact_id, e)
        # Живой человек написал и НЕ должен остаться без единого слова — молчание
        # читается как игнор, а не как техническая заминка. У DeepSeek такое бывает:
        # после всех повторов приходит пустой ответ вместо JSON (см. agent/llm.py
        # structured, _RETRIES) — до сих пор это значило полную тишину навсегда,
        # хотя причину и так видно оператору в колокольчике (see _notify_agent_down).
        # …но ровно ОДИН раз. Человек, не получив ответа по существу, обычно дописывает
        # ещё и ещё — и каждое его сообщение роняло агента заново, отправляя новую
        # отговорку. Вживую вышло четыре «сорри, отвлёкся» подряд: так не пишет ни
        # занятой человек, ни исправный бот, это читается как поломка. Если последнее
        # наше сообщение уже было отговоркой — молчим и ждём, пока агент починится.
        # Ограничение по ВРЕМЕНИ, а не «один раз навсегда». Первая версия проверяла
        # только, была ли отговоркой последняя наша реплика — и диалог, где она
        # оказалась последней, замолкал НАВСЕГДА: человек пишет через час, агент опять
        # падает, а мы молчим, потому что «уже извинялись». Поймано вживую. Пятнадцати
        # минут хватает, чтобы не сыпать извинениями в одну очередь сообщений, и при
        # этом ответить тому, кто вернулся позже.
        try:
            with database.get_conn() as conn:
                last_out = conn.execute(
                    "SELECT text, ts FROM messages WHERE contact_id=? AND direction='out' "
                    "ORDER BY id DESC LIMIT 1", (contact_id,)).fetchone()
            recent_stall = False
            if last_out and (last_out["text"] or "").strip() in _STALL_REPLIES:
                from datetime import datetime as _dt
                try:
                    age = (_dt.utcnow() - _dt.fromisoformat(str(last_out["ts"]))).total_seconds()
                except (TypeError, ValueError):
                    age = 0          # неразобранная метка времени — считаем свежей
                recent_stall = age < 900
            if recent_stall:
                print(f"[agent fallback] contact {contact_id}: отговорка уже была <15 мин назад — молчим")
                return
            peer = await event.get_input_chat()
            await _humanize_before_reply(event.client, peer)
            stall = random.choice(_STALL_REPLIES)
            await _send_parts(event.client, peer, [stall])
            with database.get_conn() as conn:
                database.add_message(conn, contact_id, "out", stall, intent=None,
                                     account_id=account_id)
        except Exception as e2:  # noqa: BLE001 — фолбэк best-effort, не роняем обработку
            print(f"[agent fallback error] contact {contact_id}: {e2}")
        return

    text_in = messages[-1]["content"]
    peer = await event.get_input_chat()
    # Человекоподобно: заметил → прочитал (галочки) → иногда отвлёкся → печатает
    await _humanize_before_reply(event.client, peer)
    await _send_parts(event.client, peer, reply.reply_parts)
    reply_text = "\n".join(p.strip() for p in reply.reply_parts if p.strip())

    # КП: если в кампании НЕСКОЛЬКО КП — агент выбрал нужное (kp_choice по названию).
    chosen = None
    if kps and reply.kp_choice:
        want = reply.kp_choice.strip().lower().strip("«»\"' ")
        for k in kps:
            if (k.get("name") or "").strip().lower() == want:
                chosen = k
                break
    if chosen:
        try:
            await asyncio.sleep(random.uniform(*REPLY_DELAY))
            if chosen.get("kp_text"):
                await _send_parts(event.client, peer, [chosen["kp_text"]])
                reply_text += f"\n[КП «{chosen.get('name')}»: {chosen['kp_text']}]"
            cp = _kp_path(chosen.get("kp_file"))
            if cp is not None:
                await asyncio.sleep(random.uniform(*REPLY_DELAY))
                await event.client.send_file(peer, str(cp))
                reply_text += f"\n[отправлен файл КП: {cp.name}]"
            print(f"[KP «{chosen.get('name')}» -> {contact_info.get('name', contact_id)}]")
        except Exception as e:
            print(f"[KP send error] contact {contact_id}: {e}")
    # Легаси: одно КП файлом на кампании (если набор КП не задан)
    elif not kps and reply.send_kp and kp_path is not None:
        try:
            await asyncio.sleep(random.uniform(*REPLY_DELAY))
            await event.client.send_file(peer, str(kp_path))
            reply_text += f"\n[отправлен файл КП: {kp_path.name}]"
            print(f"[KP -> {contact_info.get('name', contact_id)}] {kp_path.name}")
        except Exception as e:
            print(f"[KP send error] contact {contact_id}: {e}")

    # Согласие на встречу → создаём Zoom + событие (сетевые вызовы вне БД-блока)
    meeting = None
    if reply.meeting_agreed:
        meeting = await asyncio.to_thread(meetings.arrange, contact_info,
                                          reply.proposed_datetime, camp["id"] if camp else None)

    with database.get_conn() as conn:
        # входящее уже записано в _record_incoming — тут только проставляем ему intent
        # (для аналитики) и сохраняем наш ответ
        conn.execute("UPDATE messages SET intent=? WHERE id=("
                     "SELECT id FROM messages WHERE contact_id=? AND direction='in' "
                     "ORDER BY id DESC LIMIT 1)", (reply.intent, contact_id))
        database.add_message(conn, contact_id, "out", reply_text, intent=None,
                             account_id=account_id)
        who = contact_info.get("name") or contact_info.get("person_name") or (f"@{username}" if username else str(contact_id))
        if meeting is not None:
            database.record_meeting(
                conn, contact_id, meeting.meeting_at_iso, reply.notes,
                zoom_link=meeting.zoom_link, calendar_event_id=meeting.calendar_event_id,
            )
            if meeting.parsed and meeting.zoom_link:
                database.add_event(conn, "meeting", f"📅 Встреча назначена: {who}",
                                   f"{meeting.meeting_at_iso}", level="good", contact_id=contact_id)
            elif meeting.parsed:
                # Время есть, а подключаться некуда: не заданы ни PERMANENT_MEETING_URL,
                # ни доступы к Zoom. Договорённость в силе, но ссылку человеку надо дать
                # руками — иначе он придёт к назначенному часу в пустоту.
                database.add_event(
                    conn, "meeting", f"📅 Встреча назначена (без ссылки): {who}",
                    f"{meeting.meeting_at_iso} — ссылки на созвон нет: заполни "
                    f"PERMANENT_MEETING_URL в .env или доступы Zoom, а пока отправь ссылку сам.",
                    level="warn", contact_id=contact_id)
            else:
                # Время не превратилось в дату → нет ни Zoom-ссылки, ни события в
                # календаре, ни напоминания. Молчать тут нельзя: человек согласился на
                # созвон и без вмешательства оператора останется без адреса подключения.
                database.add_event(
                    conn, "meeting", f"⚠️ Созвон без времени: {who}",
                    f"человек согласился, но время «{meeting.meeting_at_iso}» не разобрано — "
                    f"ни Zoom-ссылки, ни напоминания. Проставь дату в карточке руками.",
                    level="warn", contact_id=contact_id)
        elif reply.intent == "not_interested":
            database.set_status(conn, contact_id, "nurture")
        else:
            database.set_status(conn, contact_id, "in_dialog")
            if reply.intent in ("positive", "agreed"):
                database.add_event(conn, "lead", f"🔥 Тёплый лид: {who}",
                                   (text_in or "").strip()[:160], level="good", contact_id=contact_id)

    if meeting is not None:
        print(f"[MEETING] contact {contact_id}: {meeting.meeting_at_iso} | "
              f"zoom={'yes' if meeting.zoom_link else 'no'} | cal={'yes' if meeting.calendar_event_id else 'no'}")
        if meeting.zoom_link:
            # без названия сервиса: ссылка может быть Телемост/Meet/Zoom — что задано
            # в кампании, то и уходит, а «зум» в тексте противоречил бы самой ссылке
            await _send_parts(event.client, peer, [f"вот ссылка на созвон: {meeting.zoom_link}", "до связи)"])
        from channels import notify
        await notify.notify_meeting(contact_id, meeting.meeting_at_iso, reply.notes,
                                    meeting.zoom_link, camp["id"] if camp else None)
    print(f"[reply -> {contact_info.get('name', contact_id)}] intent={reply.intent} agreed={reply.meeting_agreed}")


async def _handle_incoming(event) -> None:
    """Одиночный слушатель (--listen): найти контакт → записать входящее → ответить.
    Многоаккаунтный слушатель (channels.listener) переиспользует те же _record_incoming
    и _agent_reply, но с гейтом авто-ответа по статусу аккаунта."""
    sender = await event.get_sender()
    username = getattr(sender, "username", None)
    text_in = (event.raw_text or "").strip()
    if not text_in:
        return
    with database.get_conn() as conn:
        contact = database.find_contact_by_tg(conn, tg_user_id=int(sender.id), username=username)
    if contact is None:
        print(f"[ignore] входящее от незнакомого {sender.id} (@{username}) — не в книжке")
        return
    _record_incoming(contact["id"], text_in, username)
    await _agent_reply(event, contact["id"], username)


def _register(client: TelegramClient) -> None:
    client.add_event_handler(_handle_incoming, events.NewMessage(incoming=True, forwards=False))


class _PseudoEvent:
    """Заменитель telethon-события для ответа БЕЗ входящего апдейта.

    _agent_reply умеет отвечать только «по событию»: берёт из него клиента и чат.
    Утренняя досылка (см. night_reply_pass) отвечает на сообщение, пришедшее ночью
    несколько часов назад, — события уже нет, а весь остальной путь (агент, встреча,
    ссылка, уведомление владельцу) нужен ровно тот же. Дублировать его второй раз
    было бы приглашением к расхождению логики, поэтому подставляем минимальный
    объект с тем же интерфейсом."""

    def __init__(self, client: TelegramClient, peer) -> None:
        self.client = client
        self._peer = peer

    async def get_input_chat(self):
        return self._peer


async def night_reply_pass(limit: int = 20) -> int:
    """Ответить тем, кто написал в нерабочее время и остался без ответа.

    Ночью агент молчит намеренно (см. listener: 09:00–21:30 МСК). Без этой досылки
    такой лид остался бы без ответа НАВСЕГДА — то есть тишина, от которой лечились,
    просто переехала бы на утро. Здесь берём диалоги, где последнее сообщение —
    входящее, и отвечаем обычным путём агента.

    Возвращает число отвеченных. Вне рабочих часов не делает ничего.
    """
    if not antiban.within_work_hours():
        return 0
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT m.contact_id, m.account_id, c.username "
            "FROM messages m JOIN contacts c ON c.id = m.contact_id "
            "WHERE m.id IN (SELECT MAX(id) FROM messages GROUP BY contact_id) "
            "AND m.direction='in' AND m.account_id IS NOT NULL "
            "AND c.status IN ('in_dialog','messaged','new') "
            "ORDER BY m.id LIMIT ?", (limit,)).fetchall()
        pending = [dict(r) for r in rows]
    done = 0
    for p in pending:
        acc_id = p["account_id"]
        try:
            client, _ = client_for_account(acc_id)
        except Exception as e:  # noqa: BLE001 — мёртвый аккаунт не должен рвать проход
            print(f"[night] contact {p['contact_id']}: аккаунт #{acc_id} недоступен: {e}")
            continue
        try:
            await client.connect()
            if not await client.is_user_authorized():
                print(f"[night] аккаунт #{acc_id}: сессия не авторизована — пропускаю")
                continue
            with database.get_conn() as conn:
                row = conn.execute("SELECT * FROM contacts WHERE id=?", (p["contact_id"],)).fetchone()
            peer = await _resolve_entity(client, row)
            await _agent_reply(_PseudoEvent(client, peer), p["contact_id"],
                               p["username"], account_id=acc_id)
            done += 1
            print(f"[night] ответили контакту {p['contact_id']} (утренняя досылка)")
        except Exception as e:  # noqa: BLE001
            print(f"[night] contact {p['contact_id']}: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    return done


def _make_sender(client: TelegramClient):
    """Отдаёт планировщику способ отправки: Action → сообщение в TG."""
    async def send(action) -> None:
        if not action.tg_user_id:
            print(f"[scheduler skip] {action.kind} contact {action.contact_id}: нет tg_user_id")
            return
        await asyncio.sleep(random.uniform(*REPLY_DELAY))
        # B1: дожим/напоминание тоже шлём человекоподобно (печатает… + по частям)
        parts = [c for c in action.text.split("\n\n") if c.strip()] or [action.text]
        await _send_parts(client, int(action.tg_user_id), parts)
        print(f"[scheduler {action.kind}] -> {action.name or action.contact_id}")
    return send


# --------------------------------------------------------------------------- #
#  Точки входа                                                                 #
# --------------------------------------------------------------------------- #
async def _main(outreach: int | None, listen: bool, scheduler: bool = False) -> None:
    from agent import llm
    if not llm.available(config.agent_model()):
        print(f"Нет ключа под модель «{config.agent_model()}» в .env — агент не сможет отвечать.")
        return
    database.init_db()
    client = _build_client()
    await client.start()  # при первом запуске спросит номер и код в консоли
    me = await client.get_me()
    print(f"Подключён как @{me.username or me.id}")

    if outreach:
        await run_outreach(client, outreach)

    if scheduler:
        import scheduler as sched  # напоминания + дожим через этот же аккаунт
        asyncio.create_task(sched.run_loop(_make_sender(client)))
    if listen:
        _register(client)
        print("Слушаю входящие. Ctrl+C для остановки.")
        await client.run_until_disconnected()
    elif scheduler:
        await asyncio.Event().wait()  # только планировщик
    else:
        await client.disconnect()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Telegram-адаптер AXIOM")
    p.add_argument("--outreach", type=int, metavar="N", help="разослать до N первых сообщений")
    p.add_argument("--listen", action="store_true", help="слушать входящие и отвечать")
    p.add_argument("--scheduler", action="store_true", help="крутить напоминания + дожим")
    p.add_argument("--run", type=int, metavar="N", help="разослать N, слушать и крутить планировщик")
    p.add_argument("--night-replies", action="store_true",
                   help="ответить тем, кто написал ночью (утренняя досылка)")
    args = p.parse_args()

    if args.night_replies:
        n = asyncio.run(night_reply_pass())
        print(f"утренняя досылка: отвечено {n}")
    elif args.run is not None:
        asyncio.run(_main(outreach=args.run, listen=True, scheduler=True))
    elif args.outreach is not None and not args.listen:
        asyncio.run(_main(outreach=args.outreach, listen=False, scheduler=args.scheduler))
    elif args.listen or args.scheduler:
        asyncio.run(_main(outreach=None, listen=args.listen, scheduler=args.scheduler))
    else:
        p.print_help()
