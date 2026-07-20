"""Личный инбокс-бот: пишешь ему — ИИ раскладывает по AXIOM (задача/лид/заметка).

Это ТОНКИЙ транспорт: принял сообщение → отдал в agent.inbox.capture() → ответил
подтверждением. Весь «мозг» в agent/inbox.py (его можно гонять и без Telegram).

Бот отдельный от рабочей армии — это BotFather-бот на токене, не userbot. Принимает
ТОЛЬКО от владельца (INBOX_BOT_OWNER): бот-токен могут узнать посторонние, а он создаёт
записи в базе — чужих пускать нельзя.

Настройка (один раз):
  1) @BotFather → /newbot → получить токен → INBOX_BOT_TOKEN в .env;
  2) запустить `python -m channels.inbox_bot`, написать боту что угодно — он ответит твоим
     user id; вписать его в INBOX_BOT_OWNER в .env и перезапустить;
  3) готово: пиши задачи/лидов/заметки текстом.

Голос пока не расшифровывается (STT добавим позже) — на голосовое бот вежливо просит текст.
"""
from __future__ import annotations

from telethon import TelegramClient, events
from telethon.sessions import StringSession

import config
from agent.inbox import capture

HELP = ("Привет! Пиши как в заметки, я разложу по AXIOM:\n"
        "• «позвонить Ивану завтра в 15:00» → задача\n"
        "• «новый лид Пётр +79991234567, хочет сайт» → контакт\n"
        "• «Марина готова на встречу» → заметка к её карточке\n\n"
        "Голос пока не понимаю — присылай текстом.")


def run() -> None:
    if not config.INBOX_BOT_TOKEN:
        raise SystemExit("нет INBOX_BOT_TOKEN в .env — создай бота у @BotFather и впиши токен")
    if not config.TG_API_ID or not config.TG_API_HASH:
        raise SystemExit("нет TG_API_ID/TG_API_HASH в .env — они нужны и боту тоже")
    owner = int(config.INBOX_BOT_OWNER) if str(config.INBOX_BOT_OWNER).strip().isdigit() else None

    client = TelegramClient(StringSession(), int(config.TG_API_ID), config.TG_API_HASH)

    @client.on(events.NewMessage)
    async def handler(ev):  # noqa: ANN001
        # Пока владелец не задан — помогаем его узнать и НИЧЕГО не пишем в базу.
        if owner is None:
            await ev.reply(f"Твой telegram id: `{ev.sender_id}`\nВпиши его в INBOX_BOT_OWNER "
                           f"(.env) и перезапусти бота — тогда начну принимать.")
            return
        if ev.sender_id != owner:
            return  # чужих молча игнорируем — бот личный
        if getattr(ev, "voice", None) or getattr(ev, "audio", None):
            await ev.reply("🎤 Голос пока не расшифровываю — пришли текстом (STT добавим позже).")
            return
        text = (ev.raw_text or "").strip()
        if not text:
            return
        if text in ("/start", "/help"):
            await ev.reply(HELP)
            return
        try:
            await ev.reply(capture(text))
        except Exception as e:  # noqa: BLE001
            await ev.reply(f"⚠️ не смог разобрать: {type(e).__name__}: {e}")

    client.start(bot_token=config.INBOX_BOT_TOKEN)
    me = client.loop.run_until_complete(client.get_me())
    print(f"инбокс-бот @{me.username} запущен. owner={owner or '(не задан — напиши боту, он подскажет id)'}")
    client.run_until_disconnected()


if __name__ == "__main__":
    run()
