"""Черновик поста голосом Василия — из находки дайджеста или своей мысли.

Пишет не этот модуль, а движок контент-завода: `autopost/writer.py` в папке
CONTENT_FACTORY_DIR. Тот же, что стоит за пультом в Telegram: Claude, все
файлы правил скилла `/post`, до трёх переписываний через ИИ-детектор.
Запускаем его отдельным процессом из окружения завода — там свежий
anthropic, а правила голоса и детектор живут в одном месте.

До 14.10.2026 здесь был свой путь на DeepSeek с заданием «не пересказ, свой
угол, читатель не должен догадаться об источнике». Из донорского поста он
выбрасывал хук и досочинял Василию опыт. Теперь отмеченная находка идёт в
донорском режиме: хук и краткость донора сохраняются.

Старый путь оставлен запасным — для машины, где окружения завода нет.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import config
from agent import llm
from integrations import content_sheet

# Материалы голоса и движок лежат в соседнем проекте (Kontent-zavod-traffic-machine).
# Путь настраивается: на сервере и на рабочей машине он разный.
CONTENT_DIR = os.getenv("CONTENT_FACTORY_DIR", "")

VOICE_FILES = [
    "tone-of-voice.md",
    "03-STOP-WORDS.md",
    "11-AUDIENCE.md",
    "posts-draft.md",
    "15-HOOKS.md",     # первая строка решает судьбу поста
]

MAX_FILE = 12000
LIMIT = 500
# Claude с размышлением и тремя заходами через детектор: обычно минута, редко три.
FACTORY_TIMEOUT = 420


class WriterError(RuntimeError):
    pass


# ---------- движок завода ----------

def _factory(material: str, note: str = "", donor: bool = False) -> dict | None:
    """Черновик движком завода. None — окружения завода на этой машине нет."""
    if not CONTENT_DIR:
        return None
    base = Path(CONTENT_DIR)
    python, script = base / ".venv" / "bin" / "python", base / "autopost" / "writer.py"
    if not (python.exists() and script.exists()):
        return None

    request = {"material": material, "note": note, "donor": donor}
    try:
        # Очередь и ПАМЯТЬ читаем здесь, из кэша: пять черновиков разом, каждый
        # со своим чтением таблицы, упираются в лимит Google (60 чтений в минуту).
        request["history"], request["memory"] = content_sheet.writer_context()
    except Exception:  # noqa: BLE001 — таблица недоступна: движок прочитает сам
        pass

    env = dict(os.environ)
    # У Axiom в этой переменной своя модель (сейчас DeepSeek), движку она не годится.
    env.pop("CONTENT_WRITER_MODEL", None)
    env["PYTHONUTF8"] = "1"
    try:
        run = subprocess.run([str(python), str(script)], cwd=str(base), env=env,
                             input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                             capture_output=True, timeout=FACTORY_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise WriterError("Агент не успел написать пост, попробуйте ещё раз.") from e
    try:
        data = json.loads(run.stdout.decode("utf-8"))
    except ValueError as e:
        tail = run.stderr.decode("utf-8", "replace").strip()[-200:]
        raise WriterError(f"Движок завода не ответил: {tail or 'пустой вывод'}") from e
    if data.get("error"):
        raise WriterError(data["error"])
    return data


def _draft(res: dict, **meta) -> dict:
    """Ответ движка + то, что карточке черновика нужно знать об источнике."""
    return {"text": res["text"], "kind": res.get("kind", ""), "score": res.get("score"),
            "ok": res.get("ok", True), "found": res.get("found", []),
            "comment": res.get("comment", ""), **meta}


# ---------- запасной путь: общая модель пульта ----------

def _voice() -> str:
    """Голос, стоп-слова, аудитория и живые посты — одним куском."""
    if not CONTENT_DIR:
        raise WriterError(
            "CONTENT_FACTORY_DIR не задан в .env — не знаю, где лежат материалы голоса.")
    base = Path(CONTENT_DIR)
    if not base.exists():
        raise WriterError(f"Папка контент-завода не найдена: {CONTENT_DIR}")

    parts = []
    for name in VOICE_FILES:
        p = base / name
        if p.exists():
            parts.append(f"=== {name} ===\n{p.read_text(encoding='utf-8')[:MAX_FILE]}")
    if not parts:
        raise WriterError(f"В {CONTENT_DIR} нет файлов голоса — проверьте путь.")
    return "\n\n".join(parts)


SYSTEM = """Ты пишешь посты голосом Василия Пельца — не своим.

Ниже его материалы: голос, стоп-слова, аудитория, живые посты и банк хуков.
Живые посты важнее правил: правила описывают голос словами, а посты его
показывают. Копируй приёмы — арифметику, доведённую до денег, признание
своего провала, резкие короткие добивания отдельной строкой, перечисления
«тут… там… здесь…», разговорные обороты.

ПЕРВАЯ СТРОКА РЕШАЕТ ВСЁ.
Возьми формулу из 15-HOOKS.md — там 11 хуков с реальными цифрами отклика.
Плохой крючок тот, где читатель угадывает вторую строку. Беззубое начало
вроде «Знаю собственника, который...» уже заезжено — бери другую форму.

ЖЁСТКО, НО НЕ В ЧИТАТЕЛЯ.
Категоричное заявление о рынке, спор с общепринятым, неудобная правда о
мотивах — да. «Вы виноваты», «вы неправильно живёте» — нет: человек пришёл
читать про бизнес, а не получать диагноз.

