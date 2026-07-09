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

from telethon import events
from telethon.sessions import StringSession

import config
from channels.telegram import _agent_reply, _record_incoming, build_client
from db import database

_LOG = config.DB_PATH.parent / "logs" / "listener.log"

CLIENTS: dict[int, object] = {}                 # acc_id -> подключённый TelegramClient
STATUS: dict = {"started": None, "accounts": {}}  # снимок для веб-статуса

CONNECT_TIMEOUT = 25    # сек на подключение одного аккаунта (дохлый прокси не повесит всё)
RECHECK_SEC = 120       # как часто пере-сканировать: новые логины / отвалившиеся


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


def _make_handler(acc_id: int):
    async def handler(event) -> None:
        try:
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
        except Exception as e:  # noqa: BLE001
            _log(f"[#{acc_id}] ошибка обработки входящего: {e}")
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
    STATUS["started"] = datetime.datetime.now().isoformat()
    while True:
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
        _log(f"итог: слушаю {ok} из {len(want)} годных аккаунтов")
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
