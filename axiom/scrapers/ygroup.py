# -*- coding: utf-8 -*-
"""
YGROUP scraper — сбор контактов по объектам (ЖК) из web.ygroup.ru.

Работает через ТВОЙ залогиненный аккаунт (Bearer-токен из браузера в ygroup_token.txt).
Контакты лежат прямо в списке объектов, поэтому идём просто по страницам —
человеческим темпом (случайные паузы) и с докачкой (прервалось -> запусти снова,
продолжит с места).

Фильтр по умолчанию: Сочи + новостройки (квартиры) + 214-ФЗ — как на сайте.
Чтобы собрать по всей России: поставь CITY = None ниже.

Запуск:
  py axiom/scrapers/ygroup.py           # полный сбор -> ygroup_contacts.xlsx/.csv
  py axiom/scrapers/ygroup.py --excel   # пересобрать xlsx из уже собранного csv
"""
import os
import sys
import csv
import json
import time
import random
import argparse

import requests

# ---------------- настройки ----------------
API = "https://api-ru.ygroup.ru"
# id города Сочи. Поставь CITY = None, чтобы собрать по всей России.
CITY = {"id": "37d1e8ef-95e5-41d0-9db5-653a3e6ea662", "name": "Сочи"}
LIST_PARAMS = {"sorting": "relevant", "subtypes": "1", "fz214": "true"}  # квартиры + 214-ФЗ

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "ygroup_token.txt")
OUT_CSV = os.path.join(HERE, "ygroup_contacts.csv")
OUT_XLSX = os.path.join(HERE, "ygroup_contacts.xlsx")
DONE_FILE = os.path.join(HERE, "ygroup_done_ids.txt")

COLUMNS = [
    "ЖК", "Статус", "Дата сдачи", "Имя контакта", "Телефон",
    "WhatsApp", "Telegram", "Тип контакта", "Город", "Район", "Адрес",
    "ФЗ-214", "Комиссия %", "Ссылка на объект",
]

OWNER_TYPE = {1: "агент (добавил вручную)", 2: "контакт объекта"}


def log(*a):
    try:
        print(*a, flush=True)
    except UnicodeEncodeError:
        print(*(str(x).encode("ascii", "replace").decode() for x in a), flush=True)


def read_token():
    if not os.path.exists(TOKEN_FILE):
        log(f"[!] Нет файла с токеном: {TOKEN_FILE}")
        sys.exit(1)
    tok = open(TOKEN_FILE, encoding="utf-8").read().strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    if len(tok) < 20 or tok.startswith("СЮДА"):
        log("[!] В ygroup_token.txt нет токена. Вставь токен и запусти снова.")
        sys.exit(1)
    return tok


def make_session(token):
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Origin": "https://web.ygroup.ru",
        "Referer": "https://web.ygroup.ru/",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
    })
    return s


def human_pause(a=3.0, b=7.0):
    time.sleep(random.uniform(a, b))


def api_get(s, path, params=None, _try=0):
    try:
        r = s.get(API + path, params=params, timeout=30)
    except requests.exceptions.RequestException as e:
        if _try >= 6:
            raise
        wait = min(90, 5 * (2 ** _try)) + random.uniform(0, 4)
        log(f"[~] обрыв связи ({type(e).__name__}), пауза {int(wait)}с и повтор #{_try+1}...")
        time.sleep(wait)
        return api_get(s, path, params, _try + 1)
    if r.status_code == 401:
        log("\n[!] 401 — токен не принят. Возьми свежий токен в браузере, "
            "замени в ygroup_token.txt и запусти снова (продолжу с места).")
        sys.exit(2)
    if r.status_code == 429 or r.status_code >= 500:
        if _try >= 6:
            r.raise_for_status()
        wait = min(120, 15 * (2 ** _try)) + random.uniform(0, 5)
        log(f"[~] сервер {r.status_code} — пауза {int(wait)}с и повтор #{_try+1}...")
        time.sleep(wait)
        return api_get(s, path, params, _try + 1)
    r.raise_for_status()
    return r.json()


# ---------------- извлечение ----------------

def digits(phone):
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def facility_status(f):
    if f.get("is_commissioned"):
        return "Сдан"
    q, y = f.get("commissioning_quarter"), f.get("commissioning_year")
    return "Строится" if (q or y) else ""


def deadline_str(f):
    if f.get("is_commissioned"):
        return "Сдан"
    q, y = f.get("commissioning_quarter"), f.get("commissioning_year")
    if q and y:
        return f"{q} кв. {y}"
    return str(y) if y else ""


