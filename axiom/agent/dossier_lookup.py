"""Досье по телефону или @username — один клик от контакта к AI-портрету.

Поток (этап 1 конвейера, см. docs/superpowers/specs/2026-07-23-...):
  вход: телефон (+7...) ИЛИ @username
    1) берём живой аккаунт (сессия+прокси) для захода в Telegram
    2) resolve → TG-профиль (tg_user_id, имя, @, bio, аватар)
    3) если у человека прикреплён ЛИЧНЫЙ КАНАЛ — собираем его посты (там боли/
       желания видны ярче, чем в чужих чатах)
    4) upsert контакта + запись сырья (bio, посты) в БД
    5) enrich_person (Haiku) → боли/страхи/желания/психотип/пол/крючок → карточка

Антибан: один заход, дозированно. Личный канал читаем только если он публичный/
доступен нашему аккаунту.

CLI:
    python -m agent.dossier_lookup "+79161234567"
    python -m agent.dossier_lookup "@ivan_realtor"
"""
from __future__ import annotations

import asyncio
import random
import sys
import time

from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact, User as TLUser

import config
from db import database
from channels.telegram import build_client
from telethon.sessions import StringSession
from channels.tg_parser import _download_avatar, _display_name, _fetch_bio  # noqa
from agent import enrich_person

CHANNEL_POSTS = 30   # сколько постов личного канала собрать для досье


def _pick_live_account() -> tuple[int | None, str | None]:
    """Первый живой аккаунт (сессия жива + прокси) для захода. (id, err)."""
    with database.get_conn() as conn:
        a = conn.execute(
            "SELECT id, label FROM accounts "
            "WHERE tg_session IS NOT NULL AND tg_session<>'' "
            "AND (session_state='alive' OR session_alive=1) "
            "AND COALESCE(status,'')<>'banned' ORDER BY id LIMIT 1"
        ).fetchone()
    if not a:
        return None, "нет ни одного живого аккаунта (нужна живая сессия) — залогинь/оживи аккаунт"
    return a["id"], None


async def _client_for(acc_id: int):
    with database.get_conn() as conn:
        a = conn.execute(
            "SELECT tg_session, proxy, api_id, api_hash FROM accounts WHERE id=?", (acc_id,)
        ).fetchone()
    cl = build_client(StringSession(a["tg_session"]), a["proxy"], a["api_id"], a["api_hash"])
    await cl.connect()
    if not await cl.is_user_authorized():
        await cl.disconnect()
        raise RuntimeError("сессия аккаунта не авторизована")
    return cl


async def _resolve(client, query: str):
    """Телефон → пользователь (импорт контакта); @username/ссылка → get_entity."""
    q = query.strip()
    if q.startswith("@") or (not q.lstrip("+").isdigit()):
        return await client.get_entity(q.lstrip())
    # телефон — нормализуем: 8XXXXXXXXXX → +7XXXXXXXXXX (РФ/КЗ пишут через 8)
    digits = q.lstrip("+")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    phone = "+" + digits
    res = await client(ImportContactsRequest(
        [InputPhoneContact(client_id=random.randint(0, 2**31), phone=phone,
                           first_name="lead", last_name="")]
    ))
    if not res.users:
        raise RuntimeError(f"по номеру {phone} не нашёлся пользователь Telegram "
                           "(скрыт настройками приватности или нет аккаунта)")
    return res.users[0]


async def _collect_personal_channel(client, full, tg_user_id: int, conn) -> int:
    """Если у человека прикреплён личный канал — собрать его посты в tg_user_posts."""
    pch_id = getattr(full.full_user, "personal_channel_id", None)
    if not pch_id:
        return 0
    try:
        ch = await client.get_entity(pch_id)
    except Exception:
        return 0
    title = getattr(ch, "title", "") or "личный канал"
    n = 0
    try:
        async for m in client.iter_messages(ch, limit=CHANNEL_POSTS):
            txt = (m.message or "").strip()
            if not txt:
                continue
            ts = int(m.date.timestamp()) if m.date else int(time.time())
            database.save_user_posts(conn, tg_user_id, getattr(ch, "id", None),
                                     f"📢 канал: {title}", txt, m.id, ts)
            n += 1
    except Exception:
        pass
    return n


async def lookup(query: str) -> dict:
    """Главная точка: телефон/@ → досье. Возвращает {ok, contact_id, name, ...} или {error}."""
    database.init_db()
    acc_id, err = _pick_live_account()
    if err:
        return {"error": err}

    client = None
    try:
        client = await _client_for(acc_id)
        user = await _resolve(client, query)

        # досье строится по ЧЕЛОВЕКУ; если @ ведёт на канал/группу — честно скажем
        if not isinstance(user, TLUser):
            return {"error": "это канал или группа, а не человек. Досье собирается по людям "
                             "(телефон или @username пользователя, не канала)."}

        # полный профиль: bio + личный канал
        full = await client(GetFullUserRequest(user))
        bio = getattr(full.full_user, "about", None) or None
        has_photo = await _download_avatar(client, user)

        name = _display_name(user)
        uname = getattr(user, "username", None)
        phone = getattr(user, "phone", None)
        phone = ("+" + phone) if phone and not phone.startswith("+") else phone

        with database.get_conn() as conn:
            contact_id = database.upsert_contact(
                conn, source="досье", phone=phone, username=uname,
                tg_user_id=user.id, name=name,
                tags="досье",
            )
            database.set_bio_by_tg(conn, user.id, bio)
            if has_photo:
                database.mark_photos_by_tg(conn, [user.id])
            channel_posts = await _collect_personal_channel(client, full, user.id, conn)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    # AI-досье строим ВСЕГДА (даже без постов — по bio + аватар: пол по фото,
    # сегмент по bio, базовый портрет). Прямой вызов, чтобы не скипнуть «нет сообщений».
    try:
        with database.get_conn() as conn:
            crow = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
            posts = enrich_person._posts_for(conn, user.id)
        profile = enrich_person.enrich_person(dict(crow), posts)
        enrich_person._save(contact_id, profile)
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "contact_id": contact_id, "name": name,
                "warning": f"профиль собран, но AI-досье не построилось: {str(e)[:100]}",
                "channel_posts": channel_posts}

    return {"ok": True, "contact_id": contact_id, "name": name,
            "username": uname, "phone": phone, "has_photo": has_photo,
            "channel_posts": channel_posts, "has_bio": bool(bio)}


def main() -> None:
    if len(sys.argv) < 2:
        print("Использование: python -m agent.dossier_lookup \"+79161234567\" | \"@username\"")
        sys.exit(1)
    res = asyncio.run(lookup(sys.argv[1]))
    import json
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
