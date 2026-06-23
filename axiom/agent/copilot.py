"""Копайлот визарда запуска кампании (подсказки от Claude по шагам).

Дёшево: по умолчанию Haiku (config.MODEL). Даёт оператору готовые формулировки
цели, описания ЦА, воронки/КЭВ, оффера и промпта ИИ-агента под нишу.
"""
from __future__ import annotations

import anthropic

import config

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


_SYSTEM = (
    "Ты — копайлот AXIOM, помощник по запуску B2B-кампаний лидогенерации через "
    "Telegram/WhatsApp с ИИ-агентами (нейро-SDR). Помогаешь предпринимателю быстро "
    "собрать кампанию по шагам. Отвечай КОНКРЕТНО, по-русски, без воды, готовыми "
    "формулировками, которые можно сразу вставить. Без markdown-заголовков."
)

_STEP_PROMPTS = {
    "goal": "Сформулируй 1-2 чёткие измеримые цели кампании (например: N встреч/КЭВ в месяц).",
    "audience": "Опиши портрет ЦА и где её искать в Telegram/2ГИС (типы чатов, ключевые "
                "запросы, признаки ЛПР). Дай список ключевых слов для поиска и прослушки.",
    "funnel": "Предложи воронку из 4-6 стадий и опиши КЭВ (ключевой этап встречи) — "
              "что это за созвон и какова его цель.",
    "offer": "Сформулируй сильный оффер для этой ЦА: боль → решение → выгода в цифрах → "
             "почему сейчас. Коротко, как для первого касания в личке.",
    "prompt": "Составь промпт для ИИ-агента (характер, тон, как ведёт диалог, как "
              "отрабатывает возражения, как мягко ведёт к созвону/КЭВ). Не обещать лишнего, "
              "не врать, по-человечески.",
}


def suggest(step: str, context: str = "") -> str:
    instr = _STEP_PROMPTS.get(step, "Дай полезную подсказку по этому шагу запуска кампании.")
    user = f"Шаг визарда: {step}.\n{instr}"
    if context.strip():
        user += f"\n\nКонтекст кампании (что уже известно):\n{context.strip()}"
    resp = _get_client().messages.create(
        model=config.MODEL,
        max_tokens=800,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip() or "(пусто)"
