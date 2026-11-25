"""Кампании «посты в группы»: один пост в каждую группу со СВОЕЙ пригласительной ссылкой.

Зачем. Набор в своё сообщество (первое — «Прогрессивные», t.me/progressive_global) идёт
не рассылкой в личку, а постом в группах, где наш аккаунт состоит. Чтобы видеть, какая
группа реально приводит людей, у каждой группы своя ссылка t.me/+…: её создаёт бот-админ
сообщества (createChatInviteLink с названием «gp<id> <группа>»), а бот сообщества пишет
каждое вступление с использованной ссылкой в joins.jsonl. Отсюда колонка «вступили».

Как публикует. Через клиента СЛУШАТЕЛЯ (listener.CLIENTS + run_coroutine_threadsafe), как
channel_boost и send_via_listener: своя сессия не поднимается, слушатель аккаунт не
отпускает, ключ не горит. Аккаунт не подключён слушателем — заход пропускается.

Темп. Один пост за тик-окно: не чаще interval_min (± случайный разброс) и только в
рабочие часы кампании. Через 40+ минут после поста проверяем, жив ли он (модераторы
чужих групп удаляют рекламу) — это вторая метрика эффективности группы.

Таблицы: gp_campaigns, gp_targets (создаются сами). Пульт: блок «📣 Посты в группы» в
разделе «Кампании» (index.html, gpRender) и роуты /api/group_posts* в web/app.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from db import database

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS gp_campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        community_chat_id INTEGER NOT NULL,      -- id сообщества для Bot API (-100…)
        community_link TEXT,                     -- публичная ссылка сообщества (для справки)
        bot_token_path TEXT NOT NULL,            -- файл с токеном бота-админа сообщества
        joins_path TEXT NOT NULL,                -- joins.jsonl бота сообщества
        account_id INTEGER NOT NULL,             -- с какого аккаунта публикуем (по умолчанию)
        texts_json TEXT NOT NULL,                -- варианты поста, {link} — место ссылки
        interval_min INTEGER DEFAULT 45,
        work_start INTEGER DEFAULT 10,
        work_end INTEGER DEFAULT 19,
        tz TEXT DEFAULT 'Europe/Moscow',
        status TEXT DEFAULT 'draft',             -- draft | running | paused | done
        last_post_ts REAL DEFAULT 0,
        next_gap_min REAL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS gp_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL,
        chat_id INTEGER,                         -- chats.id каталога
        title TEXT,
        username TEXT,
        tg_chat_id INTEGER,
        members INTEGER,
        account_id INTEGER,                      -- NULL = аккаунт кампании
        topic_id INTEGER,                        -- если группа-форум: в какую тему
        variant INTEGER DEFAULT 0,
        invite_link TEXT,
        link_name TEXT,
        status TEXT DEFAULT 'pending',           -- pending | posted | error | skipped
        check_note TEXT,                         -- итог проверки «можно ли писать»
        msg_id INTEGER,
        posted_at TEXT,
        posted_ts REAL,
        alive INTEGER,                           -- 1 пост на месте, 0 удалён, NULL не проверяли
        alive_checked_ts REAL,
        error TEXT,
        joins INTEGER DEFAULT 0,
        sort INTEGER DEFAULT 0
    )""",
]

ALIVE_CHECK_AFTER = 40 * 60


def ensure(conn) -> None:
    for s in SCHEMA:
        conn.execute(s)


def _log(s: str) -> None:
    print(f"[group_post] {s}", flush=True)


# ------------------------------------------------------------------ Bot API -----
def _bot(token_path: str, method: str, **params):
    token = open(os.path.expanduser(token_path)).read().strip()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
    if not res.get("ok"):
        raise RuntimeError(res.get("description"))
    return res["result"]


# ------------------------------------------------------------------ создание -----
def create_campaign(name: str, community_chat_id: int, community_link: str, bot_token_path: str,
                    joins_path: str, account_id: int, texts: list[str], chat_ids: list[int],
                    interval_min: int = 45, work_start: int = 10, work_end: int = 19,
                    tz: str = "Europe/Moscow") -> int:
    with database.get_conn() as conn:
        ensure(conn)
        cur = conn.execute(
            "INSERT INTO gp_campaigns (name, community_chat_id, community_link, bot_token_path, "
            "joins_path, account_id, texts_json, interval_min, work_start, work_end, tz) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, community_chat_id, community_link, bot_token_path, joins_path, account_id,
             json.dumps(texts, ensure_ascii=False), interval_min, work_start, work_end, tz))
        cid = cur.lastrowid
        add_targets(conn, cid, chat_ids, len(texts))
        return cid


