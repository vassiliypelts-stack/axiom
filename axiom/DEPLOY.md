# AXIOM — деплой на сервер (VPS)

Цель: гонять задачи «в бою» 24/7. Веб-пульт + Telegram-агенты на Linux-VPS.

> ⚠️ **Безопасность №1.** Веб-пульт сейчас БЕЗ логина и имеет полный доступ к
> аккаунтам (может слать сообщения от твоих номеров). НЕ открывай порт 8000 в
> интернет. Рекомендуемый способ доступа — **SSH-туннель** (панель слушает
> только localhost на сервере, ты подключаешься через ssh). См. шаг 6.

## 0. Какой VPS взять
- ОС: **Ubuntu 22.04/24.04**. 1-2 vCPU, 2 ГБ RAM, 20 ГБ — хватит.
- Регион: такой, откуда **доступен `api.anthropic.com`** (иначе ИИ-агент не
  ответит) и Telegram. Обычно EU/нейтральные ДЦ. Из РФ-ДЦ Anthropic может быть
  закрыт — тогда нужен прокси для исходящих к Claude.
- Провайдеры: любой нормальный (Hetzner, Timeweb Cloud, Aeza, и т.п.).

## 1. Подготовка кода
На сервере (под обычным пользователем, не root). Внимание: код лежит в подпапке
`axiom/` внутри репозитория — все команды запускаем ИЗ НЕЁ.
```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git
git clone https://github.com/vassiliypelts-stack/axiom.git axiom-repo
cd axiom-repo/axiom          # ← рабочая папка (тут config.py, web/, db/, channels/)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```
Дальше во всех шагах рабочая папка = `~/axiom-repo/axiom`.

## 2. Секреты (.env)
`.env` НЕ в git — переносишь руками. Скопируй пример и заполни:
```bash
cp .env.example .env
nano .env
```
Заполни: `ANTHROPIC_API_KEY`, `TG_API_ID`, `TG_API_HASH`, **`TG_STRING_SESSION`**
(для headless — сгенерь локально: `python -m channels.login`, вставь строку),
`AXIOM_MODEL=claude-haiku-4-5`, при необходимости `TG_PROXY`, `DADATA_API_KEY`.

> Сессии аккаунтов команды (`accounts.tg_session`) лежат в БД. БД (`data/axiom.db`)
> тоже переносится руками или заводится заново на сервере (логин аккаунтов там же).

## 3. Перенос базы (если нужно поднять текущую)
```bash
# с локальной машины (из папки Leadgen-Machine-Telegram):
scp axiom/data/axiom.db user@SERVER:~/axiom-repo/axiom/data/axiom.db
```
Либо начать с чистой — она создастся автоматически при первом запуске.

## 4. Автозапуск (systemd) — веб-пульт
Скопируй юнит и включи:
```bash
sudo cp ~/axiom-repo/axiom/deploy/axiom-web.service /etc/systemd/system/
# поправь User= и пути внутри файла под свой логин (там путь ~/axiom-repo/axiom)
sudo systemctl daemon-reload
sudo systemctl enable --now axiom-web
sudo systemctl status axiom-web
journalctl -u axiom-web -f      # логи
```
Юнит запускает пульт на `127.0.0.1:8000` (только локально — это правильно).

## 5. WhatsApp (опционально, позже)
Node-мост Baileys запускается отдельно (см. `whatsapp/`). На сервере:
```bash
sudo apt install -y nodejs npm
cd whatsapp && npm install
# авторизация WhatsApp по pairing-коду: node index.js --auth <номер> --pair
```
Для старта проще сначала обкатать **только Telegram**, WhatsApp подключить вторым шагом.

## 6. Доступ к панели — SSH-туннель (рекомендуется)
С твоего компа:
```bash
ssh -N -L 8000:127.0.0.1:8000 user@SERVER
```
Затем открываешь `http://127.0.0.1:8000` в браузере — это панель на сервере,
наружу порт не торчит. Закрыл ssh — доступ закрыт. Безопасно и без логина.

Альтернативы (если нужен постоянный доступ из браузера без ssh):
- nginx + HTTP Basic-Auth + HTTPS (домен) — тогда можно открыть наружу;
- firewall (ufw) — пускать 8000 только с твоего IP;
- дождаться логина через Telegram (Волна U) — когда будет домен.

## 7. Что проверить «в бою» по нарастающей
1. Панель открывается (через туннель), разделы грузятся.
2. Прокси-пул: «Обновить пул» → есть живые.
3. Прогрев одного аккаунта: 1 ступень, смотрим лог `journalctl`.
4. Прослушка чатов по нишам → очередь находок.
5. Микро-рассылка кампании (limit 2-3) → реальные ответы → агент отвечает.
6. Наращиваем объёмы.

## Обновление кода на сервере
```bash
cd ~/axiom-repo && git pull && cd axiom && source .venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart axiom-web
```
