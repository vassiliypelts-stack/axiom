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


SYSTEM = (
    "Ты аналитик отдела лидогенерации. По названию Telegram-чата и выборке его сообщений "
    "коротко определи тематику, о чём чат и годится ли он как источник целевых лидов. "
    "Опирайся только на то, что видно. Если данных мало — не выдумывай, оставляй поля пустыми."
)


def classify(title: str | None, sample: list[str]) -> ChatProfile:
    """Одна синхронная классификация. sample — тексты недавних сообщений чата."""
    body = "\n".join(f"  • {s}" for s in sample[:60] if s)
    ctx = f"Название чата: {title or '—'}\n\nВыборка сообщений ({len(sample)} шт.):\n{body}"
    return llm.structured(
        config.MODEL, system=SYSTEM,
        messages=[{"role": "user", "content": ctx}],
        output_format=ChatProfile, max_tokens=400,
    )


def save(chat_id: int, p: ChatProfile) -> None:
    """Пишет результат в каталог. topic/city заполняем только если оператор не задал вручную."""
    with database.get_conn() as conn:
        row = conn.execute("SELECT topic, city FROM chats WHERE id=?", (chat_id,)).fetchone()
        topic = (row["topic"] if row else None) or (p.topic or None)
        city = (row["city"] if row else None) or (p.city or None)
        summary = p.summary
        if p.lead_fit:
            summary = f"{summary}\nЛиды: {p.lead_fit}" if summary else f"Лиды: {p.lead_fit}"
        conn.execute(
            "UPDATE chats SET topic=?, city=?, summary=?, enriched_at=datetime('now') WHERE id=?",
            (topic, city, summary, chat_id),
        )


def enrich(chat_id: int, title: str | None, sample: list[str]) -> ChatProfile | None:
    """Классифицировать и сохранить. Без ключа или без сырья — None (не ошибка)."""
    if not llm.available(config.MODEL) or not sample:
        return None
    p = classify(title, sample)
    save(chat_id, p)
    return p
