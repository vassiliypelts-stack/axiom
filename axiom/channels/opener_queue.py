"""Доотправка опенера по одной строке — без «портянки», с ожиданием ответа.

campaign_send.py шлёт ПЕРВУЮ строку опенера сразу, а остальные кладёт в очередь
(opener_queue) с отметкой «когда слать следующую». Этот модуль по расписанию
(каждые несколько минут — см. --tick) проверяет очередь:

  • если контакт УЖЕ ответил (его статус ушёл от 'messaged') — остаток НЕ шлём,
    строку из очереди удаляем: дальше ведёт живой диалог/агент, дожимать нечем;
  • если тишина — шлёт следующую строку С ТОГО ЖЕ аккаунта, что и первую (иначе
    получится, что человеку с одного номера прислали привет, а с другого —
    остальное: спалит мультиаккаунт), и снова откладывает остаток на 5-10 минут.

Запуск (регулярно, например Windows-задачей раз в ~10 минут):
    python -m channels.opener_queue --tick
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
from datetime import datetime, timedelta

from telethon.sessions import StringSession

from channels.antiban import classify_error
from channels.telegram import build_client, _send_parts, _resolve_entity
from db import database

# Пауза перед ЕЩЁ следующей строкой (если после этой снова есть остаток).
NEXT_LINE_MIN = (5 * 60, 10 * 60)  # секунды: 5–10 минут


def _due_rows(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT q.*, c.status AS contact_status, c.tg_user_id, c.username, c.phone, c.name "
        "FROM opener_queue q JOIN contacts c ON c.id = q.contact_id "
        "WHERE q.next_at <= datetime('now')"
    ).fetchall()
    return [dict(r) for r in rows]


def _account(conn, account_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None


async def _send_next_line(row: dict) -> None:
    with database.get_conn() as conn:
        acc = _account(conn, row["account_id"])
    if not acc or acc.get("status") == "banned" or not acc.get("tg_session"):
        # аккаунт умер/забанен между отправками — остаток отменяем, слать больше некем
        with database.get_conn() as conn:
            conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
        print(f"[skip] очередь #{row['id']}: аккаунт #{row['account_id']} недоступен — отменено")
        return
    if row["contact_status"] != "messaged":
        # контакт уже ответил / сменил статус — не дожимаем каноничными строками,
        # дальше разговор ведёт живой агент (см. _handle_incoming в telegram.py)
        with database.get_conn() as conn:
            conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
        print(f"[cancel] контакт {row['contact_id']}: уже ответил/сменил статус — остаток опенера не шлём")
        return

    parts = json.loads(row["parts_json"])
    if not parts:
        with database.get_conn() as conn:
            conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
        return

    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
    label = acc.get("label") or acc.get("phone") or f"#{acc['id']}"
    try:
        await client.start()
        # резолвим по username/телефону (не по id — свежая сессия не помнит чужой entity-кэш)
        entity = await _resolve_entity(client, row)
        await _send_parts(client, entity, parts[:1])
    except Exception as e:  # noqa: BLE001
        cat = classify_error(e)
        if cat == "ban":
            print(f"[{label}] ⛔ забанен при доотправке опенера ({e})")
            with database.get_conn() as conn:
                conn.execute("UPDATE accounts SET status='banned' WHERE id=?", (acc["id"],))
                conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
                database.add_event(conn, "account_banned", f"⛔ Аккаунт «{label}» забанен",
                                   f"Telegram: {e}", level="bad", account_id=acc["id"])
        else:
            print(f"[{label}] не удалось доотправить строку контакту {row['contact_id']}: {e}")
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return

    rest = parts[1:]
    with database.get_conn() as conn:
        database.add_message(conn, row["contact_id"], "out", parts[0], intent=None)
        if rest:
            next_at = (datetime.utcnow()
                       + timedelta(seconds=random.uniform(*NEXT_LINE_MIN))).isoformat(sep=" ", timespec="seconds")
            conn.execute("UPDATE opener_queue SET parts_json=?, next_at=? WHERE id=?",
                        (json.dumps(rest, ensure_ascii=False), next_at, row["id"]))
        else:
            conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
    print(f"[{label}] -> контакт {row['contact_id']}: строка отправлена"
          + (f" (ещё {len(rest)} впереди)" if rest else " (опенер закрыт)"))
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001
        pass


async def tick() -> int:
    database.init_db()
    with database.get_conn() as conn:
        due = _due_rows(conn)
    for row in due:
        await _send_next_line(row)
        await asyncio.sleep(random.uniform(2.0, 6.0))
    return len(due)


def main() -> None:
    p = argparse.ArgumentParser(description="Доотправка опенера AXIOM (очередь без «портянки»)")
    p.add_argument("--tick", action="store_true", help="один проход: обработать всё, чему пора")
    args = p.parse_args()
    if not args.tick:
        p.print_help()
        return
    n = asyncio.run(tick())
    print(f"готово: обработано {n} записей очереди" if n else "нечего слать прямо сейчас")


if __name__ == "__main__":
    main()
