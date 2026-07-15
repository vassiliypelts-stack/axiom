"""Сегментация лидов по сферам (модуль №1 Дениса: «автосегментация базы»).

ЗАЧЕМ. Чтобы бить по базе прицельно («риелторам — одно, маркетологам — другое»),
сегмент должен быть ОДНИМ ИЗ ФИКСИРОВАННОГО СПИСКА. Свободный текст от модели
(«Инфобиз; маркетинг; лидогенерация») выглядит умно, но не группируется и не
фильтруется — сегментации из него не выходит. Поэтому здесь общий словарь SEGMENTS,
на него же посажено досье (agent/enrich_person.PersonProfile.segment).

ДВА ПРОХОДА, ДЕШЁВЫЙ СНАЧАЛА:
  1) правила по тегам/заметкам (guess_by_rules) — бесплатно и детерминированно,
     закрывает типовое («Агентства недвижимости» → недвижимость);
  2) модель — только для тех, кого правила не взяли. Контекст: теги, bio, заметки,
     темы чатов, где человек найден. Дёшево (config.MODEL; можно DeepSeek/Gemini).

Досье (enrich_person) для сегмента НЕ требуется: оно есть у единиц (нужны посты),
а сегментировать надо всю базу.

    python -m agent.segment --limit 300           # правила + модель для остатка
    python -m agent.segment --rules-only          # только бесплатные правила
    python -m agent.segment --renorm              # привести старые вольные сегменты к словарю
    python -m agent.segment --stats               # что вообще есть в базе
"""
from __future__ import annotations

import argparse
import json
from typing import Literal

from pydantic import BaseModel, Field

import config
from agent import llm
from db import database

# Канонический словарь сфер. Менять можно, но помни: значения лежат в contacts.segment,
# после правки прогони --renorm, иначе в базе будут вперемешку старые и новые.
SEGMENTS = [
    "недвижимость",
    "IT/разработка",
    "маркетинг/реклама",
    "инфобизнес/обучение",
    "финансы/инвестиции",
    "юристы",
    "медицина/здоровье",
    "красота/бьюти",
    "строительство/ремонт",
    "торговля/e-commerce",
    "услуги/сервис",
    "HR/подбор",
    "туризм/гостеприимство",
    "транспорт/логистика",
    "производство",
    "другое",
]
SegmentName = Literal[
    "недвижимость", "IT/разработка", "маркетинг/реклама", "инфобизнес/обучение",
    "финансы/инвестиции", "юристы", "медицина/здоровье", "красота/бьюти",
    "строительство/ремонт", "торговля/e-commerce", "услуги/сервис", "HR/подбор",
    "туризм/гостеприимство", "транспорт/логистика", "производство", "другое",
]

# Правила: сегмент → слова-маркеры (в нижнем регистре, ищем вхождением).
# Порядок важен — первое совпадение выигрывает, поэтому узкое идёт раньше широкого.
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("недвижимость", ("недвижим", "риелтор", "риэлтор", "агентство недвиж", "новостройк",
                      "квартир", "ипотек", "жк ", "застройщик", "апартамент")),
    ("красота/бьюти", ("бьюти", "салон красоты", "парикмахер", "маникюр", "косметолог",
                       "барбершоп", "эпиляц", "визаж")),
    ("медицина/здоровье", ("стоматолог", "клиник", "медицин", "врач", "стоматологи",
                           "массаж", "здоровье", "фитнес", "нутрициолог")),
    ("юристы", ("юрист", "адвокат", "юридическ", "нотариус")),
    ("финансы/инвестиции", ("финанс", "инвест", "банк", "кредит", "бухгалтер", "трейд",
                            "крипт", "страхов")),
    ("IT/разработка", ("it ", "айти", "разработ", "программист", "python", "веб-студия",
                       "devops", "нейросет", "ии услуги", "ai ", "автоматизац")),
    ("маркетинг/реклама", ("маркет", "реклам", "таргет", "smm", "seo", "контекстолог",
                           "лидоген", "трафик", "продвижен", "директолог")),
    ("инфобизнес/обучение", ("инфобиз", "обучен", "курс", "школа", "образован", "тренинг",
                             "коуч", "наставник", "психолог", "эксперт")),
    ("строительство/ремонт", ("строитель", "ремонт", "отделк", "дизайн интерьер", "прораб")),
    ("HR/подбор", ("hr ", "рекрут", "подбор персонал", "ваканс", "кадров")),
    ("туризм/гостеприимство", ("туризм", "турагент", "отель", "гостиниц", "экскурс",
                               "ресторан", "кафе")),
    ("транспорт/логистика", ("логистик", "грузоперевоз", "доставк", "такси", "транспорт",
                             "автосервис")),
    ("торговля/e-commerce", ("магазин", "маркетплейс", "wildberries", "ozon", "торгов",
                             "e-commerce", "опт ")),
    ("производство", ("производств", "завод", "фабрик", "цех ")),
]


