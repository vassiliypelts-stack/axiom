"""ИИ-агент AXIOM на Claude. Канало-независимый: на вход — история диалога,
на выход — текст ответа + классификация намерения + согласована ли встреча.

Запуск офлайн-теста (нужен ANTHROPIC_API_KEY в .env):
    python -m agent.agent
"""
from __future__ import annotations

from pydantic import BaseModel, Field

import config
from agent.prompts import build_system


# Это не «рекомендация модели», а обязательный ответ на подтверждённый интерес.
# Материалы отправляет представитель вручную, поэтому агент не импровизирует и не
# прикрепляет презентацию/бизнес-план сам.
INTEREST_HANDOFF_PARTS = [
    "Спасибо за интерес!",
    "В ближайшее время с вами свяжется участник нашего проекта. Он пришлёт презентацию и бизнес-план, ответит на вопросы.",
    "При необходимости он также запишет вас на онлайн-встречу с основателем проекта.",
]


class Reply(BaseModel):
    """Структурированный ответ агента (валидируется Claude через structured outputs)."""

    reply_parts: list[str] = Field(
        description="1-3 сообщения, как в живой личке. Отправляются по очереди с паузами. "
        "Обычно: короткий отклик или кто ты / суть одним сообщением / один вопрос. Одну "
        "мысль не дроби по предложению, абзацев внутри элемента не делай. Без тире «—», "
        "без «ё», без эмодзи и без извинений.",
        min_length=1,
        # Раньше здесь стояло 4 — при описании поля «1-3». Живой диалог 13.08.2026
        # показал ровно этот разъезд: модель зацепилась за верхнюю границу схемы, а не
        # за текст описания. max_length теперь буквально совпадает с тем, что обещано.
        max_length=3,
    )
    intent: str = Field(
        description="Намерение собеседника по последней реплике",
        json_schema_extra={"enum": ["positive", "objection", "later", "not_interested", "question", "agreed"]},
    )
    meeting_agreed: bool = Field(description="True, если человек явно согласился на конкретное время")
    proposed_datetime: str | None = Field(
        description="Согласованное время созвона в ISO 8601 (YYYY-MM-DDTHH:MM:SS), иначе null. "
        "Сегодняшнюю дату см. в системном промпте: «завтра в 11» и «в пятницу к 16» переводи "
        "в конкретную дату САМ. По этому полю создаётся Zoom-ссылка и напоминание.")
    send_kp: bool = Field(
        default=False,
        description="True ТОЛЬКО если уместно отправить коммерческое предложение (КП) файлом — "
        "например человек просит подробности/презентацию/«скиньте инфо». Если КП не приложено к "
        "кампании — всегда False. Не навязывай файл в холодную.",
    )
    kp_choice: str | None = Field(
        default=None,
        description="Если к кампании приложено НЕСКОЛЬКО КП под разные типы клиентов — название "
        "КП из списка (ТОЧНО как в списке), которое уместно отправить СЕЙЧАС. Нужно отправить "
        "несколько — перечисли названия через запятую, уйдут все по очереди. null — не отправлять "
        "или КП одно/не приложено. Выбирай по типу собеседника; не отправляй в первое касание.",
    )
    voice_choice: str | None = Field(
        default=None,
        description="Если к кампании приложены ГОЛОСОВЫЕ заготовки под разные ситуации — "
        "название той (ТОЧНО как в списке), которую уместно отправить СЕЙЧАС, следом за "
        "текстовым ответом. null — подойдёт любая/выбор не важен, система возьмёт очередную. "
        "Голосовое озвучивает живой человек, оно усиливает доверие — но не отправляй его "
        "в ответ на отказ и не пытайся им заменить ответ по существу.",
    )
    hot: bool = Field(
        default=False,
        description="True — контакт ГОРЯЧИЙ, нужно немедленное личное внимание оператора: "
        "готов созвониться ПРЯМО СЕЙЧАС/в ближайшие часы (а не «на этой неделе»), назвал "
        "конкретную задачу под внедрение, предлагает совместный проект, крупное имя/компания. "
        "Обычная договорённость на конкретное время НЕ hot — для неё достаточно meeting_agreed.",
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
    campaign_id: int | None = None,
    voices: list[dict] | None = None,
) -> Reply:
    """history: [{'role': 'user'|'assistant', 'content': str}, ...]
    'user' = входящее от риелтора, 'assistant' = наши прошлые сообщения.

    history ДОЛЖНА начинаться с реплики 'user' (требование Claude API). В реальном
    канале диалог начинает наше исходящее сообщение — его передавай через `opener`,
    а в history клади только то, что идёт начиная с ответа собеседника.
    """
    # Промпт делится надвое: СНАЧАЛА всё общее для кампании, ПОТОМ персональное.
    # Порядок не косметика — Anthropic кэширует префикс до отметки, поэтому любая
    # персональная строка выше по тексту обнулила бы кэш для всех остальных
    # контактов. Общая часть у нас ~7 тыс. токенов и уходит заново на каждую
    # реплику диалога, так что на ней и держится вся экономия (см. llm.cached).
    system = build_system(slots, campaign_prompt)
    personal = ""
    if contact:
        who = ", ".join(f"{k}: {v}" for k, v in contact.items() if v)
        personal += f"\n\nЧТО ИЗВЕСТНО О СОБЕСЕДНИКЕ: {who}"
    if opener:
        personal += f"\n\nТЫ УЖЕ НАПИСАЛ ЕМУ ПЕРВЫМ (контекст, не повторяйся дословно): {opener}"
    if extra_context and extra_context.strip():
        personal += (
            "\n\nКОНТЕКСТ ОБЩЕНИЯ С ЭТИМ ЧЕЛОВЕКОМ (важно, обязательно учитывай — "
            "вы уже знакомы/общались, опирайся на это, не пиши как в холодную):\n"
            + extra_context.strip()
        )
    # Голосовые заготовки. Список идёт в ПЕРСОНАЛЬНУЮ часть промпта, а не в общую:
    # из него исключено то, что этому человеку уже отправлено, то есть у каждого
    # контакта он свой — в кэшируемом префиксе он ломал бы кэш всей кампании.
    if voices:
        lines = "\n".join(
            f"  • «{v.get('name') or v.get('file')}»"
            + (f" — {v['when_to_use']}" if v.get("when_to_use") else "")
            + (f" (содержание: {v['transcript'][:160]})" if v.get("transcript") else "")
            for v in voices
        )
        personal += (
            "\n\nГОЛОСОВЫЕ ЗАГОТОВКИ (записаны живым голосом, уходят СЛЕДОМ за твоим "
            "текстовым ответом — они его дополняют, а не заменяют):\n" + lines
            + "\nВыбери уместную в voice_choice (название точно как в списке) либо оставь "
            "null. В ответ на отказ голосовое не отправляется."
        )

    # Материалы отправляет только владелец/представитель вручную. Агент не может
    # прикреплять презентации и бизнес-планы: он благодарит за интерес и передаёт
    # лид человеку, который свяжется лично.
    system += (
        "\n\nПРЕЗЕНТАЦИИ И БИЗНЕС-ПЛАНЫ ОТПРАВЛЯЕТ ТОЛЬКО ЧЕЛОВЕК ВРУЧНУЮ. "
        "Никогда не отправляй файлы автоматически: send_kp=false, kp_choice=null."
    )

    # adaptive thinking есть только у Anthropic 4.6+/Opus. На Haiku 4.5 и у чужих
    # провайдеров параметр не передаём (дал бы ошибку и лишний расход). Короткие
    # реплики SDR в нём не нуждаются.
    from agent import llm
    kwargs: dict = {}
    model = config.agent_model(campaign_id)
    if llm.is_anthropic(model) and "haiku" not in model:
        kwargs["thinking"] = {"type": "adaptive"}

    reply = llm.structured(
        model, system=llm.cached(system, personal), messages=history,
        output_format=Reply, max_tokens=1000, **kwargs,
    )
    if reply.intent in ("positive", "agreed"):
        # Фиксированная передача «свяжется участник проекта, пришлёт бизнес-план»
        # написана под Город Гениев. Раньше она подменяла ответ в ЛЮБОЙ кампании:
        # 25.09.2026 директору ремонтной компании на «интересно» ушёл крымский текст
        # про основателя проекта. Остальные кампании ведёт их собственный сценарий.
        if _fixed_handoff(campaign_id):
            reply.reply_parts = INTEREST_HANDOFF_PARTS.copy()
        reply.send_kp = False
        reply.kp_choice = None
        # Интерес должен сразу попасть представителю в личку через notify_hot(),
        # а не затеряться среди обычных событий пульта.
        reply.hot = True
    return reply



# Проекты, где на интерес уходит фиксированный текст передачи (INTEREST_HANDOFF_PARTS).
FIXED_HANDOFF_PROJECTS = {6}   # 6 = Город Гениев


def _fixed_handoff(campaign_id: int | None) -> bool:
    if not campaign_id:
        return False
    from db import database
    with database.get_conn() as conn:
        row = conn.execute("SELECT project_id FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    return bool(row) and row["project_id"] in FIXED_HANDOFF_PROJECTS

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
    if not _llm.available(config.agent_model()):
        print(f"Нет ключа под модель «{config.agent_model()}» в .env — заполни и запусти снова.")
    else:
        _demo()
