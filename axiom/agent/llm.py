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
_RETRIES = 3          # попыток на запрос к OpenAI-совместимому провайдеру
_RETRY_PAUSE = 3.0    # база линейного бэкоффа между попытками, сек

# Каким режимом провайдер отдаёт структурированный ответ (см. structured()).
#   json_schema — строгая схема проверяется на стороне API (OpenAI, Gemini);
#   json_object — API умеет лишь «верни любой JSON», схему объясняем в промпте.
# DeepSeek на json_schema отвечает 400 «This response_format type is unavailable now».
STRUCTURED_MODE: dict[str, str] = {"deepseek": "json_object"}


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

def _to_openai(system: str | None, messages: list[dict]) -> list[dict]:
    """Anthropic-формат → OpenAI-формат. system становится первым сообщением,
    image-блоки — image_url с data:-URI."""
    out: list[dict] = []
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
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"неожиданный ответ провайдера: {str(data)[:200]}") from e


# ---- Единый фасад: text() и structured() -------------------------------- #

def text(spec: str, system: str | None, messages: list[dict], max_tokens: int = 500,
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


def structured(spec: str, system: str | None, messages: list[dict],
               output_format: type[BaseModel], max_tokens: int = 900,
               timeout: float | None = None, **kw):
    """Ответ по схеме (pydantic-модель), любой провайдер. Возвращает экземпляр модели."""
    prov, model = split(spec)
    if prov == "anthropic":
        if timeout is not None:
            kw["timeout"] = timeout
        resp = call(lambda c: c.messages.parse(
            model=model, max_tokens=max_tokens,
            **({"system": system} if system else {}), messages=messages,
            output_format=output_format, **kw))
        return resp.parsed_output
    schema = json_schema(output_format)
    if STRUCTURED_MODE.get(prov, "json_schema") == "json_object":
        # DeepSeek строгую схему не принимает («This response_format type is unavailable
        # now») — умеет только «верни JSON». Формат диктуем ШАБЛОНОМ ОТВЕТА, а не самой
        # JSON-схемой: на схему модель охотно отвечает... этой же схемой (ловили на живом
        # прогоне). Слово «json» в промпте для этого режима обязательно — иначе пустота.
        props = schema.get("properties", {})
        tmpl = {k: f"<{(v.get('description') or k)}>" for k, v in props.items()}
        system = ((system + "\n\n") if system else "") + (
            "Верни РОВНО один json-объект и ничего больше — без пояснений и без ```.\n"
            "Ниже ключи и что класть в каждый; подставь ЗНАЧЕНИЯ вместо <…>, "
            "саму подсказку не повторяй:\n" + json.dumps(tmpl, ensure_ascii=False, indent=1))
        rf = {"type": "json_object"}
    else:
        rf = {"type": "json_schema", "json_schema": {
            "name": output_format.__name__, "strict": True, "schema": schema}}
    data = _compat_post(prov, {
        "model": model, "max_tokens": max_tokens,
        "messages": _to_openai(system, messages),
        "response_format": rf,
    }, timeout)
    raw = _compat_content(data)
    try:
        return output_format(**json.loads(raw))
    except json.JSONDecodeError as e:
        # некоторые модели заворачивают JSON в ```json … ``` — вынимаем
        s = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            return output_format(**json.loads(s))
        except json.JSONDecodeError:
            raise RuntimeError(f"{prov} вернул не JSON: {raw[:200]}") from e
    except Exception as e:  # noqa: BLE001 — pydantic: не хватило поля / не тот тип
        # без сырого ответа в тексте ошибки причину не найти: «1 validation error for X»
        # ничего не говорит о том, ЧТО именно прислала модель
        raise RuntimeError(f"{prov} вернул JSON не по схеме ({e}); ответ: {raw[:200]}") from e


def json_schema(model: type[BaseModel]) -> dict:
    """JSON-схема модели в строгом виде (все поля required, без лишних) —
    годится и для Anthropic Batch, и для OpenAI-совместимого json_schema."""
    schema = model.model_json_schema()
    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}).keys())
    return schema
