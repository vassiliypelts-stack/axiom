"""Read Google Contacts, or their CSV export, and prepare a selective import.

This module never sends messages or writes to Google. Calendar authorization is
kept separate. Every search reads all pages: People API search is prefix based,
but the operator's markers can be at the end of a contact's name.
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from pathlib import Path

import config

SCOPE = "https://www.googleapis.com/auth/contacts.readonly"
TOKEN_FILE = Path(config.BASE_DIR) / "google_contacts_token.json"
PEOPLE_URL = "https://people.googleapis.com/v1/people/me/connections"


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


def from_people(people: list[dict]) -> list[dict]:
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
        result.append(dict(name=name, aliases=aliases, given_name=primary.get('givenName', ''),
                           phones=phones, email=emails[0].get('value', '')))
    return result


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
                           email=row.get('e-mail 1 - value') or row.get('email') or ''))
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
    response = session.get(PEOPLE_URL, params=params, timeout=30)
    if response.status_code == 403:
        raise ContactsError("Google не разрешил чтение контактов. Проверьте разрешение contacts.readonly и включение People API в Google Cloud.")
    if response.status_code == 401:
        raise ContactsError("Доступ к Google истёк. Подключите контакты заново.")
    if response.status_code == 429:
        raise ContactsError("Google временно ограничил запросы. Повторите поиск позже.")
    response.raise_for_status()
    return response.json()


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
    params = {'pageSize': 1000, 'personFields': 'names,phoneNumbers,emailAddresses',
              'sources': 'READ_SOURCE_TYPE_CONTACT'}
    with _session(creds) as session:
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
    return from_people(people)
