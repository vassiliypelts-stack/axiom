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


def _dossier_link(contact_id: int) -> str:
    base = (config.PUBLIC_URL or "").rstrip("/")
    return f"{base}/#dossier/{contact_id}" if base else f"#dossier/{contact_id}"


def _build_text(row, meeting_at: str | None, notes: str | None, zoom_link: str | None) -> str:
    who = (row["person_name"] or row["name"] or "контакт").strip()
    spec = (row["specialization"] or row["niche"] or "").strip()
    uname = f"@{row['username']}" if row["username"] else "без ника в TG"
    lines = [f"📅 Договорились о встрече: {who}"]
    if spec:
        lines.append(f"Чем занимается: {spec}")
    lines.append(f"TG: {uname}")
    if meeting_at:
        lines.append(f"Время: {meeting_at}")
    if zoom_link:
        lines.append(f"Zoom: {zoom_link}")
    if notes and notes.strip():
        lines.append(f"О чём говорили: {notes.strip()[:300]}")
    lines.append(_dossier_link(row["id"]))
    return "\n".join(lines)


async def notify_meeting(contact_id: int, meeting_at: str | None, notes: str | None,
                          zoom_link: str | None = None) -> None:
    """Лучшая попытка — сбой не должен ронять основной поток ответа агента, поэтому
    ничего не бросаем наружу, только логируем."""
    try:
        with database.get_conn() as conn:
            sender_id = database.get_setting(conn, NOTIFY_SENDER_SETTING)
            target = database.get_setting(conn, NOTIFY_TARGET_SETTING)
            if not sender_id or not target:
                return  # не настроено — тихо пропускаем
            row = conn.execute(
                "SELECT id, name, person_name, username, specialization, niche "
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
