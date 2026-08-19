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


def _notify_down(exc: Exception) -> None:
    """Календарь отвалился — сказать в колокольчик, а не молчать в логах сервера.

    Раньше единственным следом был print() в консоль systemd — узнавали об этом
    только когда встреча состоялась без ссылки в личном календаре, и то не всегда.
    Событие деградирует мягко и дальше (зум-ссылка и напоминание клиенту не зависят
    от Google Calendar), но оператор должен УЗНАТЬ, а не догадываться постфактум.

    invalid_grant отдельно: это истёкший/отозванный refresh-токен, чинится только
    руками в браузере (см. журнал деплоя) — не транзиентная сетевая ошибка, которая
    сама пройдёт. Дедуп 6 часов по DB (не in-memory — процессов несколько: веб и
    планировщик), чтобы пачка встреч подряд не завалила ленту одинаковыми записями."""
    import time

    from db import database

    msg = str(exc)
    if "invalid_grant" in msg:
        title = "🔴 Google Calendar отключился — токен просрочен"
        hint = ("Refresh-токен OAuth умер (обычно потому что проект в Google Cloud "
                "остался в статусе «Testing» — там токен живёт максимум 7 дней). "
                "Встречи и ссылка на созвон всё равно уходят клиенту, но событие в "
                "твой личный календарь не попадает. Почини разово: Google Cloud "
                f"Console → проект «{_project_id()}» → APIs & Services → OAuth "
                "consent screen → Publish App, затем удали google_token.json и "
                "заново открой «Календарь» в пульте для повторного входа.")
    else:
        title = "🔴 Google Calendar не отвечает"
        hint = msg[:200]
    try:
        with database.get_conn() as conn:
            last = database.get_setting(conn, "calendar_error_ts", "0")
            prev = database.get_setting(conn, "calendar_error_sig", "")
            if title == prev and (time.time() - float(last or 0)) < 21600:
                return
            database.set_setting(conn, "calendar_error_ts", str(time.time()))
            database.set_setting(conn, "calendar_error_sig", title)
            database.add_event(conn, "calendar_error", title, hint, level="warn")
    except Exception:  # noqa: BLE001 — уведомление не должно ронять создание встречи
        pass


def _project_id() -> str:
    """project_id из google_credentials.json — чтобы в подсказке была прямая ссылка
    на нужный проект, а не общее «зайди в консоль» без ориентира."""
    try:
        import json
        raw = json.loads(Path(config.GOOGLE_CREDENTIALS_FILE).read_text(encoding="utf-8"))
        return next(iter(raw.values())).get("project_id", "")
    except Exception:  # noqa: BLE001
        return ""


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
        _notify_down(e)
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
        _notify_down(e)
        return None


def update_event(
    event_id: str, start: datetime, duration_min: int, tz: str,
    summary: str | None = None, description: str | None = None,
) -> dict | None:
    """Двигает существующее событие на новое время. Возвращает {'id','htmlLink'} или None.

    Нужно для переносов: человек соглашается на созвон, потом просит «давайте не в
    четверг, а в пятницу». Создания было мало — второй insert плодил дубль (так в
    календаре и оказалось 22 копии одной встречи), а без переноса событие оставалось
    висеть на старом времени, и напоминание уходило не тогда.

    patch, а не update: PATCH меняет только переданные поля и не затирает то, что
    оператор мог поправить в самом Google Calendar руками (участников, напоминания,
    заметки). Summary/description трогаем, только если их явно передали."""
    if not enabled() or not event_id:
        return None
    try:
        svc = _service()
        end = start + timedelta(minutes=duration_min)
        body: dict = {
            "start": {"dateTime": start.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
        }
        if summary is not None:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        ev = svc.events().patch(calendarId="primary", eventId=event_id, body=body).execute()
        return {"id": ev.get("id"), "htmlLink": ev.get("htmlLink")}
    except Exception as e:
        print(f"[calendar update error] {e}")
        _notify_down(e)
        return None
