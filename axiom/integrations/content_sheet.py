"""Чтение Google-таблицы «Контент-завод» (текстовые посты Threads/VK/Telegram).

Источник данных и вся логика генерации/публикации живут в отдельном проекте
(Kontent-zavod-traffic-machine/autopost). Этот модуль только читает ту же
таблицу по её sheet_id — Axiom ничего туда не пишет.

Метрики (просмотры/лайки/ответы) сейчас собираются только для Threads —
у VK и Telegram нет автосборщика, поэтому их посты показываются без цифр.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

import gspread
from google.oauth2.service_account import Credentials

import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# индексы колонок листа ОЧЕРЕДЬ (с 0)
Q_ID, Q_DATE, Q_PLATFORMS, Q_TYPE, Q_TEXT, Q_LEN, Q_STATUS, Q_LINK_THREADS, Q_LINK_VK, Q_LINK_TG = range(10)
# индексы колонок листа МЕТРИКИ (с 0)
M_DATE, M_PLATFORM, M_ID, M_TYPE, M_FIRSTLINE, M_VIEWS, M_LIKES, M_REPLIES, M_REPOSTS, M_QUOTES, M_RATE = range(11)
# 11-13 — «Написали в личку», «Аудиты», «Продажи»; ссылка идёт следом
M_LINK = 14


class ContentSheetError(RuntimeError):
    pass


def _book():
    if not config.CONTENT_SHEET_ID:
        raise ContentSheetError("CONTENT_SHEET_ID не задан в .env — не знаю, какую таблицу читать.")
    creds = Credentials.from_service_account_file(config.CONTENT_GOOGLE_KEY_FILE, scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(config.CONTENT_SHEET_ID)


def _first_line(text: str, limit: int = 110) -> str:
    return (text or "").split("\n")[0][:limit].strip()


def _to_float(raw: str) -> float:
    try:
        return float((raw or "0").replace(",", "."))
    except ValueError:
        return 0.0


def _to_int(raw: str) -> int:
    try:
        return int(float((raw or "0").replace(",", ".")))
    except ValueError:
        return 0


def _abs_link(raw: str) -> str:
    """Телеграм в таблице лежит как «@channel/52» — без схемы такая ссылка
    считается относительной и ведёт на сам пульт, а не на пост."""
    link = (raw or "").strip()
    if not link or link.startswith(("http://", "https://")):
        return link
    if link.startswith("@"):
        return f"https://t.me/{link[1:]}"
    if link.startswith("t.me/"):
        return f"https://{link}"
    return link


def threads_posts(rows: list[list[str]]) -> list[dict]:
    out = []
    for r in rows[1:]:
        if len(r) <= M_PLATFORM or r[M_PLATFORM] != "Threads":
            continue
        r = r + [""] * (16 - len(r))
        out.append({
            "platform": "threads",
            "date": r[M_DATE],
            "first_line": r[M_FIRSTLINE] or "(без текста)",
            "views": _to_int(r[M_VIEWS]),
            "likes": _to_int(r[M_LIKES]),
            "replies": _to_int(r[M_REPLIES]),
            "reposts": _to_int(r[M_REPOSTS]),
            "rate": _to_float(r[M_RATE]),
            "link": _abs_link(r[M_LINK]),
        })
    out.sort(key=lambda p: p["date"], reverse=True)
    return out


def platform_queue_posts(rows: list[list[str]], platform: str, link_col: int) -> list[dict]:
    """Опубликованные посты ОЧЕРЕДИ для платформ без автосбора метрик (VK, TG)."""
    out = []
    for r in rows[1:]:
        if len(r) <= max(Q_PLATFORMS, link_col):
            continue
        platforms = (r[Q_PLATFORMS] or "").lower()
        link = r[link_col].strip() if len(r) > link_col else ""
        if platform not in platforms or not link:
            continue
        out.append({
            "platform": platform,
            "date": (r[Q_DATE] or "").strip()[:10],
            "type": r[Q_TYPE],
            "first_line": _first_line(r[Q_TEXT]),
            "link": _abs_link(link),
        })
    out.sort(key=lambda p: p["date"], reverse=True)
    return out


def _daily(posts: list[dict], now: datetime, days: int = 30) -> list[dict]:
    """Просмотры/посты по дням — для переключателя периода в динамике."""
    buckets: dict[str, dict] = {}
    edge = (now - timedelta(days=days)).date()
    for p in posts:
        try:
            d = datetime.strptime(p["date"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        if d < edge:
            continue
        b = buckets.setdefault(p["date"], {"label": p["date"], "posts": 0, "views": 0, "replies": 0})
        b["posts"] += 1
        b["views"] += p["views"]
        b["replies"] += p["replies"]
    return [buckets[k] for k in sorted(buckets)]


def sources() -> dict:
    """
    Что дайджест нашёл в чужих каналах, и какие темы из этого выросли.

    В бот уходит только верхушка дня и она там теряется. Здесь лежит вся
    накопленная БАЗА-ТЕМ со ссылками на оригиналы — чтобы можно было зайти
    и прочитать то, что зацепило внимание, а не только заголовок из сводки.
    """
    book = _book()

    finds = []
    try:
        rows = book.worksheet("БАЗА-ТЕМ").get_all_values()
    except Exception:
        rows = []
    for r in rows[1:]:
        r = r + [""] * (13 - len(r))
        if not (r[3] or r[4]):
            continue
        finds.append({
            "date": r[1],
            "channel": r[2],
            "title": r[3] or _first_line(r[4]),
            "text": (r[4] or "")[:600],
            "views": _to_int(r[5]),
            "reactions": _to_int(r[6]),
            "forwards": _to_int(r[7]),
            "weight": _to_int(r[8]),
            "link": _abs_link(r[9]),
            "used": bool((r[10] or "").strip()),
            # Какую боль клиента задевает — размечает дайджест (autopost/pains.py).
            # По ней отбираются темы, которые вообще стоит превращать в посты.
            "pain": (r[11] or "").strip(),
            "pain_why": (r[12] or "").strip(),
        })
    finds.sort(key=lambda f: f["date"], reverse=True)

    themes = []
    try:
        rows = book.worksheet("ПЛАН").get_all_values()
    except Exception:
        rows = []
    # Из чего выросла тема: находка по её ссылке. Без куска исходного поста
    # тема — голая формулировка, и не видно, что именно в источнике зацепило.
    finds_by_link = {f["link"]: f for f in finds if f["link"]}

    for r in rows[1:]:
        r = r + [""] * (11 - len(r))
        if not r[1]:
            continue
        status = r[8] or ""
        link = _abs_link(r[10])
        src = finds_by_link.get(link)
        # «из дайджеста 16.09 — <угол>»: угол объясняет, что своего сказать.
        angle = status.split("—", 1)[1].strip() if "—" in status else ""
        themes.append({
            "theme": r[1],
            "type": r[2],
            "status": status,
            "angle": angle,
            "source": r[9] or (src["channel"] if src else ""),
            "link": link,
            "excerpt": (src["text"][:220] if src else ""),
            "from_digest": "дайджест" in status.lower(),
        })

    channels = {}
    for f in finds:
        c = channels.setdefault(f["channel"], {"channel": f["channel"], "count": 0, "views": 0})
        c["count"] += 1
        c["views"] += f["views"]

    pains = {}
    for f in finds:
        if f["pain"]:
            pains[f["pain"]] = pains.get(f["pain"], 0) + 1

    return {
        "finds": finds[:200],
        "themes": themes[-40:],
        "channels": sorted(channels.values(), key=lambda c: c["count"], reverse=True),
        "total_finds": len(finds),
        "pains": sorted(({"pain": k, "count": v} for k, v in pains.items()),
                        key=lambda p: p["count"], reverse=True),
        "total_pains": sum(pains.values()),
    }


def trends() -> dict:
    """Read-only projection of the ТРЕНДЫ worksheet for the dashboard."""
    try:
        rows = _book().worksheet("ТРЕНДЫ").get_all_values()
    except Exception as e:
        raise ContentSheetError(f"Не удалось прочитать лист ТРЕНДЫ: {e}") from e
    out = []
    for r in rows[1:]:
        r = r + [""] * (14 - len(r))
        if not r[0]:
            continue
        out.append({"id": r[0], "date": r[1], "platform": r[2], "format": r[3],
                    "author": r[4], "title": r[5], "spike": _to_float(r[6].replace("×", "")),
                    "views": _to_int(r[7]), "reactions": _to_int(r[8]), "shares": _to_int(r[9]),
                    "pain": r[10], "angle": r[11], "link": _abs_link(r[12]), "used": bool(r[13].strip())})
    return {"trends": sorted(out, key=lambda x: x["spike"], reverse=True)}


def mark_trend_taken(trend_id: str) -> None:
    """The only Trends write, called exclusively after the user's explicit UI action."""
    ws = _book().worksheet("ТРЕНДЫ")
    for row, values in enumerate(ws.get_all_values()[1:], start=2):
        if values and values[0] == str(trend_id):
            ws.update_cell(row, 14, datetime.now().strftime("%Y-%m-%d"))
            return
    raise ContentSheetError("Тренд не найден.")


