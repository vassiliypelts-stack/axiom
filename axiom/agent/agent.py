"""ИИ-агент AXIOM на Claude. Канало-независимый: на вход — история диалога,
на выход — текст ответа + классификация намерения + согласована ли встреча.

Запуск офлайн-теста (нужен ANTHROPIC_API_KEY в .env):
    python -m agent.agent
"""
from __future__ import annotations

from pydantic import BaseModel, Field

import config
from agent.prompts import build_system


class Reply(BaseModel):
    """Структурированный ответ агента (валидируется Claude через structured outputs)."""

    reply_parts: list[str] = Field(
        description="1-3 КОРОТКИХ сообщения, как в живой личке. Отправляются по очереди с паузами. "
        "Дроби мысли: приветствие отдельно, суть отдельно, вопрос отдельно. НЕ одна простыня.",
        min_length=1,
        max_length=4,
    )
    intent: str = Field(
        description="Намерение собеседника по последней реплике",
        json_schema_extra={"enum": ["positive", "objection", "later", "not_interested", "question", "agreed"]},
    )
    meeting_agreed: bool = Field(description="True, если человек явно согласился на конкретное время")
    proposed_datetime: str | None = Field(description="Согласованный слот (ISO или как в диалоге), иначе null")
    send_kp: bool = Field(
        default=False,
        description="True ТОЛЬКО если уместно отправить коммерческое предложение (КП) файлом — "
        "например человек просит подробности/презентацию/«скиньте инфо». Если КП не приложено к "
        "кампании — всегда False. Не навязывай файл в холодную.",
    )
    kp_choice: str | None = Field(
        default=None,
        description="Если к кампании приложено НЕСКОЛЬКО КП под разные типы клиентов — название "
        "КП из списка (ТОЧНО как в списке), которое уместно отправить СЕЙЧАС. null — не отправлять "
        "или КП одно/не приложено. Выбирай по типу собеседника; не отправляй в первое касание.",
    )
    notes: str = Field(description="Короткая заметка для книжки/CRM")


def generate_reply(
    history: list[dict],
    slots: list[str],
    contact: dict | None = None,
    opener: str | None = None,
    campaign_prompt: str | None = None,
    extra_context: str | None = None,
    kp_available: bool = False,
    kps: list[dict] | None = None,
) -> Reply:
    """history: [{'role': 'user'|'assistant', 'content': str}, ...]
    'user' = входящее от риелтора, 'assistant' = наши прошлые сообщения.

    history ДОЛЖНА начинаться с реплики 'user' (требование Claude API). В реальном
    канале диалог начинает наше исходящее сообщение — его передавай через `opener`,
    а в history клади только то, что идёт начиная с ответа собеседника.
    """
    system = build_system(slots, campaign_prompt)
    if contact:
        who = ", ".join(f"{k}: {v}" for k, v in contact.items() if v)
        system += f"\n\nЧТО ИЗВЕСТНО О СОБЕСЕДНИКЕ: {who}"
    if opener:
        system += f"\n\nТЫ УЖЕ НАПИСАЛ ЕМУ ПЕРВЫМ (контекст, не повторяйся дословно): {opener}"
    if extra_context and extra_context.strip():
        system += (
            "\n\nКОНТЕКСТ ОБЩЕНИЯ С ЭТИМ ЧЕЛОВЕКОМ (важно, обязательно учитывай — "
            "вы уже знакомы/общались, опирайся на это, не пиши как в холодную):\n"
            + extra_context.strip()
        )
    if kps:
        lines = []
        for k in kps:
            nm = (k.get("name") or f"КП #{k.get('id')}").strip()
            when = (k.get("when_to_use") or "").strip()
            lines.append(f"- «{nm}» — {when}" if when else f"- «{nm}»")
        system += (
            "\n\nК КАМПАНИИ ПРИЛОЖЕНО НЕСКОЛЬКО КП ПОД РАЗНЫЕ ТИПЫ КЛИЕНТОВ. Определи по собеседнику, "
            "какое уместно. Когда человек просит подробности/презентацию/материалы (или это уместно по "
            "ходу) — выстави kp_choice РОВНО равным названию нужного КП из списка ниже. Если рано или "
            "не уверен — оставь kp_choice=null. Не отправляй в первое касание и не навязывай.\n"
            "Список КП (название — кому подходит):\n" + "\n".join(lines)
        )
    elif kp_available:
        system += (
            "\n\nК ЭТОЙ КАМПАНИИ ПРИЛОЖЕНО КП (файл). Если человек просит подробности/презентацию/"
            "материалы или это уместно по ходу — выстави send_kp=true, файл уйдёт отдельным сообщением. "
            "Не отправляй файл в первое же касание и не навязывай его."
        )
    else:
        system += "\n\nКП файлом НЕ приложено — send_kp всегда оставляй false."

    # adaptive thinking есть только у Anthropic 4.6+/Opus. На Haiku 4.5 и у чужих
    # провайдеров параметр не передаём (дал бы ошибку и лишний расход). Короткие
    # реплики SDR в нём не нуждаются.
    from agent import llm
    kwargs: dict = {}
    if llm.is_anthropic(config.AGENT_MODEL) and "haiku" not in config.AGENT_MODEL:
        kwargs["thinking"] = {"type": "adaptive"}

    return llm.structured(
        config.AGENT_MODEL, system=system, messages=history,
        output_format=Reply, max_tokens=1000, **kwargs,
    )


def _demo() -> None:
    """Офлайн-симуляция диалога до согласия на Zoom."""
    slots = ["завтра 11:00", "завтра 16:00", "послезавтра 10:00"]
    contact = {"name": "Серёга", "city": "Москва"}
    history: list[dict] = [{"role": "user", "content": "о, привет) сто лет не виделись, чем занимаешься?"}]

    for _ in range(5):
        r = generate_reply(history, slots, contact)
        for part in r.reply_parts:
            print(f"\nAXIOM -> {part}")
        print(f"   [intent={r.intent} | agreed={r.meeting_agreed} | slot={r.proposed_datetime}]")
        if r.meeting_agreed:
            print("\n[OK] Встреча согласована - дальше создаём Zoom + событие в календаре.")
            break
        history.append({"role": "assistant", "content": " ".join(r.reply_parts)})
        human = input("Риелтор -> ")
        history.append({"role": "user", "content": human})


if __name__ == "__main__":
    from agent import llm as _llm
    if not _llm.available(config.AGENT_MODEL):
        print(f"Нет ключа под модель «{config.AGENT_MODEL}» в .env — заполни и запусти снова.")
    else:
        _demo()
