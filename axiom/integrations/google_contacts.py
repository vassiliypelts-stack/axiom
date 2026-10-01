"""Read Google Contacts, or their CSV export, and prepare a selective import.

This module never sends messages or writes to Google. Calendar authorization is
kept separate. Every search reads all pages: People API search is prefix based,
but the operator's markers can be at the end of a contact's name.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config

SCOPE = "https://www.googleapis.com/auth/contacts.readonly"
TOKEN_FILE = Path(config.BASE_DIR) / "google_contacts_token.json"
PEOPLE_URL = "https://people.googleapis.com/v1/people/me/connections"
GROUPS_URL = "https://people.googleapis.com/v1/contactGroups"
# Google accepts plain http only for localhost, and the dashboard lives on a bare
# IP over http. So consent returns to the operator's own localhost: the page fails
# to load, and the operator pastes its address (with ?code=...) back into AXIOM.
LOGIN_REDIRECT = "http://localhost:8765/"
_pending: dict = {}
_pending_lock = threading.Lock()


class ContactsError(ValueError):
    pass


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def markers(text: str) -> list[str]:
    """Comma/newline separated alternatives; ск 2025/2026 expands both years."""
    result = []
    for part in re.split(r"[,;\n]+", text):
        part = compact(part)
        if not part:
            continue
        match = re.fullmatch(r"(.+?)(20\d{2})((?:/20\d{2})+)", part)
        result.extend([match[1] + year for year in [match[2], *match[3][1:].split('/')]]
                      if match else [part])
    result = list(dict.fromkeys(result))
    if not result or len(result) > 30 or any(len(x) < 3 or len(x) > 80 for x in result):
        raise ContactsError("Введите от 1 до 30 маркеров длиной от 3 до 80 символов.")
    return result


def phone_key(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    elif len(digits) == 10:
        digits = '7' + digits
    return '+' + digits if 10 <= len(digits) <= 15 else ''


def from_people(people: list[dict], groups: dict | None = None) -> list[dict]:
    """groups maps contactGroups/<id> to the label name; system groups are absent."""
    groups = groups or {}
    result = []
    for person in people:
        if person.get('metadata', {}).get('deleted'):
            continue
        names = person.get('names') or []
        primary = next((n for n in names if n.get('metadata', {}).get('primary')), names[0] if names else {})
        name = primary.get('displayName') or ''
        # Include all stored names when matching, even if displayName is a profile.
        aliases = [n.get('displayName', '') for n in names]
        phones = list(dict.fromkeys(filter(None, (
            phone_key(p.get('canonicalForm') or p.get('value', ''))
            for p in person.get('phoneNumbers', [])))))
        emails = person.get('emailAddresses') or [{}]
        labels = [groups[m['contactGroupMembership']['contactGroupResourceName']]
                  for m in person.get('memberships', [])
                  if m.get('contactGroupMembership', {}).get('contactGroupResourceName') in groups]
        result.append(dict(name=name, aliases=aliases, given_name=primary.get('givenName', ''),
                           phones=phones, email=emails[0].get('value', ''),
                           groups=list(dict.fromkeys(labels))))
    return result


def csv_labels(raw: str) -> list[str]:
    """Google CSV: "Labels" (new) or "Group Membership" (old), joined by " ::: ".
    System groups come with a "* " prefix; only operator labels are useful here."""
    return list(dict.fromkeys(
        part.strip() for part in (raw or '').split(':::')
        if part.strip() and not part.strip().startswith('*')))


def from_csv(raw: bytes) -> list[dict]:
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ContactsError("Сохраните экспорт Google Контактов в CSV UTF-8.") from exc
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    result = []
    for row in reader:
        row = {(k or '').strip().casefold(): (v or '') for k, v in row.items() if isinstance(v, str)}
        name = row.get('name') or row.get('имя') or ' '.join(filter(None, (
            row.get('first name') or row.get('given name'),
            row.get('middle name') or row.get('additional name'),
            row.get('last name') or row.get('family name'))))
        phones = []
        for key, value in row.items():
            if re.fullmatch(r'phone \d+ - value', key) or key in ('phone', 'телефон'):
                phones.extend(filter(None, (phone_key(p) for p in value.split(':::'))))
        result.append(dict(name=name, aliases=[name],
                           given_name=row.get('first name') or row.get('given name') or '',
                           phones=list(dict.fromkeys(phones)),
                           email=row.get('e-mail 1 - value') or row.get('email') or '',
                           groups=csv_labels(row.get('labels') or row.get('group membership'))))
    if not any(r['name'] for r in result):
        raise ContactsError("Не найдены имена. Выберите формат экспорта «Google CSV».")
    return result


def select_rows(rows: list[dict], query: str) -> list[dict]:
    terms = markers(query)
    result = []
    for row in rows:
        names = [row['name'], *row.get('aliases', [])]
        found = [term for term in terms if any(term in compact(n) for n in names)]
        if not found:
            continue
        given = row.get('given_name', '').strip()
        # A marker in Google's givenName is a label, not a usable salutation.
        if any(term in compact(given) for term in terms):
            given = ''
        result.append({**row, 'matched': found, 'person_name': given})
    return result


def _credentials(raw: str):
    from google.oauth2.credentials import Credentials
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not all(data.get(k) for k in ('client_id', 'client_secret', 'refresh_token')):
            raise ValueError()
        scopes = data.get('scopes') or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        if SCOPE not in scopes:
            raise ContactsError("В файле нет разрешения на чтение контактов. Токен календаря не подходит.")
        # Never follow a token_uri supplied by an uploaded file.
        data['token_uri'] = 'https://oauth2.googleapis.com/token'
        return Credentials.from_authorized_user_info(data, scopes=[SCOPE])
    except ContactsError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise ContactsError("Нужен файл google_contacts_token.json, полученный входом в Google.") from exc


def _session(creds):
    from google.auth.transport.requests import AuthorizedSession
    return AuthorizedSession(creds)


def _page(session, params):
    return _request(session, PEOPLE_URL, params)


def _request(session, url, params):
    response = session.get(url, params=params, timeout=30)
    if response.status_code == 403:
        raise ContactsError("Google не разрешил чтение контактов. Проверьте разрешение contacts.readonly и включение People API в Google Cloud.")
    if response.status_code == 401:
        raise ContactsError("Доступ к Google истёк. Подключите контакты заново.")
    if response.status_code == 429:
        raise ContactsError("Google временно ограничил запросы. Повторите поиск позже.")
    response.raise_for_status()
    return response.json()


def _groups(session) -> dict:
    """Operator labels by resource name. A failure here must not hide the contacts
    themselves: the list still works, only the label sidebar stays empty."""
    result, params = {}, {'pageSize': 1000, 'groupFields': 'name,groupType'}
    try:
        while True:
            data = _request(session, GROUPS_URL, params)
            for group in data.get('contactGroups', []):
                if group.get('groupType') == 'USER_CONTACT_GROUP' and group.get('name'):
                    result[group['resourceName']] = group['name']
            if not data.get('nextPageToken'):
                return result
            params['pageToken'] = data['nextPageToken']
    except Exception as exc:  # noqa: BLE001
        print(f"[google contacts] labels unavailable: {type(exc).__name__}")
        return {}


def _client_file() -> Path:
    path = Path(config.GOOGLE_CREDENTIALS_FILE)
    if not path.exists():
        raise ContactsError("На сервере нет OAuth-клиента Google. Загрузите google_credentials.json в разделе «Календарь».")
    return path


def _redirect(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except ValueError as exc:
        raise ContactsError("Файл OAuth-клиента Google повреждён. Загрузите его заново в «Календаре».") from exc
    uris = (data.get('web') or {}).get('redirect_uris') or []
    # The console may already list localhost even if the file was downloaded earlier,
    # so fall back to the documented address instead of refusing.
    return next((u for u in uris if u.startswith(('http://localhost', 'http://127.0.0.1'))), LOGIN_REDIRECT)


def _flow(path: Path, redirect: str, **kwargs):
    from google_auth_oauthlib.flow import Flow
    # Google may echo previously granted scopes; that is not an error for a read-only token.
    os.environ.setdefault('OAUTHLIB_RELAX_TOKEN_SCOPE', '1')
    return Flow.from_client_secrets_file(str(path), scopes=[SCOPE], redirect_uri=redirect, **kwargs)


def auth_start() -> str:
    """Consent link for the operator's browser. The PKCE verifier stays on the server."""
    path = _client_file()
    redirect = _redirect(path)
    state = secrets.token_urlsafe(24)
    flow = _flow(path, redirect, state=state, autogenerate_code_verifier=True)
    url, _ = flow.authorization_url(access_type='offline', prompt='consent')
    with _pending_lock:
        now = time.time()
        for key in [k for k, v in _pending.items() if v['expires'] < now]:
            del _pending[key]
        _pending[state] = dict(verifier=flow.code_verifier, redirect=redirect, expires=now + 15 * 60)
    return url