def _queue_date(raw: str) -> datetime | None:
    """Дата из Sheets: поддерживаем и ISO, и привычную русскую запись."""
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _schedule_slots(raw: str) -> list[tuple[int, int, int]]:
    days = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
    slots = []
    for day, hour, minute in re.findall(r"(пн|вт|ср|чт|пт|сб|вс)\s+(\d{1,2}):(\d{2})", (raw or "").lower()):
        h, m = int(hour), int(minute)
        if h < 24 and m < 60:
            slots.append((days[day], h, m))
    return sorted(set(slots))


def _next_schedule_slots(schedule: str, now: datetime, count: int) -> list[datetime]:
    """Ближайшие слоты строго после текущего момента — только прогноз, не запись."""
    slots = _schedule_slots(schedule)
    result, day = [], now.date()
    while slots and len(result) < count:
        for weekday, hour, minute in slots:
            moment = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
            if day.weekday() == weekday and moment > now:
                result.append(moment)
                if len(result) == count:
                    break
        day += timedelta(days=1)
    return result


def plan(weeks: int = 4) -> dict:
    """Календарь очереди на 1–4 недели. Только читает таблицу."""
    weeks = max(1, min(int(weeks), 4))
    book = _book()
    rows = book.worksheet("ОЧЕРЕДЬ").get_all_values()[1:]
    try:
        settings = {r[0]: r[1] for r in book.worksheet("НАСТРОЙКИ").get_all_values()[1:]
                    if len(r) >= 2 and r[0]}
    except Exception:
        settings = {}

    now = datetime.now().replace(second=0, microsecond=0)
    start = now.date() - timedelta(days=now.weekday())
    edge = start + timedelta(days=weeks * 7)
    prepared = []
    projection_count = 0
    for r in rows:
        r = r + [""] * (11 - len(r))
        if not (r[Q_ID] or r[Q_TEXT]):
            continue
        date = _queue_date(r[Q_DATE])
        status = (r[Q_STATUS] or "").strip().lower()
        # Без даты прогнозируем только ожидающие посты: опубликованные никогда
        # не должны внезапно появляться в будущем календаре.
        if date is None and status in ("ждёт", "ждет", "") and r[Q_TEXT].strip():
            projection_count += 1
        prepared.append((r, date))

    projections = iter(_next_schedule_slots(settings.get("График", ""), now, projection_count))
    posts = []
    for r, date in prepared:
        projected = date is None
        if projected and (r[Q_STATUS] or "").strip().lower() in ("ждёт", "ждет", "") and r[Q_TEXT].strip():
            date = next(projections, None)
        if date is None or not (start <= date.date() < edge):
            continue
        posts.append({"id": r[Q_ID], "date": date.date().isoformat(), "time": date.strftime("%H:%M"),
                      "platforms": r[Q_PLATFORMS], "type": r[Q_TYPE], "text": _first_line(r[Q_TEXT]),
                      "status": r[Q_STATUS], "projected": projected})
    posts.sort(key=lambda p: (p["date"], p["time"], p["id"]))
    dates = [(start + timedelta(days=i)).isoformat() for i in range(weeks * 7)]
    return {"posts": posts, "gaps": [d for d in dates if not any(p["date"] == d for p in posts)],
            "pending": summary()["pending"], "start": start.isoformat(), "weeks": weeks,
            "schedule": settings.get("График", "")}


