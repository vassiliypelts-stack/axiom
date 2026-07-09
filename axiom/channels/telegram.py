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
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact

import config
from agent.agent import generate_reply
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
    return {k: row[k] for k in ("name", "city", "agency") if row[k]}


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


async def _resolve_entity(client: TelegramClient, row):
    """Находит TG-сущность контакта: по @username, иначе импортирует номер телефона.
    Если username протух (переименован/удалён) — не падаем, а откатываемся на телефон,
    если он есть у контакта (жёсткий отказ только когда вообще нечем резолвить)."""
    if row["username"]:
        try:
            return await client.get_entity(row["username"].lstrip("@"))
        except Exception:  # noqa: BLE001
            if not row["phone"]:
                raise
    if row["phone"]:
        res = await client(
            ImportContactsRequest(
                [InputPhoneContact(client_id=0, phone=row["phone"], first_name=row["name"] or "lead", last_name="")]
            )
        )
        if res.users:
            return res.users[0]
        raise ValueError(f"номер {row['phone']} не найден в Telegram")
    raise ValueError("у контакта нет ни username, ни phone")


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
async def _handle_incoming(event) -> None:
    sender = await event.get_sender()
    username = getattr(sender, "username", None)
    text_in = event.raw_text or ""
    if not text_in.strip():
        return

    with database.get_conn() as conn:
        contact = database.find_contact_by_tg(conn, tg_user_id=int(sender.id), username=username)
        if contact is None:
            print(f"[ignore] входящее от незнакомого {sender.id} (@{username}) — не в книжке")
            return
        contact_id = contact["id"]
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
    messages.append({"role": "user", "content": text_in})

    try:
        reply = await asyncio.to_thread(
            generate_reply, messages, _default_slots(), contact_info, opener, campaign_prompt,
            extra_context, bool(kp_path), kps,
        )
    except Exception as e:
        print(f"[agent error] contact {contact_id}: {e}")
        return

    peer = await event.get_input_chat()
    await asyncio.sleep(random.uniform(*REPLY_DELAY))  # пауза перед началом ответа
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
        meeting = await asyncio.to_thread(meetings.arrange, contact_info, reply.proposed_datetime)

    with database.get_conn() as conn:
        database.add_message(conn, contact_id, "in", text_in, intent=reply.intent)
        database.add_message(conn, contact_id, "out", reply_text, intent=None)
        who = contact_info.get("name") or contact_info.get("person_name") or (f"@{username}" if username else str(contact_id))
        if meeting is not None:
            database.record_meeting(
                conn, contact_id, meeting.meeting_at_iso, reply.notes,
                zoom_link=meeting.zoom_link, calendar_event_id=meeting.calendar_event_id,
            )
            database.add_event(conn, "meeting", f"📅 Встреча назначена: {who}",
                               f"{meeting.meeting_at_iso}", level="good", contact_id=contact_id)
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
            await _send_parts(event.client, peer, [f"закинул ссылку на zoom: {meeting.zoom_link}", "до созвона напомню)"])
    print(f"[reply -> {contact_info.get('name', contact_id)}] intent={reply.intent} agreed={reply.meeting_agreed}")


def _register(client: TelegramClient) -> None:
    client.add_event_handler(_handle_incoming, events.NewMessage(incoming=True, forwards=False))


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
    if not config.ANTHROPIC_API_KEY:
        print("Нет ANTHROPIC_API_KEY в .env — агент не сможет отвечать.")
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
    args = p.parse_args()

    if args.run is not None:
        asyncio.run(_main(outreach=args.run, listen=True, scheduler=True))
    elif args.outreach is not None and not args.listen:
        asyncio.run(_main(outreach=args.outreach, listen=False, scheduler=args.scheduler))
    elif args.listen or args.scheduler:
        asyncio.run(_main(outreach=None, listen=args.listen, scheduler=args.scheduler))
    else:
        p.print_help()
