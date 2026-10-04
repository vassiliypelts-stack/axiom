"""Тренды доноров для вкладки «Видео контент завод → Тренды доноров».

Данные готовит парсер контент-завода (Kontent-zavod-traffic-machine/autopost/trendy.py)
на компьютере Василия и пишет в ту же Google-таблицу:
  ТРЕНДЫ  — залетевшие посты доноров (всплеск к норме автора, угол, обложка);
  ДОНОРЫ  — за кем следит парсер (общий список с локальным пультом);
  ПРОГОНЫ — по строке на каждый запуск парсера.

Axiom пишет в таблицу только явные действия человека в интерфейсе: оценку темы
с комментарием и добавление/паузу донора.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import config
from integrations.content_sheet import ContentSheetError, _book

RATINGS = ("да", "потом", "нет", "")
DONOR_HEADER = ["площадка", "имя", "ссылка", "статус", "добавлен", "заметка"]
# Обложки отдаём только с CDN площадок, чтобы прокси не стал открытым ретранслятором.
THUMB_HOSTS = ("ytimg.com", "cdninstagram.com", "fbcdn.net", "instagram.com", "userapi.com",
               "vkuservideo.net", "tiktokcdn.com", "ggpht.com")
THUMBS = Path(config.BASE_DIR) / "data" / "trend_thumbs"


def _rows(name: str) -> list[list[str]]:
    try:
        return _book().worksheet(name).get_all_values()
    except Exception as e:
        raise ContentSheetError(f"Не удалось прочитать лист {name}: {e}") from e


def trends() -> dict:
    rows = _rows("ТРЕНДЫ")
    header, out = rows[0] if rows else [], []
    for r in rows[1:]:
        if not any(r):
            continue
        item = dict(zip(header, r + [""] * (len(header) - len(r))))
        out.append(item)
    return {"items": out, "runs": runs()}


def runs(limit: int = 5) -> list[dict]:
    try:
        rows = _book().worksheet("ПРОГОНЫ").get_all_values()
    except Exception:
        return []
    header = rows[0] if rows else []
    return [dict(zip(header, r)) for r in rows[1:]][-limit:][::-1]


def rate(link: str, rating: str, comment: str | None) -> None:
    """Оценка темы — только по нажатию кнопки в пульте."""
    if rating not in RATINGS:
        raise ValueError("оценка: да / потом / нет")
    ws = _book().worksheet("ТРЕНДЫ")
    header = ws.row_values(1)
    try:
        c_link, c_rate, c_note = header.index("ссылка") + 1, header.index("оценка") + 1, header.index("комментарий") + 1
    except ValueError:
        raise ContentSheetError("В листе ТРЕНДЫ нет колонок «оценка»/«комментарий» — запустите свежий парсер.")
    links = ws.col_values(c_link)
    if link not in links[1:]:
        raise ValueError("тема не найдена в листе ТРЕНДЫ")
    row = links.index(link) + 1
    ws.update_cell(row, c_rate, rating)
    if comment is not None:
        ws.update_cell(row, c_note, comment[:500])


# ---------- доноры ----------

def _donor_ws():
    book = _book()
    try:
        return book.worksheet("ДОНОРЫ")
    except Exception:
        ws = book.add_worksheet(title="ДОНОРЫ", rows=500, cols=len(DONOR_HEADER))
        ws.append_row(DONOR_HEADER)
        return ws


def donors() -> dict:
    rows = _donor_ws().get_all_values()
    out = []
    for r in rows[1:]:
        r = r + [""] * (len(DONOR_HEADER) - len(r))
        if r[2].strip():
            out.append({"platform": r[0].strip().lower(), "name": r[1].strip() or r[2].strip(), "url": r[2].strip(),
                        "status": r[3].strip() or "active", "added": r[4].strip(), "note": r[5].strip()})
    return {"donors": out}


def _normalize(raw: str, platform: str = "") -> tuple[str, str, str]:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("пустая ссылка")
    if not raw.startswith("http"):
        handle = raw.lstrip("@").strip("/")
        raw = {"youtube": f"https://youtube.com/@{handle}", "instagram": f"https://www.instagram.com/{handle}/",
               "tiktok": f"https://www.tiktok.com/@{handle}"}.get(platform, raw)
    host = urlparse(raw).netloc.lower()
    parts = [p for p in urlparse(raw).path.split("/") if p]
    if "youtube.com" in host or "youtu.be" in host:
        handle = next((p for p in parts if p.startswith("@")), "")
        if not handle:
            raise ValueError("нужна ссылка на канал вида youtube.com/@имя")
        return "youtube", f"https://youtube.com/{handle}", handle.lstrip("@")
    if "instagram.com" in host:
        if not parts or parts[0] in ("p", "reel", "reels", "stories"):
            raise ValueError("нужна ссылка на профиль, не на пост")
        return "instagram", f"https://www.instagram.com/{parts[0]}/", parts[0]
    if "tiktok.com" in host:
        handle = next((p for p in parts if p.startswith("@")), "")
        if not handle:
            raise ValueError("нужна ссылка на профиль вида tiktok.com/@имя")
        return "tiktok", f"https://www.tiktok.com/{handle}", handle.lstrip("@")
    raise ValueError("поддерживаются YouTube и Instagram")


def add_donor(raw: str, platform: str = "", note: str = "") -> None:
    platform, url, name = _normalize(raw, platform)
    ws = _donor_ws()
    for row, r in enumerate(ws.get_all_values()[1:], start=2):
        if len(r) > 2 and r[2].strip().rstrip("/").lower() == url.rstrip("/").lower():
            if (r[3].strip() if len(r) > 3 else "active") in ("", "active"):
                raise ValueError("этот донор уже есть")
            ws.update_cell(row, 4, "active")
            return
    ws.append_row([platform, "@" + name, url, "active", datetime.now().strftime("%Y-%m-%d"), (note or "")[:200]],
                  value_input_option="RAW")


def set_donor_status(url: str, status: str) -> None:
    if status not in ("active", "paused"):
        raise ValueError("статус: active или paused")
    ws = _donor_ws()
    for row, r in enumerate(ws.get_all_values()[1:], start=2):
        if len(r) > 2 and r[2].strip() == url:
            ws.update_cell(row, 4, status)
            return
    raise ValueError("донор не найден")


# ---------- обложки ----------

def thumb(url: str) -> bytes | None:
    """Обложка с локальным кэшем: ссылки Instagram протухают за несколько дней."""
    host = urlparse(url).netloc.lower()
    if not any(host == h or host.endswith("." + h) for h in THUMB_HOSTS):
        return None
    path = THUMBS / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".jpg")
    if path.exists():
        return path.read_bytes()
    for candidate in (url, "https://wsrv.nl/?url=" + quote(url, safe="")):
        try:
            with urlopen(Request(candidate, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as r:
                if not r.headers.get("Content-Type", "").startswith("image"):
                    continue
                data = r.read()
            THUMBS.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return data
        except Exception:
            continue
    return None
