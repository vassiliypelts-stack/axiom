"""Планировщик AXIOM: напоминания о встрече и дожим молчунов.

Логика «что пора сделать» — чистая и тестируемая (collect_due), отдельно от
отправки. Отправку инжектируем (send), как провайдеров в checker — поэтому
модуль гоняется без Telethon, а в бою к нему подцепляется TG-клиент.

Что делает:
  • НАПОМИНАНИЕ — за REMINDER_* часов до встречи (deals.meeting_at), один раз.
  • ДОЖИМ — кому отправили, но молчит 24/48 ч: мягкий пинг, максимум 2 раза, потом nurture.
  • НЕДОШЁЛ — встреча прошла, а статус не сдвинулся: предложить новый слот.

Запуск:
    python -m scheduler                 # сухой прогон: показать, что отправилось бы (без отправки)
    python -m scheduler --apply         # пометить в книжке (всё ещё без реальной отправки)
    # боевой режим (с отправкой) подключается из channels/telegram.py — см. run_loop()
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from db import database


def _utcnow() -> datetime:
    """naive-UTC «сейчас» — в одном поясе с datetime('now') из SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ⚠️ ПРАВЬ ПОД СЕБЯ. Плейсхолдеры: {name} {time}.
REMINDER_TEMPLATE = "{name}, напоминаю про созвон сегодня в {time}) ссылку скину перед стартом. на связи?"
FOLLOWUP_TEMPLATES = [
    "{name}, привет) не пропало? я про короткое демо на zoom, 15 минут. глянешь?",
    "{name}, последний раз напомню) если не актуально, просто скажи, не буду дёргать. а если интересно, давай созвонимся минут на 15",
]
NOSHOW_TEMPLATE = "{name}, не получилось созвониться( давай перенесём? когда удобно на этой неделе?"

# Окно напоминания: за сколько часов до встречи и не позже скольки.
REMINDER_BEFORE_HOURS = 3
REMINDER_MIN_HOURS = 2
# Дожим: через сколько часов тишины пинговать. Максимум len(FOLLOWUP_TEMPLATES) раз.
FOLLOWUP_GAP_HOURS = 24
# Недошёл: через сколько часов после времени встречи предлагать перенос.
NOSHOW_AFTER_HOURS = 1
# Как часто крутить tick в боевом режиме.
TICK_INTERVAL_MIN = 15

# Статусы, по которым дожим ещё уместен (ждём ответа от человека).
ACTIVE_STATUSES = ("messaged", "in_dialog")


@dataclass
class Action:
    kind: str            # reminder | followup | noshow
    contact_id: int
    tg_user_id: int | None
    name: str
    text: str
    deal_id: int | None = None
    followup_n: int = 0   # какой по счёту пинг (для followup)


