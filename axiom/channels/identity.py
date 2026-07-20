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

from channels.avatar_gen import ensure_avatar
from channels.onboard import _onboard_one
from channels.profile_gen import generate_bio
from channels.ru_names import make_label, sample_unique
from db import database


def _take_bio_pool() -> list[str]:
    """Одноразовый пул bio, выбранный оператором в превью (настройка bio_pool_pending).
    Забираем и сразу очищаем — чтобы следующая упаковка без выбора шла как обычно."""
    import json
    with database.get_conn() as conn:
        raw = database.get_setting(conn, "bio_pool_pending", "") or ""
        if raw:
            database.set_setting(conn, "bio_pool_pending", "")
    try:
        pool = [b.strip() for b in json.loads(raw) if isinstance(b, str) and b.strip()] if raw else []
    except Exception:  # noqa: BLE001
        pool = []
    random.shuffle(pool)   # чтобы порядок раздачи не совпадал с порядком аккаунтов
    return pool


async def run(ids: list[int], bio_style: str) -> None:
    database.init_db()
    bio_pool = _take_bio_pool()   # если оператор выбрал варианты в превью — берём их
    pool_i = 0
    # само-лечение прокси перед пушем в Telegram: иначе логин идёт через мёртвый
    # прокси и валится «Server closed the connection» — оформление молча не
    # применяется (имя/био/фото не записываются). Сбой лечения не рушит пакет.
    try:
        from channels import proxy_pool
        await proxy_pool.heal(ids=ids, warming_only=False)
    except Exception as e:  # noqa: BLE001
        print(f"[identity] авто-лечение прокси пропущено: {e}")
    names = sample_unique(len(ids))
    ok = 0
    for i, (acc_id, name) in enumerate(zip(ids, names)):
        try:
            with database.get_conn() as conn:
                row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
            if not row:
                print(f"[skip] аккаунт #{acc_id} не найден")
                continue
            acc = dict(row)
            # ИДЕМПОТЕНТНОСТЬ: если у аккаунта уже присвоена личность (tg_name) — НЕ
            # перекатываем имя случайным (иначе каждый повторный запуск даёт новое
            # имя, а фото/ник остаются старыми → рассинхрон «Егор + женское фото»).
            # Берём существующее имя; фото ниже перегенерится под его пол, если надо.
            if (acc.get("tg_name") or "").strip():
                name = acc["tg_name"].strip()
            label = make_label(name, acc.get("phone"))
            # bio: приоритет — выбранный оператором пул (каждому свой, по кругу);
            # иначе держим существующее; иначе генерим по стилю (или из резервного пула)
            if bio_pool:
                bio = bio_pool[pool_i % len(bio_pool)]
                pool_i += 1
            elif bio_style or not (acc.get("description") or "").strip():
                bio = generate_bio(role=acc.get("role"), label=name, description=bio_style or None)
            else:
                bio = acc["description"]
            with database.get_conn() as conn:
                conn.execute("UPDATE accounts SET tg_name=?, label=?, description=? WHERE id=?",
                            (name, label, bio, acc_id))
            acc["tg_name"], acc["label"], acc["description"] = name, label, bio
            acc["avatar"] = ensure_avatar(acc)   # фото строго под пол имени (перегенерит при рассинхроне)
            with database.get_conn() as conn:  # синхронизируем avatar в acc и БД
                conn.execute("UPDATE accounts SET avatar=? WHERE id=?", (acc.get("avatar"), acc_id))
            success, msg = await _onboard_one(acc)   # пуш в Telegram: имя+ник+bio+аватар+приватность
            print(("[ok] " if success else "[skip] ") + f"«{name}» ({label}) — {msg}")
            # честный лог по КАЖДОМУ аккаунту (виден в колокольчике/у глазка), а не общий итог
            with database.get_conn() as conn:
                database.add_event(
                    conn, "identity" if success else "identity_fail",
                    f"{'✅' if success else '⛔'} Упаковка: {name}",
                    msg, level="good" if success else "bad", account_id=acc_id,
                )
            ok += int(success)
        except Exception as e:  # noqa: BLE001 — один сбойный аккаунт не должен рушить весь пакет
            print(f"[error] аккаунт #{acc_id}: {e}")
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