def add_to_queue(text: str, kind: str = "", platforms: str = "threads,vk,tg",
                 image: str = "") -> dict:
    """
    Дописать пост в лист ОЧЕРЕДЬ со статусом «ждёт».

    Публикатор заберёт его сам по расписанию, поэтому пишем только то, что
    Василий уже одобрил глазами: правка после записи means правка в таблице.
    """
    text = (text or "").strip()
    if not text:
        raise ContentSheetError("Пустой текст — нечего ставить в очередь.")

    ws = _book().worksheet("ОЧЕРЕДЬ")
    rows = ws.get_all_values()

    next_id = 1
    for r in rows[1:]:
        if r and r[0].strip().isdigit():
            next_id = max(next_id, int(r[0].strip()) + 1)

    ws.append_row([str(next_id), "", platforms, kind, text, str(len(text)),
                   "ждёт", "", "", "", image])
    return {"id": next_id, "len": len(text)}


def summary() -> dict:
    book = _book()
    queue_rows = book.worksheet("ОЧЕРЕДЬ").get_all_values()
    metrics_rows = book.worksheet("МЕТРИКИ").get_all_values()
    weeks_rows = book.worksheet("НЕДЕЛИ").get_all_values()

    threads = threads_posts(metrics_rows)

    now = datetime.now(timezone.utc)

    def since(days: int) -> list[dict]:
        edge = (now - timedelta(days=days)).date()
        out = []
        for p in threads:
            try:
                d = datetime.strptime(p["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            if d >= edge:
                out.append(p)
        return out

    today, week, month = since(1), since(7), since(30)

    def total(items, key):
        return sum(i[key] for i in items)

    week_views, week_replies = total(week, "views"), total(week, "replies")
    rate = round(week_replies / week_views * 100, 2) if week_views else 0.0
    best = max(week, key=lambda p: p["replies"], default=None)

    pending = 0
    for r in queue_rows[1:]:
        if len(r) > Q_STATUS:
            status = (r[Q_STATUS] or "").strip().lower()
            if status in ("ждёт", "ждет", "") and (r[Q_TEXT] or "").strip():
                pending += 1

    weeks = []
    for r in weeks_rows[1:]:
        if not r or not r[0]:
            continue
        weeks.append({
            "label": r[0],
            "posts": _to_int(r[1]) if len(r) > 1 else 0,
            "views": _to_int(r[2]) if len(r) > 2 else 0,
            "replies": _to_int(r[4]) if len(r) > 4 else 0,
            "rate": _to_float(r[5]) if len(r) > 5 else 0.0,
        })

    vk_posts = platform_queue_posts(queue_rows, "vk", Q_LINK_VK)
    tg_posts = platform_queue_posts(queue_rows, "tg", Q_LINK_TG)

    # Единая лента для таблицы с фильтрами: у VK/TG метрик нет, поля идут нулями.
    all_posts = threads + [
        {**p, "views": 0, "likes": 0, "replies": 0, "reposts": 0, "rate": 0.0}
        for p in vk_posts + tg_posts
    ]
    all_posts.sort(key=lambda p: p.get("date") or "", reverse=True)

    return {
        "today": {"posts": len(today), "views": total(today, "views"), "replies": total(today, "replies")},
        "week": {"posts": len(week), "views": week_views, "replies": week_replies, "rate": rate},
        "month": {"posts": len(month), "views": total(month, "views")},
        "best_week": best,
        "pending": pending,
        "threads_posts": threads[:20],
        "vk_posts": vk_posts,
        "tg_posts": tg_posts,
        "posts": all_posts,
        "daily": _daily(threads, now),
        "weeks": weeks[-8:],
    }
