"""Боевые имена «Василий» + цифры номера для аккаунтов без личности.

Ярлык в пульте — «Василий928» (имя + 3 цифры номера, как у остальных боевых),
имя в самом Telegram — «Василий <Фамилия>» из пула ru_names. Фамилии в пачке
не повторяются, пока хватает пула.

Родные и служебные не трогаем: их имена настоящие и осмысленные.
Аккаунты, у которых личность уже проставлена (есть tg_name), пропускаем —
скрипт идемпотентный, повторный запуск ничего не перезапишет.

Само применение в Telegram (имя, фото, скрытие номера) делает «оформить
сейчас» / прогрев — тут только готовим карточки в базе.

    .venv/bin/python tools/name_combat_vasiliy.py           # разведка
    .venv/bin/python tools/name_combat_vasiliy.py --write   # записать
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels.ru_names import NAMES, make_label, phone_digits  # noqa: E402
from db import database  # noqa: E402

FIRST = "Василий"


def surnames_pool() -> list[str]:
    """Мужские фамилии из пула имён (первые 20 записей — мужские)."""
    out = []
    for n in NAMES[:20]:
        parts = n.split()
        if len(parts) > 1:
            out.append(parts[-1])
    return out or ["Петров", "Смирнов", "Кузнецов", "Попов", "Соколов"]


def main(write: bool) -> None:
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, label, tg_name, phone, acc_role, protected, status "
            "FROM accounts WHERE COALESCE(acc_role,'combat')='combat' "
            "AND COALESCE(protected,0)=0 "
            "AND COALESCE(tg_name,'')='' "
            "AND status <> 'banned' "
            "ORDER BY id"
        ).fetchall()

        targets = [dict(r) for r in rows]
        print(f"Боевых без личности: {len(targets)}   "
              f"режим: {'ЗАПИСЬ' if write else 'разведка (--write для записи)'}\n")
        if not targets:
            print("Все боевые уже именованы.")
            return

        # фамилии, уже занятые в базе, чтобы не плодить тёзок
        used = {r["tg_name"].split()[-1] for r in conn.execute(
            "SELECT tg_name FROM accounts WHERE COALESCE(tg_name,'')<>''")
            if len((r["tg_name"] or "").split()) > 1}

        pool = [s for s in surnames_pool() if s not in used] or surnames_pool()
        random.shuffle(pool)

        for i, acc in enumerate(targets):
            surname = pool[i % len(pool)]
            tg_name = f"{FIRST} {surname}"
            label = make_label(FIRST, acc["phone"])
            if not phone_digits(acc["phone"]):
                label = f"{FIRST}{acc['id']}"   # номера нет — цепляем id, лишь бы не дубль
            print(f"  id={acc['id']:<6} {str(acc['label'])[:20]:<20} → {label:<14} «{tg_name}»")
            if write:
                conn.execute("UPDATE accounts SET tg_name=?, label=? WHERE id=?",
                             (tg_name, label, acc["id"]))
        if write:
            conn.commit()

    print(f"\n{'Записано' if write else 'Будет изменено'}: {len(targets)}")
    if write:
        print("Дальше — «🎭 оформить сейчас» в «Аккаунтах»: имя, фото и скрытие номера уйдут в Telegram.")


if __name__ == "__main__":
    main("--write" in sys.argv)