def parse_return(pasted: str) -> tuple[str, str]:
    """(code, state) from the localhost address in the browser bar, or a bare code."""
    pasted = (pasted or '').strip()
    if '?' in pasted:
        query = parse_qs(urlparse(pasted).query)
        if query.get('error'):
            raise ContactsError("Google: доступ не разрешён. Нажмите «Подключить» и разрешите чтение контактов.")
        code, state = (query.get('code') or [''])[0], (query.get('state') or [''])[0]
    else:
        code, state = (pasted if re.fullmatch(r'[\w/.~-]{20,}', pasted) else ''), ''
    if not code:
        raise ContactsError("Вставьте адрес из вкладки, куда Google вернул после разрешения (начинается с http://localhost).")
    return code, state


def auth_finish(pasted: str) -> None:
    code, state = parse_return(pasted)
    with _pending_lock:
        if not state and _pending:
            state = max(_pending, key=lambda k: _pending[k]['expires'])
        pending = _pending.pop(state, None)
    if not pending or pending['expires'] < time.time():
        raise ContactsError("Ссылка на вход устарела. Нажмите «Подключить» ещё раз.")
    flow = _flow(_client_file(), pending['redirect'], state=state, code_verifier=pending['verifier'])
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001
        raise ContactsError("Google не принял код: он одноразовый и живёт несколько минут. Нажмите «Подключить» ещё раз.") from exc
    if not flow.credentials.refresh_token:
        raise ContactsError("Google не выдал постоянный доступ. Отзовите доступ на myaccount.google.com/permissions и подключите снова.")
    save_token(flow.credentials.to_json())


