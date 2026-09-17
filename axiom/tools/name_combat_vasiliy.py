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


# Пула ru_names (≈14 мужских фамилий) на пачку из 30+ не хватает, и «Василий
# Воробьёв» повторился бы трижды — для боевых аккаунтов это заметный след.
_EXTRA_SURNAMES = [
    "Андреев", "Белов", "Гаврилов", "Дорохов", "Ершов", "Жуков", "Зайцев",
    "Ильин", "Карпов", "Лебедев", "Макаров", "Никитин", "Орлов", "Панкратов",
    "Романов", "Сафонов", "Тарасов", "Уваров", "Филатов", "Харитонов",
    "Цветков", "Чернов", "Шилов", "Щербаков", "Юдин", "Яковлев",
    "Баранов", "Власов", "Гусев", "Демидов", "Емельянов", "Зуев",
]


def surnames_pool() -> list[str]:
    """Мужские фамилии: из пула ru_names (первые 20 записей мужские) + запас."""
    out = []
    for n in NAMES[:20]:
        parts = n.split()
        if len(parts) > 1:
            out.append(parts[-1])
    out.extend(s for s in _EXTRA_SURNAMES if s not in out)
    return out


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

        # Ярлык — опора оператора в таблице, тёзки в ней недопустимы: номера разных
        # стран дают одинаковые три цифры (…2352097 и …0350097 → оба «Василий097»).
        # Заняв хвост, берём на цифру больше, и только потом падаем на id.
        taken = {r["label"] for r in conn.execute(
            "SELECT label FROM accounts WHERE COALESCE(label,'')<>''")}

        for i, acc in enumerate(targets):
            surname = pool[i % len(pool)]
            tg_name = f"{FIRST} {surname}"
            label = ""
            for n in (3, 4, 5, 6):
                digits = phone_digits(acc["phone"], n)
                if not digits:
                    break
                cand = f"{FIRST}{digits}"
                if cand not in taken:
                    label = cand
                    break
            if not label:
                label = f"{FIRST}{acc['id']}"
            taken.add(label)
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
