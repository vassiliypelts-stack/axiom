"""Авто-генерация bio профиля (антибан: пустой профиль = красный флаг для антиспама).

Делает КОРОТКОЕ человеческое описание (≤70 символов — лимит Telegram bio) под роль/
легенду аккаунта. По умолчанию — через Claude на дешёвой модели (Haiku); если ключа
нет или ошибка — берём из живого шаблонного пула, чтобы фича работала всегда.

Без «следов ИИ»: обычная короткая фраза от первого лица, без хэштегов, канцелярита
и эмодзи-простыни — иначе и человек не поверит, и антиспам флагнет.
"""
from __future__ import annotations

import random

import config

# Резервные «живые» bio на случай отсутствия ключа/ошибки сети. Нейтральные —
# не продающие (рекламное bio само по себе подозрительно для антиспама).
_FALLBACK: dict[str, list[str]] = {
    "sdr": ["на связи по будням", "пишите, отвечу", "обычно отвечаю быстро"],
    "qualifier": ["помогаю разобраться по вопросам", "на связи", "пишите в личку"],
    "closer": ["веду переговоры, на связи", "обсудим — пишите", "открыт к диалогу"],
    "scheduler": ["согласую время встреч", "на связи по расписанию", "пишите по созвонам"],
    "": ["на связи", "пишите в личку", "обычно отвечаю быстро", "тут редко, но отвечаю"],
}


def _fallback(role: str) -> str:
    return random.choice(_FALLBACK.get(role, _FALLBACK[""]))


def generate_bio(role: str | None = None, label: str | None = None,
                 description: str | None = None, city: str | None = None) -> str:
    """Короткое человеческое bio (≤70 симв.) под аккаунт. Никогда не бросает —
    при любой проблеме возвращает осмысленный шаблон."""
    role = (role or "").strip().lower()
    if not config.ANTHROPIC_API_KEY:
        return _fallback(role)
    try:
        import anthropic

        hint = ", ".join(x for x in [
            f"роль: {role}" if role else "",
            f"имя/ярлык: {label}" if label else "",
            f"город: {city}" if city else "",
            f"контекст/легенда: {description}" if description else "",
        ] if x) or "обычный живой человек"
        msg = anthropic.Anthropic().messages.create(
            model=config.AGENT_MODEL,
            max_tokens=60,
            system=(
                "Ты пишешь КОРОТКОЕ человеческое bio для Telegram-профиля, строго до 70 символов. "
                "Живая обычная фраза от первого лица, по-русски, разговорно. БЕЗ хэштегов, БЕЗ "
                "канцелярита, БЕЗ рекламы/продаж, без признаков ИИ, максимум один уместный эмодзи "
                "или вовсе без него. Верни ТОЛЬКО текст bio одной строкой, без кавычек."
            ),
            messages=[{"role": "user", "content": f"Сделай bio. Данные: {hint}"}],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content).strip()
        text = text.strip().strip('"«»').splitlines()[0].strip()
        return text[:70] if text else _fallback(role)
    except Exception:  # noqa: BLE001 — фича не должна падать из-за сети/ключа
        return _fallback(role)
