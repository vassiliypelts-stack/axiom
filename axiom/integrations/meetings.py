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


def _meeting_url() -> str:
    """Постоянная ссылка на созвон (Телемост/Zoom/Meet): сначала настройка из пульта,
    потом .env.

    Почему настройка важнее переменной окружения: .env лежит на сервере, а SSH к нему
    у оператора под рукой нет. Без ссылки агент договаривается о времени и не даёт
    адреса подключения — то есть встреча де-факто срывается. Теперь ссылку можно
    вписать в «Мои агенты» и она подхватится без перезапуска сервиса."""
    try:
        from db import database
        with database.get_conn() as conn:
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


def arrange(contact: dict, slot: str | None) -> MeetingResult:
    """Создаёт Zoom + событие под согласованный слот. Внешние вызовы синхронные —
    из async-кода зови через asyncio.to_thread."""
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
    permanent = (_meeting_url() or "").strip()
    if permanent:
        zoom_link = permanent
    else:
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
