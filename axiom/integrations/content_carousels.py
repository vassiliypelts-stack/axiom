"""Раздел «Карусели контент завод»: свои аккаунты Instagram и карусели доноров.

Данные готовит контент-завод (Kontent-zavod-traffic-machine/autopost) в той же
Google-таблице:
  АККАУНТЫ  — свои аккаунты Instagram (колонки как у листа ДОНОРЫ);
  ИНСТАГРАМ — по строке на пост своего аккаунта, пишет autopost/carousels.py;
  ТРЕНДЫ    — залетевшие посты доноров, отсюда берём формат «карусель».

Цифры собираются снаружи через Apify: лайки и комментарии есть, охвата,
сохранений и репостов нет. Норма и всплеск считаются здесь при показе.

Axiom пишет в таблицу только явные действия человека: добавить аккаунт или
поставить его на паузу.
"""
from __future__ import annotations

import re
from datetime import datetime
from statistics import median

import gspread

from integrations import content_sheet, content_trends
from integrations.content_sheet import _book, _forget

ACCOUNTS, POSTS = "АККАУНТЫ", "ИНСТАГРАМ"
ACCOUNT_HEADER = content_trends.DONOR_HEADER
SPIKE = 3.0       # «залетела»: во столько раз выше нормы аккаунта, порог как у доноров
MIN_POSTS = 5     # меньше постов одного формата — норма не считается


def _num(raw) -> float:
    """«4,2×», «1 234», «» → число; мусор и пусто → 0."""
    m = re.search(r"\d+(?:\.\d+)?", str(raw or "").replace(" ", "").replace(" ", "").replace(",", "."))
    return float(m.group()) if m else 0.0


def _table(name: str) -> list[dict]:
    """Лист словарями по шапке; листа ещё нет (сборщик не запускался) — пусто."""
    try:
        rows = content_sheet._rows(name)
    except gspread.exceptions.WorksheetNotFound:
        return []
    header = rows[0] if rows else []
    return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:] if any(r)]


# ---------- свои аккаунты ----------

def _account_ws():
    book = _book()
    try:
        return book.worksheet(ACCOUNTS)
    except gspread.exceptions.WorksheetNotFound:
        ws = book.add_worksheet(title=ACCOUNTS, rows=200, cols=len(ACCOUNT_HEADER))
        ws.append_row(ACCOUNT_HEADER)
        return ws


def accounts() -> dict:
    out = [{"platform": r.get("площадка", "").strip().lower(), "name": r.get("имя", "").strip() or r.get("ссылка", "").strip(),
            "url": r.get("ссылка", "").strip(), "status": r.get("статус", "").strip() or "active",
            "added": r.get("добавлен", "").strip(), "note": r.get("заметка", "").strip()}
           for r in _table(ACCOUNTS) if r.get("ссылка", "").strip()]
    return {"accounts": out}


def add_account(raw: str, note: str = "") -> None:
    platform, url, name = content_trends._normalize(raw, "instagram")
    if platform != "instagram":
        raise ValueError("пока только аккаунты Instagram")
    ws = _account_ws()
    _forget(ACCOUNTS)
    for row, r in enumerate(ws.get_all_values()[1:], start=2):
        if len(r) > 2 and r[2].strip().rstrip("/").lower() == url.rstrip("/").lower():
            if (r[3].strip() if len(r) > 3 else "active") in ("", "active"):
                raise ValueError("этот аккаунт уже есть")
            ws.update_cell(row, 4, "active")
            return
    ws.append_row([platform, "@" + name, url, "active", datetime.now().strftime("%Y-%m-%d"), (note or "")[:200]],
                  value_input_option="RAW")


def set_account_status(url: str, status: str) -> None:
    if status not in ("active", "paused"):
        raise ValueError("статус: active или paused")
    ws = _account_ws()
    _forget(ACCOUNTS)
    for row, r in enumerate(ws.get_all_values()[1:], start=2):
        if len(r) > 2 and r[2].strip() == url:
            ws.update_cell(row, 4, status)
            return
    raise ValueError("аккаунт не найден")


# ---------- сводка по своим постам ----------

def _metric(fmt: str, likes: float, comments: float, views: float) -> float:
    """Отклик поста. У каруселей и фото просмотров снаружи не видно: лайки + 3 × комментарии."""
    return views if fmt == "видео" and views else likes + 3 * comments


def summary() -> dict:
    posts = []
    for r in _table(POSTS):
        fmt = r.get("формат", "").strip() or "пост"
        likes, comments, views = _num(r.get("лайки")), _num(r.get("комменты")), _num(r.get("просмотры"))
        posts.append({"account": r.get("аккаунт", "").strip(), "date": r.get("дата", "").strip(), "link": r.get("ссылка", "").strip(),
                      "format": fmt, "topic": r.get("тема", "").strip(), "slides": int(_num(r.get("слайдов"))),
                      "likes": int(likes), "comments": int(comments), "views": int(views),
                      "reach": r.get("охват", "").strip(), "saves": r.get("сохранения", "").strip(), "shares": r.get("репосты", "").strip(),
                      "image": r.get("картинка", "").strip(), "updated": r.get("обновлено", "").strip(),
                      "metric": int(_metric(fmt, likes, comments, views))})
    # Норма у аккаунта своя для каждого формата: у ролика просмотры, у карусели отклик.
    groups: dict[tuple[str, str], list[int]] = {}
    for p in posts:
        groups.setdefault((p["account"].lower(), p["format"]), []).append(p["metric"])
    for p in posts:
        values = groups[(p["account"].lower(), p["format"])]
        norm = median(values) if len(values) >= MIN_POSTS else 0
        p["norm"] = round(norm, 1)
        p["spike"] = round(p["metric"] / norm, 1) if norm else None   # None — «мало данных»
        p["hit"] = bool(p["spike"] and p["spike"] >= SPIKE)
    return {"accounts": accounts()["accounts"], "posts": posts, "spike": SPIKE, "min_posts": MIN_POSTS,
            "updated": max((p["updated"] for p in posts), default="")}


# ---------- источники: карусели доноров ----------

def sources() -> dict:
    """Залетевшие карусели доноров. В листе только залёты — всю ленту донора парсер не хранит."""
    items = []
    for p in content_trends.trends()["items"]:
        if p.get("площадка") != "instagram" or p.get("формат") != "карусель":
            continue
        items.append({"author": p.get("автор", ""), "date": p.get("дата", ""), "topic": p.get("тема", ""),
                      "spike": round(_num(p.get("всплеск")), 1), "likes": int(_num(p.get("реакции"))),
                      "comments": int(_num(p.get("комменты"))), "norm": int(_num(p.get("норма"))),
                      "pain": p.get("боль", ""), "angle": p.get("угол", ""), "link": p.get("ссылка", ""),
                      "rating": p.get("оценка", ""), "comment": p.get("комментарий", ""), "image": p.get("картинка", "")})
    donors = [d for d in content_trends.donors()["donors"] if d["platform"] == "instagram"]
    return {"items": items, "donors": donors}
