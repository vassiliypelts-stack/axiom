"""Расшифровка входящих голосовых через Deepgram (REST, без SDK).

Зачем. Знакомые Василия отвечают на «как дела, чем занимаешься?» голосом, а агент на
это мог только попросить «напиши текстом»: 12.10.2026 два диалога подряд на этом и
кончились вопросом «так ты бот?». Теперь слушатель отдаёт запись сюда и кладёт в
книжку текст, на который агент отвечает по существу.

Ключ: настройка пульта `deepgram_api_key` (⚙ Настройки), запасной — DEEPGRAM_API_KEY
в .env. Нет ключа или Deepgram не ответил → None: входящее останется меткой
«[голосовое]», и агент, как раньше, попросит текст.
"""
from __future__ import annotations

from pathlib import Path

import requests

import config

_API = "https://api.deepgram.com/v1"
# Голосовое — это секунды и минуты речи. Больше 20 МБ в личку голосом не приходит,
# а случайный длинный файл не должен подвешивать ответ человеку.
MAX_BYTES = 20 * 1024 * 1024
SETTING = "deepgram_api_key"


def api_key() -> str:
    try:
        from db import database
        with database.get_conn() as conn:
            key = (database.get_setting(conn, SETTING, "") or "").strip()
        if key:
            return key
    except Exception:  # noqa: BLE001 — БД недоступна: остаётся ключ из .env
        pass
    return config.DEEPGRAM_API_KEY


def check_key(key: str) -> bool | None:
    """Ключ рабочий? True/False, None — Deepgram не ответил (сеть), проверить не вышло."""
    try:
        r = requests.get(f"{_API}/projects", headers={"Authorization": f"Token {key}"}, timeout=15)
    except requests.RequestException:
        return None
    return r.status_code == 200


def transcribe(path: str | Path, mime: str | None = None) -> str | None:
    """Текст записи или None (нет ключа, файл не подходит, ошибка, тишина)."""
    key = api_key()
    path = Path(path)
    if not key or not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        return None
    try:
        r = requests.post(
            f"{_API}/listen",
            params={"model": "nova-2", "language": "ru", "smart_format": "true",
                    "punctuate": "true"},
            headers={"Authorization": f"Token {key}",
                     "Content-Type": mime or "application/octet-stream"},
            data=path.read_bytes(),
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
    except Exception as e:  # noqa: BLE001 — расшифровка не должна ронять слушатель
        print(f"[stt] Deepgram не расшифровал {path.name}: {e}")
        return None
    return " ".join((text or "").split()) or None
