"""Анализ чата/канала для каталога AXIOM (Волна C, фаза 1 — только чтение).

По цели (@username/ссылка/id) аккаунт ЧИТАЕТ чат (НЕ вступает):
  • название, тип, число участников;
  • лёгкая оценка активности (сообщений/день по последним сообщениям);
  • список админов (часто это ЛПР) — пишется в таблицу chat_admins.

Результат сохраняется в таблицы chats / chat_admins и печатается JSON-строкой
(для веб-пульта). Дедуп чата по @username; админы перезаписываются.

⚠️ Только чтение, без вступления и без рассылки. Вступление (фаза 2) — отдельно.

Запуск:
    python -m channels.chat_scan --target @somechat
    python -m channels.chat_scan --target @somechat --id 5   # обновить чат №5
"""
from __future__ import annotations

import argparse
import asyncio
import json

from telethon.errors import ChatAdminRequiredError, FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel, Chat

from channels.telegram import client_for_account
from channels.tg_parser import _display_name, _resolve_scan_chat, collect_admins
from db import database


def _kind(entity) -> str:
    if isinstance(entity, Channel):
        if entity.megagroup:
            return "супергруппа"
        if entity.broadcast:
            return "канал"
    return "группа"


def can_write(entity) -> str:
    """Могу ли Я отправить ТЕКСТ в этот чат. Отвечает ровно на этот вопрос — по нему
    решаем, годится ли чат под рассылку.

    Два разных набора прав, которые легко перепутать (на этом и горело):
      • default_banned_rights — правила для ВСЕХ участников чата;
      • banned_rights         — мут, выданный админами ЛИЧНО МНЕ.
    Личный мут перекрывает любые общие правила, поэтому проверяем его ПЕРВЫМ. Раньше
    читались только общие права: чат, где мне персонально закрыли рот, показывался как
    «да» (реальный случай — @allbusinessproNovosibirsk), и рассылка билась в стену.

    Значения: да | только админы | заблокирован | не вступил | нужно одобрение | неизвестно.
    NB: slow mode здесь НЕ учитываем — писать он не запрещает, только замедляет, и
    вердикт «ограничено» вводил бы в заблуждение (чат-то рабочий).
    """
    is_admin = bool(getattr(entity, "creator", False) or getattr(entity, "admin_rights", None))
    # Личный бан — раньше всего, включая случай «выкинут из чата» (left=True + view_messages).
    br = getattr(entity, "banned_rights", None)
    if br and (getattr(br, "view_messages", False) or getattr(br, "send_messages", False)):
        return "заблокирован"
    if isinstance(entity, Channel):
        if getattr(entity, "left", False):
            # join_request — вступление только с одобрения админа: массово туда не зайти
            return "нужно одобрение" if getattr(entity, "join_request", False) else "не вступил"
        if getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
            return "да" if is_admin else "только админы"
        dbr = getattr(entity, "default_banned_rights", None)
        if dbr and getattr(dbr, "send_messages", False) and not is_admin:
            return "только админы"   # писать запрещено всем — значит, остались одни админы
        return "да"
    if isinstance(entity, Chat):
        # Старая группа: раньше безусловно возвращали «да», игнорируя и бан, и выход.
        if getattr(entity, "kicked", False) or getattr(entity, "deactivated", False):
            return "заблокирован"
        if getattr(entity, "left", False):
            return "не вступил"
        dbr = getattr(entity, "default_banned_rights", None)
        if dbr and getattr(dbr, "send_messages", False) and not is_admin:
            return "только админы"
        return "да"
    return "неизвестно"


# Telegram отдаёт максимум ~10k участников на запрос — больше не выгрузить никак.
# Чаты крупнее собираем не списком, а по комментаторам (см. tg_parser --mode active).
MEMBERS_HARD_LIMIT = 10_000