class SegmentGuess(BaseModel):
    """Сегмент лида из фиксированного словаря."""

    segment: SegmentName = Field(description="Сфера деятельности человека — РОВНО одно "
                                             "значение из списка. Не уверен → «другое».")
    confidence: float = Field(description="Достоверность 0.0..1.0: мало данных → ниже.",
                              ge=0, le=1)


SYSTEM = (
    "Ты сегментируешь базу лидов по сфере деятельности человека. По тегам, bio, заметкам "
    "и темам чатов, где он найден, определи его сферу — СТРОГО одно значение из списка: "
    + ", ".join(SEGMENTS) + ". "
    "Смотри, чем человек ЗАНИМАЕТСЯ (его профессия/бизнес), а не о чём он однажды писал. "
    "Данных мало или разнородно — ставь «другое» и низкий confidence, не угадывай."
)


def guess_by_rules(text: str | None) -> str | None:
    """Сегмент по словам-маркерам. None = правила не сработали, нужен вызов модели."""
    if not text:
        return None
    low = text.lower()
    for seg, words in RULES:
        if any(w in low for w in words):
            return seg
    return None


def _chat_topics(conn, tg_user_id: int | None) -> str:
    """Темы чатов, где человек засветился — сильный сигнал о сфере."""
    if not tg_user_id:
        return ""
    rows = conn.execute(
        "SELECT DISTINCT COALESCE(c.topic, p.chat_title) t FROM tg_user_posts p "
        "LEFT JOIN chats c ON c.tg_chat_id = p.chat_id WHERE p.tg_user_id=? LIMIT 6",
        (tg_user_id,),
    ).fetchall()
    return "; ".join(r["t"] for r in rows if r["t"])


def _context(conn, c: dict) -> str:
    parts = [
        f"Имя: {c.get('name') or '-'}",
        f"Теги: {c.get('tags') or '-'}",
        f"Bio: {c.get('bio') or '-'}",
        f"Город: {c.get('city') or '-'}",
        f"Заметки: {(c.get('notes') or '-')[:300]}",
    ]
    topics = _chat_topics(conn, c.get("tg_user_id"))
    if topics:
        parts.append(f"Темы чатов, где найден: {topics}")
    return "\n".join(parts)


def classify(ctx: str) -> SegmentGuess:
    return llm.structured(config.MODEL, system=SYSTEM,
                          messages=[{"role": "user", "content": ctx}],
                          output_format=SegmentGuess, max_tokens=120)


def _save(conn, cid: int, seg: str) -> None:
    conn.execute("UPDATE contacts SET segment=?, updated_at=datetime('now') WHERE id=?", (seg, cid))


