"""Парсер Telegram-каналов/чатов для AXIOM. Источник лидов №2 (после 2ГИС).

Три режима по целевому каналу/чату:
  • admins  — администраторы (всегда видны) — это часто и есть владельцы/ЛПР;
  • members — участники группы/супергруппы (если список не скрыт);
  • active  — активные комментаторы: идём по сообщениям чата/обсуждения,
              считаем частоту авторов, берём топ — это «живые» лиды.

Найденных кладём в ту же книжку (contacts) как лиды: source='tg_parse',
tg_user_id, username, имя, тег с источником. Дедуп по tg_user_id — повторный
прогон не плодит дублей.

⚠️ Telegram не любит массовый скрейп: используем отдельный прогретый аккаунт,
лимиты и паузы. Подписчики ВЕЩАТЕЛЬНОГО канала скрыты — доступны только админы
и (если есть) чат обсуждения.

Запуск (нужен авторизованный аккаунт — тот же, что в telegram.py):
    python -m channels.tg_parser --target @nedvizhka_sochi --mode admins --save
    python -m channels.tg_parser --target @somechat --mode members --limit 500 --save
    python -m channels.tg_parser --target @somechat --mode active --scan 3000 --top 50 --save
    python -m channels.tg_parser --target @chan --mode all --save   # админы + активные
"""
from __future__ import annotations

import argparse
import asyncio
import random
from collections import Counter

from telethon.errors import ChatAdminRequiredError, FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, ChannelParticipantsAdmins, User

from channels.telegram import _build_client
from db import database

# Антибан: пауза между «тяжёлыми» вызовами и порции участников.
SCRAPE_PAUSE = (2.0, 5.0)


def _display_name(u: User) -> str:
    name = " ".join(x for x in [u.first_name, u.last_name] if x).strip()
    return name or (u.username and f"@{u.username}") or str(u.id)


def _is_lead_user(u) -> bool:
    """Годится ли как лид: реальный пользователь, не бот, не удалён."""
    return isinstance(u, User) and not u.bot and not u.deleted


async def _resolve_scan_chat(client, entity):
    """Куда смотреть на «активных»: сам чат (если группа) или связанное обсуждение канала."""
    if isinstance(entity, Channel) and entity.megagroup:
        return entity  # это супергруппа — комментируют прямо здесь
    if isinstance(entity, Channel) and entity.broadcast:
        try:
            full = await client(GetFullChannelRequest(entity))
            linked = getattr(full.full_chat, "linked_chat_id", None)
            if linked:
                return await client.get_entity(linked)
        except Exception as e:  # noqa: BLE001
            print(f"[active] не нашёл чат обсуждения: {e}")
        return None
    return entity


async def collect_admins(client, entity) -> list[User]:
    try:
        ppl = await client.get_participants(entity, filter=ChannelParticipantsAdmins())
    except FloodWaitError as e:
        print(f"[floodwait] жду {e.seconds}с"); await asyncio.sleep(e.seconds + 5)
        ppl = await client.get_participants(entity, filter=ChannelParticipantsAdmins())
    return [u for u in ppl if _is_lead_user(u)]


async def collect_members(client, entity, limit: int) -> list[User]:
    try:
        ppl = await client.get_participants(entity, limit=limit)
    except ChatAdminRequiredError:
        print("[members] список участников скрыт (нужны права админа) — пропускаю.")
        return []
    except FloodWaitError as e:
        print(f"[floodwait] жду {e.seconds}с"); await asyncio.sleep(e.seconds + 5)
        ppl = await client.get_participants(entity, limit=limit)
    return [u for u in ppl if _is_lead_user(u)]


async def collect_active(client, entity, scan: int, top: int) -> list[tuple[User, int]]:
    """Топ авторов по числу сообщений в чате/обсуждении за последние `scan` сообщений."""
    chat = await _resolve_scan_chat(client, entity)
    if chat is None:
        print("[active] у цели нет чата обсуждения — нечего сканировать.")
        return []
    counts: Counter[int] = Counter()
    n = 0
    async for m in client.iter_messages(chat, limit=scan):
        if m.sender_id and m.sender_id > 0:  # >0 = пользователь (каналы/анонимы отсекаем)
            counts[m.sender_id] += 1
        n += 1
    print(f"[active] просмотрено {n} сообщений, уникальных авторов: {len(counts)}")
    out: list[tuple[User, int]] = []
    for uid, cnt in counts.most_common(top):
        try:
            u = await client.get_entity(uid)
        except Exception:  # noqa: BLE001
            continue
        if _is_lead_user(u):
            out.append((u, cnt))
        await asyncio.sleep(random.uniform(0.3, 0.8))
    return out