async def members_visible(client, entity) -> str:
    """Виден ли список участников (можно ли парсить аудиторию)."""
    try:
        await client.get_participants(entity, limit=1)
        return "да"
    except ChatAdminRequiredError:
        return "нет"
    except Exception:  # noqa: BLE001
        return "нет"


async def members_access(client, entity, members: int | None) -> tuple[str, str]:
    """Детальнее, чем members_visible: (тип списка, можно ли выгрузить целиком).

    Возвращает:
      • «открыт»    + «да»       — список читается и влезает в лимит TG → берём целиком;
      • «открыт»    + «частично» — читается, но участников > 10k → TG отдаст лишь часть,
                                   остальных добираем комментаторами;
      • «скрыт»     + «нет»      — список закрыт правами (нужен админ) → только комментаторы;
      • «закрыт»    + «нет»      — мы не в чате / нет доступа к сущности;
      • «неизвестно»+ «нет»      — иная ошибка, судить не берёмся.
    """
    if isinstance(entity, Channel) and getattr(entity, "left", False):
        return "закрыт", "нет"
    try:
        await client.get_participants(entity, limit=1)
    except ChatAdminRequiredError:
        return "скрыт", "нет"
    except Exception:  # noqa: BLE001
        return "неизвестно", "нет"
    if members and members > MEMBERS_HARD_LIMIT:
        return "открыт", "частично"
    return "открыт", "да"


async def _activity(client, entity) -> tuple[str | None, list[str]]:
    """Грубая оценка активности + выборка текстов сообщений (для AI-обогащения темы чата).

    Возвращает ("~N сообщений/день" | None, [тексты сообщений]). Один проход по последним
    ~80 сообщениям чата/обсуждения — переиспользуем и для активности, и для выборки."""
    try:
        chat = await _resolve_scan_chat(client, entity)
        if chat is None:
            return None, []
        dates, sample = [], []
        async for m in client.iter_messages(chat, limit=80):
            if m.date:
                dates.append(m.date)
            if m.message and m.message.strip():
                sample.append(m.message.strip().replace("\n", " ")[:200])
        if len(dates) < 2:
            return None, sample
        span_days = max((dates[0] - dates[-1]).total_seconds() / 86400, 0.04)
        per_day = round(len(dates) / span_days)
        return f"~{per_day} сообщений/день", sample
    except Exception:  # noqa: BLE001
        return None, []