def disconnect() -> None:
    TOKEN_FILE.unlink(missing_ok=True)


def save_token(raw: str) -> None:
    creds = _credentials(raw)
    with _session(creds) as session:
        _page(session, {'pageSize': 1, 'personFields': 'names',
                        'sources': 'READ_SOURCE_TYPE_CONTACT'})
    # Validate with Google before replacing the previous working token.
    TOKEN_FILE.write_text(creds.to_json(), encoding='utf-8')


def read_contacts() -> list[dict]:
    if not TOKEN_FILE.exists():
        raise ContactsError("Подключите Google Контакты или загрузите экспорт Google CSV.")
    creds = _credentials(TOKEN_FILE.read_text(encoding='utf-8'))
    people = []
    params = {'pageSize': 1000, 'personFields': 'names,phoneNumbers,emailAddresses,memberships',
              'sources': 'READ_SOURCE_TYPE_CONTACT'}
    with _session(creds) as session:
        groups = _groups(session)
        seen = set()
        while True:
            data = _page(session, params)
            people.extend(data.get('connections', []))
            token = data.get('nextPageToken')
            if not token:
                break
            if token in seen:
                raise ContactsError("Google повторил страницу. Повторите поиск.")
            seen.add(token)
            params['pageToken'] = token
    TOKEN_FILE.write_text(creds.to_json(), encoding='utf-8')
    return from_people(people, groups)