def _targets(conn, limit: int, renorm: bool) -> list[dict]:
    """Кого сегментировать: без сегмента, либо (при --renorm) со старым вольным значением."""
    if renorm:
        ph = ",".join("?" * len(SEGMENTS))
        rows = conn.execute(
            f"SELECT * FROM contacts WHERE segment IS NOT NULL AND segment<>'' "
            f"AND segment NOT IN ({ph}) ORDER BY id LIMIT ?", (*SEGMENTS, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM contacts WHERE segment IS NULL OR segment='' ORDER BY id LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def _breakdown() -> list[dict]:
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(segment,''),'(нет)') s, COUNT(*) n FROM contacts "
            "GROUP BY s ORDER BY n DESC"
        ).fetchall()
    return [{"segment": r["s"], "n": r["n"]} for r in rows]


def stats() -> None:
    with database.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    print(f"Сегменты по базе ({total} контактов):")
    for r in _breakdown():
        mark = "" if r["segment"] in SEGMENTS or r["segment"] == "(нет)" else "  ← не из словаря"
        print(f"  {r['n']:6}  {r['segment']}{mark}")


def _summary(**kw) -> None:
    """Последняя строка stdout — JSON-сводка (контракт web/app.py:_last_json)."""
    print(json.dumps({"ok": True, "top": _breakdown()[:6], **kw}, ensure_ascii=False))


def run(limit: int, rules_only: bool, renorm: bool) -> None:
    database.init_db()
    with database.get_conn() as conn:
        rows = _targets(conn, limit, renorm)
    if not rows:
        print("нечего сегментировать")
        _summary(by_rules=0, by_llm=0, failed=0, note="нечего сегментировать")
        return
    print(f"сегментирую {len(rows)} контакт(ов){' (только правила)' if rules_only else ''}…")

    by_rules = by_llm = failed = 0
    left: list[dict] = []
    with database.get_conn() as conn:
        for c in rows:
            # при renorm старое вольное значение — тоже подсказка для правил
            src = " ".join(x for x in [c.get("tags"), c.get("notes"), c.get("bio"),
                                       c.get("segment") if renorm else None] if x)
            seg = guess_by_rules(src)
            if seg:
                _save(conn, c["id"], seg)
                by_rules += 1
            else:
                left.append(c)
    print(f"  правилами: {by_rules}, осталось на модель: {len(left)}")

    if rules_only or not left:
        stats()
        _summary(by_rules=by_rules, by_llm=0, failed=0, left=len(left))
        return
    if not llm.available(config.MODEL):
        print(f"[инфо] нет ключа под «{config.MODEL}» — остаток без сегмента, "
              f"поставь ключ и прогони снова")
        stats()
        _summary(by_rules=by_rules, by_llm=0, failed=0, left=len(left),
                 note=f"нет ключа под «{config.MODEL}» — остаток не сегментирован")
        return

    for c in left:
        try:
            with database.get_conn() as conn:
                ctx = _context(conn, c)
            g = classify(ctx)
            with database.get_conn() as conn:
                _save(conn, c["id"], g.segment)
            by_llm += 1
            print(f"[ok #{c['id']}] {(c.get('name') or '')[:28]:28} → {g.segment} ({g.confidence:.2f})")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[fail #{c['id']}] {e}")
    print(f"\nготово: правилами {by_rules}, моделью {by_llm}, ошибок {failed}")
    stats()
    _summary(by_rules=by_rules, by_llm=by_llm, failed=failed)


def main() -> None:
    p = argparse.ArgumentParser(description="AXIOM: сегментация лидов по сферам")
    p.add_argument("--limit", type=int, default=300, help="сколько контактов за прогон")
    p.add_argument("--rules-only", action="store_true", help="только бесплатные правила, без модели")
    p.add_argument("--renorm", action="store_true",
                   help="привести старые вольные сегменты к словарю (иначе берём только пустые)")
    p.add_argument("--stats", action="store_true", help="показать разбивку по сегментам и выйти")
    args = p.parse_args()
    if args.stats:
        stats()
        return
    run(args.limit, args.rules_only, args.renorm)


if __name__ == "__main__":
    main()
