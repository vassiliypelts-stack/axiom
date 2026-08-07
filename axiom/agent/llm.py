"""Слой моделей AXIOM: выбор провайдера под задачу + пул ключей Anthropic.

ЗАЧЕМ. «Клод-код — терминал, мозги любые»: массовое обогащение (досье, темы чатов,
зацепки) не требует дорогой модели — там можно платить копейки DeepSeek/Gemini, а
Claude оставить там, где делаются деньги (диалоги с людьми). Этот модуль прячет
разницу между провайдерами за двумя функциями: text() и structured().

КАК ЗАДАЁТСЯ МОДЕЛЬ. Строка «провайдер:модель», без провайдера = anthropic:
    AXIOM_MODEL=claude-haiku-4-5           → Anthropic (по умолчанию)
    AXIOM_MODEL=deepseek:deepseek-chat     → DeepSeek (нужен DEEPSEEK_API_KEY)
    AXIOM_MODEL=gemini:gemini-flash-latest → Gemini (нужен GEMINI_API_KEY)
    AXIOM_AGENT_MODEL=claude-opus-4-8      → диалоги умнее/дороже

DeepSeek/Gemini/OpenAI ходят через их OpenAI-совместимый REST на голом httpx
(он и так есть как зависимость anthropic) — отдельный пакет openai не нужен.

ВНУТРЕННИЙ ФОРМАТ — anthropic'овский (system отдельно, content-блоки списком).
Для OpenAI-совместимых провайдеров он конвертируется в _to_openai(). Так места
вызова пишутся один раз и не знают, кто под капотом.

Использование:
    from agent import llm
    txt  = llm.text(config.MODEL, system="...", messages=[...], max_tokens=300)
    prof = llm.structured(config.MODEL, system="...", messages=[...],
                          output_format=PersonProfile, max_tokens=900)
    resp = llm.call(lambda c: c.messages.create(...))   # сырой Anthropic (батчи и т.п.)
"""
from __future__ import annotations

import json
import os
import time

import anthropic
from pydantic import BaseModel

import config

# OpenAI-совместимые провайдеры: имя → (базовый URL, переменная окружения с ключом).
# Добавить нового = одна строка здесь, менять места вызова не нужно.
OPENAI_COMPAT: dict[str, tuple[str, str]] = {
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}
HTTP_TIMEOUT = 120.0
_RETRIES = 5          # попыток на запрос к OpenAI-совместимому провайдеру. Было 3 —
# на живом трафике поймали серию из ТРЁХ подряд пустых ответов DeepSeek (не кривой
# JSON, а буквально пустая строка) на одном и том же диалоге: все попытки исчерпались,
# structured() бросил исключение, агент замолчал навсегда. Запас подешевле молчания.
_RETRY_PAUSE = 3.0    # база линейного бэкоффа между попытками, сек

# Каким режимом провайдер отдаёт структурированный ответ (см. structured()).
#   json_schema — строгая схема проверяется на стороне API (OpenAI, Gemini);
#   prompt_only — response_format НЕ шлём вообще, формат объясняем в промпте.
#
# У DeepSeek режим json_object оказался хуже, чем его отсутствие. Замер на живом
# ключе, один и тот же запрос по 6 раз:
#     с response_format=json_object → 5 пустых ответов из 6
#     без response_format           → 0 пустых из 6
# «Пустой» — это строка из пробелов при finish_reason=stop и потраченных токенах:
# модель залипает именно в режиме принудительного JSON. Отсюда и 23% потерянных
# диалогов в стресс-тесте, которые не спасали пять повторов подряд. JSON мы и так
# умеем вынимать из свободного текста (см. разбор в structured), так что теряем
# только серверную валидацию, которой у DeepSeek всё равно не было.
STRUCTURED_MODE: dict[str, str] = {"deepseek": "prompt_only"}


def split(spec: str) -> tuple[str, str]:
    """«провайдер:модель» → (провайдер, модель). Без префикса — anthropic.
    Осторожно: у Anthropic в именах моделей нет ':', так что неоднозначности нет."""
    spec = (spec or "").strip()
    if ":" in spec:
        prov, _, model = spec.partition(":")
        prov = prov.strip().lower()
        if prov in OPENAI_COMPAT or prov == "anthropic":
            return prov, model.strip()
    return "anthropic", spec


def provider_of(spec: str) -> str:
    return split(spec)[0]


def is_anthropic(spec: str) -> bool:
    return provider_of(spec) == "anthropic"