def _save_lead(conn, u: User, target: str, role: str) -> str:
    """Кладёт пользователя в книжку. Дедуп по tg_user_id. Возвращает 'new'/'dup'."""
    existing = database.find_contact_by_tg(conn, tg_user_id=u.id, username=u.username)
    tag = f"TG-парсинг: {target}" + (f" / {role}" if role else "")
    if existing:
        old = existing["tags"] or ""
        if tag not in old:
            new_tags = f"{old}, {tag}" if old else tag
            conn.execute("UPDATE contacts SET tags=?, updated_at=datetime('now') WHERE id=?", (new_tags, existing["id"]))
        return "dup"
    cid = database.upsert_contact(
        conn,
        source="tg_parse",
        username=u.username,
        tg_user_id=u.id,
        name=_display_name(u),
        tags=tag,
        notes=f"Найден парсером TG в {target} ({role})",
    )
    conn.execute("UPDATE contacts SET has_tg='yes' WHERE id=?", (cid,))
    return "new"


def _report(title: str, users: list, counts: dict | None = None) -> None:
    print(f"\n=== {title}: {len(users)} ===")
    for u in users[:60]:
        extra = f"  ×{counts[u.id]}" if counts and u.id in counts else ""
        print(f"  {_display_name(u):30} @{u.username or '-':20}{extra}")


def _persist(users: list, target: str, role: str) -> None:
    new = dup = 0
    with database.get_conn() as conn:
        for u in users:
            r = _save_lead(conn, u, target, role)
            new += r == "new"; dup += r == "dup"
    print(f"[save] {role}: добавлено {new}, уже было {dup}")


async def search_chats(client, query: str, limit: int) -> None:
    """Глобальный поиск публичных групп/каналов по запросу. Печатает кандидатов
    с @username, типом и числом участников — чтобы выбрать цели для парсинга."""
    try:
        res = await client(SearchRequest(q=query, limit=min(limit, 50)))
    except FloodWaitError as e:
        print(f"[floodwait] жду {e.seconds}с"); await asyncio.sleep(e.seconds + 5)
        res = await client(SearchRequest(q=query, limit=min(limit, 50)))
    chats = [c for c in res.chats if isinstance(c, Channel) and c.username]
    print(f"\n=== Найдено по «{query}»: {len(chats)} (с @username) ===")
    for c in chats:
        kind = "супергруппа" if c.megagroup else ("канал" if c.broadcast else "группа")
        cnt = getattr(c, "participants_count", None)
        cnt_s = f"  ~{cnt} уч." if cnt else ""
        print(f"  @{c.username:28} {kind:12} {c.title}{cnt_s}")
    print("\nПарсить выбранную: python -m channels.tg_parser --target @username --mode all --save")


async def run(target: str, mode: str, limit: int, scan: int, top: int, save: bool) -> None:
    database.init_db()
    client = _build_client()
    await client.start()
    me = await client.get_me()
    print(f"Подключён как @{me.username or me.id}; цель: {target}")

    if mode == "search":
        await search_chats(client, target, limit)
        await client.disconnect()
        return

    entity = await client.get_entity(target)

    if mode in ("admins", "all"):
        admins = await collect_admins(client, entity)
        _report("Админы", admins)
        if save:
            _persist(admins, target, "админ")
        await asyncio.sleep(random.uniform(*SCRAPE_PAUSE))

    if mode == "members":
        members = await collect_members(client, entity, limit)
        _report("Участники", members)
        if save:
            _persist(members, target, "участник")

    if mode in ("active", "all"):
        active = await collect_active(client, entity, scan, top)
        users = [u for u, _ in active]
        counts = {u.id: c for u, c in active}
        _report("Активные комментаторы", users, counts)
        if save:
            _persist(users, target, "активный")

    await client.disconnect()
    print("\nГотово." + ("" if save else "  (сухой прогон — добавь --save, чтобы записать в книжку)"))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM парсер Telegram-каналов/чатов")
    p.add_argument("--target", required=True, help="@username/ссылка/id канала; для --mode search это поисковый запрос")
    p.add_argument("--mode", choices=["admins", "members", "active", "all", "search"], default="admins")
    p.add_argument("--limit", type=int, default=500, help="макс участников в режиме members")
    p.add_argument("--scan", type=int, default=2000, help="сколько сообщений просмотреть в режиме active")
    p.add_argument("--top", type=int, default=50, help="сколько топ-авторов взять в режиме active")
    p.add_argument("--save", action="store_true", help="записать найденных в книжку (иначе только печать)")
    args = p.parse_args()
    asyncio.run(run(args.target, args.mode, args.limit, args.scan, args.top, args.save))


if __name__ == "__main__":
    main()
