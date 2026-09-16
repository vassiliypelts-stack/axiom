"""Offline integration checks: no Google account or live Telegram is touched."""
import ast
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from db import database
from integrations import google_contacts as gc
from web import google_contacts as web


def source_functions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(tree, str(path), 'exec'), namespace)
    return namespace


class ContactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(config, 'DB_PATH', Path(self.tmp.name) / 'test.db')
        self.patch.start()
        database.init_db()
        app = FastAPI()
        app.include_router(web.router)
        self.client = TestClient(app)
        web._previews.clear()

    def tearDown(self):
        self.client.close()
        self.patch.stop()
        # sqlite contexts commit but do not close. Collect unreachable handles on Windows.
        import gc as garbage
        garbage.collect()
        self.tmp.cleanup()

    def campaign(self, status='paused', tag='customers'):
        with database.get_conn() as conn:
            return conn.execute('INSERT INTO campaigns(name,status,audience_tag) VALUES (?,?,?)',
                                ('Test', status, tag)).lastrowid

    def preview(self, phone='+79161234567'):
        return web.make_preview([dict(name='Эск2025, Анна Москва', given_name='Анна',
                                      phones=[phone], email='')], 'ск 2025/2026')

    def send_import(self, preview, **kwargs):
        return self.client.post('/api/google-contacts/import', json={
            'token': preview['token'], 'campaign_name': 'Клиенты',
            'selected': [dict(id='0', phone=preview['items'][0]['phones'][0], person_name='Анна')], **kwargs})

    def test_marker_variants_and_year_shorthand(self):
        rows = [dict(name=n, phones=[], given_name='') for n in
                ['Анна ЭСК 2023', 'эск2025, Иван', 'Пётр ск 2026', 'Эск2024', 'Москва 2026']]
        self.assertEqual(len(gc.select_rows(rows, 'эск2023, ск2025/2026')), 3)
        with self.assertRaises(gc.ContactsError): gc.markers(' , ')

    def test_old_and_new_google_csv_formats(self):
        for raw in [
            'Name,Given Name,Phone 1 - Value\nАнна эск2025,Анна,8 (916) 123-45-67\n',
            'First Name,Last Name,Phone 1 - Value\nАнна,эск2025,+7 916 123 45 67\n']:
            rows = gc.select_rows(gc.from_csv(raw.encode('utf-8-sig')), 'ЭСК 2025')
            self.assertEqual(rows[0]['phones'], ['+79161234567'])
            self.assertIn('эск2025', rows[0]['name'])

    def test_csv_preview_does_not_import(self):
        raw = 'Name,Phone 1 - Value\nАнна эск2025,+79161234567\nБорис,+79161234568\n'
        response = self.client.post('/api/google-contacts/csv-preview',
            data={'markers':'эск2025'}, files={'file':('contacts.csv',raw.encode())})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['matched'], 1)
        with database.get_conn() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM contacts').fetchone()[0], 0)

    def test_all_google_pages_are_read_and_missing_phone_is_visible(self):
        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Creds:
            def to_json(self): return '{}'
        with patch.object(gc, 'TOKEN_FILE', Path(self.tmp.name)/'token.json'), \
             patch.object(gc, '_credentials', return_value=Creds()), \
             patch.object(gc, '_session', return_value=Session()), \
             patch.object(gc, '_page', side_effect=[
                 {'connections':[{'names':[{'displayName':'Другой'}]}], 'nextPageToken':'page2'},
                 {'connections':[{'names':[{'displayName':'эск2025'}]}]}]) as fetch:
            gc.TOKEN_FILE.write_text('{}')
            selected = gc.select_rows(gc.read_contacts(), 'эск2025')
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['phones'], [])

    def test_import_draft_and_exclude_other_campaign(self):
        other = self.campaign(status='running', tag=None)
        result = self.send_import(self.preview()).json()
        self.assertEqual(result['added'], 1)
        cid = result['campaign_id']
        with database.get_conn() as conn:
            row = conn.execute('SELECT * FROM contacts').fetchone()
            self.assertEqual(row['name'], 'Эск2025, Анна Москва')
            self.assertEqual(row['person_name'], 'Анна')
            self.assertEqual(row['outreach_campaign_id'], cid)
            camp = conn.execute('SELECT * FROM campaigns WHERE id=?',(cid,)).fetchone()
            self.assertEqual((camp['status'],camp['auto_send']), ('draft',0))
        ns = source_functions(config.BASE_DIR/'channels/campaign_send.py',
                              {'_channels','_audience'}, {'database':database})
        self.assertEqual(len(ns['_audience'](other,None,'telegram',10,verified_only=False)), 0)
        self.assertEqual(len(ns['_audience'](cid,camp['audience_tag'],'telegram',10,verified_only=False)), 1)
        ui = source_functions(config.BASE_DIR/'web/app.py',
                              {'_channel_clause','_audience_where','_audience_count'}, {'database':database})
        with database.get_conn() as conn:
            self.assertEqual(ui['_audience_count'](conn,other,None,'telegram'),0)
            self.assertEqual(ui['_audience_count'](conn,cid,camp['audience_tag'],'telegram'),1)

    def test_import_normalized_existing_phone_without_replacing_history(self):
        cid = self.campaign()
        with database.get_conn() as conn:
            conn.execute("INSERT INTO contacts(phone,name,person_name,notes) VALUES ('8 (916) 123-45-67','Анна CRM','Аня','История')")
        result = self.send_import(self.preview(), campaign_id=cid).json()
        self.assertEqual((result['added'],result['existing']), (0,1))
        with database.get_conn() as conn:
            row = conn.execute('SELECT * FROM contacts').fetchone()
            self.assertEqual((row['name'],row['person_name'],row['notes']), ('Анна CRM','Аня','История'))
            self.assertEqual(conn.execute('SELECT count(*) FROM contacts').fetchone()[0],1)
        again = self.send_import(self.preview(), campaign_id=cid).json()
        self.assertEqual(again['added'],0)

    def test_no_reactivation_of_existing_contact_or_move_between_campaigns(self):
        for state, deleted, test, bound in [('won',None,0,None),('new','2026-01-01',0,None),
                                          ('new',None,1,None),('new',None,0,99)]:
            with database.get_conn() as conn:
                conn.execute('DELETE FROM contacts')
                conn.execute('INSERT INTO contacts(phone,status,deleted_at,is_test,outreach_campaign_id) VALUES (?,?,?,?,?)',
                             ('+79161234567',state,deleted,test,bound))
            data = self.send_import(self.preview()).json()
            self.assertEqual(data['added'],0)
            self.assertEqual(len(data['skipped']),1)
            with database.get_conn() as conn:
                self.assertEqual(conn.execute('SELECT status FROM contacts').fetchone()[0],state)

    def test_running_and_unfiltered_campaigns_rejected(self):
        for state, tag in [('running','customers'),('paused',None)]:
            cid = self.campaign(state,tag)
            self.assertEqual(self.send_import(self.preview(),campaign_id=cid).status_code,400)

    def test_selection_cannot_inject_phone_or_import_stale_preview(self):
        data = self.preview()
        response = self.send_import(data, selected=[{'id':'0','phone':'+79999999999','person_name':'Анна'}])
        self.assertEqual(response.status_code,400)
        web._previews[data['token']]['expires'] = time.time()-1
        self.assertEqual(self.send_import(data).status_code,400)

    def test_token_rejects_calendar_and_untrusted_token_endpoint(self):
        raw = dict(client_id='client',client_secret='secret',refresh_token='refresh',
                   scopes=['https://www.googleapis.com/auth/calendar.events'])
        with self.assertRaises(gc.ContactsError): gc._credentials(json.dumps(raw))
        raw.update(scopes=[gc.SCOPE], token_uri='https://attacker.invalid/token')
        self.assertEqual(gc._credentials(json.dumps(raw)).token_uri,'https://oauth2.googleapis.com/token')


if __name__ == '__main__':
    unittest.main()
