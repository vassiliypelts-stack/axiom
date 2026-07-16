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
# FloodWait дольше — не ждём, а выходим (как MAX_FLOOD_SKIP в chat_join).
# Ловили вживую 16.07: после ~280 резолвов подряд Telegram выдал FloodWait 82169с (22.8ч),
# и прогон послушно уснул на сутки. Резолв тысяч @username с ОДНОГО аккаунта упирается в
# суточный лимит — это нормальная реакция Telegram, а не сбой: надо выйти, сказать
# оператору правду и продолжить порциями/позже.
MAX_FLOOD_WAIT = 600


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


# Кого вообще можно дозаполнить. Одно условие на выборку кандидатов И на счётчик
# «осталось»: когда они разъезжались, «осталось» считало в том числе skip/banned
# (включая помеченные дублями ниже) — цифра залипала, и оператор жал «Дозаполнить» впустую.
_RESOLVABLE = ("tg_chat_id IS NULL AND username IS NOT NULL AND username<>'' "
               "AND COALESCE(status,'') NOT IN ('skip','banned')")


class FloodStop(Exception):
    """Telegram сказал ждать слишком долго — прогон надо прекратить, а не спать сутки."""

    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"FloodWait {seconds}с")


async def _resolve(client, username: str):
    """Сущность по @username. Короткий FloodWait — ждём и повторяем ЭТУ ЖЕ запись (ради неё
    и ждали). Длинный — FloodStop наверх: аккаунт упёрся в лимит, дальше идти бессмысленно.
    None = чат не резолвится (удалён/переименован)."""
    for attempt in (1, 2):
        try:
            return await client.get_entity(username)
        except FloodWaitError as ex:
            if ex.seconds > MAX_FLOOD_WAIT:
                raise FloodStop(ex.seconds) from ex
            if attempt == 2:
                return None
            print(f"[floodwait] жду {ex.seconds}с")
            await asyncio.sleep(ex.seconds + 5)
        except Exception as ex:  # noqa: BLE001
            print(f"[skip] @{username}: {str(ex)[:70]}")
            return None
    return None


async def _backfill_chats(client, limit: int) -> dict:
    """chats без tg_chat_id, но с @username → резолвим сущность → дозаполняем id."""
    conn = database.get_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT id, title, username FROM chats WHERE {_RESOLVABLE} "
            f"ORDER BY COALESCE(favorite,0) DESC, COALESCE(members_count,0) DESC, id LIMIT ?",
            (limit,)
        ).fetchall()]
        filled = failed = 0
        flood: int | None = None
        for i, ch in enumerate(rows):
            try:
                e = await _resolve(client, ch["username"])
            except FloodStop as fs:
                # аккаунт упёрся в суточный лимит: выходим, сохранив сделанное
                flood = fs.seconds
                print(f"[стоп] Telegram просит ждать {fs.seconds}с ({fs.seconds // 3600}ч) — "
                      f"аккаунт упёрся в лимит резолвов. Дозаполнено {filled}, продолжи позже.")
                break
            tg_id = getattr(e, "id", None) if e else None
            if not tg_id:
                failed += 1
            else:
                members = getattr(e, "participants_count", None)
                # `with conn` коммитит, НЕ закрывая: коммит на каждой записи тут
                # принципиален — проход идёт десятки минут и может оборваться (уже ловили
                # тихую смерть фонового прогона), одна транзакция на 1800 чатов означала
                # бы потерю всей работы разом.
                with conn:
                    # такой tg_chat_id мог уже быть у другой записи — не плодим дубль
                    dup = conn.execute("SELECT id FROM chats WHERE tg_chat_id=? AND id<>?",
                                       (tg_id, ch["id"])).fetchone()
                    if dup:
                        print(f"[дубль] @{ch['username']} → чат #{dup['id']} уже с этим "
                              f"tg_chat_id, помечаю skip")
                        conn.execute("UPDATE chats SET status='skip', "
                                     "notes=COALESCE(notes,'')||' | дубль #'||? WHERE id=?",
                                     (dup["id"], ch["id"]))
                        failed += 1
                    else:
                        conn.execute("UPDATE chats SET tg_chat_id=?, "
                                     "members_count=COALESCE(members_count,?) WHERE id=?",
                                     (tg_id, members, ch["id"]))
                        filled += 1
            if i < len(rows) - 1:
                await asyncio.sleep(random.uniform(*RESOLVE_PAUSE))
        left = conn.execute(f"SELECT COUNT(*) FROM chats WHERE {_RESOLVABLE}").fetchone()[0]
    finally:
        conn.close()
    out = {"candidates": len(rows), "filled": filled, "failed": failed, "left": left}
    if flood:
        out["flood_wait"] = flood
        out["note"] = (f"Telegram ограничил аккаунт на {flood // 3600}ч — дозаполнено {filled}, "
                       f"остальное позже (лимит резолвов на аккаунт за сутки)")
    return out


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
    flood: int | None = None
    for i, ct in enumerate(rows):
        try:
            u = await client.get_entity(int(ct["tg_user_id"]))
        except FloodWaitError as ex:
            if ex.seconds > MAX_FLOOD_WAIT:
                flood = ex.seconds   # аккаунт упёрся в лимит — выходим, а не спим сутки
                print(f"[стоп] Telegram просит ждать {ex.seconds}с ({ex.seconds // 3600}ч) — "
                      f"скачано {len(got)}, продолжи позже.")
                break
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
    out = {"candidates": len(rows), "downloaded": len(got), "no_photo": nophoto, "failed": failed}
    if flood:
        out["flood_wait"] = flood
        out["note"] = f"Telegram ограничил аккаунт на {flood // 3600}ч — остальное позже"
    return out


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
