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

## Дальше (нужны доступы — см. чек-лист в спеке)
- `channels/telegram.py` — Telethon-адаптер (api_id/api_hash + прокси)
- `channels/whatsapp.py` — адаптер WaCombo / WhatsApp Web
- `integrations/` — Google Calendar + Zoom
- `scheduler.py` — напоминания за 2-3 ч и дожим
- NocoDB поверх `data/axiom.db` — интерфейс-книжка с фильтрами
