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
from integrations import zoom


@dataclass
class MeetingResult:
    meeting_at_iso: str | None   # ISO с таймзоной, либо исходная строка, если не распарсилось
    zoom_link: str | None
    calendar_event_id: str | None
    parsed: bool                 # удалось ли превратить слот в реальную дату


def _roll_future(dt: datetime, now: datetime) -> datetime:
    """Инвариант: встречу не назначаем в прошлое. Если дата прошедшая (частая причина —
    агент галлюцинирует год), прокатываем на ближайший будущий год."""
    if dt >= now:
        return dt
    try:
        dt = dt.replace(year=now.year)
    except ValueError:  # 29 февраля и т.п.
        return dt
    if dt < now:
        dt = dt.replace(year=now.year + 1)
    return dt


def parse_slot(slot: str | None) -> datetime | None:
    """Слот из диалога → aware datetime в MEETING_TZ. Понимает ISO и пару
    человеческих форматов ('20.06 в 11:00'). Прошедшие даты катит вперёд. Не распарсил → None."""
    if not slot:
        return None
    tz = ZoneInfo(config.MEETING_TZ)
    now = datetime.now(tz)
    s = slot.strip()
    try:
        dt = datetime.fromisoformat(s)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=tz)
        return _roll_future(dt, now)
    except ValueError:
        pass
    for fmt in ("%d.%m в %H:%M", "%d.%m %H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            return _roll_future(dt.replace(tzinfo=tz), now)
        except ValueError:
            continue
    return None


def arrange(contact: dict, slot: str | None) -> MeetingResult:
    """Создаёт Zoom + событие под согласованный слот. Внешние вызовы синхронные —
    из async-кода зови через asyncio.to_thread."""
    name = contact.get("name") or "риелтор"
    dt = parse_slot(slot)
    if dt is None:
        # не смогли распарсить время — фиксируем как есть, без внешних сервисов
        return MeetingResult(meeting_at_iso=slot, zoom_link=None, calendar_event_id=None, parsed=False)

    topic = f"AXIOM: созвон с {name}"
    z = zoom.create_meeting(topic, dt, config.MEETING_DURATION_MIN, config.MEETING_TZ)
    zoom_link = z["join_url"] if z else None

    desc = f"Созвон с {name}. {('Zoom: ' + zoom_link) if zoom_link else ''}".strip()
    ev = gcal.create_event(topic, dt, config.MEETING_DURATION_MIN, config.MEETING_TZ, description=desc)
    event_id = ev["id"] if ev else None

    return MeetingResult(
        meeting_at_iso=dt.isoformat(),
        zoom_link=zoom_link,
        calendar_event_id=event_id,
        parsed=True,
    )
