"""Комплексное оформление купленных аккаунтов — ОДНИМ прогоном (без отдельной кнопки
«просто bio»): каждому — уникальное русское имя, свой (не копия!) bio по ОДНОЙ общей
инструкции, доверие-вызывающий @ник (транслит+цифры), фото (если загружено), и
приватность (спрятать номер) — сразу пуш в Telegram.

    python -m channels.identity --ids 12,13,14 --bio-style "риелтор из Сочи, дружелюбный тон"

bio-style можно оставить пустым — тогда bio берётся из общего резервного пула
(см. channels/profile_gen.py), тоже разное на каждый аккаунт.

Разделение имён: tg_name — чистое «Имя Фамилия», уходит в сам профиль Telegram.
label — наш внутренний ярлык вида «Василий928» (имя + цифры номера), для быстрого
опознания аккаунта в таблице; в Telegram не отправляется (см. channels/warmup.py)."""
from __future__ import annotations

import argparse
import asyncio
import random

from channels.onboard import _onboard_one
from channels.profile_gen import generate_bio
from channels.ru_names import make_label, sample_unique
from db import database


async def run(ids: list[int], bio_style: str) -> None:
    database.init_db()
    names = sample_unique(len(ids))
    ok = 0
    for i, (acc_id, name) in enumerate(zip(ids, names)):
        with database.get_conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
        if not row:
            print(f"[skip] аккаунт #{acc_id} не найден")
            continue
        acc = dict(row)
        label = make_label(name, acc.get("phone"))
        # ИИ пишет РАЗНОЕ bio каждому по одной инструкции — не копирует один текст на всех
        bio = generate_bio(role=acc.get("role"), label=name, description=bio_style or None)
        with database.get_conn() as conn:
            conn.execute("UPDATE accounts SET tg_name=?, label=?, description=? WHERE id=?",
                        (name, label, bio, acc_id))
        acc["tg_name"], acc["label"], acc["description"] = name, label, bio
        success, msg = await _onboard_one(acc)   # пуш в Telegram: имя+ник+bio+аватар+приватность
        print(("[ok] " if success else "[skip] ") + f"«{name}» ({label}) — {msg}")
        ok += int(success)
        if i < len(ids) - 1:
            await asyncio.sleep(random.uniform(2.0, 5.0))  # не долбим разом
    with database.get_conn() as conn:
        database.add_event(
            conn, "info", f"🎭 Личности присвоены: {ok} из {len(ids)}",
            f"имя + ник + ИИ bio ({bio_style or 'без стиля — резервный пул'}) + фото/приватность "
            "— пуш в Telegram, там где сессия позволила",
            level="good" if ok else "warn",
        )
    print(f"\nИтого оформлено: {ok} из {len(ids)}")


def main() -> None:
    p = argparse.ArgumentParser(description="Массовая личность (имя+bio) для аккаунтов")
    p.add_argument("--ids", required=True, help="через запятую: 1,2,3")
    p.add_argument("--bio-style", default="", help="одна инструкция для ИИ на всю пачку (стиль/легенда)")
    args = p.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()]
    if not ids:
        p.error("пустой список --ids")
    asyncio.run(run(ids, args.bio_style))


if __name__ == "__main__":
    main()