def supports_batch(spec: str) -> bool:
    """Batch API (−50% к цене) есть только у Anthropic. У остальных — обычный путь."""
    return is_anthropic(spec)


# Провайдеры, НЕ понимающие картинки в запросе. DeepSeek на image_url отвечает
# 400 «unknown variant image_url» и роняет ВЕСЬ запрос — не деградирует до текста.
# Значит картинку нельзя даже прикладывать: вызывающий код обязан спросить заранее.
_NO_VISION = {"deepseek"}


def supports_vision(spec: str) -> bool:
    """Можно ли слать этой модели изображения. False → шли только текст, иначе 400."""
    return provider_of(spec) not in _NO_VISION


def available(spec: str) -> bool:
    """Есть ли ключ под этого провайдера — чтобы гейтить шаг, а не падать в рантайме."""
    prov = provider_of(spec)
    if prov == "anthropic":
        return bool(keys())
    return bool(_compat_key(prov))


def _compat_key(prov: str) -> str:
    _, env = OPENAI_COMPAT[prov]
    return (os.getenv(env, "") or getattr(config, env, "") or "").strip()


# ---- Anthropic: пул ключей с авто-переключением -------------------------- #

def keys() -> list[str]:
    """Список ключей Anthropic: основной + дополнительные (без дублей, по порядку)."""
    out: list[str] = []
    if (config.ANTHROPIC_API_KEY or "").strip():
        out.append(config.ANTHROPIC_API_KEY.strip())
    for k in (getattr(config, "ANTHROPIC_API_KEYS", "") or "").split(","):
        k = k.strip()
        if k and k not in out:
            out.append(k)
    return out


def _should_rotate(e: Exception) -> bool:
    """Стоит ли пробовать следующий ключ: лимит/квота/кредиты/перегрузка."""
    if isinstance(e, anthropic.RateLimitError):
        return True
    if isinstance(e, anthropic.APIStatusError):
        if getattr(e, "status_code", None) in (429, 529):
            return True
        msg = str(e).lower()
        return any(w in msg for w in ("quota", "credit", "rate", "limit", "overloaded", "billing"))
    return False


def call(fn):
    """Выполнить вызов Anthropic с авто-перебором ключей. fn(client) -> результат.
    Для батчей и прочего, чему нужен именно сырой SDK. Обычный код — через text()/structured()."""
    ks = keys()
    if not ks:
        raise RuntimeError("нет ANTHROPIC_API_KEY/ANTHROPIC_API_KEYS в .env")
    last: Exception | None = None
    for i, key in enumerate(ks):
        try:
            return fn(anthropic.Anthropic(api_key=key))
        except Exception as e:  # noqa: BLE001
            last = e
            if _should_rotate(e) and i < len(ks) - 1:
                print(f"[llm] ключ #{i + 1} упёрся в лимит/квоту ({type(e).__name__}) — "
                      f"переключаюсь на #{i + 2}")
                continue
            raise
    if last:
        raise last


# ---- OpenAI-совместимые провайдеры (DeepSeek / Gemini / OpenAI) ---------- #

def cached(stable: str, dynamic: str = "") -> list[dict]:
    """Собрать system из ДВУХ частей так, чтобы неизменную половину кэшировал Anthropic.

    Кэш у Anthropic — это совпадение ПРЕФИКСА: всё до отметки `cache_control`
    считается один раз, а дальше при каждом запросе платится только 10% цены.
    Поэтому порядок обязателен: сперва то, что не меняется (правила агента, промпт
    кампании), потом всё персональное (кто собеседник, что мы ему уже писали).

    Экономия заметная: в диалоге история отправляется заново на каждую реплику, а
    неизменная часть у нас ~7 тыс. токенов из ~8,5 тыс. — то есть почти всё.

    Кэш живёт 5 минут и продлевается при каждом обращении, так что внутри одного
    живого диалога он не успевает остыть. Слишком короткий стабильный кусок
    Anthropic просто не станет кэшировать (порог зависит от модели) — это не ошибка,
    просто экономии не будет.
    """
    blocks: list[dict] = [{"type": "text", "text": stable,
                           "cache_control": {"type": "ephemeral"}}]
    if dynamic and dynamic.strip():
        blocks.append({"type": "text", "text": dynamic})
    return blocks


def _system_text(system) -> str | None:
    """system → плоская строка. Чужие провайдеры блоков с кэшем не понимают —
    для них склеиваем обратно в текст (кэш есть только у Anthropic)."""
    if system is None or isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if b.get("text"))


