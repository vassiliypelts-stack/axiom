"""Отправка первого сообщения по кампании (антибан-лимиты, человеческий темп).

Запускается веб-пультом в отдельном процессе:

    python -m channels.campaign_send <campaign_id> --limit N

Берёт аудиторию кампании (status='new', подходящий канал, фильтр по тегу),
шлёт первое сообщение из шаблона кампании ({name} подставляется), соблюдает
дневной лимит и паузы, пишет в книжку и в campaign_contacts.
"""
from __future__ import annotations

import argparse
import asyncio
import random

from telethon.errors import FloodWaitError

from db import database
from channels.telegram import _build_client, _send_parts, _resolve_entity, OUTREACH_PAUSE


def _load_campaign(cid: int) -> dict | None:
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    return dict(row) if row else None


def _channels(channel: str | None) -> list[str]:
    return [c.strip() for c in (channel or "").split(",") if c.strip()]


def _audience(tag: str | None, channel: str, cap: int):
    """Аудитория для TG-отправки: контакты со status='new', достижимые по Telegram."""
    where = "status='new' AND (username IS NOT NULL OR phone IS NOT NULL)"
    params: list = []
    # Этот отправщик шлёт через Telegram, поэтому берём контакты с доступным TG.
    if "telegram" in _channels(channel):
        where += " AND has_tg IN ('yes','unknown')"
    if tag:
        where += " AND tags LIKE ?"
        params.append(f"%{tag}%")
    with database.get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM contacts WHERE {where} ORDER BY id LIMIT ?", (*params, cap)
        ).fetchall()


def _parts(template: str | None, name: str, agency: str = "") -> list[str]:
    """Шаблон → список сообщений. Каждая непустая строка — отдельное сообщение.
    {name}/{имя} — обращение, {agency}/{агентство} — название агентства."""
    ag = agency or name or ""
    text = ((template or "")
            .replace("{name}", name or "").replace("{имя}", name or "")
            .replace("{agency}", ag).replace("{агентство}", ag))
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _greeting(row) -> str:
    """Обращение для {name}: из ФИО директора → «Имя Отчество», иначе имя/название."""
    pn = (row["person_name"] or "").strip()
    if pn:
        parts = pn.split()
        if len(parts) == 3:  # Фамилия Имя Отчество → Имя Отчество (вежливо, по-деловому)
            return f"{parts[1]} {parts[2]}"
        return pn
    return (row["name"] or "").strip()


def _add_tag(raw: str | None, tag: str) -> str:
    tags = [t.strip() for t in (raw or "").split(",") if t.strip()]
    if tag not in tags:
        tags.append(tag)
    return ",".join(tags)


async def run(cid: int, limit: int) -> None:
    camp = _load_campaign(cid)
    if not camp:
        print(f"кампания #{cid} не найдена")
        return
    chans = _channels(camp["channel"])
    if "telegram" not in chans:
        print(f"канал '{camp['channel']}': отправка через WhatsApp пока не подключена "
              f"(Baileys-мост). Сейчас этот отправщик шлёт только Telegram.")
        return
    if "whatsapp" in chans:
        print("режим мультиканала: TG-достижимым шлём сейчас; WhatsApp-only контакты "
              "дождутся подключения WA-моста.")
    cap = min(limit, camp["daily_limit"] or limit)
    rows = _audience(camp["audience_tag"], camp["channel"], cap)
    if not rows:
        print("аудитория пуста — некому слать")
        return
    if not _parts(camp["message_template"], ""):
        print("пустой шаблон сообщения — нечего слать")
        return

    client = _build_client()
    await client.start()
    me = await client.get_me()
    tag = f"кампания #{cid}"
    print(f"кампания #{cid} «{camp['name']}»: шлю с @{me.username or me.id}, до {cap} контактов")

    sent = 0
    for row in rows:
        if sent >= cap:
            break
        # обращение: из ФИО директора берём «Имя Отчество», иначе имя/название агентства
        name = _greeting(row)
        parts = _parts(camp["message_template"], name, row["agency"] or row["name"])
        try:
            entity = await _resolve_entity(client, row)
            await _send_parts(client, entity, parts)
        except FloodWaitError as e:
            print(f"[floodwait] ждём {e.seconds}с")
            await asyncio.sleep(e.seconds + 5)
            continue
        except Exception as e:
            print(f"[skip] contact {row['id']}: {e}")
            with database.get_conn() as conn:
                database.set_status(conn, row["id"], "lost")
            continue

        text = "\n".join(parts)
        with database.get_conn() as conn:
            database.set_tg_user_id(conn, row["id"], int(entity.id))
            database.add_message(conn, row["id"], "out", text, intent=None)
            database.set_status(conn, row["id"], "messaged")
            conn.execute("UPDATE contacts SET tags=? WHERE id=?", (_add_tag(row["tags"], tag), row["id"]))
            conn.execute(
                "INSERT OR IGNORE INTO campaign_contacts (campaign_id, contact_id, account_id) VALUES (?,?,?)",
                (cid, row["id"], camp.get("account_id")),
            )
        sent += 1
        print(f"[sent {sent}/{cap}] -> {name or row['username'] or row['phone']}")
        if sent < cap:
            await asyncio.sleep(random.uniform(*OUTREACH_PAUSE))

    # Если в аудитории больше никого не осталось — кампания отработана.
    remaining = _audience(camp["audience_tag"], camp["channel"], 1)
    with database.get_conn() as conn:
        done = not remaining
        conn.execute(
            "UPDATE campaigns SET status=? WHERE id=?",
            ("done" if done else "running", cid),
        )
        if done:
            database.add_event(conn, "campaign_done", f"✅ Кампания «{camp['name']}» отработана",
                               f"аудитория исчерпана, в этот заход отправлено {sent}",
                               level="good", campaign_id=cid)
    print(f"кампания #{cid}: отправлено {sent}")
    await client.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description="Отправка кампании AXIOM")
    p.add_argument("cid", type=int, help="id кампании")
    p.add_argument("--limit", type=int, default=3, help="сколько контактов взять в этот заход")
    args = p.parse_args()
    asyncio.run(run(args.cid, args.limit))


if __name__ == "__main__":
    main()
