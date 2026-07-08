#!/usr/bin/env bash
# AXIOM — установка на чистый Ubuntu-VPS одной командой.
# Запуск на сервере:
#   curl -fsSL https://raw.githubusercontent.com/vassiliypelts-stack/axiom/main/axiom/deploy/install.sh | bash
# После: заполни .env, (опц.) залей data/axiom.db, включи сервис (скрипт подскажет).
set -euo pipefail

REPO="https://github.com/vassiliypelts-stack/axiom.git"
DIR="$HOME/axiom-repo"
APP="$DIR/axiom"

echo "== 1/5 системные пакеты =="
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git

echo "== 2/5 код с GitHub =="
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi

echo "== 3/5 виртуальное окружение + зависимости =="
cd "$APP"
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
mkdir -p data
[ -f .env ] || cp .env.example .env

echo "== 4/5 автозапуск (systemd) под пользователя $USER =="
sudo tee /etc/systemd/system/axiom-web.service >/dev/null <<UNIT
[Unit]
Description=AXIOM web pult (FastAPI) + scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP/.venv/bin/python -m web.app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload

echo "== 5/5 готово =="
cat <<NEXT

────────────────────────────────────────────────────────
AXIOM установлен в:  $APP
Дальше 3 шага:

1) Секреты — впиши ключи/сессии:
     nano $APP/.env
   (минимум: ANTHROPIC_API_KEY, TG_API_ID, TG_API_HASH, TG_STRING_SESSION,
    при CIS-регионе — HTTPS_PROXY для Claude)

2) (Опционально) залей свою базу с аккаунтами:
     положи файл в  $APP/data/axiom.db
   (или пропусти — создастся чистая при первом старте)

3) Запусти и смотри логи:
     sudo systemctl enable --now axiom-web
     journalctl -u axiom-web -f

Доступ к пульту с локального компа (наружу порт НЕ открыт):
     ssh -N -L 8000:127.0.0.1:8000 <user>@<IP_сервера>
   затем открой в браузере http://127.0.0.1:8000
────────────────────────────────────────────────────────
NEXT
