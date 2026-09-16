"""Marker search → preview → explicit selection → a draft/paused campaign."""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from db import database
from integrations import google_contacts as gc

router = APIRouter()
_previews: dict = {}
_lock = threading.Lock()
TTL = 30 * 60


def _error(exc):
    # OAuth/network exceptions can contain request details; do not echo them.
    msg = str(exc) if isinstance(exc, gc.ContactsError) else (
        "Не удалось прочитать Google Контакты. Проверьте подключение и повторите поиск; "
        "также можно загрузить экспорт Google CSV.")
    return JSONResponse({'error': msg}, status_code=400)


def _phone_index(conn):
    index = {}
    for row in conn.execute('SELECT id,phone,status,deleted_at,is_test,outreach_campaign_id FROM contacts WHERE phone IS NOT NULL'):
        key = gc.phone_key(row['phone'])
        if key:
            index.setdefault(key, []).append(dict(row))
    return index


def make_preview(rows, query):
    selected = gc.select_rows(rows, query)
    if len(selected) > 2000:
        raise gc.ContactsError("Найдено больше 2000 контактов. Уточните маркеры.")
    database.init_db()
    with database.get_conn() as conn:
        index = _phone_index(conn)
    for i, row in enumerate(selected):
        row['id'] = str(i)
        row['existing'] = {p: index.get(p, []) for p in row['phones']}
        row.pop('aliases', None)
    token = secrets.token_urlsafe(24)
    with _lock:
        now = time.time()
        for key in list(_previews):
            if _previews[key]['expires'] < now:
                del _previews[key]
        if len(_previews) >= 10:
            del _previews[next(iter(_previews))]
        _previews[token] = dict(expires=now + TTL, rows=selected, query=query)
    return dict(token=token, scanned=len(rows), matched=len(selected), items=selected)


@router.get('/assets/google-contacts.js')
def script():
    return FileResponse(Path(__file__).with_name('google-contacts.js'),
                        media_type='application/javascript', headers={'Cache-Control': 'no-cache'})


@router.get('/api/google-contacts/status')
def status():
    return {'connected': gc.TOKEN_FILE.exists()}


@router.post('/api/google-contacts/token')
async def upload_token(file: UploadFile = File(...)):
    try:
        raw = await file.read(65537)
        if len(raw) > 65536:
            raise gc.ContactsError('Файл токена слишком большой.')
        await run_in_threadpool(gc.save_token, raw.decode('utf-8-sig'))
        with _lock:
            _previews.clear()
        return {'ok': True}
    except Exception as exc:
        return _error(exc)


@router.post('/api/google-contacts/preview')
def preview(payload: dict = Body(...)):
    try:
        query = str(payload.get('markers') or '')
        gc.markers(query)  # Reject empty searches before reading the address book.
        return make_preview(gc.read_contacts(), query)
    except Exception as exc:
        return _error(exc)


@router.post('/api/google-contacts/csv-preview')
async def csv_preview(file: UploadFile = File(...), markers: str = Form(...)):
    try:
        gc.markers(markers)
        raw = await file.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise gc.ContactsError('Максимальный размер CSV — 10 МБ.')
        return await run_in_threadpool(lambda: make_preview(gc.from_csv(raw), markers))
    except Exception as exc:
        return _error(exc)