def _to_openai(system, messages: list[dict]) -> list[dict]:
    """Anthropic-формат → OpenAI-формат. system становится первым сообщением,
    image-блоки — image_url с data:-URI."""
    out: list[dict] = []
    system = _system_text(system)
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        parts: list[dict] = []
        for b in content or []:
            if b.get("type") == "text":
                parts.append({"type": "text", "text": b.get("text", "")})
            elif b.get("type") == "image":
                src = b.get("source", {})
                mt = src.get("media_type", "image/jpeg")
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{mt};base64,{src.get('data', '')}"}})
        out.append({"role": m["role"], "content": parts})
    return out


def _compat_post(prov: str, body: dict, timeout: float | None = None) -> dict:
    """POST к OpenAI-совместимому провайдеру с ретраями на ПРЕХОДЯЩИХ сбоях.

    Ретраи нужны не для красоты: на прогоне 478 чатов 65 (каждый седьмой!) остались без
    AI-разметки из-за разовых ConnectTimeout до api.deepseek.com — сеть моргнула, чат
    молча ушёл без темы и вердикта. Повторяем только то, что имеет смысл повторять:
    таймауты/обрывы сети и 429/5xx. На 401/402/400 (ключ, деньги, кривой запрос)
    повтор бесполезен — падаем сразу, чтобы причина была видна.
    """
    import httpx
    base, env = OPENAI_COMPAT[prov]
    key = _compat_key(prov)
    if not key:
        raise RuntimeError(f"нет {env} в .env — нужен для провайдера «{prov}»")
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            r = httpx.post(f"{base}/chat/completions", json=body,
                           headers={"Authorization": f"Bearer {key}"},
                           timeout=timeout or HTTP_TIMEOUT)
        except Exception as e:  # noqa: BLE001  — сеть: таймаут/обрыв/DNS
            last = e
            if attempt == _RETRIES - 1:
                raise
            time.sleep(_RETRY_PAUSE * (attempt + 1))   # линейный бэкофф: 3с, 6с
            continue
        if r.status_code < 400:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < _RETRIES - 1:
            time.sleep(_RETRY_PAUSE * (attempt + 1))
            continue
        raise RuntimeError(f"{prov} {r.status_code}: {r.text[:300]}")
    raise last or RuntimeError(f"{prov}: не удалось выполнить запрос")


def _compat_content(data: dict) -> str:
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"неожиданный ответ провайдера: {str(data)[:200]}") from e
    content = msg.get("content") or ""
    if content.strip():
        return content
    # Живой случай: DeepSeek отдал completion_tokens=12 (реально что-то сгенерил),
    # а content пуст — content.reasoning_content у DeepSeek-моделей несёт цепочку
    # рассуждений отдельно от финального ответа; если по какой-то причине текст
    # осел там, а не в content, мы раньше теряли его целиком и падали с «пустой
    # ответ», хотя модель фактически ответила. Дешёвый фолбэк, ничего не портит,
    # если поля нет — как и раньше, вернётся пустая строка.
    reasoning = (msg.get("reasoning_content") or "").strip()
    return reasoning if reasoning else content


# ---- Единый фасад: text() и structured() -------------------------------- #

def text(spec: str, system: "str | list[dict] | None", messages: list[dict], max_tokens: int = 500,
         timeout: float | None = None, **kw) -> str:
    """Обычный текстовый ответ, любой провайдер. Возвращает строку.
    timeout — на один запрос (важно для массовых прогонов: подвисшая сеть иначе
    вешает весь цикл, SDK по умолчанию ждёт 10 минут)."""
    prov, model = split(spec)
    if prov == "anthropic":
        if timeout is not None:
            kw["timeout"] = timeout
        resp = call(lambda c: c.messages.create(
            model=model, max_tokens=max_tokens,
            **({"system": system} if system else {}), messages=messages, **kw))
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    data = _compat_post(prov, {"model": model, "max_tokens": max_tokens,
                               "messages": _to_openai(system, messages)}, timeout)
    return (_compat_content(data) or "").strip()


