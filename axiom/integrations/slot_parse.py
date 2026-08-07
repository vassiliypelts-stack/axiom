"""Согласованное в переписке время → datetime. ЕДИНЫЙ разбор для всей системы.

Зачем отдельный модуль: время созвона появляется в диалоге живой речью — «давайте
завтра в 11», «в пятницу к 16», «12 августа в 15:00». Раньше его понимали два разных
куска кода (integrations/meetings.parse_slot и scheduler._parse_dt), и оба знали лишь
ISO и «12.08 в 11:00». Всё остальное давало None, а дальше по цепочке:
Zoom-ссылка не создавалась, события в календаре не было, напоминание не срабатывало —
человек соглашался на созвон и оставался без адреса подключения. Теперь разбор один
и понимает живую речь.

Модуль намеренно без внешних зависимостей: его тянет и scheduler, который обязан
гоняться без Telethon/Google-библиотек.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_WEEKDAYS = {
    "понедельник": 0, "пн": 0, "вторник": 1, "вт": 1, "среда": 2, "среду": 2, "ср": 2,
    "четверг": 3, "чт": 3, "пятница": 4, "пятницу": 4, "пт": 4,
    "суббота": 5, "субботу": 5, "сб": 5, "воскресенье": 6, "вс": 6,
}
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
# «в 7 вечера» — это 19:00, а не 07:00. Без этого агент назначал бы созвон на ночь.
_DAYPARTS = {"утра": (5, 11), "утром": (5, 11), "дня": (12, 17), "днём": (12, 17),
             "днем": (12, 17), "вечера": (17, 23), "вечером": (17, 23)}


def _hhmm(s: str) -> tuple[int, int] | None:
    """Время из строки: «11:00», «в 11», «к 16.30», «в 7 вечера» → (час, минута).

    Дату из строки вызывающий вырезает ЗАРАНЕЕ: иначе «12.08 в 11:00» читается как
    12 часов 8 минут — точка разделяет и время, и дату."""
    m = re.search(r"(?:^|[\s,вкна])(\d{1,2})[:.](\d{2})(?!\d)", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        # голый час: «в 11», «к 16», «на 9». Без предлога не берём — иначе «12 августа»
        # прочиталось бы как «12 часов».
        m = re.search(r"(?:^|\s)(?:в|к|на|около)\s*(\d{1,2})(?!\s*[:.\d])", s)
        if not m:
            return None
        h, mi = int(m.group(1)), 0
    for word, (lo, _hi) in _DAYPARTS.items():
        # именно \b: «дня» — часть слова «сегодня», и «сегодня в 9:00» превращалось в 21:00
        if re.search(rf"\b{word}\b", s):
            if h < 12 and lo >= 12:
                h += 12
            elif h == 12 and lo < 12:
                h = 0
            break
    return (h, mi) if 0 <= h <= 23 and 0 <= mi <= 59 else None


def parse_human(raw: str | None, now: datetime) -> datetime | None:
    """Живая формулировка времени → datetime в том же «виде», что и now (aware/naive).

    Понимает: ISO, «дд.мм[.гггг] [в] ЧЧ[:ММ]», «сегодня/завтра/послезавтра в ЧЧ»,
    дни недели («в пятницу в 16»), «12 августа в 15:00», голое «в 11:00» (ближайшее
    будущее). Не понял — None (вызывающий решает, что делать).
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # 1. ISO — так пишет сам агент, когда просим машинный формат.
    try:
        dt = datetime.fromisoformat(s)
        if (dt.tzinfo is None) != (now.tzinfo is None):
            dt = dt.replace(tzinfo=now.tzinfo) if now.tzinfo else dt.replace(tzinfo=None)
        return _roll_future(dt, now)
    except ValueError:
        pass

    low = s.lower().replace("ё", "е")

    # Дату вырезаем из строки ДО разбора времени: «12.08 в 11:00» иначе прочтётся как
    # 12 часов 8 минут (точка — разделитель и там, и там).
    date_at = None                            # (месяц, день, год|None)
    m = re.search(r"(?<![\d:])(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?(?![\d])", low)
    # 13-м месяца не бывает: «16.30 завтра» — это время, а не дата.
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31 \
            and not re.fullmatch(r"\d{1,2}[:.]\d{2}", low.strip()):
        y = int(m.group(3)) if m.group(3) else None
        date_at = (int(m.group(2)), int(m.group(1)), y + 2000 if y and y < 100 else y)
        low = (low[:m.start()] + " " + low[m.end():])
    else:
        m = re.search(r"(\d{1,2})\s+([а-я]+)", low)   # «12 августа», «5 сентября»
        for stem, mo in (_MONTHS.items() if m else ()):
            if m.group(2).startswith(stem):
                date_at = (mo, int(m.group(1)), None)
                low = (low[:m.start()] + " " + low[m.end():])
                break

    tm = _hhmm(low)
    hh, mm = tm if tm else (11, 0)          # время не названо — рабочее утро по умолчанию

    def at(day: datetime) -> datetime:
        return day.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if date_at:
        mo, d, y = date_at
        try:
            return _roll_future(at(now.replace(year=y or now.year, month=mo, day=d)), now)
        except ValueError:                   # 31 февраля и прочая ерунда от модели
            return None

    # 4. Относительные дни.
    if "послезавтра" in low:
        return at(now + timedelta(days=2))
    if "завтра" in low:
        return at(now + timedelta(days=1))
    if "сегодня" in low:
        return at(now)

    # 5. День недели: ближайший будущий (сегодняшний день недели — значит через неделю).
    for word, idx in _WEEKDAYS.items():
        if re.search(rf"\b{word}\b", low):
            ahead = (idx - now.weekday()) % 7
            if ahead == 0:
                cand = at(now)
                return cand if cand > now else at(now + timedelta(days=7))
            return at(now + timedelta(days=ahead))

    # 6. Только время («в 11:00») — ближайшее будущее.
    if tm:
        cand = at(now)
        return cand if cand > now else at(now + timedelta(days=1))
    return None


def _roll_future(dt: datetime, now: datetime) -> datetime:
    """Встречу не назначаем в прошлое: частая причина — агент указал прошлый год."""
    if dt >= now:
        return dt
    try:
        rolled = dt.replace(year=now.year)
    except ValueError:                       # 29 февраля
        return dt
    if rolled < now:
        try:
            rolled = rolled.replace(year=now.year + 1)
        except ValueError:
            return dt
    return rolled
