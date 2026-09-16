"""Чтение Google-таблицы «Контент-завод» (текстовые посты Threads/VK/Telegram).

Источник данных и вся логика генерации/публикации живут в отдельном проекте
(Kontent-zavod-traffic-machine/autopost). Этот модуль только читает ту же
таблицу по её sheet_id — Axiom ничего туда не пишет.

Метрики (просмотры/лайки/ответы) сейчас собираются только для Threads —
у VK и Telegram нет автосборщика, поэтому их посты показываются без цифр.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# индексы колонок листа ОЧЕРЕДЬ (с 0)
Q_ID, Q_DATE, Q_PLATFORMS, Q_TYPE, Q_TEXT, Q_LEN, Q_STATUS, Q_LINK_THREADS, Q_LINK_VK, Q_LINK_TG = range(10)
# индексы колонок листа МЕТРИКИ (с 0)
M_DATE, M_PLATFORM, M_ID, M_TYPE, M_FIRSTLINE, M_VIEWS, M_LIKES, M_REPLIES, M_REPOSTS, M_QUOTES, M_RATE = range(11)
M_LINK = 15


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


def threads_posts(rows: list[list[str]]) -> list[dict]:
    out = []
    for r in rows[1:]:
        if len(r) <= M_PLATFORM or r[M_PLATFORM] != "Threads":
            continue
        r = r + [""] * (16 - len(r))
        out.append({
            "date": r[M_DATE],
            "first_line": r[M_FIRSTLINE] or "(без текста)",
            "views": _to_int(r[M_VIEWS]),
            "likes": _to_int(r[M_LIKES]),
            "replies": _to_int(r[M_REPLIES]),
            "reposts": _to_int(r[M_REPOSTS]),
            "rate": _to_float(r[M_RATE]),
            "link": r[M_LINK],
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
            "type": r[Q_TYPE],
            "first_line": _first_line(r[Q_TEXT]),
            "link": link,
        })
    return out


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

    return {
        "today": {"posts": len(today), "views": total(today, "views"), "replies": total(today, "replies")},
        "week": {"posts": len(week), "views": week_views, "replies": week_replies, "rate": rate},
        "month": {"posts": len(month), "views": total(month, "views")},
        "best_week": best,
        "pending": pending,
        "threads_posts": threads[:20],
        "vk_posts": platform_queue_posts(queue_rows, "vk", Q_LINK_VK),
        "tg_posts": platform_queue_posts(queue_rows, "tg", Q_LINK_TG),
        "weeks": weeks[-8:],
    }