def _parse_dt(s: str | None) -> datetime | None:
    """meeting_at → datetime. Понимает ISO (как заполнит integrations/) и пару
    человеческих форматов. Не распарсил → None (напоминание просто не сработает)."""
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:  # ISO с таймзоной (как пишет integrations) → в naive-UTC
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt in ("%d.%m в %H:%M", "%d.%m %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=_utcnow().year)
            return dt
        except ValueError:
            continue
    return None


def _name(row) -> str:
    return (row["name"] or "").strip()


def _trailing_out_streak(history) -> tuple[int, str | None]:
    """Сколько наших сообщений подряд в хвосте диалога (без ответа) и ts последнего.
    Если хвост — входящее, значит ждём НЕ мы → (0, _)."""
    streak = 0
    last_ts = history[-1]["ts"] if history else None
    for r in reversed(history):
        if r["direction"] == "out":
            streak += 1
        else:
            break
    return streak, last_ts


def collect_due(conn, now: datetime | None = None) -> list[Action]:
    now = now or _utcnow()
    actions: list[Action] = []

    # --- НАПОМИНАНИЯ о встрече ---
    deals = conn.execute(
        "SELECT d.id AS deal_id, d.meeting_at, d.contact_id, c.name, c.tg_user_id "
        "FROM deals d JOIN contacts c ON c.id = d.contact_id "
        "WHERE d.stage = 'meeting_set' AND d.reminder_sent = 0 AND d.meeting_at IS NOT NULL"
    ).fetchall()
    for d in deals:
        dt = _parse_dt(d["meeting_at"])
        if not dt:
            continue
        hours_left = (dt - now).total_seconds() / 3600
        if REMINDER_MIN_HOURS <= hours_left <= REMINDER_BEFORE_HOURS:
            actions.append(Action(
                "reminder", d["contact_id"], d["tg_user_id"], d["name"] or "",
                REMINDER_TEMPLATE.format(name=_name(d), time=f"{dt:%H:%M}"), deal_id=d["deal_id"],
            ))

    # --- НЕДОШЁЛ: встреча прошла, стадия не сдвинулась ---
    overdue = conn.execute(
        "SELECT d.id AS deal_id, d.meeting_at, d.contact_id, c.name, c.tg_user_id "
        "FROM deals d JOIN contacts c ON c.id = d.contact_id "
        "WHERE d.stage = 'meeting_set' AND d.meeting_at IS NOT NULL "
        "AND (d.outcome IS NULL OR d.outcome = '')"
    ).fetchall()
    for d in overdue:
        dt = _parse_dt(d["meeting_at"])
        if dt and (now - dt).total_seconds() / 3600 >= NOSHOW_AFTER_HOURS:
            actions.append(Action(
                "noshow", d["contact_id"], d["tg_user_id"], d["name"] or "",
                NOSHOW_TEMPLATE.format(name=_name(d)), deal_id=d["deal_id"],
            ))

    # --- ДОЖИМ молчунов ---
    contacts = conn.execute(
        f"SELECT * FROM contacts WHERE status IN {ACTIVE_STATUSES}"
    ).fetchall()
    for c in contacts:
        history = database.get_history(conn, c["id"])
        streak, last_ts = _trailing_out_streak(history)
        if streak == 0 or last_ts is None:
            continue  # ждём не мы, либо диалога нет
        if streak > len(FOLLOWUP_TEMPLATES):
            continue  # уже дожали максимум — оставляем планировщику nurture (ниже)
        last_dt = _parse_dt(last_ts)
        if not last_dt:
            continue
        if (now - last_dt).total_seconds() / 3600 >= FOLLOWUP_GAP_HOURS:
            tmpl = FOLLOWUP_TEMPLATES[streak - 1]
            actions.append(Action(
                "followup", c["id"], c["tg_user_id"], c["name"] or "",
                tmpl.format(name=_name(c)), followup_n=streak,
            ))
    return actions


def apply(conn, action: Action) -> None:
    """Отмечает результат в книжке после успешной отправки."""
    if action.kind == "reminder" and action.deal_id:
        conn.execute("UPDATE deals SET reminder_sent = 1 WHERE id = ?", (action.deal_id,))
    elif action.kind == "noshow" and action.deal_id:
        conn.execute("UPDATE deals SET outcome = 'no_show', stage = 'lost' WHERE id = ?", (action.deal_id,))
        database.set_status(conn, action.contact_id, "nurture")
    elif action.kind == "followup":
        # фиксируем сам пинг как исходящее — счётчик дожима = trailing-out streak
        database.add_message(conn, action.contact_id, "out", action.text, intent=None)
        if action.followup_n >= len(FOLLOWUP_TEMPLATES):
            database.set_status(conn, action.contact_id, "nurture")  # дожали максимум


async def tick(send=None) -> int:
    """Один проход: собрать due, отправить (если есть send), отметить. Возвращает число действий.
    send: async callable(Action) -> None. Если None — сухой прогон (печать)."""
    with database.get_conn() as conn:
        actions = collect_due(conn)
    for a in actions:
        if send is not None:
            try:
                await send(a)
            except Exception as e:
                print(f"[send error] {a.kind} contact {a.contact_id}: {e}")
                continue
        else:
            print(f"[DRY] {a.kind} -> {a.name or a.contact_id} (tg={a.tg_user_id}): {a.text}")
        with database.get_conn() as conn:
            apply(conn, a)
    return len(actions)


async def run_loop(send, interval_min: int = TICK_INTERVAL_MIN) -> None:
    """Боевой цикл с APScheduler. send подаёт channels/telegram.py."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sch = AsyncIOScheduler()
    sch.add_job(tick, "interval", minutes=interval_min, args=[send], next_run_time=datetime.now())
    sch.start()
    print(f"Планировщик запущен (каждые {interval_min} мин). Ctrl+C для остановки.")
    await asyncio.Event().wait()  # держим процесс


def _dry_run(apply_changes: bool) -> None:
    database.init_db()
    with database.get_conn() as conn:
        actions = collect_due(conn)
        if not actions:
            print("Нечего делать: нет напоминаний/дожимов на сейчас.")
            return
        for a in actions:
            print(f"[{a.kind}] -> {a.name or a.contact_id}: {a.text}")
            if apply_changes:
                apply(conn, a)
    print(f"\nИтого действий: {len(actions)}" + (" (помечены в книжке)" if apply_changes else " (сухой прогон)"))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Планировщик AXIOM (напоминания + дожим)")
    p.add_argument("--apply", action="store_true", help="пометить в книжке (без реальной отправки)")
    args = p.parse_args()
    _dry_run(args.apply)
