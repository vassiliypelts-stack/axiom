"""Google Calendar: создание события встречи.

Нужно: файл OAuth-клиента (GOOGLE_CREDENTIALS_FILE, тип «Desktop app» из Google Cloud,
Calendar API включён). Первый запуск откроет браузер для согласия и сохранит токен в
GOOGLE_TOKEN_FILE. Дальше — без браузера.

Нет файла доступа → enabled()=False, create_event() вернёт None.
google-* либы импортируются лениво, чтобы модуль грузился даже без них.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import config

_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def enabled() -> bool:
    return Path(config.GOOGLE_CREDENTIALS_FILE).exists()


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = Path(config.GOOGLE_TOKEN_FILE)
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(config.GOOGLE_CREDENTIALS_FILE, _SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def list_events(days_ahead: int = 21, max_results: int = 50) -> list[dict] | None:
    """Ближайшие события из основного Google-календаря (для показа в Axiom).
    None = не подключено/ошибка. Иначе [{id, summary, start, end, link, location}]."""
    if not enabled():
        return None
    try:
        from datetime import timezone

        svc = _service()
        now = datetime.now(timezone.utc).isoformat()
        end = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).isoformat()
        res = svc.events().list(
            calendarId="primary", timeMin=now, timeMax=end,
            singleEvents=True, orderBy="startTime", maxResults=max_results,
        ).execute()
        out = []
        for ev in res.get("items", []):
            s, e = ev.get("start", {}), ev.get("end", {})
            out.append({
                "id": ev.get("id"),
                "summary": ev.get("summary") or "(без названия)",
                "start": s.get("dateTime") or s.get("date"),
                "end": e.get("dateTime") or e.get("date"),
                "link": ev.get("htmlLink"),
                "location": ev.get("location"),
            })
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[calendar list error] {e}")
        return None


def create_event(
    summary: str, start: datetime, duration_min: int, tz: str,
    description: str = "", attendees: list[str] | None = None,
) -> dict | None:
    """Создаёт событие в основном календаре. Возвращает {'id', 'htmlLink'} или None."""
    if not enabled():
        return None
    try:
        svc = _service()
        end = start + timedelta(minutes=duration_min)
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
        }
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        ev = svc.events().insert(calendarId="primary", body=body).execute()
        return {"id": ev.get("id"), "htmlLink": ev.get("htmlLink")}
    except Exception as e:
        print(f"[calendar error] {e}")
        return None
