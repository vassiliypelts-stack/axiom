"""Импорт твоей базы риелторов из CSV в книжку.

CSV-колонки: phone, username, name, city, agency, tags, notes
Запуск:  python -m importer.import_contacts data/contacts_example.csv
"""
from __future__ import annotations

import csv
import sys

from db import database


def normalize_phone(raw: str) -> str | None:
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit() or ch == "+")
    return digits or None


def normalize_username(raw: str) -> str | None:
    if not raw:
        return None
    return raw.strip().lstrip("@") or None


def import_csv(path: str) -> int:
    database.init_db()
    added = 0
    with database.get_conn() as conn, open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            database.upsert_contact(
                conn,
                source="import",
                phone=normalize_phone(row.get("phone", "")),
                username=normalize_username(row.get("username", "")),
                name=(row.get("name") or "").strip() or None,
                city=(row.get("city") or "").strip() or None,
                agency=(row.get("agency") or "").strip() or None,
                tags=(row.get("tags") or "").strip() or None,
                notes=(row.get("notes") or "").strip() or None,
            )
            added += 1
    return added


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "data/contacts_example.csv"
    n = import_csv(csv_path)
    print(f"Импортировано/обновлено строк: {n} из {csv_path}")
