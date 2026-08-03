"""Разметить уже накопленные находки: клиент / конкурент / не разобрано.

Классификация появилась позже самих находок, поэтому у старых записей поле intent
пустое и они висят в «не разобрано». Этот проход проставляет его задним числом —
и заодно показывает, сколько мусора накопилось до фильтра.

    python -m channels.hits_reclassify              # правилами, всё (быстро, бесплатно)
    python -m channels.hits_reclassify --model      # спорное дополнительно спросить у модели
    python -m channels.hits_reclassify --only-new   # трогать только неразмеченные
    python -m channels.hits_reclassify --dry        # показать, ничего не записывая
"""
from __future__ import annotations

import argparse

from channels import hit_intent as hi
from db import database


def run(use_model: bool = False, only_new: bool = False, dry: bool = False,
        limit: int | None = None) -> dict:
    database.init_db()
    where = "WHERE COALESCE(intent,'')=''" if only_new else ""
    sql = f"SELECT id, text, intent FROM chat_hits {where} ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with database.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]

    tally: dict[str, int] = {}
    changed = 0
    asked_model = 0
    for r in rows:
        intent, why = hi.by_rules(r["text"] or "")
        # Модель дороже и медленнее правил, поэтому спрашиваем только там, где
        # правила пожали плечами — на живой базе это около 5% записей.
        if intent == hi.UNKNOWN and use_model:
            intent, why = hi.by_model(r["text"] or "")
            asked_model += 1
        tally[intent] = tally.get(intent, 0) + 1
        if intent != (r.get("intent") or ""):
            changed += 1
            if not dry:
                with database.get_conn() as conn:
                    conn.execute("UPDATE chat_hits SET intent=?, intent_why=? WHERE id=?",
                                 (intent, why, r["id"]))

    out = {"всего": len(rows), "изменено": changed, "спрошено у модели": asked_model,
           **{hi.LABEL[k]: v for k, v in tally.items()}}
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Разметка находок: клиент/конкурент")
    p.add_argument("--model", action="store_true", help="спорное досматривать моделью")
    p.add_argument("--only-new", action="store_true", help="только неразмеченные")
    p.add_argument("--dry", action="store_true", help="показать, не записывая")
    p.add_argument("--limit", type=int, help="ограничить число записей")
    a = p.parse_args()
    res = run(use_model=a.model, only_new=a.only_new, dry=a.dry, limit=a.limit)
    print(("[сухой прогон] " if a.dry else "") + " · ".join(f"{k}: {v}" for k, v in res.items()))


if __name__ == "__main__":
    main()