def import_selection(conn, snapshot, choices, campaign_id=None, campaign_name=''):
    """Caller holds a write transaction. Recheck duplicates and campaign state."""
    if not isinstance(choices, list) or not 1 <= len(choices) <= 500:
        raise gc.ContactsError('Выберите от 1 до 500 контактов.')
    rows = {r['id']: r for r in snapshot['rows']}
    selected, seen = [], set()
    for choice in choices:
        row = rows.get(str(choice.get('id')))
        phone = choice.get('phone', '')
        name = str(choice.get('person_name') or '').strip()
        if not row or phone not in row['phones']:
            raise gc.ContactsError('Выбор не соответствует предпросмотру. Повторите поиск.')
        if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
            raise gc.ContactsError('Заполните имя для обращения у каждого выбранного контакта.')
        if any(m in gc.compact(name) for m in gc.markers(snapshot['query'])):
            raise gc.ContactsError('Уберите поисковый маркер из имени для обращения.')
        if phone not in seen:
            selected.append((row, phone, name))
            seen.add(phone)
    camp = None
    if campaign_id:
        camp = conn.execute('SELECT * FROM campaigns WHERE id=?', (int(campaign_id),)).fetchone()
        if not camp or camp['status'] not in ('draft', 'paused') or camp['archived']:
            raise gc.ContactsError('Выберите черновик или кампанию на паузе. Работающую сначала остановите в «Кампаниях».')
        if not (camp['audience_tag'] or '').strip():
            raise gc.ContactsError('У этой кампании не задана аудитория. Задайте отдельный тег в настройках кампании или создайте новый черновик.')
        if any(c in camp['audience_tag'] for c in ('%', '_')):
            raise gc.ContactsError('В теге кампании есть шаблонные символы % или _. Выберите отдельный буквенный тег.')
    elif not campaign_name.strip() or len(campaign_name) > 150:
        raise gc.ContactsError('Введите название нового черновика (до 150 символов).')
    index = _phone_index(conn)
    ready, skipped = [], []
    for row, phone, name in selected:
        matches = index.get(phone, [])
        reason = ''
        if len(matches) > 1:
            reason = 'В CRM несколько карточек с этим телефоном — требуется разбор дублей.'
        elif matches:
            old = matches[0]
            if old['deleted_at'] or old['is_test'] or old['status'] != 'new':
                reason = 'Контакт уже в работе, тестовый или в корзине; повторную рассылку не включали.'
            elif old['outreach_campaign_id'] and old['outreach_campaign_id'] != campaign_id:
                reason = 'Контакт закреплён за другой кампанией.'
        if reason:
            skipped.append(dict(name=row['name'], reason=reason))
        else:
            ready.append((row, phone, name, matches[0]['id'] if matches else None))
    if not ready:
        return dict(ok=True, added=0, existing=0, campaign_id=campaign_id, skipped=skipped)
    if camp is None:
        tag = 'Google-' + secrets.token_hex(8)
        campaign_id = conn.execute(
            "INSERT INTO campaigns (name,audience_tag,status,channel,auto_send,daily_limit) "
            "VALUES (?,?,'draft','telegram',0,5)", (campaign_name.strip(), tag)).lastrowid
    else:
        tag = camp['audience_tag']
    added = existing = 0
    ids = []
    for row, phone, name, cid in ready:
        if cid is None:
            cid = conn.execute(
                "INSERT INTO contacts (source,phone,name,person_name,email,tags,outreach_campaign_id) "
                "VALUES ('google_contacts',?,?,?,?,?,?)",
                (phone, row['name'], name, row.get('email') or None, tag, campaign_id)).lastrowid
            added += 1
        else:
            old = conn.execute('SELECT tags FROM contacts WHERE id=?', (cid,)).fetchone()
            tags = database._merge_tags(old['tags'], tag)
            # Preserve CRM name, conversation, source, and enrichment on repeat import.
            conn.execute("UPDATE contacts SET tags=?,outreach_campaign_id=?, "
                         "person_name=COALESCE(NULLIF(person_name,''),?),updated_at=datetime('now') WHERE id=?",
                         (tags, campaign_id, name, cid))
            existing += 1
        ids.append(cid)
    return dict(ok=True, added=added, existing=existing, campaign_id=campaign_id,
                contact_ids=ids, skipped=skipped)


@router.post('/api/google-contacts/import')
def import_contacts(payload: dict = Body(...)):
    try:
        with _lock:
            snapshot = _previews.get(str(payload.get('token') or ''))
        if not snapshot or snapshot['expires'] < time.time():
            raise gc.ContactsError('Предпросмотр устарел. Повторите поиск и выбор контактов.')
        database.init_db()
        with database.get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            result = import_selection(conn, snapshot, payload.get('selected'),
                                      int(payload['campaign_id']) if payload.get('campaign_id') else None,
                                      str(payload.get('campaign_name') or ''))
        return result
    except Exception as exc:
        return _error(exc)
