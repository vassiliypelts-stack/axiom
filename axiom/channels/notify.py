"""Уведомление владельца в личку Telegram, когда агент договорился о встрече.

ЗАЧЕМ. Встреча и так видна в колокольчике пульта, но туда нужно зайти и посмотреть.
Гораздо надёжнее долетает то, что упало прямо в личку Telegram — там оператор и так
весь день. Шлёт один из аккаунтов (обычно «родной» личный — назначается в пульте),
обычным сообщением через Telethon, а не через бота — выглядит как обычная личка,
не служебное уведомление.

Настройка — пульт → Аккаунты → «уведомляет о встречах» (кому назначить отправителем)
+ поле «получатель уведомлений» (телефон/@ник, куда слать). Хранится в app_settings
под ключами notify_sender_account_id / notify_owner_target. Не настроено — молча
пропускаем: в отличие от listen_account_id здесь нет безопасного отката (слать
уведомление больше некому и незачем гадать получателя).
"""
from __future__ import annotations

import config
from channels.telegram import client_for_account
from db import database

NOTIFY_SENDER_SETTING = "notify_sender_account_id"
NOTIFY_TARGET_SETTING = "notify_owner_target"


def _chat_link(contact_id: int) -> str:
    """Ссылка на РЕАЛЬНУЮ переписку (#chats/{id} → openThread), а не на карточку CRM
    (#dossier). Раньше уведомление вело в досье, а там нужно было ещё сообразить
    нажать «переписка» — оператору важнее всего сразу увидеть, о чём говорили."""
    base = (config.PUBLIC_URL or "").rstrip("/")
    return f"{base}/#chats/{contact_id}" if base else f"#chats/{contact_id}"


def _phone_link(phone: str | None) -> str | None:
    """Номер как кликабельная ссылка Telegram: https://t.me/+79137876067 — открывает
    диалог с этим номером сразу, без ручного набора/копирования в поиск."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"https://t.me/+{digits}" if digits else None


def _build_text(row, meeting_at: str | None, notes: str | None, zoom_link: str | None) -> str:
    who = (row["person_name"] or row["name"] or "контакт").strip()
    spec = (row["specialization"] or row["niche"] or "").strip()
    uname = f"@{row['username']}" if row["username"] else "без ника в TG"
    lines = [f"📅 Договорились о встрече: {who}"]
    if spec:
        lines.append(f"Чем занимается: {spec}")
    lines.append(f"TG: {uname}")
    phone_link = _phone_link(row["phone"])
    if phone_link:
        lines.append(f"Номер: {phone_link}")
    if meeting_at:
        lines.append(f"Время: {meeting_at}")
    if zoom_link:
        lines.append(f"Ссылка: {zoom_link}")
    if notes and notes.strip():
        lines.append(f"О чём говорили: {notes.strip()[:300]}")
    lines.append(_chat_link(row["id"]))
    return "\n".join(lines)


async def notify_meeting(contact_id: int, meeting_at: str | None, notes: str | None,
                          zoom_link: str | None = None, campaign_id: int | None = None) -> None:
    """Лучшая попытка — сбой не должен ронять основной поток ответа агента, поэтому
    ничего не бросаем наружу, только логируем.

    Кому и с какого аккаунта — сначала смотрим настройки САМОЙ кампании: разные
    кампании ведут разные люди, и «договорились о встрече» должно падать тому, кто эту
    встречу проводит. Пусто в кампании → общие настройки пульта (прежнее поведение)."""
    try:
        with database.get_conn() as conn:
            sender_id = target = None
            if campaign_id:
                c = conn.execute("SELECT notify_account_id, notify_target FROM campaigns "
                                 "WHERE id=?", (campaign_id,)).fetchone()
                if c:
                    sender_id = c["notify_account_id"]
                    target = (c["notify_target"] or "").strip() or None
            # Отправителя и получателя добираем по отдельности: в кампании может быть
            # задан только один из них, и терять из-за этого второй нельзя.
            sender_id = sender_id or database.get_setting(conn, NOTIFY_SENDER_SETTING)
            target = target or database.get_setting(conn, NOTIFY_TARGET_SETTING)
            if not sender_id or not target:
                return  # не настроено — тихо пропускаем
            row = conn.execute(
                "SELECT id, name, person_name, username, phone, specialization, niche "
                "FROM contacts WHERE id=?", (contact_id,),
            ).fetchone()
        if not row:
            return
        text = _build_text(row, meeting_at, notes, zoom_link)

        client, _ = client_for_account(int(sender_id))
        await client.connect()
        try:
            if not await client.is_user_authorized():
                print(f"[notify] аккаунт #{sender_id} не авторизован — уведомление не ушло")
                return
            await client.send_message(target, text)
            print(f"[notify] владелец уведомлён о встрече (контакт #{contact_id})")
        finally:
            await client.disconnect()
    except Exception as e:  # noqa: BLE001 — вспомогательное уведомление, не должно ронять диалог
        print(f"[notify] сбой отправки владельцу: {e}")
