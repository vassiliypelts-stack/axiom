"""Русское ФИО → вежливое обращение («Имя Отчество»).

Зачем отдельный модуль. 07.08.2026 живому лиду ушло «Викторович Комиссаренко,
добрый вечер». В карточке лежало «Николай Викторович Комиссаренко», а обращение
собиралось правилом «взять второе и третье слово» — оно верно ровно для одного
порядка ФИО. А порядков в базе два: ручной импорт и парсеры дают «Имя Отчество
Фамилия», обогащение из ЕГРЮЛ/досье — «Фамилия Имя Отчество». На половине базы
правило давало «Отчество Фамилия» — обращение, которым в жизни не пользуется
никто, и первое же сообщение читается как рассылка робота.

Поэтому позицию НЕ угадываем. Ищем отчество по суффиксу («-ович/-евич/-овна/…»):
слово рядом с ним и есть имя, остаток — фамилия. По фамилии в личной переписке не
обращаются, поэтому в обращение она не идёт вообще.

Отдельная ловушка — фамилии, неотличимые по суффиксу от отчеств: «Ольга
Парханович», «Слободан Милошевич». Их отсеиваем согласованием по полу: женское
имя с мужским отчеством не сочетается. Пол берём только когда он ОЧЕВИДЕН (имя из
пула ru_names либо кончается на «-а/-я»); для нерусских имён («Дильбар Эминовна»)
пол не гадаем и отчество принимаем как есть.
"""
from __future__ import annotations

import re
from typing import NamedTuple

from channels import ru_names

# Отчества: мужские и женские суффиксы.
_PATR_MALE = ("ович", "евич", "ёвич", "ьич")
_PATR_FEMALE = ("овна", "евна", "ёвна", "ична", "инична", "инишна")
# Мужские отчества на «-ич» без «-ов/-ев» — суффикс «ич» целиком брать нельзя
# (под него попадает пол-Балкан), поэтому перечисляем поимённо.
_PATR_MALE_SHORT = {
    "ильич", "кузьмич", "фомич", "лукич", "никитич", "саввич", "фаддеич",
    "гаврилыч", "иваныч", "палыч",
}
# Тюркские/кавказские отчества — идут отдельным словом после имени отца.
_PATR_SUFFIX_EXTRA = ("оглы", "оглу", "улы", "кызы", "гызы", "уулу")

# Суффиксы русских/славянских фамилий. Нужны, только чтобы в паре из двух слов
# понять, где имя: «Рудченко Павел» — фамилия впереди.
_SURNAME_SUFFIX = (
    "ов", "ев", "ёв", "ин", "ын", "ский", "цкий", "ской", "цкой",
    "ова", "ева", "ёва", "ина", "ына", "ская", "цкая",
    "енко", "енка", "чук", "юк", "ук", "ых", "их", "ян", "швили", "дзе", "иа",
)

_CYRILLIC = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")


def _letters(token: str) -> str:
    """Слово без пунктуации/цифр, в нижнем регистре. «Аксёнов,» → «аксёнов»."""
    return "".join(ch for ch in token if ch.isalpha()).lower()


def _clean(raw: str | None) -> list[str]:
    """Сырое поле карточки → слова ФИО.

    Из базы приходит не только «Имя Отчество Фамилия»: импорт таскает город в
    скобках («Илья Шмелёв (Москва)»), кавычки, должность через запятую. Скобки и
    всё, что в них, режем целиком: «Илья Шмелёв (Москва), добрый день» — это уже
    не обращение, а адресная строка.
    """
    s = (raw or "").strip()
    if not s:
        return []
    s = re.sub(r"[(\[{<].*?[)\]}>]", " ", s)          # «(Москва)», «[ИП]»
    s = re.sub(r"[«»\"'`]", " ", s)
    s = s.split(",")[0]                                # «Иванов И.И., директор»
    out = []
    for tok in s.split():
        w = _letters(tok)
        if len(w) < 2:                                 # инициал «И.», мусор
            continue
        if "." in tok.rstrip("."):                     # «И.И.» — тоже инициалы
            continue
        out.append(tok.strip(".,;:!?").strip())
    return out


