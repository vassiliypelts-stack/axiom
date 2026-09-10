"""Google Calendar: создание события встречи.

Нужно: файл OAuth-клиента (GOOGLE_CREDENTIALS_FILE, тип «Web application» из Google
Cloud, Calendar API включён, в Authorized redirect URIs — адрес из redirect_uri()).
Согласие даётся один раз в браузере ОПЕРАТОРА через кнопку в пульте (/api/gcal/auth),
токен сохраняется в GOOGLE_TOKEN_FILE, дальше всё молча обновляется само.

Нет файла доступа → enabled()=False; нет токена → authorized()=False и _service()
бросает NeedsAuth. create_event() в обоих случаях вернёт None.
google-* либы импортируются лениво, чтобы модуль грузился даже без них.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import config


class NeedsAuth(Exception):
    """Нужно согласие оператора в браузере — не «сломалось», а «ещё не вошли»."""


# calendar.events хватает на создание/перенос встреч, но не на чтение чужих/всех
# событий основного календаря — а пульт показывает ленту «что у меня на неделе».
# readonly добавлен именно ради показа. Порядок важен: Google возвращает scope в
# ответе, и рассинхрон списка приводит к вечному «Scope has changed» при refresh.
_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def enabled() -> bool:
    return Path(config.GOOGLE_CREDENTIALS_FILE).exists()


def authorized() -> bool:
    """Файл клиента лежит И согласие уже получено. enabled() отвечает только на
    первое: без этой пары пульт писал «подключено», пока первый же запрос не падал
    в попытку открыть браузер на сервере."""
    return enabled() and Path(config.GOOGLE_TOKEN_FILE).exists()


def redirect_uri() -> str:
    """Куда Google вернёт оператора после согласия. Тот же адрес нужно вписать в
    OAuth-клиенте (Authorized redirect URIs), иначе Google ответит redirect_uri_mismatch."""
    return config.PUBLIC_URL.rstrip("/") + "/api/gcal/callback"


def _flow():
    """Web-flow вместо InstalledAppFlow.run_local_server().

    run_local_server() поднимал локальный сервер и открывал браузер В ТОМ ЖЕ
    процессе — на GCP-сервере браузера нет, поэтому первый же заход в «Календарь»
    после протухания токена вешал запрос вместо того, чтобы дать войти. Здесь
    согласие даёт оператор в СВОЁМ браузере, а сервер только принимает код."""
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_secrets_file(
        config.GOOGLE_CREDENTIALS_FILE, scopes=_SCOPES, redirect_uri=redirect_uri(),
    )


def auth_url() -> str:
    """Ссылка на согласие Google. prompt=consent + access_type=offline —
    чтобы refresh_token пришёл ОБЯЗАТЕЛЬНО: без него токен живёт час, и календарь
    «отваливается» к вечеру того же дня."""
    url, _ = _flow().authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent",
    )
    return url


def finish_auth(code: str) -> None:
    """Меняем код из редиректа на токен и сохраняем. Бросает — вызывающий покажет."""
    flow = _flow()
    flow.fetch_token(code=code)
    # Перезаписываем безусловно: сюда приходят в том числе поверх мёртвого
    # (invalid_grant) токена — именно ради того, чтобы его заменить.
    Path(config.GOOGLE_TOKEN_FILE).write_text(flow.credentials.to_json(), encoding="utf-8")


def disconnect() -> None:
    """Забыть согласие (кнопка «отключить» / принудительный перезаход)."""
    Path(config.GOOGLE_TOKEN_FILE).unlink(missing_ok=True)


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
                "твой личный календарь не попадает. Чинится в пульте: раздел "
                "«Календарь» → кнопка «Подключить Google-календарь». Чтобы не "
                "повторялось каждую неделю — Google Cloud Console → проект "
                f"«{_project_id()}» → APIs & Services → OAuth consent screen → Publish App.")
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
    # Проверка токена ДО импорта google-либ: иначе на машине без зависимостей
    # (и при кривой установке на сервере) вместо честного «нужен вход» прилетал
    # ImportError, который выше читался как «календарь сломался».
    token_path = Path(config.GOOGLE_TOKEN_FILE)
    if not token_path.exists():
        raise NeedsAuth("Google-календарь ещё не подключён")

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:  # noqa: BLE001
                # invalid_grant = refresh-токен отозван/просрочен (частый случай:
                # проект в Google Cloud остался в статусе Testing, где токен живёт
                # 7 дней). Чинится ТОЛЬКО повторным входом, поэтому это needs_auth
                # с кнопкой, а не «Google не отвечает» с предложением подождать.
                if "invalid_grant" in str(e):
                    raise NeedsAuth("Доступ к Google отозван — нужен повторный вход") from e
                raise
            token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            # Refresh невозможен (нет refresh_token или он отозван) — раньше здесь
            # молча открывался браузер на сервере и запрос висел. Теперь честно
            # говорим «нужен повторный вход», а пульт показывает кнопку.
            raise NeedsAuth("Требуется повторный вход в Google")
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
    except NeedsAuth:
        raise            # «не вошли» — это не авария, наверх, там покажут кнопку входа
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
    except NeedsAuth:
        print(f"[calendar error] не подключён Google-календарь — нужен вход в пульте")
        return None
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
    except NeedsAuth:
        print(f"[calendar update error] не подключён Google-календарь — нужен вход в пульте")
        return None
    except Exception as e:
        print(f"[calendar update error] {e}")
        _notify_down(e)
        return None