async def scan_one(client, target: str, chat_id: int | None) -> dict:
    """Просканировать ОДИН чат уже готовым клиентом и записать в БД. Клиент не трогаем
    (не подключаем и не рвём) — это забота вызывающего. Вынесено из run(), чтобы
    массовый сканер (chat_scan_all) гонял сотни чатов через один коннект рабочего
    аккаунта, а не поднимал по клиенту на чат.

    Возвращает словарь-результат (он же печатается как JSON в одиночном режиме).
    Исключения НЕ глушим: наверху решают, мёртвая это ссылка или сбой связи."""
    entity = await client.get_entity(target)

    title = getattr(entity, "title", None) or target
    username = getattr(entity, "username", None)
    kind = _kind(entity)
    members = getattr(entity, "participants_count", None)
    if not members and isinstance(entity, Channel):
        try:
            full = await client(GetFullChannelRequest(entity))
            members = getattr(full.full_chat, "participants_count", None)
        except Exception:  # noqa: BLE001
            pass

    # Админы — по возможности: во многих чатах список скрыт (ChatAdminRequiredError).
    # Это НЕ повод терять весь скан: чат живой, участники и активность нам доступны.
    # FloodWait намеренно пропускаем наверх — там решают, сколько ждать.
    try:
        admins = await collect_admins(client, entity)
    except FloodWaitError:
        raise
    except Exception as e:  # noqa: BLE001
        admins = []
        print(f"[admins] {getattr(entity, 'username', None) or target}: не собрать ({type(e).__name__})")
    activity, sample = await _activity(client, entity)
    cw = can_write(entity)
    access, export_all = await members_access(client, entity, members)
    mv = "да" if access == "открыт" else "нет"   # старое грубое поле — из нового
    tg_id = getattr(entity, "id", None)

    link = target if (target.startswith("http") or target.startswith("t.me")) else None
    with database.get_conn() as conn:
        cid = chat_id
        if not cid and username:
            row = conn.execute("SELECT id FROM chats WHERE username=?", (username,)).fetchone()
            cid = row["id"] if row else None
        if not cid and tg_id:
            row = conn.execute("SELECT id FROM chats WHERE tg_chat_id=?", (tg_id,)).fetchone()
            cid = row["id"] if row else None
        if cid:
            conn.execute(
                # members_count через COALESCE: скан мог не добыть число (нет прав/сбой) —
                # тогда сохраняем ранее известное, а не затираем нулём.
                "UPDATE chats SET title=?, username=COALESCE(?,username), kind=?, "
                "members_count=COALESCE(?,members_count), "
                "activity=?, can_write=?, members_visible=?, members_access=?, can_export_all=?, "
                "tg_chat_id=COALESCE(?,tg_chat_id), status='analyzed', scan_error=NULL, "
                "last_scanned_at=datetime('now') WHERE id=?",
                (title, username, kind, members, activity, cw, mv, access, export_all, tg_id, cid),
            )
        else:
            cur = conn.execute(
                "INSERT INTO chats (title, username, link, kind, members_count, activity, "
                "can_write, members_visible, members_access, can_export_all, tg_chat_id, "
                "status, last_scanned_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'analyzed', datetime('now'))",
                (title, username, link, kind, members, activity, cw, mv, access, export_all, tg_id),
            )
            cid = cur.lastrowid
        conn.execute("DELETE FROM chat_admins WHERE chat_id=?", (cid,))
        for u in admins:
            conn.execute(
                "INSERT OR IGNORE INTO chat_admins (chat_id, tg_user_id, username, name) VALUES (?,?,?,?)",
                (cid, u.id, u.username, _display_name(u)),
            )

    # AI-обогащение темы/описания чата (best-effort: нет ключа/сырья → тихо пропускаем).
    topic = summary = ai_err = None
    try:
        from agent.enrich_chat import enrich as enrich_chat
        # members/activity — в промпт: иначе ИИ судит только по теме и одобряет мёртвые чаты
        prof = enrich_chat(cid, title, sample, members, activity)
        if prof:
            topic, summary = prof.topic, prof.summary
    except Exception as e:  # noqa: BLE001
        # Раньше это молча уходило в print, и провал AI (напр. протухший ключ → 401)
        # был не виден в пульте: чат просто оставался без темы. Теперь причина едет
        # в ответе, а массовый сканер её агрегирует и показывает.
        ai_err = f"{type(e).__name__}: {e}"
        print(f"[enrich_chat] пропущено: {e}")

    return {
        "ok": True, "chat_id": cid, "title": title, "username": username,
        "kind": kind, "members": members, "activity": activity,
        "can_write": cw, "members_visible": mv,
        "members_access": access, "can_export_all": export_all,
        "topic": topic, "summary": summary, "ai_error": ai_err,
        "admins": [{"username": u.username, "name": _display_name(u)} for u in admins],
    }


async def run(target: str, chat_id: int | None, account_id: int | None = None) -> None:
    """Одиночный скан из CLI/пульта. account_id=None — главный аккаунт из .env."""
    database.init_db()
    client, _ = client_for_account(account_id)
    await client.connect()
    try:
        res = await scan_one(client, target, chat_id)
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps(res, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM анализ чата (только чтение)")
    p.add_argument("--target", required=True, help="@username / ссылка / id чата")
    p.add_argument("--id", type=int, default=None, help="id строки в chats для обновления")
    p.add_argument("--account", type=int, default=None,
                   help="id рабочего аккаунта (по умолчанию — главный из .env)")
    args = p.parse_args()
    asyncio.run(run(args.target, args.id, args.account))


if __name__ == "__main__":
    main()
