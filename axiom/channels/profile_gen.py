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
from agent import llm

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
    if not llm.available(config.AGENT_MODEL):
        return _fallback(role)
    try:
        hint = ", ".join(x for x in [
            f"роль: {role}" if role else "",
            f"имя/ярлык: {label}" if label else "",
            f"город: {city}" if city else "",
            f"контекст/легенда: {description}" if description else "",
        ] if x) or "обычный живой человек"
        # таймаут: SDK по умолчанию ждёт до 600с (10 мин) — при подвисшей сети это
        # молча вешало ВЕСЬ массовый прогон (bio генерится синхронно в цикле по
        # аккаунтам, без него следующие 19 аккаунтов просто не доходили до очереди).
        text = llm.text(
            config.AGENT_MODEL,
            system=(
                "Ты пишешь КОРОТКОЕ человеческое bio для Telegram-профиля, строго до 70 символов. "
                "Живая обычная фраза от первого лица, по-русски, разговорно. БЕЗ хэштегов, БЕЗ "
                "канцелярита, БЕЗ рекламы/продаж, без признаков ИИ, максимум один уместный эмодзи "
                "или вовсе без него. Верни ТОЛЬКО текст bio одной строкой, без кавычек."
            ),
            messages=[{"role": "user", "content": f"Сделай bio. Данные: {hint}"}],
            max_tokens=60, timeout=15.0,
        )
        text = text.strip().strip('"«»').splitlines()[0].strip() if text.strip() else ""
        return text[:70] if text else _fallback(role)
    except Exception:  # noqa: BLE001 — фича не должна падать из-за сети/ключа
        return _fallback(role)


def generate_bio_variants(brief: str | None, count: int = 6, link: str | None = None,
                          gender: str | None = None) -> list[str]:
    """N РАЗНЫХ вариантов bio под бриф оператора — для превью в пульте (оператор
    выбирает удачные до упаковки). Одним запросом к модели (дёшево). Пусто/ошибка —
    отдаём резервный пул, чтобы кнопка всегда что-то показывала.
    link — если задан, дописывается к каждому варианту (напр. ссылка на канал)."""
    count = max(1, min(count, 12))
    brief = (brief or "").strip()
    link = (link or "").strip()
    # запас под ссылку: если она есть, сам текст режем короче, чтобы влезть в 70
    body_limit = 70 - (len(link) + 1) if link else 70
    body_limit = max(20, body_limit)

    if not llm.available(config.AGENT_MODEL):
        base = _FALLBACK[""]
        out = [(v[:body_limit] + (" " + link if link else "")) for v in base]
        return (out * ((count // len(out)) + 1))[:count]
    try:
        g = "" if not gender else (", мужчина" if gender == "male" else ", женщина")
        text = llm.text(
            config.AGENT_MODEL,
            system=(
                f"Ты пишешь КОРОТКИЕ человеческие bio для Telegram-профиля, строго до {body_limit} "
                "символов КАЖДОЕ. Живая обычная фраза от первого лица, по-русски, разговорно. "
                "БЕЗ хэштегов, БЕЗ канцелярита, без признаков ИИ, без кавычек. Вербуй доверие "
                "естественностью, не рекламой. Верни РОВНО "
                f"{count} РАЗНЫХ вариантов, каждый с новой строки, без нумерации и пояснений."
            ),
            messages=[{"role": "user", "content":
                       f"Бриф/легенда аккаунта{g}: {brief or 'обычный живой человек'}.\n"
                       f"Дай {count} разных bio."}],
            max_tokens=400, timeout=30.0,
        )
        lines = [l.strip().strip('"«»-–•').strip() for l in (text or "").splitlines()]
        lines = [l[:body_limit] for l in lines if l and len(l) > 2]
        # дедуп с сохранением порядка
        seen: set[str] = set()
        uniq = [l for l in lines if not (l.lower() in seen or seen.add(l.lower()))]
        if not uniq:
            uniq = _FALLBACK[""][:]
        if link:
            uniq = [f"{l} {link}" for l in uniq]
        return uniq[:count]
    except Exception:  # noqa: BLE001
        base = _FALLBACK[""]
        return [(v + (" " + link if link else "")) for v in base][:count]
