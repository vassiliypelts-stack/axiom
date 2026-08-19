"""Планировщик AXIOM: напоминания о встрече и дожим молчунов.

Логика «что пора сделать» — чистая и тестируемая (collect_due), отдельно от
отправки. Отправку инжектируем (send), как провайдеров в checker — поэтому
модуль гоняется без Telethon, а в бою к нему подцепляется TG-клиент.

Что делает:
  • НАПОМИНАНИЕ — за REMINDER_* часов до встречи (deals.meeting_at), один раз.
  • ДОЖИМ — кому отправили, но молчит 24/48 ч: мягкий пинг из FOLLOWUP_TEMPLATES
    (общий на все кампании), максимум len(FOLLOWUP_TEMPLATES) раз, потом nurture.
    Кампания может добавить СВОЙ третий шаг (campaigns.extra_followup_template) —
    оффер у каждой кампании свой, а общий список один, поэтому доп. шаг привязан
    именно к кампании, которая сейчас ведёт контакт (см. _campaign_extra_followup).
  • НЕДОШЁЛ — встреча прошла, а статус не сдвинулся: предложить новый слот.
  • СТОРОЖ (check_stuck_replies) — контакту пришло входящее, агент не ответил дольше
    STUCK_REPLY_HOURS. Ничего не шлёт контакту, только тревога в колокольчик — ловит
    ЛЮБУЮ причину молчания (упавший listener, баг в обработке, что угодно ещё не
    найденное), не только те, что уже нашли и починили. См. её докстринг.

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

from channels import antiban
from db import database


def _utcnow() -> datetime:
    """naive-UTC «сейчас» — в одном поясе с datetime('now') из SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ⚠️ ПРАВЬ ПОД СЕБЯ. Плейсхолдеры: {name} {time}.
# {link} подставляется ссылкой на созвон, если она есть у сделки. Раньше шаблон обещал
# «скину перед стартом», но не слал НИЧЕГО: ссылка уходила клиенту в момент
# договорённости и к началу тонула в переписке. Теперь напоминание и есть тот момент,
# когда ссылка нужна. Сделки без ссылки (звонок по телефону) остаются с прежним текстом.
REMINDER_TEMPLATE = "{name}, напоминаю про созвон сегодня в {time}) на связи?{link}"
FOLLOWUP_TEMPLATES = [
    "{name}, привет) не пропало? я про короткое демо на zoom, 15 минут. глянешь?",
    "{name}, последний раз напомню) если не актуально, просто скажи, не буду дёргать. а если интересно, давай созвонимся минут на 15",
]
NOSHOW_TEMPLATE = "{name}, не получилось созвониться( давай перенесём? когда удобно на этой неделе?"

# Окно напоминания: за сколько часов до встречи и не позже скольки. Целимся В ЧАС до
# старта — именно тогда ссылка нужнее всего: раньше она теряется в переписке, позже
# человек уже не успевает перестроить планы. Окно шире точки (тик раз в 15 минут),
# иначе напоминание проскакивало бы мимо.
REMINDER_BEFORE_HOURS = 1.5
REMINDER_MIN_HOURS = 0.5
# Дожим: через сколько часов тишины пинговать. Максимум len(FOLLOWUP_TEMPLATES) раз.
FOLLOWUP_GAP_HOURS = 24
# Недошёл: через сколько часов после времени встречи предлагать перенос.
NOSHOW_AFTER_HOURS = 1
# Как часто крутить tick в боевом режиме.
TICK_INTERVAL_MIN = 15
# Сторож: сколько часов молчания агента после входящего считать подвисшим.
# Час — заметно дольше нормального REPLY_DELAY (30-60с), но не настолько долго,
# чтобы живой лид успел остыть и уйти, пока никто не заметил тишину.
STUCK_REPLY_HOURS = 1.0

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
    followup_n: int = 0    # какой по счёту пинг (для followup)
    followup_max: int = 0  # сколько пингов допустимо ИМЕННО для этого контакта
                            # (глобальный список + доп. шаг кампании, если задан)


def _parse_dt(s: str | None) -> datetime | None:
    """meeting_at → naive-UTC datetime. Не распарсил → None (напоминание не сработает).

    Разбор живой речи («завтра в 11») — общий с integrations/slot_parse, тем же кодом
    читает время и оркестратор встречи. Локальные формулировки считаем в MEETING_TZ и
    приводим к naive-UTC: в этом виде здесь живут все сравнения со временем."""
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
    from zoneinfo import ZoneInfo

    import config
    from integrations import slot_parse
    tz = ZoneInfo(config.MEETING_TZ)
    dt = slot_parse.parse_human(s, datetime.now(tz))
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt else None


def _local_hhmm(dt: datetime) -> str:
    """Время встречи ЧЕЛОВЕКУ — в MEETING_TZ. Внутри планировщика всё живёт в naive-UTC,
    и напоминание уходило клиенту с этим самым UTC: созвон в 11:00 по Москве
    превращался в «напоминаю про созвон сегодня в 08:00»."""
    from zoneinfo import ZoneInfo

    import config
    return f"{dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(config.MEETING_TZ)):%H:%M}"


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


