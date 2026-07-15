"""Бэкфилл старых записей: tg_chat_id у чатов и фото у лидов.

Зачем. Поля появились позже данных, поэтому у записей, заведённых до них, они пустые:
  • `chats.tg_chat_id` — без него ломается связка «в каком чате найден человек»
    (досье джойнит `tg_user_posts.chat_id` → `chats.tg_chat_id`), пропадают ссылки на чат;
  • `contacts.has_photo` + `data/avatars/{tg_user_id}.jpg` — раньше аватар качался только
    для активных авторов при --harvest, у остальных лидов фото нет.

Модуль идемпотентный: гоняй сколько угодно, трогает только пустое.

    python -m channels.backfill --chats            # дозаполнить tg_chat_id (по @username)
    python -m channels.backfill --photos           # докачать аватары лидов
    python -m channels.backfill --all --limit 300  # и то, и другое
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random

from telethon.errors import FloodWaitError

import config
from channels.telegram import _build_client
from db import database

RESOLVE_PAUSE = (0.6, 1.4)   # антибан: дозируем резолвы сущностей
PHOTO_PAUSE = (0.5, 1.2)     # антибан: дозируем скачивания фото


def _mark_photos_from_disk() -> int:
    """Фото уже на диске → проставить has_photo. Без сети, бесплатно.
    Файлы лидов называются `{tg_user_id}.jpg`; аватары агентов лежат тут же
    (`a1.png`, `gen_*.jpg`), поэтому берём ТОЛЬКО числовые имена."""
    from channels.tg_parser import AVATAR_DIR
    if not AVATAR_DIR.exists():
        return 0
    ids = [int(p.stem) for p in AVATAR_DIR.glob("*.jpg") if p.stem.isdigit() and p.stat().st_size > 0]
    if not ids:
        return 0
    with database.get_conn() as conn:
        database.mark_photos_by_tg(conn, set(ids))
        return conn.execute(
            f"SELECT COUNT(*) FROM contacts WHERE COALESCE(has_photo,0)=1 "
            f"AND tg_user_id IN ({','.join('?' * len(ids))})", ids
        ).fetchone()[0]


async def _backfill_chats(client, limit: int) -> dict:
    """chats без tg_chat_id, но с @username → резолвим сущность → дозаполняем id."""
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, username FROM chats WHERE tg_chat_id IS NULL "
            "AND username IS NOT NULL AND username<>'' AND COALESCE(status,'') NOT IN ('skip','banned') "
            "ORDER BY COALESCE(favorite,0) DESC, COALESCE(members_count,0) DESC, id LIMIT ?", (limit,)
        ).fetchall()]
    filled = failed = 0
    for i, ch in enumerate(rows):
        try:
            e = await client.get_entity(ch["username"])
        except FloodWaitError as ex:
            # ждём РАДИ ЭТОЙ записи — значит её и повторяем, а не проскакиваем дальше
            print(f"[floodwait] жду {ex.seconds}с")
            await asyncio.sleep(ex.seconds + 5)
            try:
                e = await client.get_entity(ch["username"])
            except Exception as ex2:  # noqa: BLE001
                failed += 1
                print(f"[skip] @{ch['username']}: {str(ex2)[:70]}")
                continue
        except Exception as ex:  # noqa: BLE001
            failed += 1
            print(f"[skip] @{ch['username']}: {str(ex)[:70]}")
            await asyncio.sleep(random.uniform(*RESOLVE_PAUSE))
            continue
        tg_id = getattr(e, "id", None)
        if not tg_id:
            failed += 1
            continue
        members = getattr(e, "participants_count", None)
        with database.get_conn() as conn:
            # чат с таким tg_chat_id мог уже быть заведён отдельной записью — не плодим дубль
            dup = conn.execute("SELECT id FROM chats WHERE tg_chat_id=? AND id<>?",
                               (tg_id, ch["id"])).fetchone()
            if dup:
                print(f"[дубль] @{ch['username']} → чат #{dup['id']} уже с этим tg_chat_id, помечаю skip")
                conn.execute("UPDATE chats SET status='skip', notes=COALESCE(notes,'')||' | дубль #'||? "
                             "WHERE id=?", (dup["id"], ch["id"]))
                failed += 1
            else:
                conn.execute(
                    "UPDATE chats SET tg_chat_id=?, members_count=COALESCE(members_count,?) WHERE id=?",
                    (tg_id, members, ch["id"]),
                )
                filled += 1
        if i < len(rows) - 1:
            await asyncio.sleep(random.uniform(*RESOLVE_PAUSE))
    with database.get_conn() as conn:
        # условие ОДИН В ОДИН с выборкой кандидатов выше, иначе «осталось» считает и то,
        # что мы никогда не возьмём (skip/banned, в т.ч. помеченные дублями), и цифра
        # навсегда залипает — оператор жмёт «Дозаполнить» впустую
        left = conn.execute("SELECT COUNT(*) FROM chats WHERE tg_chat_id IS NULL "
                            "AND username IS NOT NULL AND username<>'' "
                            "AND COALESCE(status,'') NOT IN ('skip','banned')").fetchone()[0]
    return {"candidates": len(rows), "filled": filled, "failed": failed, "left": left}


async def _backfill_photos(client, limit: int) -> dict:
    """Лиды с tg_user_id, но без фото → качаем аватар в data/avatars/{tg_user_id}.jpg."""
    from channels.tg_parser import _download_avatar
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, tg_user_id, username FROM contacts WHERE tg_user_id IS NOT NULL "
            "AND COALESCE(has_photo,0)=0 ORDER BY COALESCE(score,0) DESC, id LIMIT ?", (limit,)
        ).fetchall()]
    got: set[int] = set()
    nophoto = failed = 0
    for i, ct in enumerate(rows):
        try:
            u = await client.get_entity(int(ct["tg_user_id"]))
        except FloodWaitError as ex:
            print(f"[floodwait] жду {ex.seconds}с")
            await asyncio.sleep(ex.seconds + 5)
            continue
        except Exception as ex:  # noqa: BLE001
            failed += 1   # удалён/недоступен по приватности
            print(f"[skip] #{ct['id']} ({ct['tg_user_id']}): {str(ex)[:70]}")
            await asyncio.sleep(random.uniform(*PHOTO_PAUSE))
            continue
        if await _download_avatar(client, u):
            got.add(int(ct["tg_user_id"]))
        else:
            nophoto += 1   # аватара просто нет или скрыт приватностью
        if i < len(rows) - 1:
            await asyncio.sleep(random.uniform(*PHOTO_PAUSE))
    if got:
        with database.get_conn() as conn:
            database.mark_photos_by_tg(conn, got)
    return {"candidates": len(rows), "downloaded": len(got), "no_photo": nophoto, "failed": failed}


async def run(do_chats: bool, do_photos: bool, limit: int) -> None:
    database.init_db()
    summary: dict = {"ok": True}

    if do_photos:
        # сначала бесплатный проход: что уже лежит на диске — просто отметить
        summary["photos_from_disk"] = _mark_photos_from_disk()

    need_net = do_chats or do_photos
    if not need_net:
        print(json.dumps(summary, ensure_ascii=False))
        return

    client = _build_client()
    await client.start()
    try:
        if do_chats:
            summary["chats"] = await _backfill_chats(client, limit)
            print(f"[chats] {summary['chats']}")
        if do_photos:
            summary["photos"] = await _backfill_photos(client, limit)
            print(f"[photos] {summary['photos']}")
    finally:
        await client.disconnect()

    print(json.dumps(summary, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM бэкфилл: tg_chat_id у чатов, фото у лидов")
    p.add_argument("--chats", action="store_true", help="дозаполнить chats.tg_chat_id по @username")
    p.add_argument("--photos", action="store_true", help="докачать аватары лидов")
    p.add_argument("--all", action="store_true", help="и чаты, и фото")
    p.add_argument("--limit", type=int, default=200, help="сколько записей за прогон (на каждый вид)")
    args = p.parse_args()
    do_chats = args.chats or args.all
    do_photos = args.photos or args.all
    if not do_chats and not do_photos:
        p.error("нужен --chats, --photos или --all")
    if not config.TG_API_ID:
        print(json.dumps({"ok": False, "error": "нет TG_API_ID в .env"}, ensure_ascii=False))
        return
    asyncio.run(run(do_chats, do_photos, args.limit))


if __name__ == "__main__":
    main()