def rows_from_facility(f, city_name):
    contacts = f.get("phone_contacts") or []
    if not contacts and f.get("display_phone_contact"):
        contacts = [f["display_phone_contact"]]
    if not contacts:
        return []
    # дедуп по номеру
    seen, uniq = set(), []
    for c in contacts:
        ph = c.get("phone_number")
        if ph and ph not in seen:
            seen.add(ph)
            uniq.append(c)

    name = f.get("name") or f.get("complex_name") or ""
    district = (f.get("district") or {}).get("name", "")
    city = city_name or (f.get("city") or {}).get("name", "") if isinstance(f.get("city"), dict) else (city_name or "")
    commission = f.get("commission_percent")
    commission = f"{round(commission * 100, 2)}%" if isinstance(commission, (int, float)) else ""
    link = f"https://web.ygroup.ru/app/apartment-complexes/{f.get('id')}"

    rows = []
    for c in uniq:
        d = digits(c.get("phone_number"))
        rows.append({
            "ЖК": name,
            "Статус": facility_status(f),
            "Дата сдачи": deadline_str(f),
            "Имя контакта": c.get("contact_owner_name") or "",
            "Телефон": c.get("phone_number") or "",
            "WhatsApp": f"https://wa.me/{d}" if d else "",
            "Telegram": f"https://t.me/+{d}" if d else "",
            "Тип контакта": OWNER_TYPE.get(c.get("owner_type"), str(c.get("owner_type", ""))),
            "Город": city,
            "Район": district,
            "Адрес": f.get("address") or "",
            "ФЗ-214": "да" if f.get("fz214") else "нет",
            "Комиссия %": commission,
            "Ссылка на объект": link,
        })
    return rows


# ---------------- пагинация ----------------

def iter_facilities(s):
    params0 = dict(LIST_PARAMS)
    if CITY:
        params0["city_id"] = CITY["id"]
    page, total, seen = 1, None, 0
    while True:
        data = api_get(s, "/v1/facilities", dict(params0, page=str(page)))["data"]
        if total is None:
            total = data.get("meta", {}).get("total")
            log(f"[i] Объектов по фильтру ({CITY['name'] if CITY else 'вся Россия'}): {total}")
        facs = data.get("facilities") or []
        if not facs:
            break
        for f in facs:
            yield f
        seen += len(facs)
        log(f"[i] стр.{page}: +{len(facs)}  (просмотрено {seen}/{total})")
        page += 1
        human_pause()


# ---------------- запись ----------------

def load_done():
    if not os.path.exists(DONE_FILE):
        return set()
    return set(x.strip() for x in open(DONE_FILE, encoding="utf-8") if x.strip())


def mark_done(fid):
    with open(DONE_FILE, "a", encoding="utf-8") as f:
        f.write(fid + "\n")


def append_rows(rows):
    new = not os.path.exists(OUT_CSV)
    with open(OUT_CSV, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def build_xlsx():
    if not os.path.exists(OUT_CSV):
        log("[!] Нет CSV.")
        return
    import openpyxl
    from openpyxl.styles import Font, Alignment
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Контакты"
    n = 0
    with open(OUT_CSV, encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.reader(f)):
            ws.append(row)
            n = i
            if i == 0:
                for c in ws[1]:
                    c.font = Font(bold=True)
                    c.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    widths = {"ЖК": 34, "Адрес": 46, "Имя контакта": 16, "Телефон": 16,
              "WhatsApp": 26, "Telegram": 24, "Ссылка на объект": 30}
    for col in ws.columns:
        head = col[0].value
        w = widths.get(head, max(12, min(30, max((len(str(c.value)) for c in col if c.value), default=12) + 2)))
        ws.column_dimensions[col[0].column_letter].width = w
    wb.save(OUT_XLSX)
    log(f"[✓] Excel готов: {OUT_XLSX}  (строк-контактов: {n})")


# ---------------- прогон ----------------

def run(s):
    done = load_done()
    city_name = CITY["name"] if CITY else ""
    log(f"[i] Уже обработано объектов: {len(done)} (пропущу)")
    total_rows, empty = 0, 0
    for f in iter_facilities(s):
        fid = f.get("id")
        if not fid or fid in done:
            continue
        rows = rows_from_facility(f, city_name)
        if rows:
            append_rows(rows)
            total_rows += len(rows)
            log(f"[+] {(f.get('name') or '?')[:38]:38} контактов: {len(rows)}  (всего {total_rows})")
        else:
            empty += 1
        mark_done(fid)
    log(f"\n[✓] Готово. Контактов: {total_rows}. Объектов без контактов: {empty}.")
    build_xlsx()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--excel", action="store_true", help="только собрать xlsx из csv")
    args = ap.parse_args()
    if args.excel:
        build_xlsx()
        return
    run(make_session(read_token()))


if __name__ == "__main__":
    main()
