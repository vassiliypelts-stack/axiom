"""Один исследовательский слот внутри прогрева аккаунта.

Аккаунт вступает в назначенную публичную группу, сохраняет за собой членство и
собирает полную карточку. Клиент уже открыт прогревом, поэтому второй сессии и
риска подключения с другого IP здесь нет.
"""
from __future__ import annotations

import json

from channels.chat_scan import scan_one
from channels.chat_join import _record_membership
from db import database
from telethon.tl.functions.channels import JoinChannelRequest


# Заголовки из ручных таблиц иногда приезжают как обычная строка каталога.
# Это не username и не повод десятки раз расходовать слоты прогрева на одну
# и ту же «Ссылку».
_PLACEHOLDER_USERNAMES = {"ссылка", "ссылки", "username", "канал", "чат", "link", "url"}


def _daily_budget(account_id: int) -> int:
    """Стабильная на сутки квота 1--2 вступления для конкретного аккаунта.

    В отличие от random() она не меняется между перезапусками: повторный заход
    не превращает лимит в лотерею и не позволяет случайно выйти за дневной темп.
    """
    with database.get_conn() as conn:
        lo = int(database.get_setting(conn, "research_daily_min", "1") or 1)
        hi = int(database.get_setting(conn, "research_daily_max", "2") or 2)
    # Базовый режим намеренно консервативный: один-два входа выглядят как
    # обычное пользование Telegram. Ускорение выше двух -- отдельное решение,
    # а не скрытая смена темпа при обновлении кода.
    lo, hi = max(1, min(lo, 2)), max(1, min(hi, 2))
    if lo > hi:
        lo, hi = hi, lo
    import hashlib
    token = hashlib.sha256(f"{account_id}:{__import__('datetime').date.today().isoformat()}".encode()).digest()[0]
    return lo + token % (hi - lo + 1)


def _claim(account_id: int) -> dict | None:
    """Атомарно закрепить одну группу, не превышая дневную квоту аккаунта."""
    with database.get_conn() as conn:
        done_today = conn.execute(
            "SELECT COUNT(*) n FROM chat_research_runs WHERE account_id=? "
            "AND date(created_at)=date('now')", (account_id,)
        ).fetchone()["n"]
        if done_today >= _daily_budget(account_id):
            return None
        row = conn.execute(
            "SELECT id, title, username, link FROM chats "
            "WHERE COALESCE(research_status,'new') IN ('new','retry') "
            "AND last_scanned_at IS NULL AND username IS NOT NULL AND username<>'' "
            "AND LOWER(username) NOT IN ('ссылка','ссылки','username','канал','чат','link','url') "
            "AND (verdict IS NULL OR verdict<>'мёртвый') ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        task = dict(row)
        updated = conn.execute(
            "UPDATE chats SET research_status='assigned', research_account_id=?, "
            "research_assigned_at=datetime('now'), research_error=NULL WHERE id=? "
            "AND COALESCE(research_status,'new') IN ('new','retry') AND last_scanned_at IS NULL",
            (account_id, task["id"]),
        )
        if updated.rowcount != 1:
            return None
        cur = conn.execute(
            "INSERT INTO chat_research_runs (chat_id, account_id, status) VALUES (?,?,'assigned')",
            (task["id"], account_id),
        )
        task["run_id"] = cur.lastrowid
        return task


async def _join_and_keep(client, task: dict, account_id: int) -> None:
    """Вступить в публичную группу и записать постоянное членство.

    Не вызываем LeaveChannelRequest: по ТЗ исследованный чат остаётся частью
    естественной ленты аккаунта. Повторный JoinChannelRequest Telegram трактует
    как already participant -- это тоже успешное, уже существующее членство.
    """
    target = task["username"] or task["link"]
    entity = await client.get_entity(target)
    try:
        await client(JoinChannelRequest(entity))
    except Exception as exc:
        low = str(exc).lower()
        if "already" not in low and "participant" not in low:
            raise
    fresh = await client.get_entity(entity)
    from channels.chat_scan import _kind, can_write
    _record_membership(account_id, task, can_write(fresh), _kind(fresh), getattr(fresh, "id", None))


async def run_one(client, account_id: int) -> dict | None:
    """Выполнить одно задание и оставить проверяемую историю результата."""
    task = _claim(account_id)
    if not task:
        return None
    try:
        await _join_and_keep(client, task, account_id)
        # Полный, а не light-скан: нужны админы и нормальная выборка активности
        # для карточки из ТЗ, а не только отметка «ссылка открылась».
        result = await scan_one(client, task["username"] or task["link"], task["id"], light=False)
        with database.get_conn() as conn:
            conn.execute("UPDATE chats SET research_status='done', research_finished_at=datetime('now'), "
                         "research_error=NULL, status='joined' WHERE id=?", (task["id"],))
            conn.execute("UPDATE chat_research_runs SET status='done', result_json=?, "
                         "finished_at=datetime('now') WHERE id=?",
                         (json.dumps(result, ensure_ascii=False), task["run_id"]))
        return {"chat_id": task["id"], "title": task["title"], "status": "done"}
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"[:300]
        # ValueError от get_entity означает, что строка не резолвится как TG-сущность.
        # Повторять её на следующем аккаунте бессмысленно: это битая ссылка/заголовок,
        # а не временный сетевой сбой.
        unavailable = type(exc).__name__ in {"UsernameInvalidError", "UsernameNotOccupiedError", "ValueError"}
        state = "unavailable" if unavailable else "retry"
        with database.get_conn() as conn:
            conn.execute("UPDATE chats SET research_status=?, research_error=? WHERE id=?",
                         (state, reason, task["id"]))
            conn.execute("UPDATE chat_research_runs SET status=?, error=?, finished_at=datetime('now') WHERE id=?",
                         (state, reason, task["run_id"]))
        return {"chat_id": task["id"], "title": task["title"], "status": state, "error": reason}