def _campaign_extra_followup(conn, contact_id: int) -> str | None:
    """Доп. шаблон дожима (шаг сверх FOLLOWUP_TEMPLATES) кампании, которая СЕЙЧАС
    ведёт этот контакт. «Сейчас ведёт» — последняя запись в campaign_contacts:
    тестовые/повторно используемые номера успевают побывать в нескольких
    кампаниях, и брать нужно не первую попавшуюся, а самую свежую."""
    row = conn.execute(
        "SELECT c.extra_followup_template AS t FROM campaign_contacts cc "
        "JOIN campaigns c ON c.id = cc.campaign_id "
        # ORDER BY по sent_at, а НЕ по cc.id: в campaign_contacts столбца id нет вовсе
        # (ключ — пара campaign_id+contact_id). Запрос падал с «no such column: cc.id»,
        # и падал он ВНУТРИ collect_due — то есть весь тик планировщика умирал целиком:
        # ни дожима молчунов, ни напоминаний о созвоне, ни «не дошёл» не отправлялось
        # НИ РАЗУ. Наружу это выглядело как «дожим просто не настроен».
        "WHERE cc.contact_id = ? ORDER BY cc.sent_at DESC LIMIT 1", (contact_id,)
    ).fetchone()
    t = (row["t"] or "").strip() if row else ""
    return t or None


def collect_due(conn, now: datetime | None = None) -> list[Action]:
    now = now or _utcnow()
    actions: list[Action] = []

    # --- НАПОМИНАНИЯ о встрече ---
    deals = conn.execute(
        "SELECT d.id AS deal_id, d.meeting_at, d.contact_id, d.zoom_link, c.name, c.tg_user_id "
        "FROM deals d JOIN contacts c ON c.id = d.contact_id "
        "WHERE d.stage = 'meeting_set' AND d.reminder_sent = 0 AND d.meeting_at IS NOT NULL"
    ).fetchall()
    for d in deals:
        dt = _parse_dt(d["meeting_at"])
        if not dt:
            continue
        hours_left = (dt - now).total_seconds() / 3600
        if REMINDER_MIN_HOURS <= hours_left <= REMINDER_BEFORE_HOURS:
            link = (d["zoom_link"] or "").strip()
            actions.append(Action(
                "reminder", d["contact_id"], d["tg_user_id"], d["name"] or "",
                REMINDER_TEMPLATE.format(name=_name(d), time=_local_hhmm(dt),
                                         link=f"\n\nссылка: {link}" if link else ""),
                deal_id=d["deal_id"],
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
        extra = _campaign_extra_followup(conn, c["id"])
        templates = FOLLOWUP_TEMPLATES + ([extra] if extra else [])
        if streak > len(templates):
            continue  # уже дожали максимум — оставляем планировщику nurture (ниже)
        last_dt = _parse_dt(last_ts)
        if not last_dt:
            continue
        if (now - last_dt).total_seconds() / 3600 >= FOLLOWUP_GAP_HOURS:
            tmpl = templates[streak - 1]
            actions.append(Action(
                "followup", c["id"], c["tg_user_id"], c["name"] or "",
                tmpl.format(name=_name(c)), followup_n=streak, followup_max=len(templates),
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
        cap = action.followup_max or len(FOLLOWUP_TEMPLATES)
        if action.followup_n >= cap:
            database.set_status(conn, action.contact_id, "nurture")  # дожали максимум


def check_stuck_replies(conn, now: datetime | None = None) -> int:
    """Сторож: контакту пришло входящее, а мы не ответили дольше STUCK_REPLY_HOURS.

    ЗАЧЕМ. 12-13.08.2026 агент дважды промолчал живым тестовым лидам подряд — один
    раз слушатель пропустил ответ мимо рестарта сервиса, второй раз голосовое
    сообщение падало в необрабатываемую ветку. Обе причины нашли и починили
    (channels/listener.py), но узнали о них СЛУЧАЙНО — оператор сам заметил в своём
    Telegram, что бот не отвечает. Причина завтра может быть третьей, ещё не
    найденной. Эта функция не чинит причины — она делает так, чтобы молчание было
    ВИДНО в колокольчике, какой бы ни была причина, включая ещё не открытые баги.

    Ничего не шлёт контакту (в отличие от дожима/reminder/noshow выше) — только
    пишет тревогу оператору. Ночью (antiban.within_work_hours()=False) не бьём:
    задержка ответа ночью сделана намеренно (см. listener._handle_private, «ночью
    живым людям не пишем»), это не сбой, и будить оператора этим не нужно.

    Не спамит на каждый tick: если тревога по этому же входящему уже стоит в
    колокольчике — молчит, пока не появится новое (более позднее) входящее.
    """
    if not antiban.within_work_hours(now):
        return 0
    now = now or _utcnow()
    fired = 0
    contacts = conn.execute(
        f"SELECT id, name FROM contacts WHERE status IN {ACTIVE_STATUSES}"
    ).fetchall()
    for c in contacts:
        history = database.get_history(conn, c["id"])
        if not history or history[-1]["direction"] != "in":
            continue  # ждём не мы — либо диалога нет, либо последний ход наш
        last_ts = history[-1]["ts"]
        last_dt = _parse_dt(last_ts)
        if not last_dt or (now - last_dt).total_seconds() / 3600 < STUCK_REPLY_HOURS:
            continue
        already = conn.execute(
            "SELECT 1 FROM events WHERE contact_id=? AND type='stuck_reply' AND ts >= ? LIMIT 1",
            (c["id"], last_ts),
        ).fetchone()
        if already:
            continue  # по ЭТОМУ входящему уже сообщили, не дублируем каждые 15 минут
        mins = int((now - last_dt).total_seconds() // 60)
        database.add_event(
            conn, "stuck_reply", f"⏳ Не отвечено вовремя: {c['name'] or ('#' + str(c['id']))}",
            f"Написал(а) {mins} мин назад, ответа от агента до сих пор нет. Причина может "
            f"быть любая (слушатель отвалился, ошибка модели, ещё не найденный баг) — "
            f"открой карточку и посмотри диалог.",
            level="bad", contact_id=c["id"],
        )
        fired += 1
    return fired


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
    with database.get_conn() as conn:
        stuck = check_stuck_replies(conn)
        if stuck:
            print(f"[сторож] новых тревог «не отвечено вовремя»: {stuck}")
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
