"""Импорт выгрузки из Parser2GIS (https://github.com/interlark/parser-2gis) в книжку.

В отличие от import_2gis.py (старый инструмент, фиксированные индексы колонок),
Parser2GIS пишет CSV с именованными заголовками — читаем по имени, а не по номеру,
это переживает смену версии парсера и порядка колонок.

Формат файла: CSV, кодировка utf-8-sig (дефолт Parser2GIS), разделитель ','.
Колонки (см. вики проекта, "CSV и XLSX таблицы"): Наименование, Описание, Рубрики,
Адрес, Город, Телефон 1..N, E-mail 1..N, Веб-сайт 1..N, ВКонтакте 1..N,
WhatsApp 1..N, Telegram 1..N, 2GIS URL, и т.д.

Получить сам CSV:
  1) pip install parser-2gis
  2) parser-2gis -i "https://2gis.ru/<город>/search/<категория>" -o data/out.csv -f csv
     (можно перечислить несколько -i ссылок — по городам и/или категориям сразу)
  3) python -m importer.import_parser2gis data/out.csv

Запуск:  python -m importer.import_parser2gis data/out.csv
"""
from __future__ import annotations

import csv
import re
import sys

from db import database

MAX_COLUMNS_PER_ENTITY = 10  # с запасом; реально их обычно 1-3, лишние столбцы просто отсутствуют


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


def _numbered(row: dict[str, str], prefix: str) -> list[str]:
    """Собирает значения колонок 'Prefix N'. Если в выгрузке у поля всегда было
    не больше одного значения, Parser2GIS схлопывает колонку в 'Prefix' без номера
    (remove_empty_columns) — проверяем оба варианта."""
    values = []
    bare = (row.get(prefix) or "").strip()
    if bare:
        values.append(bare)
    for n in range(1, MAX_COLUMNS_PER_ENTITY + 1):
        v = (row.get(f"{prefix} {n}") or "").strip()
        if v:
            values.append(v)
    return values


def import_csv(path: str) -> int:
    database.init_db()
    added = 0
    with database.get_conn() as conn, open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Наименование") or "").strip()
            if not name:
                continue

            phones = _numbered(row, "Телефон")
            emails = _numbered(row, "E-mail")
            sites = _numbered(row, "Веб-сайт")
            vk = _numbered(row, "ВКонтакте")
            wa_links = _numbered(row, "WhatsApp")
            tg_links = _numbered(row, "Telegram")

            phone = (norm_phone(phones[0]) if phones else None) \
                or (phone_from_link(wa_links[0]) if wa_links else None) \
                or (phone_from_link(tg_links[0]) if tg_links else None)
            wa_phone = phone_from_link(wa_links[0]) if wa_links else None
            username = tg_username(*tg_links)

            has_wa = "yes" if wa_links else "unknown"
            has_tg = "yes" if (tg_links or username) else "unknown"
            preferred = "telegram" if has_tg == "yes" else ("whatsapp" if has_wa == "yes" else "telegram")

            city = (row.get("Город") or "").strip() or None
            category = (row.get("Рубрики") or "").strip() or None
            address = (row.get("Адрес") or "").strip()
            url = (row.get("2GIS URL") or "").strip()

            notes_parts = [emails[0] if emails else "", sites[0] if sites else "", vk[0] if vk else "", address]
            notes = " | ".join(p for p in notes_parts if p)

            cid = database.upsert_contact(
                conn,
                source="parser2gis",
                phone=phone,
                username=username,
                name=name,
                city=city,
                agency=name,
                tags=category,
                notes=notes or None,
                email=emails[0] if emails else None,
                site=url or (sites[0] if sites else None),
            )
            conn.execute(
                "UPDATE contacts SET wa_phone=COALESCE(?,wa_phone), has_wa=?, has_tg=?, "
                "preferred_channel=?, checked_at=datetime('now') WHERE id=?",
                (wa_phone, has_wa, has_tg, preferred, cid),
            )
            added += 1
    return added


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "data/parser2gis_out.csv"
    n = import_csv(csv_path)
    print(f"Импортировано из Parser2GIS: {n} записей из {csv_path}")
