# -*- coding: utf-8 -*-
"""
Второй проход: добавляет к таблице контактов данные ЗАСТРОЙЩИКА/ПРОДАВЦА.

Логика: у каждого объекта есть creator_user_id (кто выложил). Профиль этого
пользователя (`GET /v1/users/{id}`) = отдел продаж застройщика/агентства:
имя (sales_department_name), email, телефон, сколько объектов ведёт.

Запуск (после того как отработал ygroup.py):
  py axiom/scrapers/ygroup_enrich.py
Результат: ygroup_contacts_full.csv / .xlsx  (+ колонки застройщика)
"""
import os
import re
import csv
import json
import time

import ygroup as y   # переиспользуем сессию/пагинацию/ретраи

HERE = os.path.dirname(os.path.abspath(__file__))
FAC_MAP = os.path.join(HERE, "ygroup_fac_creator.json")   # facility_id -> creator_user_id
DEV_CACHE = os.path.join(HERE, "ygroup_developers.json")  # creator_user_id -> {dev...}
OUT_CSV = os.path.join(HERE, "ygroup_contacts_full.csv")
OUT_XLSX = os.path.join(HERE, "ygroup_contacts_full.xlsx")

DEV_COLS = ["Продавец", "Тип продавца", "Email продавца", "Телефон продавца",
            "Объектов у продавца"]

SELLER_TYPE = {1: "Агент", 2: "Застройщик/компания"}


def load_json(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path, obj):
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False)


def build_fac_creator_map(s):
    """facility_id -> creator_user_id (пагинация списка, докачиваемо)."""
    m = load_json(FAC_MAP, {})
    if m:
        y.log(f"[i] карта объект→создатель уже есть: {len(m)} — использую её "
              f"(удали {os.path.basename(FAC_MAP)}, чтобы пересобрать)")
        return m
    y.log("[i] собираю карту объект→создатель...")
    n = 0
    for f in y.iter_facilities(s):
        fid = f.get("id")
        if fid:
            m[fid] = f.get("creator_user_id")
            n += 1
            if n % 400 == 0:
                save_json(FAC_MAP, m)
    save_json(FAC_MAP, m)
    y.log(f"[✓] объектов в карте: {len(m)}")
    return m


def resolve_developers(s, creator_ids):
    cache = load_json(DEV_CACHE, {})
    todo = [c for c in creator_ids if c and c not in cache]
    y.log(f"[i] уникальных застройщиков всего: {len(creator_ids)} | "
          f"нужно запросить: {len(todo)} (остальное из кэша)")
    for i, cid in enumerate(todo, 1):
        try:
            u = y.api_get(s, f"/v1/users/{cid}")["data"]["user"]
            fio = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip()
            name = (u.get("sales_department_name") or u.get("organization_name") or fio or "")
            cache[cid] = {
                "name": name,
                "type": SELLER_TYPE.get(u.get("type"), str(u.get("type") or "")),
                "email": u.get("email") or "",
                "phone": u.get("phone_number") or "",
                "count": u.get("facilities_count") or "",
            }
        except Exception as e:
            y.log(f"[~] продавец {cid}: не получен ({type(e).__name__})")
            cache[cid] = {"name": "", "type": "", "email": "", "phone": "", "count": ""}
        if i % 25 == 0:
            save_json(DEV_CACHE, cache)
            y.log(f"    ...{i}/{len(todo)}")
        y.human_pause(0.9, 2.2)
    save_json(DEV_CACHE, cache)
    return cache


ID_RE = re.compile(r"apartment-complexes/([0-9a-f-]{36})")


def merge():
    if not os.path.exists(y.OUT_CSV):
        y.log("[!] Нет ygroup_contacts.csv — сначала запусти ygroup.py")
        return None, None, None
    rows = list(csv.DictReader(open(y.OUT_CSV, encoding="utf-8-sig")))
    fac_creator = load_json(FAC_MAP, {})
    devs = load_json(DEV_CACHE, {})
    out_cols = y.COLUMNS[:]
    # вставим колонки застройщика сразу после «ЖК»
    pos = out_cols.index("ЖК") + 1
    for c in reversed(DEV_COLS):
        out_cols.insert(pos, c)
    filled = 0
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=out_cols)
        w.writeheader()
        for r in rows:
            mo = ID_RE.search(r.get("Ссылка на объект", ""))
            dev = devs.get(fac_creator.get(mo.group(1))) if mo else None
            r["Продавец"] = dev["name"] if dev else ""
            r["Тип продавца"] = dev.get("type", "") if dev else ""
            r["Email продавца"] = dev["email"] if dev else ""
            r["Телефон продавца"] = dev["phone"] if dev else ""
            r["Объектов у продавца"] = dev["count"] if dev else ""
            if dev and dev["name"]:
                filled += 1
            w.writerow(r)
    return rows, filled, out_cols


def build_xlsx(cols):
    import openpyxl
    from openpyxl.styles import Font, Alignment
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Контакты+застройщик"
    with open(OUT_CSV, encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.reader(f)):
            ws.append(row)
            if i == 0:
                for c in ws[1]:
                    c.font = Font(bold=True)
                    c.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wide = {"ЖК": 32, "Продавец": 22, "Email продавца": 24,
            "Телефон продавца": 18, "Адрес": 44, "WhatsApp": 26,
            "Telegram": 24, "Ссылка на объект": 30, "Имя контакта": 16}
    for col in ws.columns:
        h = col[0].value
        ws.column_dimensions[col[0].column_letter].width = wide.get(
            h, max(12, min(28, max((len(str(c.value)) for c in col if c.value), default=12) + 2)))
    wb.save(OUT_XLSX)


def main():
    s = y.make_session(y.read_token())
    fac_creator = build_fac_creator_map(s)
    creator_ids = sorted(set(v for v in fac_creator.values() if v))
    resolve_developers(s, creator_ids)
    rows, filled, cols = merge()
    if rows is None:
        return
    build_xlsx(cols)
    y.log(f"\n[✓] Готово: {OUT_XLSX}")
    y.log(f"    строк: {len(rows)} | с застройщиком: {filled} | застройщиков: {len(creator_ids)}")


if __name__ == "__main__":
    main()
