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

from channels import antiban, deslop, opener_lint
from channels.antiban import classify_error
from channels.telegram import build_client, _send_parts, _resolve_entity
from db import database

# Третье и последнее касание — только через сутки после второго. После него
# контакт без ответа получает статус «ignored», чтобы больше не попасть в дожим.
NEXT_LINE_MIN = (24 * 60 * 60, 24 * 60 * 60)

# После третьего касания, если человек ПРОЧИТАЛ и промолчал, можно дожать ещё раз
# (правило Василия, 25.09.2026). Метка в начале строки очереди говорит «слать только
# прочитавшему»: не прочитал, значит пинг он не увидит, а спам-жалобу копит.
IF_READ = "[[if_read]]"
LAST_NUDGE_AFTER = (44 * 60 * 60, 52 * 60 * 60)


def _due_rows(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT q.*, c.status AS contact_status, c.tg_user_id, c.username, c.phone, c.name, "
        "       cm.status AS campaign_status, cm.name AS campaign_name, cm.work_hours_tz, "
        "       cm.work_hours_start, cm.work_hours_end "
        "FROM opener_queue q JOIN contacts c ON c.id = q.contact_id "
        "LEFT JOIN campaigns cm ON cm.id = q.campaign_id "
        "WHERE q.next_at <= datetime('now')"
    ).fetchall()
    return [dict(r) for r in rows]


def _close_ignored(conn, row: dict, account_id: int, why: str) -> None:
    conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
    database.set_status(conn, row["contact_id"], "ignored")
    database.add_event(
        conn, "ignored", f"🔕 Не ответил: контакт {row['contact_id']}",
        why + " Автоматизация больше не пишет этому человеку.",
        level="info", contact_id=row["contact_id"], campaign_id=row.get("campaign_id"),
        account_id=account_id,
    )


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
    # Человек ОТВЕТИЛ — остаток опенера не шлём, что бы ни говорил статус контакта.
    # Статус на 'in_dialog' переводит слушатель, а он видит только те аккаунты, что
    # сейчас подключены, и только знакомые контакты. Стоит ему не опознать входящее —
    # статус остаётся 'messaged', и заготовленные строки идут ПОВЕРХ живого ответа:
    # человек написал «Да», а ему прилетело «Меня зовут Василий» и «Нашёл ваш профиль».
    # Факт входящего сообщения надёжнее статуса, поэтому смотрим на него.
    with database.get_conn() as conn:
        answered = conn.execute(
            "SELECT 1 FROM messages WHERE contact_id=? AND direction='in' "
            "AND ts >= (SELECT MIN(ts) FROM messages WHERE contact_id=? AND direction='out') "
            "LIMIT 1", (row["contact_id"], row["contact_id"])).fetchone()
    if answered:
        with database.get_conn() as conn:
            conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
        print(f"[cancel] контакт {row['contact_id']}: уже ответил — остаток опенера не шлём")
        return
    if row["contact_status"] != "messaged":
        # контакт уже ответил / сменил статус — не дожимаем каноничными строками,
        # дальше разговор ведёт живой агент (см. _handle_incoming в telegram.py)
        with database.get_conn() as conn:
            conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
        print(f"[cancel] контакт {row['contact_id']}: уже ответил/сменил статус — остаток опенера не шлём")
        return
    # «Стоп» кампании раньше не останавливал ЭТУ очередь: статус кампании здесь не
    # смотрели вообще, и оператор, нажав ⏸, ещё полчаса наблюдал, как остаток опенера
    # капает человеку по строке в 1-3 минуты. Кампания не 'running' — остаток гасим.
    if row.get("campaign_id") and (row.get("campaign_status") or "") != "running":
        with database.get_conn() as conn:
            conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
        print(f"[cancel] контакт {row['contact_id']}: кампания «{row.get('campaign_name')}» "
              f"не запущена (статус {row.get('campaign_status')}) — остаток опенера не шлём")
        return

    parts = json.loads(row["parts_json"])
    if not parts:
        with database.get_conn() as conn:
            conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
        return

    # Последний дожим — только тому, кто прочитал предыдущее и промолчал.
    is_last_nudge = parts[0].startswith(IF_READ)
    if is_last_nudge:
        with database.get_conn() as conn:
            if not database.last_out_read(conn, row["contact_id"]):
                _close_ignored(conn, row, acc["id"],
                               "Ушли все касания, последнее не прочитано, поэтому без дожима.")
                print(f"[close] контакт {row['contact_id']}: не прочитал — последний дожим не шлём")
                return
        parts = [parts[0][len(IF_READ):]] + parts[1:]

    # ПОСЛЕДНИЙ рубеж: линтер опенера стоит в campaign_send._parts и проверяет шаблон
    # в момент отправки ПЕРВОЙ строки. Остаток лежит здесь уже готовым списком, и до
    # этой проверки уходил человеку вообще без контроля — раз в 1-3 минуты, строка за
    # строкой. Поэтому очередь, поставленная до появления линтера (или мимо него),
    # продолжала слать промпт даже после его выката. Проверяем КАЖДУЮ строку перед
    # отправкой: нашли промпт — гасим весь остаток и говорим оператору.
    bad = opener_lint.severe(opener_lint.lint("\n".join(parts)))
    if bad:
        with database.get_conn() as conn:
            conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
            database.add_event(
                conn, "opener_blocked",
                f"🛑 Остаток опенера не отправлен: в тексте промпт (контакт {row['contact_id']})",
                opener_lint.report(bad), level="warn",
                contact_id=row["contact_id"], campaign_id=row.get("campaign_id"),
                account_id=row["account_id"])
        print(f"[БЛОК] контакт {row['contact_id']}: в остатке опенера промпт — очередь очищена\n"
              + opener_lint.report(bad))
        return

    client = build_client(StringSession(acc["tg_session"]), acc.get("proxy"),
                          acc.get("api_id"), acc.get("api_hash"))
    label = acc.get("label") or acc.get("phone") or f"#{acc['id']}"
    try:
        await client.start()
        # резолвим по username/телефону (не по id — свежая сессия не помнит чужой entity-кэш)
        entity = await _resolve_entity(client, row)
        sent_ids = await _send_parts(client, entity, parts[:1])
    except Exception as e:  # noqa: BLE001
        cat = classify_error(e)
        if cat == "ban":
            print(f"[{label}] ⛔ забанен при доотправке опенера ({e})")
            with database.get_conn() as conn:
                conn.execute("UPDATE accounts SET status='banned' WHERE id=?", (acc["id"],))
                conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
                database.add_event(conn, "account_banned", f"⛔ Аккаунт «{label}» забанен",
                                   f"Telegram: {e}", level="bad", account_id=acc["id"])
        elif cat == "blocked":
            # Контакт заблокировал этот аккаунт — остаток опенера не имеет смысла
            # слать; статус 'blocked' вместо тихого бесконечного ретрая очереди.
            print(f"[{label}] 🚫 контакт {row['contact_id']} заблокировал аккаунт — остаток опенера отменён")
            with database.get_conn() as conn:
                conn.execute("DELETE FROM opener_queue WHERE id=?", (row["id"],))
                database.set_status(conn, row["contact_id"], "blocked")
        else:
            print(f"[{label}] не удалось доотправить строку контакту {row['contact_id']}: {e}")
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return

    rest = parts[1:]
    last_delay = NEXT_LINE_MIN
    if not rest and not is_last_nudge:
        # Опенер кончился — ставим последний дожим. Уйдёт, только если человек
        # прочитает третье касание и промолчит (проверка выше, в момент отправки).
        rest = [IF_READ + deslop.last_nudge(row.get("name") or "")]
        last_delay = LAST_NUDGE_AFTER
    with database.get_conn() as conn:
        database.add_message(conn, row["contact_id"], "out", parts[0], intent=None,
                             account_id=acc["id"], tg_msg_ids=sent_ids)
        if rest:
            next_at = (datetime.utcnow()
                       + timedelta(seconds=random.uniform(*last_delay))).isoformat(sep=" ", timespec="seconds")
            conn.execute("UPDATE opener_queue SET parts_json=?, next_at=? WHERE id=?",
                        (json.dumps(rest, ensure_ascii=False), next_at, row["id"]))
        else:
            _close_ignored(conn, row, acc["id"], "Ушли все касания и последний дожим, ответа нет.")
    print(f"[{label}] -> контакт {row['contact_id']}: строка отправлена"
          + (f" (ещё {len(rest)} впереди)" if rest else " (опенер закрыт)"))
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001
        pass


