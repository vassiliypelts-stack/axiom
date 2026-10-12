"""Имена знакомых из телефонной книжки номера, до первого сообщения.

Знакомые владельца заведены списком номеров, без имён: в карточке «lead» или пусто.
Подпись из книжки приходила только в момент стука (campaign_send пишет contacts.tg_name),
поэтому до него «Кто в рассылке» показывал заглушки. Здесь один раз читаем книжку
каждого номера, за которым закреплены такие люди, и переносим подписи в карточки.

Один запрос contacts.GetContacts на номер, людям ничего не пишем.

Запуск:
    python -m channels.book_names            # все номера, у которых есть карточки без имени
    python -m channels.book_names --account 3
"""
from __future__ import annotations

import argparse
import asyncio

from telethon.tl import functions

from channels.telegram import client_for_account
from db import database


def _tail(phone: str | None) -> str:
    """Последние 10 цифр: «8 701 749-87-54» и «+77017498754» — один номер."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]


def _pending(account_id: int | None = None) -> dict[int, list[dict]]:
    """Карточки без имени по номерам, за которыми они закреплены."""
    sql = ("SELECT id, phone, outreach_account_id FROM contacts "
           "WHERE outreach_account_id IS NOT NULL AND deleted_at IS NULL "
           "AND COALESCE(phone,'')<>'' AND COALESCE(tg_name,'')='' "
           "AND (lower(trim(COALESCE(name,''))) IN ('', 'lead') OR COALESCE(person_name,'')='')")
    args: list = []
    if account_id:
        sql += " AND outreach_account_id=?"
        args.append(account_id)
    out: dict[int, list[dict]] = {}
    with database.get_conn() as conn:
        for r in conn.execute(sql, args).fetchall():
            out.setdefault(r["outreach_account_id"], []).append(dict(r))
    return out


def _label(user) -> str:
    return " ".join(x for x in (getattr(user, "first_name", "") or "",
                                getattr(user, "last_name", "") or "") if x).strip()


async def sync(account_id: int | None = None) -> dict:
    database.init_db()
    named = missing = 0
    for acc_id, cards in _pending(account_id).items():
        try:
            client, _ = client_for_account(acc_id)
        except Exception as e:  # noqa: BLE001 — номер без сессии не должен рвать остальные
            print(f"[book] номер #{acc_id} недоступен: {e}")
            continue
        try:
            await client.connect()
            if not await client.is_user_authorized():
                print(f"[book] номер #{acc_id}: сессия не авторизована — пропускаю")
                continue
            res = await client(functions.contacts.GetContactsRequest(hash=0))
            book = {_tail(u.phone): _label(u) for u in getattr(res, "users", []) if u.phone}
        except Exception as e:  # noqa: BLE001
            print(f"[book] номер #{acc_id}: книжку прочитать не удалось: {e}")
            continue
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        got = 0
        with database.get_conn() as conn:
            for c in cards:
                label = book.get(_tail(c["phone"]))
                if not label:
                    missing += 1
                    continue
                conn.execute("UPDATE contacts SET tg_name=? WHERE id=?", (label, c["id"]))
                database.adopt_book_name(conn, c["id"], label)
                got += 1
        named += got
        print(f"[book] номер #{acc_id}: в книжке {len(book)} человек, "
              f"имя записано в {got} из {len(cards)} карточек")
    print(f"[book] итого: с именем {named}, нет в книжке {missing}")
    return {"named": named, "missing": missing}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Имена из телефонной книжки номера")
    p.add_argument("--account", type=int, help="только этот номер (id аккаунта)")
    asyncio.run(sync(p.parse_args().account))
