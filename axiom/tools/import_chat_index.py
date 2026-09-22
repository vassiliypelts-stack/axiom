"""Импорт тематического индекса Google Sheets в каталог AXIOM.

Индекс XLSX содержит ссылки на тематические Google-таблицы, а не сами чаты.
Скрипт читает их как данные, выгружает публичный CSV каждой таблицы и добавляет
только Telegram @username в `chats`. Он не открывает Telegram, не вступает в чаты
и не запускает исследование — это отдельные, явно управляемые процессы.

Запуск из папки axiom:
    python -m tools.import_chat_index "..\\🎁  16 000 чатов Телеграм.xlsx" --dry-run
    python -m tools.import_chat_index "..\\🎁  16 000 чатов Телеграм.xlsx"
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from db import database

NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
SHEET_RE = re.compile(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)(?:/[^?#]*)?(?:\?[^#]*)?(?:#gid=(\d+))?", re.I)
USERNAME_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{3,31})\b|(?:https?://)?(?:www\.)?t\.me/(?!joinchat/|\+|c/)([A-Za-z][A-Za-z0-9_]{3,31})\b", re.I)
SKIP_NAMES = {"share", "addstickers", "proxy", "iv", "login", "telegram", "s"}


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    value = cell.find("x:v", NS)
    if value is None or value.text is None:
        return ""
    return shared[int(value.text)] if cell.attrib.get("t") == "s" else value.text


def sources_from_xlsx(path: Path) -> list[tuple[str, str]]:
    """Вернуть уникальные пары «тема, ссылка Google Sheets» из индексного XLSX."""
    with zipfile.ZipFile(path) as book:
        shared_xml = ET.fromstring(book.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in item.findall(".//x:t", NS))
                  for item in shared_xml.findall("x:si", NS)]
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in sheet.findall(".//x:sheetData/x:row", NS):
        cells = [_cell_value(c, shared).strip() for c in row.findall("x:c", NS)]
        if len(cells) < 2 or not cells[1]:
            continue
        match = SHEET_RE.search(cells[1])
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        out.append((cells[0] or "Без темы", cells[1]))
    return out


def export_url(source_url: str) -> str:
    match = SHEET_RE.search(source_url)
    if not match:
        raise ValueError("не ссылка Google Sheets")
    gid_match = re.search(r"[?#&]gid=(\d+)", source_url)
    suffix = f"&gid={gid_match.group(1)}" if gid_match else ""
    return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=csv{suffix}"


def download_csv(source_url: str) -> str:
    request = urllib.request.Request(export_url(source_url), headers={"User-Agent": "AXIOM catalog importer/1.0"})
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
    return raw.decode("utf-8-sig", errors="replace")


def usernames_from_csv(text: str) -> set[str]:
    users: set[str] = set()
    for row in csv.reader(io.StringIO(text)):
        for cell in row:
            for match in USERNAME_RE.finditer(cell or ""):
                username = (match.group(1) or match.group(2) or "").lower()
                if username and username not in SKIP_NAMES:
                    users.add(username)
    return users


def run(index_path: Path, dry_run: bool) -> dict:
    sources = sources_from_xlsx(index_path)
    gathered: dict[str, set[str]] = {}
    failed: list[dict[str, str]] = []
    # В индексе почти 50 независимых публичных листов. Ограниченная параллельность
    # делает импорт терпимым при одном зависшем листе, но не превращает его в шквал
    # запросов к Google.
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(download_csv, url): (topic, url) for topic, url in sources}
        for future in as_completed(futures):
            topic, url = futures[future]
            try:
                gathered[topic] = usernames_from_csv(future.result())
            except Exception as exc:  # один недоступный лист не отменяет весь индекс
                failed.append({"topic": topic, "url": url, "error": str(exc)[:180]})

    all_users: dict[str, str] = {}
    duplicates = 0
    # «Вся база» — резервный лист: он не должен перетирать тематическую метку
    # конкретного листа. Порядок источников из XLSX стабильный, а результаты
    # параллельной загрузки — нет, поэтому объединяем именно по исходному порядку.
    ordered_sources = sorted(sources, key=lambda pair: pair[0].strip().lower() == "вся база")
    for topic, _url in ordered_sources:
        users = gathered.get(topic, set())
        for username in users:
            if username in all_users:
                duplicates += 1
                continue
            all_users[username] = topic

    result = {
        "sources_total": len(sources), "sources_loaded": len(gathered),
        "sources_failed": failed, "usernames_unique": len(all_users),
        "duplicates_between_sources": duplicates, "by_topic": dict(Counter(all_users.values())),
        "added": 0, "already_in_catalog": 0,
    }
    if dry_run:
        return result

    database.init_db()
    with database.get_conn() as conn:
        for username, topic in all_users.items():
            exists = conn.execute("SELECT id FROM chats WHERE LOWER(COALESCE(username,''))=?", (username,)).fetchone()
            if exists:
                conn.execute("UPDATE chats SET source=COALESCE(source, '16тысТГ') WHERE id=?", (exists["id"],))
                result["already_in_catalog"] += 1
                continue
            conn.execute(
                "INSERT INTO chats (title, username, link, topic, source, status, notes) VALUES (?,?,?,?,?,'new', ?)",
                (username, username, f"https://t.me/{username}", topic, "16тысТГ",
                 "Импорт: тематический индекс «16 000 чатов Телеграм»"),
            )
            result["added"] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Импорт тематического индекса Telegram-чатов")
    parser.add_argument("index", type=Path, help="XLSX с темами и ссылками Google Sheets")
    parser.add_argument("--dry-run", action="store_true", help="только собрать и посчитать, без записи в БД")
    args = parser.parse_args()
    print(json.dumps(run(args.index, args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
