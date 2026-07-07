"""Пул ключей Anthropic с авто-переключением при дневном лимите / 429 / нехватке кредитов.

Все вызовы Claude в рантайме AXIOM идут через call(): если текущий ключ упёрся в лимит
или закончилась квота/кредиты — автоматически пробуем следующий ключ. Так агент не встаёт,
когда выходит дневной лимит на одном ключе.

Ключи берутся из config:
  • ANTHROPIC_API_KEY    — основной
  • ANTHROPIC_API_KEYS   — дополнительные через запятую

Использование:
    from agent import llm
    resp = llm.call(lambda c: c.messages.create(model=..., ...))
"""
from __future__ import annotations

import anthropic

import config


def keys() -> list[str]:
    """Список ключей: основной + дополнительные (без дублей, по порядку)."""
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
    """Выполнить вызов Claude с авто-перебором ключей. fn(client) -> результат."""
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
