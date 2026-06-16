"""Конфигурация AXIOM. Читает .env, даёт пути и настройки."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- Claude ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = os.getenv("AXIOM_MODEL", "claude-opus-4-8")

# --- БД ---
DB_PATH = BASE_DIR / "data" / "axiom.db"
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"

# --- Telegram ---
TG_API_ID = os.getenv("TG_API_ID", "")
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", "axiom_session")
TG_PROXY = os.getenv("TG_PROXY", "")

# --- Антибан ---
DAILY_FIRST_MESSAGES = int(os.getenv("DAILY_FIRST_MESSAGES", "15"))