Жёсткие ограничения:
- строго до 500 знаков, включая пробелы. Посчитай перед выдачей и,
  если вышло больше, режь целыми предложениями
- одна мысль
- ни одного тире: запятая, точка, двоеточие или союз
- 0-1 эмодзи
- никаких выдуманных цифр, кейсов, имён клиентов и опыта Василия
- религии нет никогда

Отдай ТОЛЬКО текст поста. Без заголовков, без пояснений, без кавычек."""

# Коротко то же, что раздел «ДОНОРСКИЙ ПОСТ» скилла /post в контент-заводе.
DONOR_TASK = """Ниже чужой пост (донор), Василий выбрал его сам. Перепиши его для Василия.

Главное в доноре хук и краткость, терять их нельзя:
- хук остаётся: те же число, предмет и обещание, что в первой строке донора.
  Менять можно подачу (утверждение → вопрос, «Слышали новость?») и слова
- суть остаётся целиком: инструкция идёт шагами в одном-двух абзацах,
  команда или настройка дословно; новость без искажения факта
- длина не больше, чем у донора
- от Василия одна короткая реакция его голосом, не абзац
- опыт Василию не выдумывай: «проверил», «поменял», «не сработало» нельзя
- ни одно предложение донора не переноси целиком

Правило «финал — вопрос или наблюдение» и хуки из 15-HOOKS.md здесь не
действуют: хук уже есть, он у донора.
"""


def _fit(text: str) -> str:
    """Ужать до лимита площадки, если модель его превысила.

    Считать знаки модели не умеют: черновики стабильно выходят на 550-700 при
    лимите Threads в 500. Просить «покороче» в промпте бесполезно — проверяем
    после и переписываем отдельным заходом.
    """
    if len(text) <= LIMIT:
        return text
    try:
        out = shorten(text, LIMIT)["text"]
    except WriterError:
        return text               # не ужалось — отдаём как есть, правится руками
    return out if len(out) < len(text) else text


def _ask(user: str, max_tokens: int = 900) -> str:
    spec = os.getenv("CONTENT_WRITER_MODEL", "") or config.AGENT_MODEL or config.MODEL
    system = [{"type": "text", "text": SYSTEM},
              {"type": "text", "text": _voice()}]
    try:
        out = llm.text(spec, system, [{"role": "user", "content": user}],
                       max_tokens=max_tokens, timeout=120)
    except Exception as e:  # noqa: BLE001
        raise WriterError(f"Модель не ответила: {str(e)[:150]}") from e
    text = (out or "").strip().strip('"').strip()
    if not text:
        raise WriterError("Модель вернула пустой ответ.")
    return text


# ---------- что зовёт пульт ----------

def from_source(title: str, excerpt: str, channel: str = "", note: str = "",
                link: str = "") -> dict:
    """Донорский пост: чужая публикация своими словами, хук и краткость сохранены."""
    donor = excerpt.strip()
    # Заголовок находки обычно и есть первая строка поста, то есть хук.
    if title.strip() and title.strip()[:60] not in donor:
        donor = f"{title.strip()}\n\n{donor}".strip()
    donor = donor[:4000]
    if not donor:
        raise WriterError("У находки нет текста — нечего переписывать.")

    # Исходник едет вместе с черновиком: правя текст, надо видеть, из чего он
    # вырос, — иначе не поймать, где агент переврал чужую мысль.
    meta = {"source": channel, "title": title, "origin": donor, "link": link}
    res = _factory(donor, note, donor=True)
    if res is not None:
        return _draft(res, **meta)

    user = f"{DONOR_TASK}\nВ доноре {len(donor)} знаков.\n\nДонор:\n{donor}\n"
    if note:
        user += f"\nПожелание Василия, оно важнее прочего: {note}\n"
    return {"text": _fit(_ask(user)), **meta}


def shorten(text: str, limit: int = 500) -> dict:
    """Ужать до лимита площадки, не потеряв голос.

    Модели плохо считают знаки и регулярно превышают 500 — а пост длиннее
    лимита Threads просто не примет.
    """
    user = (
        f"Сократи пост до {limit} знаков. Сейчас в нём {len(text)}.\n\n"
        "Режь целыми предложениями и абзацами, а не отдельными словами. "
        "Что обязано остаться: первая строка-хук, центральная мысль, цифры, "
        "команды и настройки дословно, разговорные обороты. Голос не приглаживай.\n\n"
        f"{text}"
    )
    return {"text": _ask(user, max_tokens=700)}


def from_idea(idea: str, note: str = "") -> dict:
    """Пост из своей мысли — надиктованной или написанной."""
    meta = {"source": "своя мысль", "title": idea[:80], "origin": idea[:800], "link": ""}
    res = _factory(idea[:4000], note)
    if res is not None:
        return _draft(res, **meta)

    user = (
        "Ниже сырая мысль Василия — надиктованная или набранная наспех. "
        "Найди в ней центральную мысль и конкретику, выкинь лишнее и "
        "напиши пост.\n\n"
        "Факты бери только отсюда и из его материалов. Ничего не досочиняй: "
        "если конкретики не хватает, лучше короче.\n\n"
        f"Мысль: {idea[:4000]}\n"
    )
    if note:
        user += f"\nПожелание Василия, оно важнее прочего: {note}\n"
    return {"text": _fit(_ask(user)), **meta}
