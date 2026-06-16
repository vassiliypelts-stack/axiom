"""Импорт выгрузки из 2GIS (агентства недвижимости) в книжку.

Формат файла: CSV, кодировка windows-1251, разделитель ';'.
Бонус: раз в строке есть ссылка wa.me/ или t.me/ — канал точно есть,
поэтому сразу проставляем has_wa/has_tg = 'yes' (бесплатный чекер, без запросов).

Запуск:  python -m importer.import_2gis data/agentstva_sochi.csv
"""
from __future__ import annotations

import csv
import re
import sys

from db import database

# Индексы колонок 2GIS-выгрузки (0-based), структура фиксированная.
COL = {
    "company": 0, "category": 2, "address": 3,
    "district": 7, "city": 8,
    "phone": 16, "phone2": 17, "email": 19, "web": 20,
    "tg_channel": 21, "vk": 24,
    "wa1": 25, "wa2": 26, "tg1": 29, "tg2": 30,
}


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def norm_phone(raw: str) -> str | None:
    """Нормализует телефон к +7XXXXXXXXXX. Принимает и сырой номер, и ссылку."""
    d = digits(raw)
    if len(d) == 11 and d[0] in "78":
        return "+7" + d[1:]
    if len(d) == 10:
        return "+7" + d
    return None


def phone_from_link(link: str) -> str | None:
    """Достаёт номер из wa.me/79..., t.me/+79..., chat/?number=79..."""
    if not link:
        return None
    m = re.search(r"(?:wa\.me/|number=|t\.me/\+)(\d{10,15})", link)
    return norm_phone(m.group(1)) if m else None


def tg_username(*links: str) -> str | None:
    """Достаёт @username из t.me/handle (не из t.me/+phone и не из ботов)."""
    for link in links:
        if not link:
            continue
        m = re.search(r"t\.me/([A-Za-z][\w]{3,})", link)
        if m and not m.group(1).endswith("_bot"):
            return m.group(1)
    return None


def import_csv(path: str) -> int:
    database.init_db()
    added = 0
    with database.get_conn() as conn, open(path, encoding="cp1251", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        next(reader, None)  # пропустить заголовок
        for row in reader:
            if len(row) <= COL["tg2"] or not row[COL["company"]].strip():
                continue

            def g(key: str) -> str:
                return row[COL[key]].strip()

            phone = norm_phone(g("phone")) or norm_phone(g("phone2")) \
                or phone_from_link(g("wa1")) or phone_from_link(g("tg1"))
            wa_phone = phone_from_link(g("wa1")) or phone_from_link(g("wa2"))
            username = tg_username(g("tg1"), g("tg2"), g("tg_channel"))

            has_wa = "yes" if (g("wa1") or g("wa2")) else "unknown"
            has_tg = "yes" if (g("tg1") or g("tg2") or "t.me/" in g("tg_channel")) else "unknown"
            preferred = "telegram" if has_tg == "yes" else ("whatsapp" if has_wa == "yes" else "telegram")

            notes = " | ".join(p for p in [g("email"), g("web"), g("vk"), g("address")] if p)

            cid = database.upsert_contact(
                conn,
                source="2gis",
                phone=phone,
                username=username,
                name=g("company"),
                city=g("city") or None,
                agency=g("company"),
                tags=g("category") or None,
                notes=notes or None,
            )
            # проставляем канал-флаги и wa_phone из ссылок (бесплатный чекер)
            conn.execute(
                "UPDATE contacts SET wa_phone=COALESCE(?,wa_phone), has_wa=?, has_tg=?, "
                "preferred_channel=?, checked_at=datetime('now') WHERE id=?",
                (wa_phone, has_wa, has_tg, preferred, cid),
            )
            added += 1
    return added


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "data/agentstva_sochi.csv"
    n = import_csv(csv_path)
    print(f"Импортировано из 2GIS: {n} агентств из {csv_path}")
