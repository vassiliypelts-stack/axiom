"""Настройки приватности TG-аккаунта (антибан + спрятать номер).

Рекомендованный набор под холодную рассылку:
  • найти по номеру → Контакты   (никто чужой не свяжет номер с аккаунтом)
  • номер телефона  → Никто       (спрятать номер от всех)
  • «был в сети»    → Никто        (меньше палит автоматику)
  • фото профиля    → Все          (получатель видит аватар = больше доверия)
  • звать в группы  → Контакты     (защита от «спам-репорт» групп — частая причина бана)

Каждый ключ ставим отдельно и в try/except: если один не прошёл (версия слоя/
ограничение) — остальные всё равно применятся. «Найти по номеру: контакты» ставим
ПЕРЕД «номер → Никто»: Telegram требует, чтобы поиск по номеру был не «для всех»,
иначе может не дать полностью спрятать номер.
"""
from __future__ import annotations

from telethon.tl.functions.account import SetPrivacyRequest
from telethon.tl.types import (
    InputPrivacyKeyAddedByPhone,
    InputPrivacyKeyChatInvite,
    InputPrivacyKeyPhoneNumber,
    InputPrivacyKeyProfilePhoto,
    InputPrivacyKeyStatusTimestamp,
    InputPrivacyValueAllowAll,
    InputPrivacyValueAllowContacts,
    InputPrivacyValueDisallowAll,
)

# (человеческое название, конструктор ключа, правила). Порядок важен — см. модульный docstring.
_RECOMMENDED = [
    ("найти по номеру: контакты", InputPrivacyKeyAddedByPhone,    [InputPrivacyValueAllowContacts()]),
    ("номер спрятан (никто)",     InputPrivacyKeyPhoneNumber,     [InputPrivacyValueDisallowAll()]),
    ("«был в сети» скрыт",        InputPrivacyKeyStatusTimestamp, [InputPrivacyValueDisallowAll()]),
    ("фото видно всем",           InputPrivacyKeyProfilePhoto,    [InputPrivacyValueAllowAll()]),
    ("в группы: только контакты", InputPrivacyKeyChatInvite,      [InputPrivacyValueAllowContacts()]),
]


async def apply_privacy(client) -> list[str]:
    """Применяет рекомендованный набор приватности к уже подключённому клиенту.
    Возвращает список того, что реально удалось выставить (для отчёта в интерфейсе)."""
    done: list[str] = []
    for title, key_cls, rules in _RECOMMENDED:
        try:
            await client(SetPrivacyRequest(key=key_cls(), rules=rules))
            done.append(title)
            print(f"  приватность: {title}")
        except Exception as e:  # noqa: BLE001 — один ключ не должен рушить остальные
            print(f"  [privacy] {title}: {e}")
    return done
