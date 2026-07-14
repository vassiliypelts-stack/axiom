"""Реал-тайм слушатель Telegram-чатов по ключам ниш (H2). Источник лидов №3.

В отличие от channels/chat_keywords.py (поллинг «раз в день»), здесь МГНОВЕННАЯ
реакция через events.NewMessage: как только в чате, где состоит аккаунт, кто-то
пишет фразу-триггер из активных ниш (`niches`), находка тут же кладётся в очередь
`chat_hits` — НА ОБЗОР ОПЕРАТОРУ (раздел «Запросы»), как и у поллинга. Оператор сам
решает, брать ли в лиды. Это «скорость», о которой говорил Денис: ловим того, кто
нуждается здесь и сейчас, в ту же секунду.

По умолчанию НЕ пишет в ЛС (антибан/чужой чат). Авто-ЛС — опция на будущее (см.
--auto-dm, пока заглушка): включать осознанно, с лимитами и пулом аккаунтов.

Только ГРУППЫ/чаты — личку слушает channels/telegram.py.
⚠️ Аккаунт должен СОСТОЯТЬ в нужных чатах. Ключи берутся из активных ниш в БД
(если ниш нет — fallback на DEFAULT_KEYWORDS).

Запуск:
    python -m channels.tg_listener            # ключи из активных ниш → chat_hits
    python -m channels.tg_listener --dry      # только печатать совпадения
"""
from __future__ import annotations

import argparse
import asyncio

from telethon import events
from telethon.tl.types import User

from channels.telegram import _build_client
from db import database

# Fallback-ключи (ниша недвижимости/ипотеки), если в БД нет активных ниш.
DEFAULT_KEYWORDS = [
    "ищу риелтора", "нужен риелтор", "посоветуйте риелтора",
    "куплю квартиру", "продаю квартиру", "сниму квартиру", "сдаю квартиру",
    "нужен ипотечный", "ищу ипотеку", "помогите с ипотекой",
    "новостройк", "вторичк", "переуступк",
]

_niches: list[tuple[int | None, list[str]]] = []  # [(niche_id, [ключи]), ...]
_dry = False
_auto_dm = False  # на будущее: авто-ЛС по триггеру (пока не реализовано — только лог)


def _load_niches() -> list[tuple[int | None, list[str]]]:
    """Активные ниши из БД (как у chat_keywords). Пусто → один псевдо-набор DEFAULT."""
    with database.get_conn() as conn:
        rows = conn.execute("SELECT id, keywords FROM niches WHERE active=1").fetchall()
    out: list[tuple[int | None, list[str]]] = []
    for r in rows:
        kws = [k.strip().lower() for k in (r["keywords"] or "").split(",") if k.strip()]
        if kws:
            out.append((r["id"], kws))
    if not out:
        out.append((None, [k.lower() for k in DEFAULT_KEYWORDS]))
    return out


def _match(text: str):
    low = text.lower()
    for nid, kws in _niches:
        for kw in kws:
            if kw in low:
                return nid, kw
    return None


def _display_name(u: User) -> str:
    name = " ".join(x for x in [u.first_name, u.last_name] if x).strip()
    return name or (u.username and f"@{u.username}") or str(u.id)


def _save_hit(niche_id, tg_chat_id, chat_title, chat_username, sender, text, kw, msg_id, ts) -> bool:
    """Кладёт находку в chat_hits (на обзор оператору). Дедуп по (chat_id, msg_id).

    chat_hits.chat_id — КАТАЛОЖНЫЙ chats.id (так же пишет chat_keywords.py), поэтому
    сырой telegram-id события сначала резолвим в каталог. Раньше сюда клался сырой
    telegram-id — из-за этого JOIN на chats не находил чат и в «Запросах» пропадала
    ссылка на сообщение.
    """
    with database.get_conn() as conn:
        cat_id = database.resolve_catalog_chat(conn, tg_chat_id, chat_title, chat_username)
        cur = conn.execute(
            "INSERT OR IGNORE INTO chat_hits (niche_id, chat_id, chat_title, tg_user_id, "
            "username, name, text, keyword, source_msg_id, ts, status) VALUES (?,?,?,?,?,?,?,?,?,?, 'new')",
            (niche_id, cat_id, chat_title, sender.id, sender.username,
             _display_name(sender), text.strip()[:500], kw, msg_id, str(ts) if ts else None),
        )
        return cur.rowcount > 0


async def _handle(event) -> None:
    # только группы/супергруппы — личку слушает telegram.py
    if not (event.is_group or event.is_channel):
        return
    text = event.raw_text or ""
    if not text.strip():
        return
    m = _match(text)
    if not m:
        return
    nid, kw = m
    sender = await event.get_sender()
    if not isinstance(sender, User) or sender.bot or sender.deleted:
        return

    chat = await event.get_chat()
    chat_title = getattr(chat, "title", None) or "чат"
    snippet = text.strip().replace("\n", " ")[:200]
    name = _display_name(sender)

    if _dry:
        print(f"[dry] «{kw}» от {name} (@{sender.username or '-'}) в «{chat_title}»: {snippet}")
        return

    # chat.id — СЫРОЙ id (без -100), как и tg_user_posts.chat_id у парсера. Берём
    # именно его, а не event.chat_id (тот «помеченный», вида -100123…) — иначе одному
    # чату соответствовали бы два разных числа.
    new = _save_hit(nid, getattr(chat, "id", None), chat_title, getattr(chat, "username", None),
                    sender, text, kw, event.message.id, getattr(event.message, "date", None))
    if new:
        print(f"[hit] «{kw}» от {name} (@{sender.username or '-'}) в «{chat_title}» → Запросы")
    if _auto_dm:
        # TODO H2-фаза2: авто-ЛС по триггеру из позиции заботы, с лимитами и пулом
        # аккаунтов (antiban). Пока осознанно НЕ шлём — только обзор оператору.
        print("  [auto-dm] пропущено: авто-ЛС ещё не включено (только обзор оператору)")


async def run() -> None:
    global _niches
    database.init_db()
    _niches = _load_niches()
    total_kw = sum(len(k) for _, k in _niches)
    client = _build_client()
    await client.start()
    me = await client.get_me()
    print(f"Подключён как @{me.username or me.id}")
    print(f"Активных ниш: {len(_niches)}, ключей всего: {total_kw}")
    print("Реал-тайм слушаю группы/чаты → находки в «Запросы». Ctrl+C для остановки."
          + ("  [DRY]" if _dry else ""))
    client.add_event_handler(_handle, events.NewMessage(incoming=True))
    await client.run_until_disconnected()


def main() -> None:
    global _dry, _auto_dm
    p = argparse.ArgumentParser(description="AXIOM реал-тайм слушатель чатов по ключам ниш (H2)")
    p.add_argument("--dry", action="store_true", help="только печатать совпадения, не писать в Запросы")
    p.add_argument("--auto-dm", action="store_true", help="(заглушка) авто-ЛС по триггеру — пока не шлёт")
    args = p.parse_args()
    _dry = args.dry
    _auto_dm = args.auto_dm
    asyncio.run(run())


if __name__ == "__main__":
    main()
