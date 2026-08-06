"""Линтер опенера кампании: в поле «Первое сообщение» должен лежать ТЕКСТ,
который дословно уйдёт человеку, а не промпт/инструкция для ИИ.

Зачем. Первое касание генерит не модель, а campaign_send._parts(): шаблон режется
по строкам, каждая непустая строка = отдельное сообщение в Telegram (остаток
досылает opener_queue раз в 1-3 минуты). Поэтому всё, что оператор или копайлот
написал «про формат» — «# Формат вывода первого сообщения», «ВАЖНО:», «Правила:»,
«- каждая новая строка = отдельное сообщение» — уходит живым людям как сообщения.
Реальный случай (тест кампании «Нетворкинг», 2026-08-02): контакт получил 15 таких
«сообщений» с шагом в 2 минуты.

Две степени тяжести:
  • severe — текст почти наверняка промпт (markdown-заголовки, мета-фразы про формат
    вывода, неизвестные плейсхолдеры). Отправку блокируем, обхода нет: правь текст.
  • soft — подозрительно, но бывает и в живой переписке (длинный список, много строк).
    Отдаём оператору на подтверждение.
"""
from __future__ import annotations

import re

# Больше 8 сообщений подряд в первом касании — это уже не «привет», а портянка:
# opener_queue растянет их на полчаса монолога в чужой личке.
MAX_PARTS = 8
MAX_LINE = 700

# Плейсхолдеры, которые реально подставляет campaign_send._parts(). Всё остальное
# в фигурных скобках уйдёт получателю КАК ЕСТЬ — «{ИМЯ_ОТПРАВИТЕЛЯ}» в личке.
KNOWN_VARS = {"name", "имя", "agency", "агентство", "decision", "sender", "от_кого",
              "greeting", "приветствие",
              # чем человек занимается — строка с ним выпадает, если в карточке пусто
              "спец", "деятельность", "specialization", "специализация"}

# (регулярка, степень, объяснение оператору) — по одной строке шаблона
_LINE_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^\s*#{1,6}\s"), "severe", "markdown-заголовок «#» — разметка промпта"),
    (re.compile(r"^\s*>\s"), "severe", "цитата «>» — разметка промпта"),
    (re.compile(r"\*\*"), "severe", "жирный markdown «**» — в Telegram уйдёт звёздочками"),
    (re.compile(r"^\s*(важно|правила|формат|инструкц\w*|пример\w*|роль|цель|задача|тон|"
                r"шаг\s*\d+|стоп[- ]правила|эскалация)\b[^.!?]*:\s*$", re.I),
     "severe", "строка-заголовок раздела промпта, а не реплика"),
    (re.compile(r"(формат\s+вывода|кажд\w+\s+строка|отдельно\w+\s+сообщени\w+|"
                r"не\s+объединяй|выводи\s+строго|системный\s+промпт|"
                r"ты\s+ведёшь\s+переписку|напиши\s+первое\s+сообщение)", re.I),
     "severe", "мета-инструкция для модели («как выводить»), а не текст человеку"),
    (re.compile(r"^\s*[-*•]\s+"), "soft", "пункт списка «- …» — в личке так не пишут"),
    (re.compile(r"^\s*\d+[.)]\s+"), "soft", "нумерованный пункт — похоже на инструкцию"),
]

_VAR_RE = re.compile(r"\{([^{}]*)\}")


def _bad_vars(line: str) -> list[str]:
    """Плейсхолдеры, которые никто не подставит. Спинтакс {а|б} — не плейсхолдер."""
    out = []
    for raw in _VAR_RE.findall(line):
        if "|" in raw:  # синонимизация, её раскроет _spin()
            continue
        if raw.strip().lower() not in KNOWN_VARS:
            out.append("{" + raw + "}")
    return out


def lint(template: str | None) -> list[dict]:
    """Проблемы шаблона: [{'level': 'severe'|'soft', 'line': N|None, 'text': …, 'why': …}].
    Пустой список — шаблон выглядит как нормальное первое сообщение."""
    text = (template or "").strip()
    if not text:
        return []
    problems: list[dict] = []
    lines = [ln.strip() for ln in text.splitlines()]
    parts = [ln for ln in lines if ln]

    for i, ln in enumerate(lines, start=1):
        if not ln:
            continue
        for rx, level, why in _LINE_RULES:
            if rx.search(ln):
                problems.append({"level": level, "line": i, "text": ln[:120], "why": why})
                break  # одной причины на строку достаточно — не заваливаем оператора
        for v in _bad_vars(ln):
            problems.append({"level": "severe", "line": i, "text": ln[:120],
                             "why": f"плейсхолдер {v} никто не подставит — уйдёт как есть"})
        if len(ln) > MAX_LINE:
            problems.append({"level": "soft", "line": i, "text": ln[:120],
                             "why": f"строка длиннее {MAX_LINE} символов — одним сообщением в личку"})

    if len(parts) > MAX_PARTS:
        problems.append({"level": "soft", "line": None, "text": "",
                         "why": f"{len(parts)} сообщений подряд (лимит {MAX_PARTS}) — "
                                f"opener_queue растянет это на {len(parts) * 2} минут монолога"})
    return problems


def severe(problems: list[dict]) -> list[dict]:
    return [p for p in problems if p["level"] == "severe"]


def report(problems: list[dict], limit: int = 8) -> str:
    """Человеческий текст для оператора/лога."""
    if not problems:
        return ""
    out = []
    for p in problems[:limit]:
        where = f"строка {p['line']}: «{p['text']}» — " if p["line"] else ""
        out.append(f"• {where}{p['why']}")
    if len(problems) > limit:
        out.append(f"• …и ещё {len(problems) - limit}")
    return "\n".join(out)


def blocking_message(problems: list[dict]) -> str:
    """Готовый текст ошибки, если в шаблоне есть severe-проблемы (иначе пусто)."""
    bad = severe(problems)
    if not bad:
        return ""
    return ("В поле «Первое сообщение» лежит промпт, а не текст письма. Каждая строка "
            "этого поля уходит получателю ОТДЕЛЬНЫМ сообщением в Telegram.\n\n"
            + report(bad)
            + "\n\nОставь в поле только сами реплики (то, что человек должен прочитать). "
              "Инструкции для ИИ — в поле «Промпт общения ИИ-агента».")
