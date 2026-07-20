"""AI-обогащение чата: «что это за чат, о чём» — по выборке его сообщений.

Дополняет каталог чатов (Backlog-P0): модель по названию + выборке недавних сообщений
определяет тематику/нишу (короткий тег для группировки) и человекочитаемое описание,
чтобы оператор с одного взгляда понимал, стоит ли работать чат. Дёшево — на runtime-модели
(config.MODEL, по умолчанию Haiku; можно поставить DeepSeek/Gemini — см. agent/llm.py).
Без ключа провайдера — тихо ничего не делает.

Вызывается из channels/chat_scan.py сразу после анализа (там уже есть выборка сообщений)
и из веб-эндпоинта /api/chatcat/{id}/enrich.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

import config
from agent import llm
from db import database


class ChatProfile(BaseModel):
    """Портрет чата для каталога лидгена."""

    topic: str = Field(description="Тематика/ниша 1-3 слова для группировки: недвижимость/IT/"
                                   "маркетинг/крипта/бизнес/… Если непонятно — ''.")
    summary: str = Field(description="1-2 фразы: что это за чат, о чём общаются, кто аудитория.")
    city: str = Field(description="Город, если явно про конкретный город (напр. чат по Сочи). Иначе ''.")
    lead_fit: str = Field(description="Годится ли как источник лидов и почему, кратко: "
                                      "«да — активные покупатели недвижимости» / «нет — флудилка».")
    fit: str = Field(description="Вывод РОВНО одним из трёх слов, без пояснений: "
                                 "'годен' — целевая живая аудитория; "
                                 "'не годен' — флудилка/реклама/не та тема/мертвечина; "
                                 "'не понять' — данных мало, нужен человек.")


SYSTEM = (
    "Ты аналитик отдела лидогенерации. По названию Telegram-чата, его размеру, активности "
    "и выборке сообщений определи тематику, о чём чат и годится ли он как ИСТОЧНИК ЛИДОВ. "
    "Опирайся только на то, что видно. Если данных мало — не выдумывай, оставляй поля пустыми.\n\n"
    "Годность — это НЕ только тема. Чат-источник должен давать кому писать:\n"
    "• меньше ~50 участников ИЛИ ~0-1 сообщений/день → 'не годен' («писать некому»), "
    "даже если тема идеально подходит: мёртвый чат лидов не даст;\n"
    "• тема не та, флудилка, взаимопиар, спам, реклама → 'не годен';\n"
    "• 'годен' — только если тема подходит И чат живой И людей достаточно.\n"
    "В lead_fit коротко объясни причину, называя размер/активность, если дело в них."
)


def classify(title: str | None, sample: list[str], members: int | None = None,
             activity: str | None = None) -> ChatProfile:
    """Одна синхронная классификация. sample — тексты недавних сообщений чата.

    members/activity обязательно передаём в промпт: без них модель судит только по теме
    и уверенно штампует «годен» чату на 14 человек с нулём сообщений (реально ловили —
    60 из 94 «годных» оказались мертвечиной). Тема — не единственный критерий источника.
    """
    body = "\n".join(f"  • {s}" for s in sample[:60] if s)
    facts = (f"Участников: {members if members is not None else 'неизвестно'}\n"
             f"Активность: {activity or 'неизвестно'}\n")
    ctx = (f"Название чата: {title or '—'}\n{facts}\n"
           f"Выборка сообщений ({len(sample)} шт.):\n{body}")
    return llm.structured(
        config.MODEL, system=SYSTEM,
        messages=[{"role": "user", "content": ctx}],
        output_format=ChatProfile, max_tokens=400,
    )


_FIT_TO_VERDICT = {"годен": "годен", "не годен": "не годен"}   # «не понять» → вердикт не ставим

# Порог «есть ли кому писать». Держим в КОДЕ, а не в промпте, сознательно:
# модель факты видит, но охотно перетолковывает их под свой вывод — реальные цитаты
# с прогона: «активный чат (57 участников, ~1 сообщ/день)», «3292 участника,
# ~1 сообщение/день — уместно для ниши». Числа мы знаем ТОЧНО, и решение по ним
# арифметическое — спрашивать о нём LLM незачем. За моделью остаётся то, что она
# правда умеет: понять тему и характер чата.
MIN_MEMBERS = 50        # меньше — это не источник лидов, а переписка
MIN_MSGS_PER_DAY = 2    # ~0-1 сообщений/день — чат мёртв, писать некому


def _msgs_per_day(activity: str | None) -> int | None:
    """«~47 сообщений/день» → 47. Не распарсили → None (не судим)."""
    m = re.match(r"~\s*(\d+)", (activity or "").strip())
    return int(m.group(1)) if m else None


def _why_not_viable(members: int | None, activity: str | None) -> str | None:
    """Причина, по которой чат не может быть источником лидов, или None."""
    if members is not None and members < MIN_MEMBERS:
        return f"всего {members} участников"
    n = _msgs_per_day(activity)
    if n is not None and n < MIN_MSGS_PER_DAY:
        return f"почти нет сообщений ({activity})"
    return None


def save(chat_id: int, p: ChatProfile) -> None:
    """Пишет результат в каталог. topic/city заполняем только если оператор не задал вручную."""
    with database.get_conn() as conn:
        row = conn.execute("SELECT topic, city, verdict, verdict_src, members_count, activity "
                           "FROM chats WHERE id=?", (chat_id,)).fetchone()
        topic = (row["topic"] if row else None) or (p.topic or None)
        city = (row["city"] if row else None) or (p.city or None)
        summary = p.summary
        if p.lead_fit:
            summary = f"{summary}\nЛиды: {p.lead_fit}" if summary else f"Лиды: {p.lead_fit}"
        # ПРЕДВАРИТЕЛЬНЫЙ вердикт от ИИ — только как подсказка человеку. Решение человека
        # (verdict_src='человек') не трогаем никогда: он смотрел глазами, ИИ — нет.
        v = _FIT_TO_VERDICT.get((p.fit or "").strip().lower())
        if v == "годен" and row:
            # Последнее слово — за арифметикой, а не за моделью (см. _why_not_viable).
            why = _why_not_viable(row["members_count"], row["activity"])
            if why:
                v = "не годен"
                summary = f"{summary}\n⚠ Тема подходит, но писать некому: {why}."
        conn.execute(
            "UPDATE chats SET topic=?, city=?, summary=?, enriched_at=datetime('now') WHERE id=?",
            (topic, city, summary, chat_id),
        )
        human_decided = bool(row and row["verdict"] and row["verdict_src"] == "человек")
        if v and not human_decided:
            conn.execute(
                "UPDATE chats SET verdict=?, verdict_src='ai', verdict_at=datetime('now') WHERE id=?",
                (v, chat_id),
            )


def enrich(chat_id: int, title: str | None, sample: list[str],
           members: int | None = None, activity: str | None = None) -> ChatProfile | None:
    """Классифицировать и сохранить. Без ключа или без сырья — None (не ошибка)."""
    if not llm.available(config.MODEL) or not sample:
        return None
    p = classify(title, sample, members, activity)
    save(chat_id, p)
    return p
