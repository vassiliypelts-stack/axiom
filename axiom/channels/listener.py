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
from channels.telegram import _agent_reply, _record_incoming, build_client
from db import database

# Дохлые прокси у прогреваемых аккаунтов заваливают консоль сервера простынёй
# «Attempt N at connecting failed…» (внутренние ретраи Telethon). Сервер от этого не
# падает, но выглядит «сломанным». Глушим этот шум — осмысленные строки пишем сами.
logging.getLogger("telethon").setLevel(logging.CRITICAL)

_LOG = config.DB_PATH.parent / "logs" / "listener.log"

CLIENTS: dict[int, object] = {}                 # acc_id -> подключённый TelegramClient
STATUS: dict = {"started": None, "accounts": {}, "hits": 0}  # снимок для веб-статуса
_NICHES: list[tuple[int | None, list[str]]] = []  # [(niche_id, [ключи]), ...] — кэш ключей

CONNECT_TIMEOUT = 25    # сек на подключение одного аккаунта (дохлый прокси не повесит всё)
RECHECK_SEC = 120       # как часто пере-сканировать: новые логины / отвалившиеся


def _load_niches() -> list[tuple[int | None, list[str]]]:
    """Активные ниши (ключи) из БД. Пусто → слушаем только личку, чаты не сканируем."""
    with database.get_conn() as conn:
        rows = conn.execute("SELECT id, keywords FROM niches WHERE active=1").fetchall()
    out: list[tuple[int | None, list[str]]] = []
    for r in rows:
        kws = [k.strip().lower() for k in (r["keywords"] or "").split(",") if k.strip()]
        if kws:
            out.append((r["id"], kws))
    return out


def _match_niche(text: str):
    low = text.lower()
    for nid, kws in _NICHES:
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


def _should_reply(acc_id: int) -> bool:
    """Авто-отвечать ли с этого аккаунта прямо сейчас: глобальный тумблер включён И
    аккаунт сейчас 'active'. Читаем из БД каждый раз — статус мог поменяться в рантайме."""
    with database.get_conn() as conn:
        if database.get_setting(conn, "tg_auto_reply", "on") != "on":
            return False
        row = conn.execute("SELECT status FROM accounts WHERE id=?", (acc_id,)).fetchone()
    return bool(row) and row["status"] == "active"


def _listenable() -> list[dict]:
    """Кого слушаем: есть авторизованная сессия, не забанен, не «родной» (protected)."""
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE tg_session IS NOT NULL AND tg_session<>'' "
            "AND status IN ('active','warming','paused') AND COALESCE(protected,0)=0"
        ).fetchall()
    return [dict(r) for r in rows]


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
        return  # незнакомый (в т.ч. взаимный прогрев) — не наш контакт, молчим
    _record_incoming(contact["id"], text_in, username, account_id=acc_id)
    _log(f"[#{acc_id}] ← {username or sender.id}: {text_in[:60]!r} (сохранено в Диалоги)")
    if _should_reply(acc_id):
        await _agent_reply(event, contact["id"], username)
        _log(f"[#{acc_id}] → авто-ответ контакту {contact['id']}")


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
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO chat_hits (niche_id, chat_id, chat_title, tg_user_id, "
            "username, name, text, keyword, source_msg_id, ts, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'new')",
            (nid, event.chat_id, title, sender.id, sender.username, name,
             text[:500], kw, event.message.id,
             str(getattr(event.message, "date", None)) if getattr(event.message, "date", None) else None),
        )
        if cur.rowcount > 0:
            STATUS["hits"] = STATUS.get("hits", 0) + 1
            database.add_event(conn, "hit", f"🎯 Запрос в «{title}»: {name}",
                               f"«{kw}» — {text[:140]}", level="good")
            _log(f"[#{acc_id}] 🎯 запрос «{kw}» от {name} в «{title}» → Запросы")


def _make_handler(acc_id: int):
    async def handler(event) -> None:
        try:
            if event.is_private:
                await _handle_private(event, acc_id)
            elif event.is_group or event.is_channel:
                await _scan_group(event, acc_id)
        except Exception as e:  # noqa: BLE001
            _log(f"[#{acc_id}] ошибка обработки сообщения: {e}")
    return handler


async def _connect(acc: dict):
    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
    await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
    if not await client.is_user_authorized():
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("сессия не авторизована — нужен повторный вход")
    client.add_event_handler(_make_handler(acc["id"]),
                             events.NewMessage(incoming=True, forwards=False))
    return client


async def _supervise() -> None:
    global _NICHES
    STATUS["started"] = datetime.datetime.now().isoformat()
    while True:
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

        if to_add:
            await asyncio.gather(*[_try(a) for a in to_add])
        ok = sum(1 for v in STATUS["accounts"].values() if v.get("ok"))
        kw = sum(len(k) for _, k in _NICHES)
        _log(f"итог: слушаю {ok} из {len(want)} аккаунтов · ниш {len(_NICHES)}/ключей {kw} · найдено запросов {STATUS.get('hits',0)}")
        await asyncio.sleep(RECHECK_SEC)


async def run() -> None:
    database.init_db()
    _log("=== старт многоаккаунтного слушателя входящих ===")
    await _supervise()


def start_in_thread() -> None:
    """Запуск в отдельном демон-потоке со своим event loop — для веб-пульта."""
    import threading

    def _runner() -> None:
        try:
            asyncio.run(run())
        except Exception as e:  # noqa: BLE001
            _log(f"слушатель аварийно остановлен: {e}")

    threading.Thread(target=_runner, name="tg-listener", daemon=True).start()


if __name__ == "__main__":
    asyncio.run(run())
