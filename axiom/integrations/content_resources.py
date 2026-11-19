"""Раздел «Мои ресурсы»: ссылки на все страницы Василия в одном месте.

Список живёт в Google-таблице контент-завода, лист РЕСУРСЫ
(площадка, название, ссылка, заметка, добавлен). Axiom его показывает и пишет
туда только по явному действию человека: добавить или убрать ссылку.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

import gspread

from integrations import content_sheet
from integrations.content_sheet import _book, _forget

SHEET = "РЕСУРСЫ"
HEADER = ["площадка", "название", "ссылка", "заметка", "добавлен"]

# Площадка по адресу: чтобы не выбирать её руками при добавлении.
HOSTS = [("threads.", "threads"), ("instagram.", "instagram"), ("vk.com", "vk"), ("vk.ru", "vk"),
         ("t.me", "telegram"), ("telegram.", "telegram"), ("dzen.ru", "dzen"),
         ("youtube.", "youtube"), ("youtu.be", "youtube"), ("max.ru", "max"),
         ("wa.me", "whatsapp"), ("whatsapp.", "whatsapp"), ("facebook.", "facebook"),
         ("tiktok.", "tiktok"), ("tenchat.", "tenchat"), ("vc.ru", "vc")]


def _ws():
    book = _book()
    try:
        return book.worksheet(SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = book.add_worksheet(title=SHEET, rows=100, cols=len(HEADER))
        ws.append_row(HEADER)
        return ws


def platform_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return next((p for key, p in HOSTS if key in host), "другое")


def resources() -> dict:
    try:
        rows = content_sheet._rows(SHEET)
    except gspread.exceptions.WorksheetNotFound:
        rows = []
    header = rows[0] if rows else HEADER
    out = []
    for i, r in enumerate(rows[1:], start=2):
        d = dict(zip(header, r + [""] * (len(header) - len(r))))
        if d.get("ссылка", "").strip():
            out.append({"row": i, "platform": d.get("площадка", "").strip() or "другое",
                        "title": d.get("название", "").strip() or d["ссылка"].strip(),
                        "url": d["ссылка"].strip(), "note": d.get("заметка", "").strip(),
                        "added": d.get("добавлен", "").strip()})
    return {"resources": out}


def add(url: str, title: str = "", note: str = "") -> None:
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url.lstrip("/")
    if not urlparse(url).netloc or "." not in urlparse(url).netloc:
        raise ValueError("это не похоже на ссылку")
    ws = _ws()
    _forget(SHEET)
    for r in ws.get_all_values()[1:]:
        if len(r) > 2 and r[2].strip().rstrip("/").lower() == url.rstrip("/").lower():
            raise ValueError("такая ссылка уже есть")
    ws.append_row([platform_of(url), (title or "").strip()[:80] or urlparse(url).netloc,
                   url, (note or "").strip()[:200], datetime.now().strftime("%Y-%m-%d")],
                  value_input_option="RAW")


def remove(url: str) -> None:
    ws = _ws()
    _forget(SHEET)
    for i, r in enumerate(ws.get_all_values()[1:], start=2):
        if len(r) > 2 and r[2].strip() == (url or "").strip():
            ws.delete_rows(i)
            return
    raise ValueError("такой ссылки нет")
