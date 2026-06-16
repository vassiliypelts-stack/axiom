"""ИИ-агент AXIOM на Claude. Канало-независимый: на вход — история диалога,
на выход — текст ответа + классификация намерения + согласована ли встреча.

Запуск офлайн-теста (нужен ANTHROPIC_API_KEY в .env):
    python -m agent.agent
"""
from __future__ import annotations

import anthropic
from pydantic import BaseModel, Field

import config
from agent.prompts import build_system


class Reply(BaseModel):
    """Структурированный ответ агента (валидируется Claude через structured outputs)."""

    reply: str = Field(description="Текст сообщения, который отправим человеку")
    intent: str = Field(
        description="Намерение собеседника по последней реплике",
        json_schema_extra={"enum": ["positive", "objection", "later", "not_interested", "question", "agreed"]},
    )
    meeting_agreed: bool = Field(description="True, если человек явно согласился на конкретное время")
    proposed_datetime: str | None = Field(description="Согласованный слот (ISO или как в диалоге), иначе null")
    notes: str = Field(description="Короткая заметка для книжки/CRM")


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # читает ANTHROPIC_API_KEY из окружения
    return _client


def generate_reply(history: list[dict], slots: list[str], contact: dict | None = None) -> Reply:
    """history: [{'role': 'user'|'assistant', 'content': str}, ...]
    'user' = входящее от риелтора, 'assistant' = наши прошлые сообщения.
    """
    system = build_system(slots)
    if contact:
        who = ", ".join(f"{k}: {v}" for k, v in contact.items() if v)
        system += f"\n\nЧТО ИЗВЕСТНО О СОБЕСЕДНИКЕ: {who}"

    response = _get_client().messages.parse(
        model=config.MODEL,
        max_tokens=2000,
        thinking={"type": "adaptive"},
        system=system,
        messages=history,
        output_format=Reply,
    )
    return response.parsed_output


def _demo() -> None:
    """Офлайн-симуляция диалога до согласия на Zoom."""
    slots = ["завтра 11:00", "завтра 16:00", "послезавтра 10:00"]
    contact = {"name": "Иван", "city": "Сочи", "agency": "Этажи"}
    history: list[dict] = [{"role": "user", "content": "Привет! Да, я риелтор. Что хотел?"}]

    for _ in range(5):
        r = generate_reply(history, slots, contact)
        print(f"\nAXIOM -> {r.reply}")
        print(f"   [intent={r.intent} | agreed={r.meeting_agreed} | slot={r.proposed_datetime}]")
        if r.meeting_agreed:
            print("\n[OK] Встреча согласована - дальше создаём Zoom + событие в календаре.")
            break
        history.append({"role": "assistant", "content": r.reply})
        human = input("Риелтор -> ")
        history.append({"role": "user", "content": human})


if __name__ == "__main__":
    if not config.ANTHROPIC_API_KEY:
        print("Нет ANTHROPIC_API_KEY в .env — заполни и запусти снова.")
    else:
        _demo()
