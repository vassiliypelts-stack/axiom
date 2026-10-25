"""Отправить текст владельцу от бота Аксиома (@Jarvisvvp_bot) — «шина» для любых проектов.

Зачем: из любой папки (VS Code, Claude Code) Василий говорит «отправь это мне в
телеграм» — и сообщение уходит от бота Аксиома, а не с личного номера и не
через холодные аккаунты кампаний.

Куда шлём (первое найденное):
  1. --chat <id> в аргументах;
  2. OWNER_CHAT_ID в axiom/.env;
  3. настройка owner_bot_chat_id в базе — её пишет вход в пульт через бота
     (channels/bot_auth.verify_code), так что после одного входа «✈️ Telegram»
     адрес известен сам.

Запуск:
    python tools/send_owner.py --file plan.md
    echo "текст" | python tools/send_owner.py
    python tools/send_owner.py --text "Привет"

Текст уходит как есть, без разметки: Markdown из планов ломал бы parse_mode.
Длинное режется по 4000 символов по границам строк.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402,F401 — грузит axiom/.env в окружение
from db import database  # noqa: E402

TG_LIMIT = 4000


def _chunks(text: str) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > TG_LIMIT:
            if cur:
                out.append(cur); cur = ""
            out.append(line[:TG_LIMIT]); line = line[TG_LIMIT:]
        if len(cur) + len(line) > TG_LIMIT:
            out.append(cur); cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out


def _owner_chat(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    env = (os.environ.get("OWNER_CHAT_ID") or "").strip()
    if env:
        return env
    try:
        with database.get_conn() as conn:
            return database.get_setting(conn, "owner_bot_chat_id", "") or None
    except Exception:  # noqa: BLE001 — нет базы на этой машине
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Сообщение владельцу от бота Аксиома")
    ap.add_argument("--file", help="файл с текстом (utf-8)")
    ap.add_argument("--text", help="текст прямо в аргументе")
    ap.add_argument("--chat", help="chat_id получателя (по умолчанию — владелец)")
    args = ap.parse_args()

    if args.text:
        text = args.text
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        text = sys.stdin.buffer.read().decode("utf-8")
    if not text.strip():
        print("Пустой текст — нечего отправлять.", file=sys.stderr)
        return 2

    token = (os.environ.get("TG_BOT_TOKEN") or "").strip()
    if not token:
        print("Нет TG_BOT_TOKEN в axiom/.env — бот Аксиома не настроен на этой машине.", file=sys.stderr)
        return 2
    chat = _owner_chat(args.chat)
    if not chat:
        print("Не знаю, куда слать: добавь OWNER_CHAT_ID в axiom/.env "
              "или один раз войди в пульт через «✈️ Telegram».", file=sys.stderr)
        return 2

    parts = _chunks(text)
    for i, part in enumerate(parts, 1):
        body = json.dumps({"chat_id": chat, "text": part,
                           "disable_web_page_preview": True}).encode()
        req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=20) as r:
                ok = json.loads(r.read()).get("ok")
        except Exception as e:  # noqa: BLE001
            print(f"Часть {i}/{len(parts)} не ушла: {e}", file=sys.stderr)
            return 1
        if not ok:
            print(f"Telegram не принял часть {i}/{len(parts)}.", file=sys.stderr)
            return 1
    print(f"Отправлено ботом Аксиома: {len(parts)} сообщ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
