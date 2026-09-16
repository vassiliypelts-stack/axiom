"""
Экспорт папок (dialog filters) и списка чатов аккаунта 988 в JSON.
Только чтение — ничего не меняет, ничего не отправляет.

Запуск НА СЕРВЕРЕ (сессия 988 привязана к его IP — не гонять больше нигде):
    cd ~/axiom-repo/axiom
    .venv/bin/python tools/export_folders_988.py

Использует client_for_account() из channels/telegram.py — тот же путь подключения,
что и остальной AXIOM (свой api_id/api_hash аккаунта, если задан, иначе .env;
свой прокси, если задан аккаунту, иначе общий с запретом на shared IP).

Результат: axiom/data/export_988_folders.json
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon.tl.functions.messages import GetDialogFiltersRequest

from channels.telegram import client_for_account  # noqa: E402

ACCOUNT_ID = 5  # Василий988 +79881678180
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "export_988_folders.json"


async def main():
    client, _ = client_for_account(ACCOUNT_ID)
    await client.connect()
    if not await client.is_user_authorized():
        print("Сессия 988 не авторизована — читать нечего")
        await client.disconnect()
        return

    me = await client.get_me()
    print(f"Вошли как: {me.first_name} (id={me.id})")

    filters_result = await client(GetDialogFiltersRequest())
    folders = getattr(filters_result, "filters", filters_result)

    dialogs = await client.get_dialogs(limit=None)
    by_id = {d.entity.id: d for d in dialogs}

    export = {"account_id": ACCOUNT_ID, "folders": []}

    for f in folders:
        title = getattr(f, "title", None)
        if title is None:
            continue  # "All Chats" псевдо-папка — пропускаем
        # в новых слоях API title приходит как TextWithEntities, а не str
        title = getattr(title, "text", title)
        chats = []
        for peer in getattr(f, "include_peers", []):
            peer_id = (getattr(peer, "channel_id", None)
                       or getattr(peer, "chat_id", None)
                       or getattr(peer, "user_id", None))
            d = by_id.get(peer_id)
            if d:
                chats.append({
                    "id": d.entity.id,
                    "title": d.title,
                    "type": type(d.entity).__name__,
                    "username": getattr(d.entity, "username", None),
                })
            else:
                chats.append({"id": peer_id, "title": "??? (не в текущих диалогах)", "type": None, "username": None})

        export["folders"].append({"title": title, "count": len(chats), "chats": chats})

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nПапок: {len(export['folders'])}")
    total = 0
    for f in export["folders"]:
        print(f"  {f['title']}: {f['count']} чатов")
        total += f["count"]
    print(f"Всего чатов в папках: {total}")
    print(f"\nСохранено в {OUT_PATH}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