def add_targets(conn, cid: int, chat_ids: list[int], n_texts: int) -> int:
    have = {r["chat_id"] for r in conn.execute("SELECT chat_id FROM gp_targets WHERE campaign_id=?", (cid,))}
    base = conn.execute("SELECT COUNT(*) c FROM gp_targets WHERE campaign_id=?", (cid,)).fetchone()["c"]
    added = 0
    for i, chat_id in enumerate(chat_ids):
        if chat_id in have:
            continue
        r = conn.execute("SELECT id, title, username, tg_chat_id, members_count FROM chats WHERE id=?",
                         (chat_id,)).fetchone()
        if not r:
            continue
        n = base + added
        conn.execute(
            "INSERT INTO gp_targets (campaign_id, chat_id, title, username, tg_chat_id, members, variant, sort) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cid, chat_id, r["title"], r["username"], r["tg_chat_id"], r["members_count"],
             n % max(n_texts, 1), n))
        added += 1
    return added


def ensure_links(cid: int) -> int:
    """Создать недостающие пригласительные ссылки (ботом-админом сообщества)."""
    made = 0
    with database.get_conn() as conn:
        ensure(conn)
        c = conn.execute("SELECT * FROM gp_campaigns WHERE id=?", (cid,)).fetchone()
        rows = conn.execute("SELECT id, title FROM gp_targets WHERE campaign_id=? "
                            "AND COALESCE(invite_link,'')=''", (cid,)).fetchall()
    for t in rows:
        name = f"gp{t['id']} {t['title'] or ''}"[:32]
        res = _bot(c["bot_token_path"], "createChatInviteLink", chat_id=c["community_chat_id"], name=name)
        with database.get_conn() as conn:
            conn.execute("UPDATE gp_targets SET invite_link=?, link_name=? WHERE id=?",
                         (res["invite_link"], name, t["id"]))
        made += 1
    return made


# ------------------------------------------------------------------ статистика ---
def refresh_joins(conn, c) -> None:
    """joins.jsonl бота сообщества → gp_targets.joins (по ссылке, запасной ключ — название)."""
    path = os.path.expanduser(c["joins_path"])
    if not os.path.exists(path):
        return
    by_link: dict[str, set] = {}
    by_name: dict[str, set] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("link"):
                by_link.setdefault(r["link"], set()).add(r.get("user_id"))
            if r.get("link_name"):
                by_name.setdefault(r["link_name"], set()).add(r.get("user_id"))
    for t in conn.execute("SELECT id, invite_link, link_name FROM gp_targets WHERE campaign_id=?", (c["id"],)):
        users = by_link.get(t["invite_link"] or "", set()) | by_name.get(t["link_name"] or "", set())
        conn.execute("UPDATE gp_targets SET joins=? WHERE id=?", (len(users), t["id"]))


# ------------------------------------------------------------------ Telegram -----
def _run(coro, timeout: float = 120.0):
    from channels import listener
    if listener._LOOP is None:
        raise RuntimeError("слушатель не запущен")
    return asyncio.run_coroutine_threadsafe(coro, listener._LOOP).result(timeout=timeout)


def _client(acc_id: int):
    from channels import listener
    return listener.CLIENTS.get(acc_id)


async def _entity(client, t):
    if t["username"]:
        return await client.get_input_entity(t["username"])
    from telethon.tl.types import PeerChannel
    try:
        return await client.get_input_entity(PeerChannel(int(t["tg_chat_id"])))
    except ValueError:
        async for d in client.iter_dialogs():
            if getattr(d.entity, "id", None) == int(t["tg_chat_id"]):
                return d.input_entity
        raise


