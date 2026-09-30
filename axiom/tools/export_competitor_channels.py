"""
Выгрузка истории каналов/ботов конкурента из диалогов личного аккаунта в JSON.
Только чтение — ни во что не вступает, ничего не отправляет, кнопки не нажимает.

Ищет среди УЖЕ открытых диалогов аккаунта те, чьё название или username подходит
под --match, и выгружает всю историю каждого: текст, просмотры, реакции, пересылки,
закреп, кнопки, ссылки, тип медиа. Для бота это весь диалог с ним (out=true — наши
сообщения), по датам видно тайминги дожимов.

Запуск НА СЕРВЕРЕ (сессии 988/702 привязаны к его IP — не гонять больше нигде):
    cd ~/axiom-repo/axiom
    .venv/bin/python tools/export_competitor_channels.py --account-id 5
    .venv/bin/python tools/export_competitor_channels.py --account-id 3 --list

    --account-id 5  Василий988,  3  Василий702
    --match         регулярка по названию/username (по умолчанию — Сергеев)
    --list          только показать совпавшие диалоги, историю не качать
    --media         none | light (голосовые, кружки, фото — по умолчанию) | all

Результат: axiom/data/competitor/<account_id>/<username-или-id>.json (+ media/)
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon.errors import FloodWaitError
from telethon.tl.types import (KeyboardButtonCallback, KeyboardButtonUrl,
                               MessageEntityTextUrl, MessageEntityUrl, ReactionEmoji)

from channels.telegram import client_for_account  # noqa: E402

DEFAULT_MATCH = r"сергеев|sergeev|вайбмаркетинг|vibemarketing|ии-лагерь|ailager"
OUT_ROOT = Path(__file__).resolve().parent.parent / "data" / "competitor"
MAX_FLOOD_WAIT = 900


def _reactions(msg) -> dict:
    res = getattr(getattr(msg, "reactions", None), "results", None) or []
    out = {}
    for r in res:
        key = r.reaction.emoticon if isinstance(r.reaction, ReactionEmoji) else "custom"
        out[key] = out.get(key, 0) + r.count
    return out


def _buttons(msg) -> list[dict]:
    rows = getattr(getattr(msg, "reply_markup", None), "rows", None) or []
    out = []
    for row in rows:
        for b in row.buttons:
            item = {"text": b.text, "kind": type(b).__name__}
            if isinstance(b, KeyboardButtonUrl):
                item["url"] = b.url
            elif isinstance(b, KeyboardButtonCallback):
                item["data"] = b.data.decode("utf-8", "replace")
            out.append(item)
    return out


def _links(msg) -> list[str]:
    links = []
    for ent, txt in msg.get_entities_text():
        if isinstance(ent, MessageEntityTextUrl):
            links.append(ent.url)
        elif isinstance(ent, MessageEntityUrl):
            links.append(txt)
    return links


def _media_kind(msg) -> str | None:
    if not msg.media:
        return None
    for kind in ("voice", "video_note", "photo", "gif", "video", "audio", "sticker", "poll"):
        if getattr(msg, kind, None):
            return kind
    if msg.document:
        return "document"
    return type(msg.media).__name__


def _want_media(kind: str | None, mode: str) -> bool:
    if not kind or mode == "none":
        return False
    if mode == "all":
        return kind not in ("sticker", "poll", "MessageMediaWebPage")
    return kind in ("voice", "video_note", "photo")


def _row(msg, media_file: str | None) -> dict:
    fwd = msg.fwd_from
    return {
        "id": msg.id,
        "date": msg.date.isoformat(),
        "edit_date": msg.edit_date.isoformat() if msg.edit_date else None,
        "out": bool(msg.out),
        "text": msg.message or "",
        "views": msg.views,
        "forwards": msg.forwards,
        "replies": getattr(msg.replies, "replies", None),
        "reactions": _reactions(msg),
        "pinned": bool(msg.pinned),
        "grouped_id": msg.grouped_id,
        "reply_to": getattr(msg.reply_to, "reply_to_msg_id", None),
        "fwd_from": (fwd.from_name or str(getattr(fwd.from_id, "channel_id", "") or "")) if fwd else None,
        "post_author": msg.post_author,
        "buttons": _buttons(msg),
        "links": _links(msg),
        "media": _media_kind(msg),
        "media_file": media_file,
    }


async def _export_dialog(client, d, out_dir: Path, media_mode: str) -> int:
    ent = d.entity
    slug = getattr(ent, "username", None) or str(ent.id)
    media_dir = out_dir / "media" / slug
    rows, last_id = [], 0
    while True:
        try:
            async for msg in client.iter_messages(ent, reverse=True, min_id=last_id):
                media_file = None
                kind = _media_kind(msg)
                if _want_media(kind, media_mode):
                    media_dir.mkdir(parents=True, exist_ok=True)
                    path = await msg.download_media(file=str(media_dir / f"{msg.id}"))
                    media_file = str(Path(path).relative_to(out_dir)) if path else None
                rows.append(_row(msg, media_file))
                last_id = msg.id
            break
        except FloodWaitError as e:
            if e.seconds > MAX_FLOOD_WAIT:
                print(f"  FloodWait {e.seconds} с — останавливаюсь на id={last_id}, выгружено частично")
                break
            print(f"  FloodWait {e.seconds} с — жду и продолжаю с id={last_id}")
            await asyncio.sleep(e.seconds + 5)

    pinned = [r["id"] for r in rows if r["pinned"]]
    data = {
        "title": d.title,
        "username": getattr(ent, "username", None),
        "id": ent.id,
        "type": type(ent).__name__,
        "is_bot": bool(getattr(ent, "bot", False)),
        "participants": getattr(ent, "participants_count", None),
        "pinned_ids": pinned,
        "count": len(rows),
        "messages": rows,
    }
    (out_dir / f"{slug}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--match", default=DEFAULT_MATCH)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--media", choices=("none", "light", "all"), default="light")
    args = ap.parse_args()

    rx = re.compile(args.match, re.IGNORECASE)
    client, _ = client_for_account(args.account_id)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print(f"Сессия аккаунта #{args.account_id} не авторизована — читать нечего")
            return
        me = await client.get_me()
        print(f"Вошли как: {me.first_name} (id={me.id})")

        dialogs = await client.get_dialogs(limit=None)
        hits = [d for d in dialogs
                if rx.search(d.title or "") or rx.search(getattr(d.entity, "username", None) or "")]
        print(f"Совпало диалогов: {len(hits)}")
        for d in hits:
            print(f"  {d.title} | @{getattr(d.entity, 'username', None)} | {type(d.entity).__name__}")
        if args.list or not hits:
            return

        out_dir = OUT_ROOT / str(args.account_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        for d in hits:
            n = await _export_dialog(client, d, out_dir, args.media)
            print(f"  ✓ {d.title}: {n} сообщений")
        print(f"\nСохранено в {out_dir}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
