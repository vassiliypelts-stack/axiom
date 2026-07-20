"""Авто-вступление армии аккаунтов в чаты каталога (Волна C, фаза 2).

Распределяет чаты из каталога между боевыми/прогреваемыми аккаунтами и вступает в
них человекоподобно: лимит на аккаунт за заход, большие паузы, обработка FloodWait
и авто-детект бана. Пишет членство в account_chats (many-to-many) — основа отчёта
покрытия «сколько агентов в скольких чатах». Поддерживает публичные (@username) и
закрытые по инвайту (+hash) чаты. Чаты раздаются round-robin — чтобы армия покрыла
как можно БОЛЬШЕ разных чатов (широта), а не толпилась в одном.

⚠️ Массовые вступления — быстрый путь к бану. Лимит на аккаунт по умолчанию мал,
паузы большие. Прогреваемые аккаунты тоже вступают (это пассив), но осторожно.

Запуск:
    python -m channels.chat_join --per 3              # каждый акк — max 3 новых чата
    python -m channels.chat_join --per 3 --favorites  # только ⭐ избранные чаты
    python -m channels.chat_join --id 9 --per 5       # только аккаунт #9
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random

from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from channels.chat_scan import _kind, can_write
from channels.tg_invites import INVITE_RE
from channels.telegram import build_client
from db import database

JOIN_PAUSE = (35, 90)      # антибан-пауза между вступлениями ОДНОГО аккаунта, сек
MAX_FLOOD_SKIP = 600       # если FloodWait дольше — пропускаем аккаунт на этот заход


def _joinable_accounts(only_id: int | None):
    """Кем вступаем. Отсеиваем ЗАВЕДОМО мёртвые сессии (session_alive=0): наличие строки
    tg_session ещё не значит, что она рабочая (см. channels/session_check.py) — иначе
    половина заходов уходила бы в стену. NULL (не проверяли) пропускаем: «не знаю» —
    не повод не работать."""
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE tg_session IS NOT NULL AND tg_session<>'' "
            "AND status IN ('active','warming') AND COALESCE(protected,0)=0 "
            "AND COALESCE(session_alive,1)=1"
        ).fetchall()
    accs = [dict(r) for r in rows]
    if only_id is not None:
        accs = [a for a in accs if a["id"] == only_id]
    return accs


def _candidate_chats(favorites: bool, chat_ids: list[int] | None = None):
    """Чаты каталога, куда можно вступить: публичные (@) или с инвайт-ссылкой.
    Избранные — вперёд, дальше по числу участников. status skip/joined-целиком не режем:
    членство пер-аккаунт проверяем отдельно (в чат могут войти несколько аккаунтов).
    chat_ids — сузить до конкретных чатов (напр. только что найденных авто-поиском)."""
    if chat_ids is not None and not chat_ids:
        return []
    with database.get_conn() as conn:
        sql = ("SELECT id, title, username, link FROM chats "
               "WHERE COALESCE(status,'') NOT IN ('skip','banned') "
               "AND ((username IS NOT NULL AND username<>'') OR (link LIKE '%t.me/+%') "
               "OR (link LIKE '%joinchat%'))")
        params: list = []
        if favorites:
            sql += " AND COALESCE(favorite,0)=1"
        if chat_ids:
            sql += f" AND id IN ({','.join('?' * len(chat_ids))})"
            params += list(chat_ids)
        sql += " ORDER BY COALESCE(favorite,0) DESC, COALESCE(members_count,0) DESC, id"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _already() -> set[tuple[int, int]]:
    with database.get_conn() as conn:
        return {(r["account_id"], r["chat_id"])
                for r in conn.execute("SELECT account_id, chat_id FROM account_chats")}


def _plan(accs: list[dict], chats: list[dict], per: int) -> dict[int, list[dict]]:
    """Round-robin: раздаём чаты по аккаунтам, максимизируя ШИРОТУ охвата."""
    already = _already()
    assign: dict[int, list[dict]] = {a["id"]: [] for a in accs}
    if not accs:
        return assign
    ai = 0
    n = len(accs)
    for ch in chats:
        if all(len(assign[a["id"]]) >= per for a in accs):
            break
        for _ in range(n):
            a = accs[ai % n]; ai += 1
            if len(assign[a["id"]]) >= per:
                continue
            if (a["id"], ch["id"]) in already:
                continue
            assign[a["id"]].append(ch)
            break
    return assign


def _invite_hash(link: str | None) -> str | None:
    if not link:
        return None
    m = INVITE_RE.search(link)
    return m.group(1) if m else None


def _record_membership(acc_id: int, chat: dict, cw: str | None, kind: str | None,
                       tg_chat_id: int | None = None) -> None:
    with database.get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO account_chats (account_id, chat_id, can_write) VALUES (?,?,?)",
            (acc_id, chat["id"], cw),
        )
        conn.execute(
            "UPDATE account_chats SET can_write=COALESCE(?,can_write) WHERE account_id=? AND chat_id=?",
            (cw, acc_id, chat["id"]),
        )
        # tg_chat_id — СЫРОЙ telegram-id (без -100). Без него чат без @username потом не
        # открыть: слушатель подставит каталожный chats.id и уедет не в тот (или в никуда).
        conn.execute(
            "UPDATE chats SET in_account='yes', status='joined', joined_by=COALESCE(joined_by,?), "
            "can_write=COALESCE(?,can_write), kind=COALESCE(kind,?), "
            "tg_chat_id=COALESCE(?,tg_chat_id) WHERE id=?",
            (acc_id, cw, kind, tg_chat_id, chat["id"]),
        )


async def _join_one(acc: dict, chats: list[dict], report: dict) -> None:
    acc_id = acc["id"]
    r = report[acc_id] = {"label": acc.get("label") or f"#{acc_id}",
                          "joined": [], "failed": [], "pending": []}
    if not chats:
        return
    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
    try:
        await asyncio.wait_for(client.connect(), timeout=25)
        if not await client.is_user_authorized():
            r["failed"].append({"chat": "—", "err": "сессия не авторизована (нужен вход)"})
            return
    except Exception as e:  # noqa: BLE001
        r["failed"].append({"chat": "—", "err": f"не подключился: {str(e)[:80]}"})
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        for i, ch in enumerate(chats):
            title = ch.get("title") or ch.get("username") or f"#{ch['id']}"
            try:
                if ch.get("username"):
                    upd = await client(JoinChannelRequest(ch["username"]))
                else:
                    h = _invite_hash(ch.get("link"))
                    if not h:
                        r["failed"].append({"chat": title, "err": "нет @username и валидного инвайта"})
                        continue
                    upd = await client(ImportChatInviteRequest(h))
                # Права/тип ПОСЛЕ вступления. Сущность из ответа на join отражает ещё
                # до-вступленческое состояние (left=True), поэтому берём её только как
                # запасной вариант, а основной путь — перечитать entity заново.
                # Иначе в каталоге залипает «не вступил» у чата, где мы уже сидим, —
                # ровно та ложь, из-за которой рассылка бьётся в стену.
                cw = kind = tg_id = None
                try:
                    fresh = await client.get_entity(ch.get("username") or (getattr(upd, "chats", None) or [None])[0])
                    cw = can_write(fresh); kind = _kind(fresh); tg_id = getattr(fresh, "id", None)
                except Exception:  # noqa: BLE001
                    try:
                        ent = (getattr(upd, "chats", None) or [None])[0]
                        if ent is not None:
                            kind = _kind(ent)
                            tg_id = getattr(ent, "id", None)
                            cw = can_write(ent)
                            if cw == "не вступил":
                                cw = None   # заведомая неправда сразу после входа — лучше пусто
                    except Exception:  # noqa: BLE001
                        pass
                _record_membership(acc_id, ch, cw, kind, tg_id)
                r["joined"].append(title)
            except FloodWaitError as e:
                if e.seconds > MAX_FLOOD_SKIP:
                    r["failed"].append({"chat": title, "err": f"FloodWait {e.seconds}с — стоп аккаунта"})
                    break
                await asyncio.sleep(e.seconds + 5)
                continue
            except Exception as e:  # noqa: BLE001
                from channels.antiban import classify_error
                low = str(e).lower()
                if "already" in low or "participant" in low:
                    _record_membership(acc_id, ch, None, None)  # уже внутри — фиксируем членство
                    r["joined"].append(title + " (уже был)")
                elif (type(e).__name__ == "InviteRequestSentError"
                      or "successfully requested to join" in low):
                    # Это НЕ ошибка: чат принимает только по заявке, заявка ушла и ждёт
                    # админа. Раньше падало в «не вышло» и выглядело как провал вступления.
                    r["pending"].append(title)
                    with database.get_conn() as conn:
                        conn.execute("UPDATE chats SET can_write='нужно одобрение' WHERE id=?",
                                     (ch["id"],))
                elif classify_error(e) == "ban":
                    with database.get_conn() as conn:
                        conn.execute("UPDATE accounts SET status='banned' WHERE id=?", (acc_id,))
                        database.add_event(conn, "account_banned",
                                           f"⛔ Аккаунт «{r['label']}» забанен при вступлении",
                                           str(e)[:160], level="bad", account_id=acc_id)
                    r["failed"].append({"chat": title, "err": "БАН аккаунта — остановлен"})
                    break
                else:
                    r["failed"].append({"chat": title, "err": str(e)[:80]})
            if i < len(chats) - 1:
                await asyncio.sleep(random.uniform(*JOIN_PAUSE))
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def run(per: int, favorites: bool, only_id: int | None,
              chat_ids: list[int] | None = None) -> dict:
    """Возвращает сводку (не печатает) — чтобы модули-искатели могли вызвать вступление
    у себя внутри и вложить этот отчёт в свою сводку. Печать — в main()."""
    database.init_db()
    accs = _joinable_accounts(only_id)
    if not accs:
        return {"ok": False, "error": "нет годных аккаунтов (нужны active/warming с сессией)"}
    chats = _candidate_chats(favorites, chat_ids)
    if not chats:
        return {"ok": False, "error": "нет чатов-кандидатов в каталоге"
                + (" среди избранных" if favorites else "")}
    plan = _plan(accs, chats, per)
    report: dict = {}
    # аккаунты идут параллельно (разные IP/сессии), внутри аккаунта — по одному с паузой
    await asyncio.gather(*[_join_one(a, plan[a["id"]], report) for a in accs])
    total_joined = sum(len(r["joined"]) for r in report.values())
    total_failed = sum(len(r["failed"]) for r in report.values())
    total_pending = sum(len(r.get("pending") or []) for r in report.values())
    return {"ok": True, "accounts": len(accs), "joined": total_joined,
            "failed": total_failed, "pending": total_pending, "report": report}


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM авто-вступление армии в чаты каталога")
    p.add_argument("--per", type=int, default=3, help="макс новых чатов на один аккаунт за заход")
    p.add_argument("--favorites", action="store_true", help="только ⭐ избранные чаты")
    p.add_argument("--id", type=int, default=None, dest="acc_id", help="только один аккаунт по id")
    p.add_argument("--chats", default=None, help="только эти каталожные чаты (id через запятую)")
    args = p.parse_args()
    ids = [int(x) for x in args.chats.split(",") if x.strip()] if args.chats else None
    res = asyncio.run(run(args.per, args.favorites, args.acc_id, ids))
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