async def _inspect(client, t) -> str:
    """Можно ли сюда писать: форум, закрытая General, медленный режим, бан."""
    from telethon.tl.functions.channels import GetFullChannelRequest, GetParticipantRequest
    ent = await _entity(client, t)
    full = await client(GetFullChannelRequest(ent))
    ch = full.chats[0]
    notes = []
    if getattr(ch, "forum", False):
        notes.append("форум" + (f", тема {t['topic_id']}" if t["topic_id"] else ", пишем в General"))
    dbr = getattr(ch, "default_banned_rights", None)
    try:
        me = await client(GetParticipantRequest(ent, "me"))
        p = me.participant
        if getattr(p, "banned_rights", None) and p.banned_rights.send_messages:
            return "⛔ аккаунту запрещено писать"
        is_admin = type(p).__name__ in ("ChannelParticipantAdmin", "ChannelParticipantCreator")
    except Exception as e:  # noqa: BLE001
        return f"⛔ не участник: {e}"
    if dbr and dbr.send_messages and not is_admin:
        return "⛔ писать могут только админы"
    if dbr and dbr.embed_links and not is_admin:
        notes.append("⚠️ ссылки запрещены правами группы")
    sm = getattr(full.full_chat, "slowmode_seconds", None)
    if sm:
        notes.append(f"медленный режим {sm} с")
    return "✅ можно писать" + (" · " + ", ".join(notes) if notes else "")


def inspect_all(cid: int) -> list[dict]:
    with database.get_conn() as conn:
        ensure(conn)
        c = conn.execute("SELECT * FROM gp_campaigns WHERE id=?", (cid,)).fetchone()
        ts = conn.execute("SELECT * FROM gp_targets WHERE campaign_id=? ORDER BY sort", (cid,)).fetchall()
    out = []
    for t in ts:
        client = _client(t["account_id"] or c["account_id"])
        if client is None:
            note = "⛔ аккаунт не подключён слушателем"
        else:
            try:
                note = _run(_inspect(client, t), 60)
            except Exception as e:  # noqa: BLE001
                note = f"⛔ {type(e).__name__}: {e}"
        with database.get_conn() as conn:
            conn.execute("UPDATE gp_targets SET check_note=? WHERE id=?", (note, t["id"]))
        out.append({"id": t["id"], "title": t["title"], "note": note})
        time.sleep(2)
    return out


async def _post(client, t, text: str) -> int:
    ent = await _entity(client, t)
    m = await client.send_message(ent, text, link_preview=False,
                                  reply_to=t["topic_id"] or None)
    return m.id


async def _alive(client, t) -> bool:
    ent = await _entity(client, t)
    m = await client.get_messages(ent, ids=t["msg_id"])
    return m is not None and not type(m).__name__ == "MessageEmpty"


def _in_hours(c) -> bool:
    now = datetime.now(ZoneInfo(c["tz"] or "Europe/Moscow"))
    return now.weekday() < 6 and c["work_start"] <= now.hour < c["work_end"]


def post_one(cid: int, force: bool = False) -> str:
    """Опубликовать следующий пост кампании. force — без проверки окна и интервала."""
    with database.get_conn() as conn:
        ensure(conn)
        c = conn.execute("SELECT * FROM gp_campaigns WHERE id=?", (cid,)).fetchone()
        if not c:
            return "нет кампании"
        if not force:
            if c["status"] != "running":
                return "кампания не запущена"
            if not _in_hours(c):
                return "вне рабочих часов"
            if time.time() - (c["last_post_ts"] or 0) < (c["next_gap_min"] or c["interval_min"]) * 60:
                return "ещё рано"
        t = conn.execute("SELECT * FROM gp_targets WHERE campaign_id=? AND status='pending' "
                         "ORDER BY sort LIMIT 1", (cid,)).fetchone()
        if not t:
            conn.execute("UPDATE gp_campaigns SET status='done' WHERE id=? AND status='running'", (cid,))
            return "все группы пройдены"
    if not t["invite_link"]:
        ensure_links(cid)
        with database.get_conn() as conn:
            t = conn.execute("SELECT * FROM gp_targets WHERE id=?", (t["id"],)).fetchone()
    texts = json.loads(c["texts_json"])
    text = texts[t["variant"] % len(texts)].replace("{link}", t["invite_link"])
    acc = t["account_id"] or c["account_id"]
    client = _client(acc)
    if client is None:
        return f"аккаунт #{acc} не подключён слушателем — жду"
    gap = c["interval_min"] * random.uniform(0.8, 1.4)
    try:
        mid = _run(_post(client, t, text))
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"[:300]
        flood = "Flood" in type(e).__name__ or "SlowMode" in type(e).__name__
        with database.get_conn() as conn:
            if not flood:
                conn.execute("UPDATE gp_targets SET status='error', error=? WHERE id=?", (err, t["id"]))
            conn.execute("UPDATE gp_campaigns SET last_post_ts=?, next_gap_min=? WHERE id=?",
                         (time.time(), gap * (3 if flood else 0.3), cid))
        _log(f"#{cid} → {t['title']}: {err}")
        return f"ошибка: {err}"
    with database.get_conn() as conn:
        conn.execute("UPDATE gp_targets SET status='posted', msg_id=?, posted_at=datetime('now'), "
                     "posted_ts=?, error=NULL WHERE id=?", (mid, time.time(), t["id"]))
        conn.execute("UPDATE gp_campaigns SET last_post_ts=?, next_gap_min=? WHERE id=?",
                     (time.time(), gap, cid))
        try:
            database.add_event(conn, "group_post", f"📣 Пост в «{t['title']}» опубликован (кампания «{c['name']}»)")
        except Exception:  # noqa: BLE001
            pass
    _log(f"#{cid} → {t['title']}: опубликовано, msg {mid}")
    return f"опубликовано в «{t['title']}»"


