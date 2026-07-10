"""Конфигурация AXIOM. Читает .env, даёт пути и настройки."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows-консоль по умолчанию cp1251 — любой print() с эмодзи/«→» роняет процесс
# (UnicodeEncodeError). Это тихо крошило автопрогрев/парсинг, запускаемые как
# дочерние процессы. Принудительно переводим вывод в utf-8 при импорте config —
# а его импортирует КАЖДЫЙ модуль, так что защищены все точки входа разом.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — старый Python/необычный поток: не критично
        pass

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- Claude ---
# Runtime AXIOM по умолчанию на Haiku 4.5 (в 5 раз дешевле Opus). Opus оставляем
# для разработки приложения, не для боевой работы агентов.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Доп. ключи Anthropic через запятую — авто-переключение при дневном лимите/429/нехватке
# кредитов (agent/llm.py). Пример в .env: ANTHROPIC_API_KEYS=sk-ant-...,sk-ant-...
ANTHROPIC_API_KEYS = os.getenv("ANTHROPIC_API_KEYS", "")
# Обогащение (массово, дёшево) — Haiku.
MODEL = os.getenv("AXIOM_MODEL", "claude-haiku-4-5")
# Диалоги агента (где делаются деньги) — можно умнее/дороже: claude-opus-4-8 или
# claude-sonnet-4-6. По умолчанию = MODEL (Haiku). Переключить через .env: AXIOM_AGENT_MODEL.
AGENT_MODEL = os.getenv("AXIOM_AGENT_MODEL", MODEL)

# --- БД ---
DB_PATH = BASE_DIR / "data" / "axiom.db"
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"

# --- Telegram ---
TG_API_ID = os.getenv("TG_API_ID", "")
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", "axiom_session")
# StringSession для деплоя на сервер: логинишься раз локально (python -m channels.login),
# вставляешь строку сюда — на headless-сервере код подтверждения уже не нужен.
TG_STRING_SESSION = os.getenv("TG_STRING_SESSION", "")
TG_PROXY = os.getenv("TG_PROXY", "")

# --- Антибан ---
DAILY_FIRST_MESSAGES = int(os.getenv("DAILY_FIRST_MESSAGES", "15"))

# --- Обогащение (DaData: ИНН/ОГРН/ФИО руководителя из ЕГРЮЛ) ---
# Бесплатный токен на dadata.ru → API → ключ доступа. Пусто = шаг ЕГРЮЛ пропускается.
DADATA_API_KEY = os.getenv("DADATA_API_KEY", "")

# --- Встречи (Calendar + Zoom) ---
MEETING_TZ = os.getenv("MEETING_TZ", "Europe/Moscow")
MEETING_DURATION_MIN = int(os.getenv("MEETING_DURATION_MIN", "30"))
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE") or str(BASE_DIR / "google_credentials.json")
GOOGLE_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE") or str(BASE_DIR / "google_token.json")
ZOOM_ACCOUNT_ID = os.getenv("ZOOM_ACCOUNT_ID", "")
ZOOM_CLIENT_ID = os.getenv("ZOOM_CLIENT_ID", "")
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET", "")

# --- Proxy6.net (автопокупка/подбор прокси по стране аккаунта) ---
# Ключ — в личном кабинете proxy6.net → API. Пусто = кнопки покупки покажут понятную ошибку.
PROXY6_API_KEY = os.getenv("PROXY6_API_KEY", "")
