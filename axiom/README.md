# AXIOM — личный ИИ-SDR

Книжка + ИИ-агент, который ведёт переписку с риелторами и доводит до Zoom-встречи.
Канало-независимое ядро: один и тот же агент и книжка работают для **Telegram и WhatsApp**.
Полная спека: [../AXIOM_Pilot.md](../AXIOM_Pilot.md).

## Архитектура (ядро vs адаптеры каналов)

```
[Книжка SQLite]  ──  [ИИ-агент Claude]   ← ядро (канало-независимое, готово к тесту)
        ▲                    │
        │   текст ответа     │
   ┌────┴─────────┬──────────┴──────────┐
   │ TG-адаптер   │  WA-адаптер          │  ← каналы (дни 3+)
   │ Telethon     │  WaCombo / WA Web    │
   └──────────────┴─────────────────────┘
```

Агент просто отдаёт **текст + намерение + согласован ли слот**; *как* и *куда* отправить —
дело адаптера канала. Поэтому добавить WhatsApp = написать ещё один адаптер, ядро не трогаем.

## Установка

```bash
cd axiom
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
copy .env.example .env        # заполни ANTHROPIC_API_KEY
```

## Что уже можно запустить (дни 1-2, без аккаунтов/прокси)

```bash
python -m db.database                              # создать книжку
python -m importer.import_contacts data/contacts_example.csv   # импорт базы
python -m agent.agent                              # офлайн-диалог с ИИ-агентом (нужен ANTHROPIC_API_KEY)
```

В `agent/prompts.py` впиши свой **оффер** (что предлагаешь риелторам и зачем им Zoom) —
без этого агент звучит не как ты.

## Канал Telegram (нужны доступы)

`channels/telegram.py` — Telethon-адаптер (основной канал). Браузер не нужен: работает по MTProto.

```bash
# 1. поставить зависимости (telethon, python-socks, anthropic, pydantic>=2 ...)
pip install -r requirements.txt
# 2. в .env заполнить TG_API_ID / TG_API_HASH (my.telegram.org), ANTHROPIC_API_KEY и при нужде TG_PROXY
# 3. вход в аккаунт (один раз; спросит номер + код, для сервера распечатает TG_STRING_SESSION):
python -m channels.login
# 4. запуск:
python -m channels.telegram --outreach 2    # разослать до 2 первых сообщений (тест)
python -m channels.telegram --listen         # слушать ответы и вести диалог через ИИ-агента
python -m channels.telegram --run 2           # разослать 2, слушать и крутить планировщик
```

Антибан: дневной лимит (`DAILY_FIRST_MESSAGES`), рандомные паузы, ловля FloodWait.
**Человекоподобный диалог:** агент дробит ответ на 1-3 коротких сообщения, бот шлёт их
по очереди с индикатором «печатает…» и паузами (∝ длине). Первое сообщение — в
`FIRST_MESSAGE_TEMPLATE`, дальше диалог ведёт `agent/`.

Деплой на сервер: получи `TG_STRING_SESSION` через `python -m channels.login` локально,
вставь в `.env` на VPS — вход без ввода кода.

## Планировщик: напоминания + дожим

`scheduler.py` — напоминает о встрече (за 2-3 ч), дожимает молчунов (24/48 ч, максимум 2 раза → nurture),
предлагает перенос недошедшим. Логика отделена от отправки — гоняется без аккаунтов.

```bash
python -m scheduler            # сухой прогон: что отправилось бы сейчас
python -m scheduler --apply    # пометить в книжке (без реальной отправки)
# в бою — вместе с TG-аккаунтом:
python -m channels.telegram --run 5        # outreach + слушать + планировщик
python -m channels.telegram --listen --scheduler
```

## Встречи: Google Calendar + Zoom

`integrations/` — на согласии о встрече агент создаёт Zoom-ссылку (`zoom.py`) и событие
в календаре (`calendar.py`), `meetings.arrange()` всё оркестрирует и нормализует время
встречи в ISO с таймзоной (тогда напоминания бьют точно). **Деградирует мягко:** нет
ключей Zoom/Google → встреча всё равно фиксируется в книжке, просто без ссылки/события.

Доступы (в `.env`): `GOOGLE_CREDENTIALS_FILE` (OAuth Desktop, Calendar API),
`ZOOM_ACCOUNT_ID/CLIENT_ID/CLIENT_SECRET` (Server-to-Server OAuth). Первый запуск
календаря откроет браузер для согласия и сохранит токен в `GOOGLE_TOKEN_FILE`.

## Дальше
- `channels/whatsapp.py` / экспорт в рассыльщик — запасной канал (уже есть `exporter/`)
- NocoDB поверх `data/axiom.db` — интерфейс-книжка с фильтрами
