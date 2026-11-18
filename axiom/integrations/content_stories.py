"""Раздел «Сторис контент завод»: что ушло в сторис и сколько набрало.

Данные готовит контент-завод (Kontent-zavod-traffic-machine/autopost) в той же
Google-таблице, лист СТОРИС:
  строку пишет пульт @vassiliycontent_bot по кнопке «Отправить»;
  цифры дописывает autopost/stories.py stats каждые 2 часа первые 30 часов
  (Instagram отдаёт статистику сторис только сутки).

Axiom только читает.
"""
from __future__ import annotations

import re

import gspread

from integrations import content_sheet

SHEET = "СТОРИС"
NETS = (("ВК", "vk"), ("ТГ", "tg"), ("ИГ", "ig"), ("ФБ", "fb"))


def _num(raw) -> int:
    m = re.search(r"\d+", str(raw or "").replace(" ", "").replace(" ", ""))
    return int(m.group()) if m else 0


def summary() -> dict:
    try:
        rows = content_sheet._rows(SHEET)
    except gspread.exceptions.WorksheetNotFound:
        rows = []
    header = rows[0] if rows else []
    stories, updated = [], ""
    for r in rows[1:]:
        if not any(r):
            continue
        d = dict(zip(header, r + [""] * (len(header) - len(r))))
        per = {key: {"views": _num(d.get(f"{name} просмотры")),
                     "reactions": _num(d.get(f"{name} реакции") or d.get(f"{name} лайки"))}
               for name, key in NETS}
        stories.append({
            "id": d.get("ID", ""), "date": d.get("Создано", ""), "hook": d.get("Хук", ""),
            "target": d.get("Ведём", ""), "nets": d.get("Сети", ""),
            "per": per,
            "views": _num(d.get("Всего просмотров")),
            "reactions": sum(p["reactions"] for p in per.values()),
            "clicks": _num(d.get("ВК переходы")),
            "replies": _num(d.get("ВК ответы")) + _num(d.get("ИГ ответы")),
            "forwards": _num(d.get("ТГ пересылки")),
            "updated": d.get("Обновлено", ""),
        })
        updated = max(updated, d.get("Обновлено", ""))
    stories.sort(key=lambda s: s["date"], reverse=True)
    return {"stories": stories, "updated": updated}
