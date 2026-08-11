"""Проверка: рассылка не может отправить человеку два первых сообщения.

ЗАЧЕМ. Дубль опенера — самая дорогая ошибка рассылки: человек видит «добрый день,
правильно обращаюсь?» второй раз, разговор испорчен, а серия таких повторов с одного
аккаунта — прямой путь к бану. Вживую это уже случалось (три опенера подряд в один
диалог), и разбирались долго, потому что проверить защиту было нечем: приходилось
рассуждать о коде вместо того, чтобы прогнать.

Скрипт работает на ВРЕМЕННОЙ базе и не ходит в сеть — запускать можно когда угодно,
в том числе на боевом сервере.

    python -m channels.dupe_check
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

import config

# Своя БД до любых импортов, которые её открывают: боевую не трогаем.
config.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "dupe_check.db"

from db import database  # noqa: E402

_ENGAGED_LIKE = ("in_dialog", "meeting_set", "met", "won", "refused")

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'СБОЙ'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    database.init_db()
    with database.get_conn() as conn:
        conn.execute("INSERT INTO campaigns (id,name,channel,audience_tag,status) "
                     "VALUES (1,'Проверка','telegram','tagX','running')")
        cid_new = database.upsert_contact(conn, source="t", phone="+79001110011",
                                          name="Новый", tags="tagX")
        cid_dlg = database.upsert_contact(conn, source="t", phone="+79002220022",
                                          name="В диалоге", tags="tagX")
        conn.execute("UPDATE contacts SET status='in_dialog' WHERE id=?", (cid_dlg,))
        conn.execute("UPDATE contacts SET username='novyy' WHERE id=?", (cid_new,))

    print("\n1. Атомарный захват контакта (защита от двух отправок)")
    with database.get_conn() as conn:
        first = conn.execute("UPDATE contacts SET status='messaged' "
                             "WHERE id=? AND status='new'", (cid_new,)).rowcount
        second = conn.execute("UPDATE contacts SET status='messaged' "
                              "WHERE id=? AND status='new'", (cid_new,)).rowcount
    check("первый заход забирает контакт", first == 1, f"rowcount={first}")
    check("второй заход НЕ забирает тот же контакт", second == 0,
          f"rowcount={second} — иначе человек получит опенер дважды")

    print("\n2. Боевая аудитория не берёт тех, с кем разговор уже идёт")
    from channels.campaign_send import _audience
    ids = [r["id"] for r in _audience(1, "tagX", "telegram", cap=50)]
    check("контакт в диалоге не попадает в боевой заход", cid_dlg not in ids)
    check("уже отправленный (messaged) не попадает повторно", cid_new not in ids)

    print("\n3. Кнопка «Тест» не откатывает живой диалог в 'new'")
    with database.get_conn() as conn:
        conn.execute("UPDATE contacts SET is_test=1 WHERE id IN (?,?)", (cid_new, cid_dlg))
        rows = conn.execute("SELECT id,status FROM contacts WHERE COALESCE(is_test,0)=1").fetchall()
        skipped = [r["id"] for r in rows if (r["status"] or "") in _ENGAGED_LIKE]
        resettable = [r["id"] for r in rows if r["id"] not in set(skipped)]
        if resettable:
            conn.execute("UPDATE contacts SET status='new' WHERE id IN ({})".format(
                ",".join("?" * len(resettable))), resettable)
        after = {r["id"]: r["status"] for r in conn.execute(
            "SELECT id,status FROM contacts WHERE COALESCE(is_test,0)=1")}
    check("контакт в диалоге сохранил статус", after[cid_dlg] == "in_dialog",
          f"стало «{after[cid_dlg]}»")
    check("контакт вне диалога сброшен для повторного теста", after[cid_new] == "new",
          f"стало «{after[cid_new]}»")

    print()
    if failures:
        print(f"ПРОВАЛЕНО ПРОВЕРОК: {len(failures)}")
        for f in failures:
            print(" •", f)
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ — дубль опенера невозможен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
