"""Слушатель Telegram-чатов по ключевым словам. Источник лидов №3.

Ловит сообщения в группах/чатах, где состоит аккаунт, и ищет ключевые слова
(«ищу риелтора», «куплю квартиру», «нужен ипотечный»…). При совпадении кладёт
автора в книжку как горячего лида и сохраняет сообщение-триггер.

Только ГРУППЫ/чаты — личку (диалоги) обрабатывает channels/telegram.py, чтобы
два слушателя не дрались за одни и те же входящие.

⚠️ Аккаунт должен СОСТОЯТЬ в нужных чатах — вступи в целевые группы заранее.
Не отвечает автоматически (чтобы не спамить в чужом чате): только ловит лид,
дальше пишешь ему в личку через outreach/агента.

Запуск:
    python -m channels.tg_listener
    python -m channels.tg_listener --keywords "ищу риелтора, куплю квартиру, нужен ипотечный, продаю квартиру"
    python -m channels.tg_listener --dry      # только печатать совпадения, не писать в книжку
"""
from __future__ import annotations

import argparse
import asyncio

from telethon import events
from telethon.tl.types import User

from channels.telegram import _build_client
from db import database

# Ключи по умолчанию (ниша недвижимости/ипотеки). Регистр не важен.
DEFAULT_KEYWORDS = [
    "ищу риелтора", "нужен риелтор", "посоветуйте риелтора",
    "куплю квартиру", "продаю квартиру", "сниму квартиру", "сдаю квартиру",
    "нужен ипотечный", "ищу ипотеку", "помогите с ипотекой",
    "новостройк", "вторичк", "переуступк",
]

_keywords: list[str] = []
_dry = False


def _match(text: str) -> str | None:
    low = text.lower()
    for kw in _keywords:
        if kw in low:
            return kw
    return None


def _display_name(u: User) -> str:
    name = " ".join(x for x in [u.first_name, u.last_name] if x).strip()
    return name or (u.username and f"@{u.username}") or str(u.id)


async def _handle(event) -> None:
    # только группы/супергруппы — личку слушает telegram.py
    if not (event.is_group or event.is_channel):
        return
    text = event.raw_text or ""
    if not text.strip():
        return
    kw = _match(text)
    if not kw:
        return
    sender = await event.get_sender()
    if not isinstance(sender, User) or sender.bot or sender.deleted:
        return

    chat = await event.get_chat()
    chat_title = getattr(chat, "title", None) or "чат"
    snippet = text.strip().replace("\n", " ")[:200]
    name = _display_name(sender)
    print(f"[lead] «{kw}» от {name} (@{sender.username or '-'}) в «{chat_title}»: {snippet}")

    if _dry:
        return

    with database.get_conn() as conn:
        existing = database.find_contact_by_tg(conn, tg_user_id=sender.id, username=sender.username)
        tag = f"Ключ TG: {kw}"
        note = f"[{chat_title}] триггер «{kw}»: {snippet}"
        if existing:
            old_tags = existing["tags"] or ""
            tags = old_tags if tag in old_tags else (f"{old_tags}, {tag}" if old_tags else tag)
            old_notes = existing["notes"] or ""
            notes = f"{old_notes} | {note}" if old_notes else note
            conn.execute(
                "UPDATE contacts SET tags=?, notes=?, updated_at=datetime('now') WHERE id=?",
                (tags, notes, existing["id"]),
            )
        else:
            cid = database.upsert_contact(
                conn, source="tg_keyword", username=sender.username, tg_user_id=sender.id,
                name=name, tags=tag, notes=note,
            )
            conn.execute("UPDATE contacts SET has_tg='yes' WHERE id=?", (cid,))


async def run() -> None:
    database.init_db()
    client = _build_client()
    await client.start()
    me = await client.get_me()
    print(f"Подключён как @{me.username or me.id}")
    print(f"Ключи ({len(_keywords)}): {', '.join(_keywords)}")
    print("Слушаю группы/чаты на ключевые слова. Ctrl+C для остановки." + ("  [DRY: не пишу в книжку]" if _dry else ""))
    client.add_event_handler(_handle, events.NewMessage(incoming=True))
    await client.run_until_disconnected()


def main() -> None:
    global _keywords, _dry
    p = argparse.ArgumentParser(description="AXIOM слушатель Telegram-чатов по ключам")
    p.add_argument("--keywords", help="свои ключи через запятую (иначе список по умолчанию)")
    p.add_argument("--dry", action="store_true", help="только печатать совпадения, не писать в книжку")
    args = p.parse_args()
    raw = args.keywords or ",".join(DEFAULT_KEYWORDS)
    _keywords = [k.strip().lower() for k in raw.split(",") if k.strip()]
    _dry = args.dry
    asyncio.run(run())


if __name__ == "__main__":
    main()