def structured(spec: str, system: "str | list[dict] | None", messages: list[dict],
               output_format: type[BaseModel], max_tokens: int = 900,
               timeout: float | None = None, **kw):
    """Ответ по схеме (pydantic-модель), любой провайдер. Возвращает экземпляр модели."""
    prov, model = split(spec)
    if prov != "anthropic":
        # дальше system дописывается строкой (режим json_object) — блоки с кэшем
        # схлопываем заранее, они всё равно только для Anthropic
        system = _system_text(system)
    if prov == "anthropic":
        if timeout is not None:
            kw["timeout"] = timeout
        resp = call(lambda c: c.messages.parse(
            model=model, max_tokens=max_tokens,
            **({"system": system} if system else {}), messages=messages,
            output_format=output_format, **kw))
        return resp.parsed_output
    schema = json_schema(output_format)
    mode = STRUCTURED_MODE.get(prov, "json_schema")
    if mode in ("json_object", "prompt_only"):
        # DeepSeek строгую схему не принимает («This response_format type is unavailable
        # now») — умеет только «верни JSON». Формат диктуем ШАБЛОНОМ ОТВЕТА, а не самой
        # JSON-схемой: на схему модель охотно отвечает... этой же схемой (ловили на живом
        # прогоне). Слово «json» в промпте обязательно — иначе модель отвечает прозой.
        props = schema.get("properties", {})
        # enum лежит в json_schema_extra поля, а не в description — если не подмешать
        # его сюда явно, модель в json_object-режиме (DeepSeek и т.п.) не видит СПИСОК
        # допустимых значений вообще и придумывает произвольный текст вместо одного из
        # enum (ловили вживую: intent="Готов слушать, открыт к диалогу" вместо "positive").
        def _hint(v: dict) -> str:
            base = v.get("description") or ""
            enum_vals = v.get("enum")
            if enum_vals:
                choices = " | ".join(str(e) for e in enum_vals)
                return f"{base} — РОВНО ОДНО ИЗ: {choices}" if base else f"ОДНО ИЗ: {choices}"
            return base
        tmpl = {k: f"<{_hint(v) or k}>" for k, v in props.items()}
        system = ((system + "\n\n") if system else "") + (
            "Верни РОВНО один json-объект и ничего больше — без пояснений и без ```.\n"
            "Ниже ключи и что класть в каждый; подставь ЗНАЧЕНИЯ вместо <…>, "
            "саму подсказку не повторяй:\n" + json.dumps(tmpl, ensure_ascii=False, indent=1))
        # prompt_only — response_format НЕ шлём: именно он заставляет DeepSeek залипать
        # и печатать пробелы (замер: 5 пустых из 6 против 0 из 6 без него).
        rf = None if mode == "prompt_only" else {"type": "json_object"}
    else:
        rf = {"type": "json_schema", "json_schema": {
            "name": output_format.__name__, "strict": True, "schema": schema}}
    # DeepSeek (json_object) отвечает по формату, объяснённому ТЕКСТОМ, а не проверенному
    # сервером — и на живом трафике каждый N-й ответ приходит пустым или мимо схемы. Это
    # HTTP 200: _compat_post его не ретраит (у него нет причин — запрос дошёл, ответ
    # получен), и раньше единственный такой промах сразу валил structured() — то есть
    # агент молчал в ответ живому человеку из-за одного неудачного семплирования. Здесь
    # тот же запрос просто пробуется ещё раз: новый семпл почти всегда приходит валидным.
    last_err: Exception | None = None
    for attempt in range(_RETRIES):
        msgs = _to_openai(system, messages)
        if mode == "prompt_only":
            # Без response_format модель охотно забывает про формат и отвечает прозой:
            # инструкция утонула в длинном system, а последним она видит живую реплику
            # собеседника. Напоминание отдельной строкой В КОНЦЕ — единственное место,
            # которое модель точно прочитает перед тем, как начать писать.
            msgs = msgs + [{"role": "user", "content":
                            "Ответь ТОЛЬКО одним json-объектом по описанной схеме, "
                            "без пояснений и без ``` — сразу с символа «{»."}]
        body = {"model": model, "max_tokens": max_tokens, "messages": msgs}
        if rf is not None:
            body["response_format"] = rf
        # ПУСТОЙ ответ (HTTP 200, content="") — отдельная болезнь json_object-режима у
        # DeepSeek: на длинном системном промпте он изредка отдаёт пустоту вместо JSON.
        # Ретрай тем же телом лечит не всегда, поэтому со второй попытки снимаем
        # response_format: без него модель отвечает обычным текстом, а JSON мы и так
        # умеем вынимать (см. разбор ```json ниже). Лучше ответ в свободной форме,
        # чем молчание живому человеку.
        if attempt and isinstance(last_err, RuntimeError) and "пустой ответ" in str(last_err):
            body.pop("response_format", None)
        data = _compat_post(prov, body, timeout)
        raw = _compat_content(data)
        if not raw.strip():
            # в текст ошибки кладём ДИАГНОСТИКУ, иначе в колокольчике опять будет
            # «вернул не JSON:» и пустота — по такому сообщению причину не найти
            ch = (data.get("choices") or [{}])[0]
            last_err = RuntimeError(
                f"{prov} вернул пустой ответ (finish_reason={ch.get('finish_reason')}, "
                f"usage={data.get('usage')}, промпт={len(_system_text(system) or '')} симв.)")
            if attempt < _RETRIES - 1:
                print(f"[llm] {prov}: пустой ответ, попытка {attempt + 1}/{_RETRIES}")
                continue
            raise last_err
        try:
            return output_format(**_coerce_to_schema(json.loads(raw), schema))
        except json.JSONDecodeError as e:
            # некоторые модели заворачивают JSON в ```json … ``` — вынимаем
            s = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            try:
                return output_format(**_coerce_to_schema(json.loads(s), schema))
            except json.JSONDecodeError:
                pass
            # …а без response_format (см. обход пустого ответа выше) модель охотно
            # добавляет преамбулу: «Вот ответ: {…}». Вырезаем самый большой кусок от
            # первой «{» до последней «}» — это и есть объект.
            i, j = s.find("{"), s.rfind("}")
            if i != -1 and j > i:
                try:
                    return output_format(**_coerce_to_schema(json.loads(s[i:j + 1]), schema))
                except Exception:  # noqa: BLE001 — не JSON и не по схеме: идём на повтор
                    pass
            last_err = RuntimeError(f"{prov} вернул не JSON: {raw[:200]}")
            last_err.__cause__ = e
        except Exception as e:  # noqa: BLE001 — pydantic: не хватило поля / не тот тип
            # без сырого ответа в тексте ошибки причину не найти: «1 validation error for X»
            # ничего не говорит о том, ЧТО именно прислала модель
            last_err = RuntimeError(f"{prov} вернул JSON не по схеме ({e}); ответ: {raw[:200]}")
            last_err.__cause__ = e
        if attempt < _RETRIES - 1:
            print(f"[llm] {prov}: попытка {attempt + 1}/{_RETRIES} дала кривой ответ — повторяю")
    raise last_err or RuntimeError(f"{prov}: не вернул валидный ответ")


