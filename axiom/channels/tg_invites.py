"""Сборщик инвайт-ссылок на ЗАКРЫТЫЕ Telegram-группы. Источник целей для парсинга.

Закрытые группы не находятся глобальным поиском Telegram (он отдаёт только
публичные @). Зато инвайты (t.me/+xxxx, t.me/joinchat/xxxx) люди постят внутри
чатов/каналов. Этот модуль:
  • выгребает инвайт-ссылки из сообщений (одного чата или всех твоих диалогов);
  • по каждой ссылке смотрит, ЧТО за группой — название и число участников —
    БЕЗ вступления (Telegram CheckChatInvite);
  • печатает список, куда можно вступить вручную и потом спарсить.

Сам НЕ вступает (вступление в закрытые — твоё решение, и так безопаснее для аккаунта).

Запуск:
    python -m channels.tg_invites --target @какой_то_чат --limit 3000
    python -m channels.tg_invites --dialogs            # пройтись по всем своим группам
    python -m channels.tg_invites --dialogs --per 800  # глубина на каждый диалог
"""
from __future__ import annotations

import argparse
import asyncio
import random
import re

from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInvite, ChatInviteAlready

from channels.telegram import _build_client
from db import database  # noqa: F401  (init на всякий случай не нужен, но единообразие)

# t.me/+HASH, t.me/joinchat/HASH, tg://join?invite=HASH
INVITE_RE = re.compile(r"(?:t\.me/(?:joinchat/|\+)|tg://join\?invite=)([A-Za-z0-9_-]{12,})")


def _urls_of_message(m) -> str:
    """Весь текст сообщения + ссылки из сущностей и кнопок — одной строкой для regex."""
    blob = m.raw_text or ""
    for ent in (m.entities or []):
        url = getattr(ent, "url", None)
        if url:
            blob += " " + url
    try:
        for row in (m.buttons or []):
            for b in row:
                if getattr(b, "url", None):
                    blob += " " + b.url
    except Exception:  # noqa: BLE001
        pass
    return blob


async def _scan(client, chat, limit: int) -> set[str]:
    hashes: set[str] = set()
    try:
        async for m in client.iter_messages(chat, limit=limit):
            for h in INVITE_RE.findall(_urls_of_message(m)):
                hashes.add(h)
    except FloodWaitError as e:
        print(f"[floodwait] жду {e.seconds}с"); await asyncio.sleep(e.seconds + 5)
    except Exception as e:  # noqa: BLE001
        print(f"[scan skip] {getattr(chat,'title',chat)}: {e}")
    return hashes


async def _resolve(client, h: str) -> tuple[str, str, int | None] | None:
    """(статус, название, ~участников) по инвайту, без вступления. None — не удалось."""
    try:
        inv = await client(CheckChatInviteRequest(h))
    except FloodWaitError as e:
        print(f"[floodwait] жду {e.seconds}с"); await asyncio.sleep(e.seconds + 5)
        try:
            inv = await client(CheckChatInviteRequest(h))
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001  (протух/невалиден)
        return None
    if isinstance(inv, ChatInviteAlready):
        ch = inv.chat
        return ("уже вступил", getattr(ch, "title", "?"), getattr(ch, "participants_count", None))
    if isinstance(inv, ChatInvite):
        return ("закрытая", inv.title, inv.participants_count)
    return None


async def run(target: str | None, dialogs: bool, limit: int, per: int) -> None:
    client = _build_client()
    await client.start()
    me = await client.get_me()
    print(f"Подключён как @{me.username or me.id}")

    hashes: set[str] = set()
    if target:
        ent = await client.get_entity(target)
        print(f"Сканирую «{getattr(ent,'title',target)}» (до {limit} сообщений)…")
        hashes |= await _scan(client, ent, limit)
    if dialogs:
        print("Сканирую все диалоги-группы…")
        async for d in client.iter_dialogs():
            if d.is_group or d.is_channel:
                found = await _scan(client, d.entity, per)
                if found:
                    print(f"  +{len(found)} в «{d.name}»")
                hashes |= found
                await asyncio.sleep(random.uniform(0.5, 1.2))

    if not hashes:
        print("Инвайт-ссылок не нашлось.")
        await client.disconnect()
        return

    print(f"\nНайдено уникальных инвайтов: {len(hashes)}. Проверяю, что за группами…\n")
    rows: list[tuple[str, str, int | None, str]] = []
    for h in hashes:
        info = await _resolve(client, h)
        await asyncio.sleep(random.uniform(0.5, 1.2))
        if not info:
            continue
        status, title, cnt = info
        rows.append((title or "?", cnt or 0, status, f"https://t.me/+{h}"))

    rows.sort(key=lambda r: r[1], reverse=True)  # по числу участников
    print(f"=== Закрытые группы по инвайтам: {len(rows)} ===")
    for title, cnt, status, link in rows:
        cnt_s = f"~{cnt} уч." if cnt else "—"
        print(f"  {title[:40]:40} {cnt_s:10} [{status}]  {link}")
    print("\nВступаешь вручную по ссылке, затем парсишь:")
    print("  python -m channels.tg_parser --target <ссылка|@username> --mode all --save")

    await client.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM сборщик инвайтов закрытых TG-групп")
    p.add_argument("--target", help="@username/ссылка/id чата для сканирования сообщений")
    p.add_argument("--dialogs", action="store_true", help="пройтись по всем своим группам/каналам")
    p.add_argument("--limit", type=int, default=3000, help="сколько сообщений смотреть в --target")
    p.add_argument("--per", type=int, default=800, help="глубина сканирования на каждый диалог в --dialogs")
    args = p.parse_args()
    if not args.target and not args.dialogs:
        p.error("укажи --target @чат или --dialogs")
    asyncio.run(run(args.target, args.dialogs, args.limit, args.per))


if __name__ == "__main__":
    main()
