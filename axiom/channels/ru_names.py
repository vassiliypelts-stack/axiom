"""Пул русских имён для массового оформления купленных аккаунтов.

Пары «Имя Фамилия» подобраны с согласованным родом (мужское имя — мужская
фамилия, женское — женская), чтобы не было нелепых сочетаний вроде «Иван
Иванова». Обычные, не редкие имена — не должны выделяться в переписке.
"""
from __future__ import annotations

import random

NAMES: list[str] = [
    # мужские
    "Александр Смирнов", "Дмитрий Кузнецов", "Максим Попов", "Сергей Волков",
    "Андрей Соколов", "Алексей Морозов", "Иван Новиков", "Никита Фёдоров",
    "Артём Егоров", "Илья Павлов", "Кирилл Семёнов", "Михаил Голубев",
    "Даниил Виноградов", "Егор Богданов", "Роман Воробьёв", "Владимир Орлов",
    "Павел Никитин", "Антон Захаров", "Игорь Борисов", "Олег Медведев",
    # женские
    "Анна Смирнова", "Мария Кузнецова", "Елена Попова", "Ольга Волкова",
    "Наталья Соколова", "Татьяна Морозова", "Ирина Новикова", "Светлана Фёдорова",
    "Юлия Егорова", "Екатерина Павлова", "Дарья Семёнова", "Виктория Голубева",
    "Полина Виноградова", "Ксения Богданова", "Алина Воробьёва", "Кристина Орлова",
    "Валентина Никитина", "Марина Захарова", "Людмила Борисова", "Софья Медведева",
]


def sample_unique(n: int) -> list[str]:
    """n уникальных имён из пула. Если n больше пула — добирает вторым проходом
    (пул перемешивается заново), чтобы не отдать пустые/повторы подряд."""
    pool = NAMES.copy()
    random.shuffle(pool)
    out: list[str] = []
    while len(out) < n:
        need = n - len(out)
        take = pool[:need] if need <= len(pool) else pool
        out.extend(take)
        random.shuffle(pool)
    return out[:n]


# ГОСТ-подобная транслитерация для генерации Telegram @username из русского имени.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def translit(text: str) -> str:
    """«Василий» → «vasiliy». Всё, что не кириллица/латиница/цифра — отбрасывается."""
    out = []
    for ch in text.lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and (ch.isalnum()):
            out.append(ch)
    return "".join(out)


def phone_digits(phone: str | None, n: int = 3) -> str:
    """Последние n цифр номера («+7 928…» → «928»). Пусто, если номера нет."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return digits[-n:] if len(digits) >= n else digits


def make_label(full_name: str, phone: str | None) -> str:
    """Внутренний ярлык для НАШЕЙ таблицы: «Имя» + последние цифры номера,
    например «Василий928» — быстро узнать аккаунт по номеру, не путая с профилем
    в самом Telegram (там имя чистое, без цифр — см. accounts.tg_name)."""
    first = (full_name or "").split()[0] if full_name else "Акк"
    digits = phone_digits(phone)
    return f"{first}{digits}" if digits else first


def make_username_base(full_name: str, phone: str | None) -> str:
    """База для Telegram @username: транслит имени + цифры номера, напр. «vasiliy928».
    Обычный, не «спамный» вид ника — с именем и цифрами, как у живых людей."""
    first = (full_name or "").split()[0] if full_name else "user"
    base = translit(first) or "user"
    digits = phone_digits(phone, 3)
    return f"{base}{digits}" if digits else base
