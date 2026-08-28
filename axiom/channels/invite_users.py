"""Добавление людей из книжки в НАШУ Telegram-группу («инвайтинг»).

⚠️ САМЫЙ БАНИМЫЙ СЦЕНАРИЙ В TELEGRAM. Читай перед тем, как крутить лимиты.

Массовое добавление незнакомых людей в группу Telegram считает спамом прямо и
наказывает быстро: сначала PeerFlood («слишком много действий с незнакомцами»),
дальше — бан НОМЕРА, а не только сессии. Плюс каждый добавленный видит, кто его
затащил, и жмёт «пожаловаться»: жалобы бьют по аккаунту сильнее лимитов.

Поэтому здесь всё намеренно медленно и с потолками:
  • DAILY_CAP приглашений на аккаунт в СУТКИ (не «за заход» — за сутки, считаем по
    журналу invite_log, чтобы перезапуск не обнулял счётчик);
  • пауза между приглашениями — десятки секунд, не «как быстрее»;
  • первый же PeerFlood/FloodWait ОСТАНАВЛИВАЕТ аккаунт на этот заход целиком;
  • защищённые (protected=1) не приглашают вообще — это рабочие номера;
  • приглашаем только тех, у кого известен tg_user_id (без резолва @username:
    ResolveUsername — самый лимитируемый вызов, и на нём горят первыми).

Кого Telegram не даст добавить (это НОРМА, не поломка):
  • UserPrivacyRestricted — человек запретил добавлять себя в группы. Таких много,
    в отчёте они идут отдельной строкой и больше не переспрашиваются;
  • UserNotMutualContact — можно добавить только взаимный контакт;
  • UserChannelsTooMuch — у человека предел групп.

Запуск:
    python -m channels.invite_users --chat 2963 --limit 10 --dry
    python -m channels.invite_users --chat 2963 --source 280826citrus --limit 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random

from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.types import InputPeerUser

from channels.antiban import classify_error
from channels.telegram import build_client
from db import database

# Потолки. Меньше — безопаснее; больше — быстрее и ближе к бану.
DAILY_CAP = 15                 # приглашений на ОДИН аккаунт в сутки
PAUSE = (45.0, 110.0)          # пауза между приглашениями одного аккаунта, сек
MAX_PER_RUN = 30               # сколько максимум за один заход всеми аккаунтами


def _ensure_log() -> None:
    """Журнал приглашений: кого, куда, кем и с каким исходом.

    Нужен не для отчётности, а для ЛИМИТА: без него перезапуск обнулял бы счётчик
    суток, и аккаунт за вечер уходил бы в бан несколькими заходами по 15.
    """
    with database.get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invite_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,      -- chats.id (каталожный)
                contact_id INTEGER,
                tg_user_id INTEGER,
                account_id INTEGER,
                result     TEXT,                  -- ok | privacy | flood | spam | error
                detail     TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invite_log_acc "
                     "ON invite_log(account_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invite_log_pair "
                     "ON invite_log(chat_id, tg_user_id)")


def _used_today() -> dict[int, int]:
    with database.get_conn() as conn:
        return {r["account_id"]: r["n"] for r in conn.execute(
            "SELECT account_id, COUNT(*) n FROM invite_log "
            "WHERE date(created_at)=date('now') AND result='ok' GROUP BY account_id")}


def _inviters(chat_id: int, only_ids: list[int] | None) -> list[dict]:
    """Кем приглашаем: аккаунт должен СОСТОЯТЬ в этой группе и не быть защищённым."""
    with database.get_conn() as conn:
        sql = ("SELECT a.id, a.label, a.tg_session, a.proxy, a.api_id, a.api_hash "
               "FROM accounts a JOIN account_chats ac ON ac.account_id=a.id "
               "WHERE ac.chat_id=? AND COALESCE(a.protected,0)=0 "
               "AND a.tg_session IS NOT NULL AND a.tg_session<>'' "
               "AND COALESCE(a.session_alive,1)=1 AND COALESCE(a.status,'')<>'banned' "
               "AND a.proxy IS NOT NULL AND a.proxy<>''")
        params: list = [chat_id]
        if only_ids:
            sql += f" AND a.id IN ({','.join('?' * len(only_ids))})"
            params += only_ids
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _targets(chat_id: int, source: str | None, tag: str | None, limit: int) -> list[dict]:
    """Кого приглашаем: контакты с известным tg_user_id, которых ещё не звали в ЭТОТ чат.

    Повторно не зовём никого, даже с отказом по приватности: если человек закрыл
    добавление, второй заход ничего не изменит, а лимит аккаунта сожжёт.
    """
    with database.get_conn() as conn:
        sql = ("SELECT id, tg_user_id, name, username FROM contacts "
               "WHERE tg_user_id IS NOT NULL AND deleted_at IS NULL "
               "AND id NOT IN (SELECT contact_id FROM invite_log "
               "               WHERE chat_id=? AND contact_id IS NOT NULL)")
        params: list = [chat_id]
        if source:
            sql += " AND source=?"
            params.append(source)
        if tag:
            sql += " AND COALESCE(tags,'') LIKE ?"
            params.append(f"%{tag}%")
        sql += " ORDER BY COALESCE(parse_priority, 9), id LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _chat(chat_id: int) -> dict | None:
    with database.get_conn() as conn:
        r = conn.execute("SELECT id, title, username, tg_chat_id, tg_access_hash "
                         "FROM chats WHERE id=?", (chat_id,)).fetchone()
    return dict(r) if r else None


def _log(chat_id: int, c: dict, acc_id: int, result: str, detail: str = "") -> None:
    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO invite_log (chat_id, contact_id, tg_user_id, account_id, result, detail) "
            "VALUES (?,?,?,?,?,?)",
            (chat_id, c.get("id"), c.get("tg_user_id"), acc_id, result, detail[:200]))


async def _invite_batch(acc: dict, chat, people: list[dict], chat_id: int,
                        report: dict) -> None:
    """Один аккаунт зовёт свою пачку. Флуд/спам — немедленный выход из аккаунта."""
    label = acc.get("label") or f"#{acc['id']}"
    res = {"ok": 0, "privacy": 0, "failed": 0, "stopped": None}
    report[acc["id"]] = res
    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
    try:
        await client.connect()
        if not await client.is_user_authorized():
            res["stopped"] = "сессия не авторизована"
            return
        entity = await client.get_entity(chat)
        for c in people:
            try:
                user = await client.get_input_entity(int(c["tg_user_id"]))
                await client(InviteToChannelRequest(entity, [user]))
                res["ok"] += 1
                _log(chat_id, c, acc["id"], "ok")
                print(f"[{label}] + {c.get('name') or c.get('tg_user_id')}")
            except FloodWaitError as e:
                # Ждать часами внутри захода нельзя: аккаунт уже под подозрением.
                res["stopped"] = f"FloodWait {e.seconds}с — аккаунт снят с захода"
                _log(chat_id, c, acc["id"], "flood", str(e)[:150])
                print(f"[{label}] ⚠ {res['stopped']}")
                break
            except Exception as e:  # noqa: BLE001
                kind = classify_error(e)
                name = type(e).__name__
                if kind == "spam" or "too many" in str(e).lower():
                    res["stopped"] = "PeerFlood — Telegram считает действия спамом"
                    _log(chat_id, c, acc["id"], "spam", name)
                    print(f"[{label}] ⛔ {res['stopped']}, останавливаюсь")
                    break
                if "Privacy" in name or "NotMutual" in name or "TooMuch" in name:
                    res["privacy"] += 1
                    _log(chat_id, c, acc["id"], "privacy", name)
                else:
                    res["failed"] += 1
                    _log(chat_id, c, acc["id"], "error", f"{name}: {str(e)[:120]}")
            await asyncio.sleep(random.uniform(*PAUSE))
    except Exception as e:  # noqa: BLE001
        res["stopped"] = f"{type(e).__name__}: {str(e)[:100]}"
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def run(chat_id: int, limit: int, source: str | None = None, tag: str | None = None,
              account_ids: list[int] | None = None, dry: bool = False) -> dict:
    database.init_db()
    _ensure_log()
    chat = _chat(chat_id)
    if not chat:
        return {"ok": False, "error": f"чата #{chat_id} нет в каталоге"}
    accs = _inviters(chat_id, account_ids)
    if not accs:
        return {"ok": False, "error": "нет аккаунтов, которые СОСТОЯТ в этой группе "
                                      "(защищённые не приглашают). Сначала вступи в неё"}
    used = _used_today()
    quota = {a["id"]: max(0, DAILY_CAP - used.get(a["id"], 0)) for a in accs}
    free = sum(quota.values())
    if not free:
        return {"ok": False, "error": f"суточный лимит исчерпан у всех ({DAILY_CAP} на аккаунт) "
                                      f"— продолжить можно завтра"}
    limit = min(limit, free, MAX_PER_RUN)
    people = _targets(chat_id, source, tag, limit)
    if not people:
        return {"ok": False, "error": "некого приглашать: нужны контакты с известным "
                                      "Telegram-id, которых ещё не звали в этот чат"}
    if dry:
        return {"ok": True, "dry": True, "would_invite": len(people),
                "chat": chat.get("title"), "accounts": len(accs),
                "quota_left": free, "daily_cap": DAILY_CAP,
                "sample": [p.get("name") or p.get("username") or p.get("tg_user_id")
                           for p in people[:10]]}
    # Раздаём людей по аккаунтам в пределах их остатка на сегодня.
    plan: dict[int, list[dict]] = {a["id"]: [] for a in accs}
    i = 0
    for c in people:
        for _ in range(len(accs)):
            a = accs[i % len(accs)]
            i += 1
            if len(plan[a["id"]]) < quota[a["id"]]:
                plan[a["id"]].append(c)
                break
    peer = chat["tg_chat_id"]
    if chat.get("username"):
        peer = chat["username"]
    report: dict = {}
    # Последовательно, не параллельно: одновременные приглашения в одну группу с
    # разных аккаунтов — заметный почерк, за него прилетает быстрее.
    for a in accs:
        if plan[a["id"]]:
            await _invite_batch(a, peer, plan[a["id"]], chat_id, report)
    ok = sum(r["ok"] for r in report.values())
    privacy = sum(r["privacy"] for r in report.values())
    failed = sum(r["failed"] for r in report.values())
    stopped = {k: v["stopped"] for k, v in report.items() if v.get("stopped")}
    return {"ok": True, "chat": chat.get("title"), "invited": ok, "privacy": privacy,
            "failed": failed, "stopped": stopped, "daily_cap": DAILY_CAP,
            "quota_left": max(0, free - ok)}


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: пригласить людей в нашу группу")
    p.add_argument("--chat", type=int, required=True, help="chats.id целевой группы")
    p.add_argument("--limit", type=int, default=10, help="сколько пригласить за заход")
    p.add_argument("--source", help="брать только контакты с этим источником")
    p.add_argument("--tag", help="брать только контакты с этим тегом")
    p.add_argument("--accounts", help="приглашать этими аккаунтами (id через запятую)")
    p.add_argument("--dry", action="store_true", help="показать план, никого не звать")
    args = p.parse_args()
    ids = [int(x) for x in args.accounts.split(",") if x.strip()] if args.accounts else None
    try:
        res = asyncio.run(run(args.chat, args.limit, args.source, args.tag, ids, args.dry))
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
