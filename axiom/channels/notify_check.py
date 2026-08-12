"""Проверка: уведомление о встрече и ссылка на созвон не теряются и не роняют диалог.

ЗАЧЕМ. У каждой кампании теперь своя ссылка на созвон и свой получатель уведомлений
(см. campaigns.meeting_url / notify_target / notify_account_id), с откатом на общие
настройки пульта, если в кампании не заполнено. Переключение проверялось руками через
SSH на боевой базе один раз — здесь то же самое воспроизводимо, без сети и без
боевых данных, чтобы будущая правка не сломала откат молча.

Отдельно проверяется то, что несколько раз ловили вживую: notify_meeting() не имеет
права уронить ответ агента, даже если получатель настроен на битые данные (несуществующая
кампания, несуществующий аккаунт-отправитель) — сбой должен тихо логироваться, а не
всплывать исключением.

    python -m channels.notify_check
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

import config

# Своя БД до любых импортов, которые её открывают: боевую не трогаем.
config.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "notify_check.db"

from db import database  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'СБОЙ'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    database.init_db()
    from channels import notify
    from integrations import meetings

    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO campaigns (id,name,channel,status,meeting_url,notify_target,"
            "notify_account_id) VALUES (1,'Своя','telegram','running',"
            "'https://own.example/j/1','@owner1',NULL)")
        conn.execute("INSERT INTO campaigns (id,name,channel,status) "
                     "VALUES (2,'Без своих','telegram','running')")
        database.set_setting(conn, "meeting_url", "https://global.example/j/2")
        cid = database.upsert_contact(conn, source="t", phone="+79000000001", name="Тест")

    print("1. Своя ссылка кампании перекрывает общую")
    check("кампания со своей ссылкой", meetings._meeting_url(1) == "https://own.example/j/1")

    print("\n2. У кампании без своей ссылки — откат на общую")
    check("откат на общую (кампания есть, поле пустое)",
          meetings._meeting_url(2) == "https://global.example/j/2")

    print("\n3. Вне кампании (campaign_id=None) — тоже общая")
    check("без кампании", meetings._meeting_url(None) == "https://global.example/j/2")

    print("\n4. notify_meeting: ничего не настроено — тихо пропускает, не падает")
    try:
        asyncio.run(notify.notify_meeting(cid, "2026-08-13T11:00:00", "заметка", None, campaign_id=2))
        check("не упало без настроек", True)
    except Exception as e:  # noqa: BLE001
        check("не упало без настроек", False, str(e))

    print("\n5. notify_meeting: campaign_id указывает на несуществующую кампанию")
    try:
        asyncio.run(notify.notify_meeting(cid, "2026-08-13T11:00:00", None, None, campaign_id=9999))
        check("не упало на несуществующей кампании", True)
    except Exception as e:  # noqa: BLE001
        check("не упало на несуществующей кампании", False, str(e))

    print("\n6. notify_meeting: отправитель — несуществующий аккаунт")
    with database.get_conn() as conn:
        database.set_setting(conn, "notify_sender_account_id", "999999")
        database.set_setting(conn, "notify_owner_target", "@owner")
    try:
        asyncio.run(notify.notify_meeting(cid, "2026-08-13T11:00:00", None, None, campaign_id=None))
        check("не упало на несуществующем аккаунте-отправителе", True)
    except Exception as e:  # noqa: BLE001
        check("не упало на несуществующем аккаунте-отправителе", False, str(e))

    print()
    if failures:
        print(f"ПРОВАЛЕНО ПРОВЕРОК: {len(failures)}")
        for f in failures:
            print(" •", f)
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