def _coerce_to_schema(raw_obj: dict, schema: dict) -> dict:
    """Причесать ответ дешёвой модели под схему ДО валидации.

    Строгую схему проверяет только Anthropic/OpenAI. DeepSeek её объясняют текстом, и
    он систематически (не случайно!) промахивается ровно двумя способами — оба поймали
    на живом диалоге:
      • поле-СПИСОК приходит одной строкой: reply_parts="привет\\n\\nкак дела?" вместо
        ["привет","как дела?"] — а от этого зависит, уйдёт реплика человеку или нет;
      • поле с ENUM приходит вольным пересказом: intent="Собеседник подтвердил, что это
        он" вместо "positive".
    Повторный запрос тут не помогает: модель ведёт себя так стабильно, поэтому агент
    молчал ВСЕГДА, а не иногда. Чиним не надеждой на модель, а приведением типов.
    """
    props = schema.get("properties", {})
    out = dict(raw_obj)
    for key, spec in props.items():
        if key not in out or out[key] is None:
            continue
        val = out[key]
        # список из строки: разбиваем по пустой строке (так модель разделяет реплики),
        # иначе кладём целиком одним элементом — лучше одно сообщение, чем ошибка
        if spec.get("type") == "array" and isinstance(val, str):
            parts = [p.strip() for p in val.split("\n\n") if p.strip()] or [val.strip()]
            out[key] = parts
        # enum: точное совпадение → как есть; иначе ищем допустимое значение внутри
        # текста; не нашли — берём первое из списка, чтобы не терять весь ответ из-за
        # одной классификации (реплики человеку важнее, чем метка для аналитики)
        enum_vals = spec.get("enum")
        if enum_vals and isinstance(out[key], str) and out[key] not in enum_vals:
            low = out[key].lower()
            match = next((e for e in enum_vals if str(e).lower() in low), None)
            out[key] = match if match is not None else enum_vals[0]
        # булево словом («да»/«true») вместо настоящего bool
        if spec.get("type") == "boolean" and isinstance(out[key], str):
            out[key] = out[key].strip().lower() in ("true", "да", "yes", "1")
    return out


def json_schema(model: type[BaseModel]) -> dict:
    """JSON-схема модели в строгом виде (все поля required, без лишних) —
    годится и для Anthropic Batch, и для OpenAI-совместимого json_schema."""
    schema = model.model_json_schema()
    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}).keys())
    return schema
