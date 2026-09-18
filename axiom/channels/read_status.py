"""Прочитал ли человек наше сообщение (галочки Telegram) → messages.read_at.

ЗАЧЕМ. Воронка кампании обрывалась на «отправлено»: в списке рассылки было видно,
что письмо ушло, но не видно, что с ним стало дальше. А «прочитал и молчит» и «не
открывал вовсе» — это две разные болезни с разным лечением:
  • прочитал, но не ответил → текст не цепляет, надо менять оффер/первую строку;
  • не прочитал вообще      → письмо не доходит до глаз: аккаунт помечен спамом,
                              человек неактивен или скрыл нас — надо менять
                              отправителя, а не текст.
Без этой разницы оператор правит текст, когда проблема в аккаунте, и наоборот.

ЧТО ТАКОЕ «ДОСТАВЛЕНО» В TELEGRAM. Отдельного статуса доставки, как в WhatsApp,
здесь нет: если send_message вернул id, сообщение уже лежит в диалоге собеседника.
Поэтому delivered_at ставится в момент отправки (db.add_message), а этот модуль
занимается только прочтением.

КАК УЗНАЁМ ПРОЧТЕНИЕ. Telegram по диалогу отдаёт read_outbox_max_id — номер
последнего НАШЕГО сообщения, которое собеседник дочитал. Всё, что с меньшим или
равным id, прочитано. Один запрос GetPeerDialogs накрывает до 100 диалогов разом,
поэтому на сотню контактов уходит один вызов, а не сотня.

⚠️ СВОИ СЕССИИ НЕ ПОДНИМАЕМ. Клиент берётся у слушателя (listener.CLIENTS), как в
send_via_listener: одна сессия в двух процессах — это AuthKeyDuplicatedError, так
уже сгорела пачка оплаченных аккаунтов. Если слушатель аккаунт не держит — просто
пропускаем его до следующего прогона, это не ошибка.

Запуск:
    python -m channels.read_status              # все кампании, свежие сутки
    python -m channels.read_status --days 7     # заглянуть глубже
    python -m channels.read_status --campaign 9407
"""
from __future__ import annotations

import argparse
import asyncio

from db import database


# Сколько диалогов спрашиваем одним GetPeerDialogs. Ограничение Telegram — 100.
_BATCH = 100


def _pending(conn, days: int, campaign_id: int | None) -> list[dict]:
    """Наши отправленные сообщения, про которые ещё не знаем, прочитаны ли.

    Берём только исходящие с известным tg_msg_id (без него нечего сопоставлять) и
    только у контактов с tg_user_id — диалог адресуется именно по нему.
    """
    where = ["m.direction='out'", "m.read_at IS NULL",
             "m.tg_msg_id IS NOT NULL AND m.tg_msg_id<>''",
             "c.tg_user_id IS NOT NULL",
             f"m.ts >= datetime('now','-{int(days)} day')"]
    args: list = []
    if campaign_id:
        where.append("m.contact_id IN (SELECT contact_id FROM campaign_contacts "
                     "WHERE campaign_id=?)")
        args.append(int(campaign_id))
    rows = conn.execute(
        "SELECT m.id, m.contact_id, m.account_id, m.tg_msg_id, c.tg_user_id "
        "FROM messages m JOIN contacts c ON c.id=m.contact_id "
        f"WHERE {' AND '.join(where)} ORDER BY m.account_id, m.contact_id", args
    ).fetchall()
    return [dict(r) for r in rows]


def _max_msg_id(raw: str | None) -> int:
    """Наибольший id из «12,13,14» — сообщение пишется блоком в несколько реплик."""
    best = 0
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            best = max(best, int(part))
    return best


async def _read_marks(client, user_ids: list[int]) -> dict[int, int]:
    """{tg_user_id: read_outbox_max_id} для пачки диалогов одним запросом."""
    from telethon.tl.functions.messages import GetPeerDialogsRequest

    out: dict[int, int] = {}
    for i in range(0, len(user_ids), _BATCH):
        chunk = user_ids[i:i + _BATCH]
        try:
            res = await client(GetPeerDialogsRequest(peers=chunk))
        except Exception as e:  # noqa: BLE001 — один аккаунт не рушит прогон
            print(f"  [dialogs] {e}")
            continue
        for d in getattr(res, "dialogs", []):
            peer = getattr(d, "peer", None)
            uid = getattr(peer, "user_id", None)
            if uid is not None:
                out[int(uid)] = int(getattr(d, "read_outbox_max_id", 0) or 0)
    return out


def run(days: int = 1, campaign_id: int | None = None) -> dict:
    """Проставить read_at там, где собеседник уже дочитал. Возвращает сводку."""
    from channels import listener

    with database.get_conn() as conn:
        pending = _pending(conn, days, campaign_id)
    if not pending:
        return {"checked": 0, "read": 0, "skipped_no_client": 0}

    # Группируем по аккаунту: прочтение видно только тому, кто отправлял.
    by_acc: dict[int, list[dict]] = {}
    for row in pending:
        acc = row.get("account_id")
        if acc:
            by_acc.setdefault(int(acc), []).append(row)

    loop = listener._LOOP
    marked = 0
    skipped = 0
    for acc_id, rows in by_acc.items():
        client = listener.CLIENTS.get(acc_id)
        if loop is None or client is None:
            # Слушатель этот аккаунт сейчас не держит — свой клиент поднимать НЕЛЬЗЯ
            # (AuthKeyDuplicated сжигает сессию). Подождём следующего прогона.
            skipped += len(rows)
            continue
        uids = sorted({int(r["tg_user_id"]) for r in rows})
        fut = asyncio.run_coroutine_threadsafe(_read_marks(client, uids), loop)
        try:
            marks = fut.result(timeout=120)
        except Exception as e:  # noqa: BLE001
            print(f"  [#{acc_id}] прочтения не спросились: {e}")
            skipped += len(rows)
            continue
        hits = [r["id"] for r in rows
                if _max_msg_id(r["tg_msg_id"]) <= marks.get(int(r["tg_user_id"]), 0)
                and marks.get(int(r["tg_user_id"]), 0) > 0]
        if hits:
            with database.get_conn() as conn:
                qm = ",".join("?" * len(hits))
                conn.execute(
                    f"UPDATE messages SET read_at=datetime('now') WHERE id IN ({qm})", hits)
            marked += len(hits)
    return {"checked": len(pending), "read": marked, "skipped_no_client": skipped}


def main() -> None:
    ap = argparse.ArgumentParser(description="Отметить прочитанные наши сообщения")
    ap.add_argument("--days", type=int, default=1, help="как глубоко смотреть (по умолчанию сутки)")
    ap.add_argument("--campaign", type=int, default=None, help="только эта кампания")
    a = ap.parse_args()
    res = run(days=a.days, campaign_id=a.campaign)
    print(f"проверено {res['checked']}, прочитано {res['read']}, "
          f"пропущено (аккаунт не в слушателе) {res['skipped_no_client']}")


if __name__ == "__main__":
    main()