def check_alive(cid: int) -> None:
    with database.get_conn() as conn:
        c = conn.execute("SELECT * FROM gp_campaigns WHERE id=?", (cid,)).fetchone()
        due = conn.execute(
            "SELECT * FROM gp_targets WHERE campaign_id=? AND status='posted' AND msg_id IS NOT NULL "
            "AND posted_ts < ? AND (alive_checked_ts IS NULL OR (alive=1 AND alive_checked_ts < ?)) LIMIT 3",
            (cid, time.time() - ALIVE_CHECK_AFTER, time.time() - 24 * 3600)).fetchall()
    for t in due:
        client = _client(t["account_id"] or c["account_id"])
        if client is None:
            continue
        try:
            ok = _run(_alive(client, t), 60)
        except Exception as e:  # noqa: BLE001
            _log(f"проверка поста {t['id']}: {e}")
            continue
        with database.get_conn() as conn:
            conn.execute("UPDATE gp_targets SET alive=?, alive_checked_ts=? WHERE id=?",
                         (1 if ok else 0, time.time(), t["id"]))


def tick() -> None:
    """Фоновый тик пульта (раз в минуту): ссылки, вступления, публикация, проверка постов."""
    with database.get_conn() as conn:
        ensure(conn)
        camps = conn.execute("SELECT * FROM gp_campaigns WHERE status IN ('running','paused','done')").fetchall()
        for c in camps:
            refresh_joins(conn, c)
    for c in camps:
        if c["status"] == "running":
            res = post_one(c["id"])
            if res.startswith(("опубликовано", "ошибка")):
                _log(f"#{c['id']}: {res}")
        check_alive(c["id"])


# ------------------------------------------------------------------ для пульта ---
def overview() -> list[dict]:
    with database.get_conn() as conn:
        ensure(conn)
        out = []
        for c in conn.execute("SELECT * FROM gp_campaigns ORDER BY id DESC"):
            ts = [dict(t) for t in conn.execute(
                "SELECT * FROM gp_targets WHERE campaign_id=? ORDER BY joins DESC, sort", (c["id"],))]
            d = dict(c)
            d["texts"] = json.loads(d.pop("texts_json"))
            d.pop("bot_token_path", None)
            d["targets"] = ts
            d["total_joins"] = sum(t["joins"] or 0 for t in ts)
            d["posted"] = sum(1 for t in ts if t["status"] == "posted")
            out.append(d)
        return out


def set_status(cid: int, status: str) -> None:
    assert status in ("running", "paused", "draft")
    with database.get_conn() as conn:
        conn.execute("UPDATE gp_campaigns SET status=? WHERE id=?", (status, cid))


def set_target(tid: int, **fields) -> None:
    allowed = {"status", "topic_id", "account_id", "variant"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    with database.get_conn() as conn:
        conn.execute(f"UPDATE gp_targets SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                     (*sets.values(), tid))
