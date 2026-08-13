"""Оркестратор встречи: слот из диалога → Zoom-ссылка + событие в календаре.

Точка входа — arrange(): её зовёт channels/telegram.py, когда агент поймал согласие.
Всё деградирует мягко: нет Zoom/Calendar-доступов → встреча всё равно фиксируется,
просто без ссылки/события. meeting_at нормализуется в ISO с таймзоной — тогда
scheduler бьёт напоминания точно.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from integrations import calendar as gcal
from integrations import slot_parse
from integrations import zoom


@dataclass
class MeetingResult:
    meeting_at_iso: str | None   # ISO с таймзоной, либо исходная строка, если не распарсилось
    zoom_link: str | None
    calendar_event_id: str | None
    parsed: bool                 # удалось ли превратить слот в реальную дату


def _meeting_url(campaign_id: int | None = None) -> str:
    """Постоянная ссылка на созвон (Телемост/Zoom/Meet). Порядок: комната КАМПАНИИ →
    общая настройка пульта → .env.

    Комната у каждой кампании своя: разные продукты ведут в разные переговорки, и одна
    ссылка на всё быстро становится неверной. Общая настройка остаётся запасной, а .env
    — последним рубежом: он лежит на сервере, куда у оператора нет SSH, поэтому
    полагаться на него нельзя (без ссылки агент договорится о времени и не даст адреса
    подключения, то есть встреча сорвётся)."""
    try:
        from db import database
        with database.get_conn() as conn:
            if campaign_id:
                row = conn.execute("SELECT meeting_url FROM campaigns WHERE id=?",
                                   (campaign_id,)).fetchone()
                own = (row["meeting_url"] or "").strip() if row else ""
                if own:
                    return own
            url = (database.get_setting(conn, "meeting_url", "") or "").strip()
        if url:
            return url
    except Exception:  # noqa: BLE001 — БД недоступна: не мешаем встрече состояться
        pass
    return config.PERMANENT_MEETING_URL or ""


def parse_slot(slot: str | None) -> datetime | None:
    """Слот из диалога → aware datetime в MEETING_TZ. Не распарсил → None.

    Сам разбор — в integrations/slot_parse: живую речь («завтра в 11», «в пятницу к
    16», «12 августа») тем же кодом читает и планировщик напоминаний. Раньше здесь
    лежал свой список из четырёх strptime-форматов, и всё, что человек говорил
    словами, давало None — созвон фиксировался без Zoom-ссылки и без напоминания."""
    tz = ZoneInfo(config.MEETING_TZ)
    return slot_parse.parse_human(slot, datetime.now(tz))


def _campaign_line(campaign_id: int | None) -> str:
    """Название и ссылка на кампанию — событие в Google Calendar раньше не говорило,
    ОТКУДА этот лид, и вернуться к контексту (промпт, оффер, остальная аудитория)
    можно было только вспомнив/угадав кампанию по имени контакта."""
    if not campaign_id:
        return ""
    try:
        from db import database
        with database.get_conn() as conn:
            row = conn.execute("SELECT name FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row:
            return ""
        base = (config.PUBLIC_URL or "").rstrip("/")
        link = f"{base}/#campaigns/{campaign_id}" if base else f"#campaigns/{campaign_id}"
        return f"Кампания: {row['name']} — {link}"
    except Exception:  # noqa: BLE001 — не мешаем встрече состояться
        return ""


def arrange(contact: dict, slot: str | None, campaign_id: int | None = None,
           notes: str | None = None, contact_id: int | None = None,
           username: str | None = None, phone: str | None = None) -> MeetingResult:
    """Создаёт Zoom + событие под согласованный слот. Внешние вызовы синхронные —
    из async-кода зови через asyncio.to_thread.

    campaign_id — чтобы взять переговорку именно этой кампании (см. _meeting_url).
    notes/contact_id/username/phone — та же информация, что уходит владельцу личным
    уведомлением (channels/notify.py): раньше событие в календаре несло только имя и
    Zoom-ссылку, а чем человек занимается, из какой он кампании и о чём вообще был
    разговор — можно было узнать только вернувшись в переписку."""
    name = contact.get("name") or "риелтор"
    dt = parse_slot(slot)
    if dt is None:
        # не смогли распарсить время — фиксируем как есть, без внешних сервисов
        return MeetingResult(meeting_at_iso=slot, zoom_link=None, calendar_event_id=None, parsed=False)

    topic = f"AXIOM: созвон с {name}"
    # Постоянная ссылка (личная комната Zoom, Телемост, Meet) — если задана, берём её
    # и в Zoom API не идём вовсе. Так встреча получает ссылку даже когда внешние
    # сервисы недоступны (Google Calendar и часть Zoom API закрыты из РФ) — раньше в
    # этом случае человек соглашался на созвон и оставался без адреса подключения.
    permanent = (_meeting_url(campaign_id) or "").strip()
    if permanent:
        zoom_link = permanent
    else:
        z = zoom.create_meeting(topic, dt, config.MEETING_DURATION_MIN, config.MEETING_TZ)
        zoom_link = z["join_url"] if z else None

    lines = [f"Созвон с {name}."]
    if zoom_link:
        lines.append(f"Zoom: {zoom_link}")
    spec = (contact.get("specialization") or contact.get("niche") or "").strip()
    if spec:
        lines.append(f"Чем занимается: {spec}")
    if username:
        lines.append(f"TG: @{username}")
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if digits:
            lines.append(f"Номер: https://t.me/+{digits}")
    if notes and notes.strip():
        lines.append(f"О чём говорили: {notes.strip()[:300]}")
    camp_line = _campaign_line(campaign_id)
    if camp_line:
        lines.append(camp_line)
    if contact_id:
        base = (config.PUBLIC_URL or "").rstrip("/")
        chat_link = f"{base}/#chats/{contact_id}" if base else f"#chats/{contact_id}"
        lines.append(f"Переписка: {chat_link}")
    desc = "\n".join(lines)
    ev = gcal.create_event(topic, dt, config.MEETING_DURATION_MIN, config.MEETING_TZ, description=desc)
    event_id = ev["id"] if ev else None

    return MeetingResult(
        meeting_at_iso=dt.isoformat(),
        zoom_link=zoom_link,
        calendar_event_id=event_id,
        parsed=True,
    )
