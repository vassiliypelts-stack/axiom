"""
Вступление аккаунта 702 в чаты/группы/каналы из export_988_folders.json.

НЕ ЗАПУСКАТЬ АВТОМАТИЧЕСКИ. Запускает только Василий вручную, когда сам решит
(см. правило "рассылки/вступления запускает только Василий" — 702 живой личный номер).

Запуск на сервере:
    cd ~/axiom-repo/axiom
    .venv/bin/python tools/join_chats_702.py [--folder "Название папки"] [--dry-run] [--delay 45]

Без --folder — идёт по всем папкам из экспорта.
--dry-run — только печатает, что вступил бы, ничего не делает.
--delay N — пауза между вступлениями в секундах (по умолчанию 45, чтобы не словить FloodWait/спам-блок).

Ограничения (важно понимать перед запуском):
  - Личные диалоги (User) и уже открытые приватные чаты пропускаются — вступить в
    личку с другим человеком с чужого номера невозможно и не нужно, они просто
    выводятся в списке "skip".
  - Приватные группы без публичной ссылки/username пропускаются, если только
    account 988 не может выдать invite-ссылку (это отдельная ручная операция).
  - Между вступлениями обязательна пауза — массовые джойны за минуту -> ban risk.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon.tl.functions.channels import JoinChannelRequest  # noqa: E402
from telethon.errors import FloodWaitError, UserAlreadyParticipantError  # noqa: E402

from channels.telegram import client_for_account  # noqa: E402

ACCOUNT_ID = 3  # Василий702 +77027417272
EXPORT_PATH = Path(__file__).resolve().parent.parent / "data" / "export_988_folders.json"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default=None, help="Вступать только в чаты этой папки")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=float, default=45.0)
    args = ap.parse_args()

    if not EXPORT_PATH.exists():
        print(f"Нет файла {EXPORT_PATH} — сначала запусти export_folders_988.py")
        return

    export = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))

    targets = []
    for f in export["folders"]:
        if args.folder and f["title"] != args.folder:
            continue
        for c in f["chats"]:
            if c["type"] in ("Channel", "Chat") and c.get("username"):
                targets.append(c)

    print(f"К вступлению: {len(targets)} чатов (с username, тип канал/группа)")
    skipped = sum(len(f["chats"]) for f in export["folders"]
                  if not args.folder or f["title"] == args.folder) - len(targets)
    if skipped:
        print(f"Пропущено (личные/приватные без ссылки): {skipped}")

    if args.dry_run:
        for c in targets:
            print(f"  [dry-run] вступил бы в @{c['username']} ({c['title']})")
        return

    client, _ = client_for_account(ACCOUNT_ID)
    await client.connect()
    if not await client.is_user_authorized():
        print("Сессия 702 не авторизована")
        return

    for i, c in enumerate(targets, 1):
        try:
            await client(JoinChannelRequest(c["username"]))
            print(f"[{i}/{len(targets)}] вступил: @{c['username']} ({c['title']})")
        except UserAlreadyParticipantError:
            print(f"[{i}/{len(targets)}] уже участник: @{c['username']}")
        except FloodWaitError as e:
            print(f"FloodWait {e.seconds}s на @{c['username']} — останавливаюсь")
            break
        except Exception as e:
            print(f"[{i}/{len(targets)}] ошибка @{c['username']}: {e}")
        await asyncio.sleep(args.delay)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
