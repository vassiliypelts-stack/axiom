"""Инспектор аккаунта: смотрим ЖИВОЙ профиль и переписки купленного аккаунта прямо
из AXIOM, не выходя в Telegram. Read-only — ничего не отправляем и не меняем.

Веб-эндпоинты (app.py) дергают эти функции:
  • inspect()          — как оформлен профиль (аватар/имя/bio/спрятан ли номер) + список диалогов;
  • dialog_messages()  — сами сообщения выбранного диалога (мини-Telegram внутри пульта).

Каждый вызов открывает свою короткую сессию через СОБСТВЕННЫЕ api_id/прокси аккаунта
(как и оформление профиля) — чтобы не спалить купленную сессию чужим api_id.
"""
from __future__ import annotations

from telethon.sessions import StringSession
from telethon.tl.functions.account import GetPrivacyRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputPrivacyKeyPhoneNumber, PrivacyValueDisallowAll

from channels.telegram import build_client


def _client_for(acc: dict):
    """Клиент под сессию аккаунта с его же api_id/прокси."""
    return build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                        acc.get("api_id"), acc.get("api_hash"))


async def inspect(acc: dict, dialogs_limit: int = 100) -> dict:
    """Профиль + последние диалоги аккаунта. Read-only."""
    client = _client_for(acc)
    try:
        await client.start()
        me = await client.get_me()
        try:
            full = await client(GetFullUserRequest("me"))
            about = getattr(full.full_user, "about", None)
        except Exception:  # noqa: BLE001
            about = None
        phone_hidden = None       # спрятан ли номер (приватность = «Никто»)
        try:
            pr = await client(GetPrivacyRequest(key=InputPrivacyKeyPhoneNumber()))
            phone_hidden = any(isinstance(r, PrivacyValueDisallowAll) for r in (pr.rules or []))
        except Exception:  # noqa: BLE001
            pass
        profile = {
            "id": me.id,
            "name": " ".join(x for x in [me.first_name, me.last_name] if x) or None,
            "username": me.username,
            "phone": ("+" + me.phone) if me.phone else None,
            "bio": about,
            "has_photo": bool(getattr(me, "photo", None)),
            "premium": bool(getattr(me, "premium", False)),
            "phone_hidden": phone_hidden,
        }
        dialogs = []
        async for d in client.iter_dialogs(limit=dialogs_limit):
            msg = d.message
            dialogs.append({
                "peer_id": d.id,
                "title": d.name or "—",
                "username": getattr(d.entity, "username", None),
                "kind": "user" if d.is_user else ("channel" if d.is_channel else "group"),
                "unread": d.unread_count,
                "last_text": (msg.message or ("[медиа]" if msg and msg.media else "")) [:120] if msg else "",
                "last_out": bool(msg.out) if msg else False,
                "last_date": msg.date.isoformat() if (msg and msg.date) else None,
            })
        return {"ok": True, "profile": profile, "dialogs": dialogs}
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def dialog_messages(acc: dict, peer: int, limit: int = 40) -> dict:
    """Сообщения выбранного диалога (хронологически, старые сверху). Read-only."""
    client = _client_for(acc)
    try:
        await client.start()
        await client.get_dialogs()                 # прогрев кэша сущностей → peer по id разрешится
        entity = await client.get_entity(peer)
        out = []
        async for m in client.iter_messages(entity, limit=limit):
            text = m.message or ("[медиа]" if m.media else "")
            if not text:
                continue
            # Кто писал: для исходящих — сам аккаунт; иначе имя отправителя (в группах разные).
            snd = getattr(m, "sender", None)
            sender = (getattr(snd, "first_name", None) or getattr(snd, "title", None)
                      or getattr(snd, "username", None)) if snd is not None else None
            out.append({
                "id": m.id,
                "out": bool(m.out),
                "text": text,
                "sender": sender,
                "date": m.date.isoformat() if m.date else None,
            })
        out.reverse()
        return {"ok": True, "messages": out}
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