def _patronymic_gender(token: str) -> str | None:
    """'male'/'female', если слово похоже на отчество; иначе None."""
    w = _letters(token)
    if len(w) < 5:
        return None
    if w.endswith(_PATR_FEMALE):
        return "female"
    if w.endswith(_PATR_MALE) or w in _PATR_MALE_SHORT:
        return "male"
    if w.endswith(_PATR_SUFFIX_EXTRA):
        return "male" if w.endswith(("оглы", "оглу", "улы", "уулу")) else "female"
    return None


def _confident_gender(token: str) -> str | None:
    """Пол по имени, но ТОЛЬКО когда он очевиден: имя из пула ru_names либо
    кончается на «-а/-я» (кроме мужских «Никита», «Илья»). Для всего остального
    возвращаем None — «Дильбар», «Сероб», «Айгуль» гадать нельзя, иначе честное
    отчество «Эминовна» будет отброшено как фамилия."""
    w = _letters(token)
    if not w or w[0] not in _CYRILLIC:
        return None
    cap = w.capitalize()
    if cap in ru_names._MALE_FIRST:
        return "male"
    if cap in ru_names._FEMALE_FIRST:
        return "female"
    if w in ru_names._MALE_A_ENDING:
        return "male"
    if w in ru_names._FEMALE_CONSONANT:
        return "female"
    if w.endswith(("а", "я")):
        return "female"
    return None


def _is_known_first(token: str) -> bool:
    """Имя есть в пуле распространённых русских имён (ru_names)."""
    cap = _letters(token).capitalize()
    return cap in ru_names._MALE_FIRST or cap in ru_names._FEMALE_FIRST


def _looks_like_surname(token: str) -> bool:
    w = _letters(token)
    return len(w) >= 4 and w.endswith(_SURNAME_SUFFIX)


class Person(NamedTuple):
    first: str          # имя («Николай»)
    patronymic: str     # отчество («Викторович»), пусто если не нашли
    last: str           # фамилия («Комиссаренко»), пусто если не нашли


def parse(raw: str | None) -> Person:
    """ФИО в любом порядке → (имя, отчество, фамилия).

    «Николай Викторович Комиссаренко» и «Комиссаренко Николай Викторович» дают
    одно и то же. Что не разобралось — пустая строка, а не догадка.
    """
    tokens = _clean(raw)
    if not tokens:
        return Person("", "", "")
    if len(tokens) == 1:
        return Person(tokens[0], "", "")

    # 1. Ищем отчество. Оно и задаёт разметку: имя — соседнее слово слева
    #    («Николай Викторович…») либо справа («…Владимир Васильевич»).
    for i, tok in enumerate(tokens):
        pg = _patronymic_gender(tok)
        if not pg:
            continue
        # Кандидат на имя — сосед. У «Фамилия Имя Отчество» отчество последнее,
        # у «Имя Отчество Фамилия» — среднее; в обоих случаях имя стоит слева,
        # кроме пары из двух слов, где отчество может быть и первым.
        first_idx = i - 1 if i > 0 else i + 1
        if first_idx >= len(tokens):
            continue
        first = tokens[first_idx]
        # Фамилия, притворившаяся отчеством: «Ольга Парханович». Женское имя с
        # мужским отчеством не бывает — значит это фамилия, ищем дальше.
        fg = _confident_gender(first)
        if fg and fg != pg:
            continue
        rest = [t for j, t in enumerate(tokens) if j not in (i, first_idx)]
        return Person(first, tok, rest[0] if rest else "")

    # 2. Отчества нет. Осталось понять, где имя, а где фамилия. Пул
    #    распространённых имён надёжнее суффиксов: «Ирина» кончается на «-ина»,
    #    как фамилия, но это имя.
    a, b = tokens[0], tokens[1]
    if _is_known_first(a) and not _is_known_first(b):
        first, last = a, b
    elif _is_known_first(b) and not _is_known_first(a):
        first, last = b, a
    elif _looks_like_surname(a) and not _looks_like_surname(b):
        first, last = b, a                      # «Рудченко Павел»
    else:
        first, last = a, b                      # обычный порядок «Имя Фамилия»
    return Person(first, "", last)