def purge_prompt_rows() -> int:
    """Выбрасывает из очереди ВСЕ остатки, в которых лежит промпт, — не дожидаясь, пока
    у каждого подойдёт срок отправки. Иначе после выката линтера уже стоящие в очереди
    строки продолжали бы капать людям ещё часами, по одной раз в 1-3 минуты.
    Возвращает число вычищенных записей."""
    killed = 0
    with database.get_conn() as conn:
        for r in conn.execute("SELECT id, contact_id, campaign_id, account_id, parts_json "
                              "FROM opener_queue").fetchall():
            try:
                parts = json.loads(r["parts_json"]) or []
            except Exception:  # noqa: BLE001 — битый JSON тоже незачем слать
                parts = None
            bad = opener_lint.severe(opener_lint.lint("\n".join(parts))) if parts else None
            if parts and not bad:
                continue
            conn.execute("DELETE FROM opener_queue WHERE id=?", (r["id"],))
            killed += 1
            database.add_event(
                conn, "opener_blocked",
                f"🛑 Из очереди убран остаток опенера с промптом (контакт {r['contact_id']})",
                opener_lint.report(bad) if bad else "нечитаемый parts_json",
                level="warn", contact_id=r["contact_id"], campaign_id=r["campaign_id"],
                account_id=r["account_id"])
    if killed:
        print(f"[purge] вычищено записей очереди с промптом: {killed}")
    return killed


async def tick() -> int:
    database.init_db()
    # чистим ДО отправки: иначе первый же due-строкой уйдёт очередной кусок промпта
    purge_prompt_rows()
    with database.get_conn() as conn:
        due = _due_rows(conn)
    for row in due:
        # Воскресенье — отдых на исход: остаток опенера (третье касание через сутки)
        # это наша инициатива, строка просто полежит в очереди до понедельника.
        if database.is_rest_day(row):
            continue
        # Ночью не шлём: раньше очередь смотрела только на «прошли сутки», и третье
        # касание уходило в 02:07 (ГГКрым, 25.09.2026). Строка ждёт утра в очереди.
        if not (database.in_work_hours(row) and antiban.within_work_hours()):
            continue
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
