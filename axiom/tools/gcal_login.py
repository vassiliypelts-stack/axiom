"""Разовый вход в Google Calendar на машине оператора → google_token.json.

Зачем отдельный скрипт, а не кнопка в пульте: Google принимает в Authorized
redirect URIs только https либо http://localhost. Пульт слушает голый http:8000
(порты 80/443 наружу закрыты, сертификата нет), а localhost сервера — не браузер
оператора. Поэтому согласие даётся здесь, локально, где localhost настоящий,
а полученный токен заливается в пульт кнопкой «🔑 Загрузить google_token.json».

Делать это нужно ОДИН раз: токен самообновляемый, refresh_token живёт, пока его
не отозвали (при условии, что приложение в Google Cloud опубликовано — в статусе
Testing Google убивает токен через 7 дней).

Запуск:
    pip install google-auth-oauthlib google-api-python-client
    python tools/gcal_login.py            # рядом нужен google_credentials.json

В Google Cloud → Credentials → твой OAuth-клиент → Authorized redirect URIs
должен быть добавлен http://localhost:8765/ (порт ниже).
"""
from __future__ import annotations

import sys
from pathlib import Path

PORT = 8765
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def main() -> int:
    here = Path(__file__).resolve().parent
    creds_file = next(
        (p for p in (here.parent / "google_credentials.json", here / "google_credentials.json")
         if p.exists()), None)
    if creds_file is None:
        print("Не нашёл google_credentials.json — положи его рядом со скриптом "
              "или в папку axiom/ и запусти снова.")
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Нет библиотеки. Выполни:\n    pip install google-auth-oauthlib google-api-python-client")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    # access_type/prompt — чтобы ОБЯЗАТЕЛЬНО пришёл refresh_token: без него доступ
    # умирает через час, и пульт «отвалится» тем же вечером.
    creds = flow.run_local_server(port=PORT, access_type="offline", prompt="consent")

    out = here.parent / "google_token.json"
    out.write_text(creds.to_json(), encoding="utf-8")
    print(f"\nГотово: {out}")
    print("Теперь в пульте: Календарь → «🔑 Загрузить google_token.json» → выбери этот файл.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