def address(raw: str | None) -> str:
    """Вежливое обращение: «Имя Отчество», если отчество известно, иначе «Имя».

    Фамилию не подставляем никогда: «Николай Комиссаренко, добрый вечер» в личке
    звучит как обращение отдела кадров. Если разобрать не удалось — возвращаем
    исходную строку без скобок (лучше как есть, чем пусто)."""
    p = parse(raw)
    if p.first and p.patronymic:
        return f"{p.first} {p.patronymic}"
    if p.first:
        return p.first
    return " ".join(_clean(raw))


def address_soft(raw: str | None) -> str:
    """То же, но для полей, где может лежать НЕ человек, а организация
    («Эталон недвижимость», «Агентство Аякс»). Переписываем строку, только если
    в ней найдено отчество — это надёжный признак живого человека. Во всех
    остальных случаях отдаём как есть: срезать слово у названия компании хуже,
    чем оставить длинное обращение."""
    p = parse(raw)
    if p.first and p.patronymic:
        return f"{p.first} {p.patronymic}"
    return (raw or "").strip()


# ── творительный падеж: «поговорить С Николаем Викторовичем» ─────────────────
# Обращение из карточки лежит в именительном, а фраза «обсудить с {decision}»
# требует творительного. «Поговорить с Владимир Васильевич» — такой же провал
# первого касания, как и «Викторович Комиссаренко».

_INSTR_PATRONYMIC = (
    ("ович", "овичем"), ("евич", "евичем"), ("ёвич", "ёвичем"), ("ьич", "ьичем"),
    ("ич", "ичем"),
    ("овна", "овной"), ("евна", "евной"), ("ёвна", "ёвной"),
    ("ична", "ичной"), ("инична", "иничной"), ("инишна", "инишной"),
)


# Имена, где по общему правилу выходит не то: беглая гласная («Павел» → «Павлом»,
# а не «Павелом»), несклоняемое на вид «Любовь» и мягкая основа «Илья».
_INSTR_EXCEPTIONS = {
    "павел": "павлом", "лев": "львом", "пётр": "петром", "петр": "петром",
    "любовь": "любовью", "илья": "ильёй", "илия": "илиёй", "яков": "яковом",
}


def _instr_word(word: str, female: bool) -> str | None:
    """Слово в творительный падеж. None — не уверены, склонять не будем."""
    w = word.lower()
    if w in _INSTR_EXCEPTIONS:
        return _INSTR_EXCEPTIONS[w].capitalize() if word[:1].isupper() else _INSTR_EXCEPTIONS[w]
    # Короткие отчества на «-ич» («Ильич», «Кузьмич») после шипящей берут «-ом»,
    # а не «-ем»: «с Ильичом», не «с Ильичем».
    if w in _PATR_MALE_SHORT:
        return word + "ом"
    for src, dst in _INSTR_PATRONYMIC:
        if w.endswith(src):
            return word[: -len(src)] + dst
    if female:
        if w.endswith("а"):
            return word[:-1] + "ой"          # Елена → Еленой
        if w.endswith("я"):
            return word[:-1] + "ей"          # Наталья → Натальей
        return None                           # «Любовь» и прочие несклоняемые — не трогаем
    if w.endswith("й"):
        return word[:-1] + "ем"              # Николай → Николаем
    if w.endswith("ь"):
        return word[:-1] + "ем"              # Игорь → Игорем
    if w.endswith("а"):
        return word[:-1] + "ой"              # Никита → Никитой
    if w.endswith("я"):
        return word[:-1] + "ей"
    if w and w[-1] in _CYRILLIC and w[-1] not in "аеёиоуыэюя":
        return word + "ом"                   # Владимир → Владимиром
    return None


def instrumental(raw: str | None) -> str:
    """«Николай Викторович» → «Николаем Викторовичем».

    Склоняем по словам: что не склонилось уверенно, оставляем в именительном.
    Так «Дильбар Эминовна» даёт «Дильбар Эминовной» — иностранные женские имена на
    согласную в русском и не склоняются, а отчество склониться обязано."""
    p = parse(raw)
    if not p.first:
        return (raw or "").strip()
    female = (_confident_gender(p.first) == "female"
              or _patronymic_gender(p.patronymic) == "female")
    words = [p.first] + ([p.patronymic] if p.patronymic else [])
    return " ".join(_instr_word(w, female) or w for w in words)
