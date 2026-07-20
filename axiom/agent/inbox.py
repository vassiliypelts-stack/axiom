"""Инбокс: свободное сообщение → ИИ разбирает и кладёт в нужное место AXIOM.

Василий пишет личному боту как в «Заметки»:
  • «позвонить Ивану завтра в 15:00»          → задача (inbox_items + событие в колокольчик)
  • «новый лид Пётр +79991234567 хочет сайт»  → контакт (contacts через upsert_contact)
  • «Марина из чата готова на встречу»         → заметка к её карточке (contacts.agent_context)

ИИ САМ решает тип (kind). Классификатор — config.MODEL (дёшево, DeepSeek): голоса пока нет,
только текст (STT добавим позже). Транспорт — channels/inbox_bot.py; здесь только «мозг»,
чтобы его можно было проверить без Telegram.

Точка входа: capture(text) -> строка-подтверждение (её бот отправляет обратно).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

import config
from agent import llm
from db import database


class Capture(BaseModel):
    """Разбор одного сообщения. ИИ заполняет только поля своего kind, прочие оставляет пустыми."""

    kind: str = Field(description="Тип РОВНО одним словом: "
                                  "'task' — дело/напоминание себе; "
                                  "'lead' — новый человек/компания-контакт; "
                                  "'note' — заметка про УЖЕ известного человека.")
    summary: str = Field(description="Короткое подтверждение на русском, что понято (1 фраза).")
    # task
    task_text: str = Field(description="Суть задачи (для kind=task), иначе ''.")
    due_at: str = Field(description="Срок задачи как 'YYYY-MM-DD HH:MM' по абсолютной дате, "
                                    "разрешив «завтра/в пятницу/через час» относительно 'Сейчас'. "
                                    "Если срок не назван или kind!=task — ''.")
    # lead
    name: str = Field(description="Имя лида (kind=lead), иначе ''.")
    phone: str = Field(description="Телефон лида как +7… (kind=lead), иначе ''.")
    city: str = Field(description="Город лида, если назван, иначе ''.")
    about: str = Field(description="Чем интересен/что хочет лид (kind=lead), иначе ''.")
    # note
    who: str = Field(description="К кому заметка (kind=note): имя/телефон/@username, иначе ''.")
    note_text: str = Field(description="Текст заметки (kind=note), иначе ''.")


SYSTEM = (
    "Ты — секретарь в CRM для продаж. На вход приходит короткое сообщение от владельца "
    "(он диктует на бегу). Определи, что это, и разложи по полям:\n"
    "• task — поручение/напоминание себе («позвонить», «отправить КП», «не забыть»). "
    "Вытащи срок в абсолютную дату относительно «Сейчас».\n"
    "• lead — впервые упомянут человек/компания как потенциальный клиент (есть имя/телефон/"
    "чем интересен).\n"
    "• note — уточнение про УЖЕ известного человека («Иван передумал», «Марина готова на встречу»).\n"
    "Бери только то, что реально сказано, не выдумывай телефоны и даты. summary — одна фраза."
)


def classify(text: str) -> Capture:
    now = datetime.now(ZoneInfo(config.MEETING_TZ))
    ctx = f"Сейчас: {now:%Y-%m-%d %H:%M} ({now:%A}), таймзона {config.MEETING_TZ}.\n\nСообщение:\n{text}"
    return llm.structured(
        config.MODEL, system=SYSTEM,
        messages=[{"role": "user", "content": ctx}],
        output_format=Capture, max_tokens=400,
    )


def _find_contact(conn, who: str):
    """Ищем уже известного человека по телефону / @username / имени. None — не нашли."""
    who = (who or "").strip()
    if not who:
        return None
    digits = "".join(c for c in who if c.isdigit())
    if len(digits) >= 10:
        r = conn.execute(
            "SELECT id, name FROM contacts WHERE replace(replace(phone,'+',''),' ','') LIKE ?",
            (f"%{digits[-10:]}%",),
        ).fetchone()
        if r:
            return r
    if who.startswith("@"):
        r = conn.execute("SELECT id, name FROM contacts WHERE username=?", (who.lstrip("@"),)).fetchone()
        if r:
            return r
    return conn.execute("SELECT id, name FROM contacts WHERE name LIKE ? ORDER BY id LIMIT 1",
                        (f"%{who}%",)).fetchone()


def apply(cap: Capture, raw: str) -> str:
    """Разобранное сообщение → запись в БД. Возвращает подтверждение для владельца."""
    kind = (cap.kind or "").strip().lower()
    with database.get_conn() as conn:
        if kind == "lead":
            cid = database.upsert_contact(
                conn, source="inbox", name=cap.name or None, phone=cap.phone or None,
                city=cap.city or None, notes=cap.about or None)
            database.add_event(conn, "lead", "📥 Лид из инбокса",
                               f"{cap.name or '—'} {cap.phone or ''} {cap.about or ''}".strip(),
                               level="good", contact_id=cid)
            who = cap.name or cap.phone or "контакт"
            return f"👤 Новый лид: {who}" + (f" · {cap.phone}" if cap.phone else "") + \
                   (f"\n{cap.about}" if cap.about else "")

        if kind == "note":
            row = _find_contact(conn, cap.who)
            note = cap.note_text or raw
            if row:
                conn.execute(
                    "UPDATE contacts SET agent_context = "
                    "TRIM(COALESCE(agent_context,'') || char(10) || ?), updated_at=datetime('now') "
                    "WHERE id=?", (f"[инбокс] {note}", row["id"]))
                database.add_event(conn, "info", "📝 Заметка к лиду", note,
                                   contact_id=row["id"])
                return f"📝 Заметка → {row['name'] or '#'+str(row['id'])}:\n{note}"
            # не нашли к кому — не теряем, кладём как свободную заметку в инбокс
            conn.execute("INSERT INTO inbox_items (kind, text, raw) VALUES ('note',?,?)", (note, raw))
            return f"📝 Заметка сохранена (кому «{cap.who or '?'}» — не нашёл в базе, лежит в инбоксе):\n{note}"

        # по умолчанию — задача (в т.ч. если ИИ вернул незнакомый kind)
        due = cap.due_at or None
        conn.execute("INSERT INTO inbox_items (kind, text, due_at, raw) VALUES ('task',?,?,?)",
                     (cap.task_text or raw, due, raw))
        database.add_event(conn, "info", "🗒 Задача из инбокса",
                           (cap.task_text or raw) + (f"\n⏰ {due}" if due else ""), level="info")
        return f"🗒 Задача: {cap.task_text or raw}" + (f"\n⏰ срок: {due}" if due else "")


def capture(text: str) -> str:
    """Единственная точка входа для бота: разобрать и записать. Возвращает подтверждение."""
    if not llm.available(config.MODEL):
        return "⚠️ ИИ не настроен (нет ключа провайдера) — не могу разобрать сообщение."
    cap = classify(text)
    return apply(cap, text)
