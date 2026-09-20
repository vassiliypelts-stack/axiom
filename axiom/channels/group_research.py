"""Один read-only исследовательский слот внутри прогрева аккаунта.

Не вступает в чаты и не отправляет сообщений. Использует уже открытый клиент
прогрева, поэтому не создаёт второе подключение Telegram-сессии.
"""
from __future__ import annotations

import json

from channels.chat_scan import scan_one
from db import database


def _claim(account_id: int) -> dict | None:
    """Атомарно закрепить один неначатый публичный чат за аккаунтом."""
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, username, link FROM chats "
            "WHERE COALESCE(research_status,'new') IN ('new','retry') "
            "AND last_scanned_at IS NULL AND username IS NOT NULL AND username<>'' "
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


async def run_one(client, account_id: int) -> dict | None:
    """Выполнить одно задание и оставить проверяемую историю результата."""
    task = _claim(account_id)
    if not task:
        return None
    try:
        result = await scan_one(client, task["username"] or task["link"], task["id"], light=True)
        with database.get_conn() as conn:
            conn.execute("UPDATE chats SET research_status='done', research_finished_at=datetime('now'), "
                         "research_error=NULL WHERE id=?", (task["id"],))
            conn.execute("UPDATE chat_research_runs SET status='done', result_json=?, "
                         "finished_at=datetime('now') WHERE id=?",
                         (json.dumps(result, ensure_ascii=False), task["run_id"]))
        return {"chat_id": task["id"], "title": task["title"], "status": "done"}
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"[:300]
        unavailable = type(exc).__name__ in {"UsernameInvalidError", "UsernameNotOccupiedError"}
        state = "unavailable" if unavailable else "retry"
        with database.get_conn() as conn:
            conn.execute("UPDATE chats SET research_status=?, research_error=? WHERE id=?",
                         (state, reason, task["id"]))
            conn.execute("UPDATE chat_research_runs SET status=?, error=?, finished_at=datetime('now') WHERE id=?",
                         (state, reason, task["run_id"]))
        return {"chat_id": task["id"], "title": task["title"], "status": state, "error": reason}
