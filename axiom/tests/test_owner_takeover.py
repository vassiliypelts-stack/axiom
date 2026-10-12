"""Offline: when the owner writes to a campaign contact by hand, the agent steps aside."""
import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telethon.tl.types import User

import config
from db import database
from channels import listener as ls


class Event(SimpleNamespace):
    async def get_chat(self):
        return self.peer


class TakeoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(config, 'DB_PATH', Path(self.tmp.name) / 'test.db'),
                        patch.object(ls, 'OWNER_CHECK_DELAY', 0),
                        patch.object(ls, '_log', lambda *a, **k: None)]
        for p in self.patches:
            p.start()
        database.init_db()
        with database.get_conn() as conn:
            self.camp = conn.execute(
                "INSERT INTO campaigns(name,status,channel,message_template) "
                "VALUES ('ИИ-Прорыв','running','telegram','Привет, {name}!')").lastrowid
            self.acc = conn.execute("INSERT INTO accounts(label,phone,status,protected) "
                                    "VALUES ('702','+7702','active',1)").lastrowid
            self.friend = conn.execute(
                "INSERT INTO contacts(name,phone,status,outreach_account_id) "
                "VALUES ('lead','+7 (985) 830-00-20','in_dialog',?)", (self.acc,)).lastrowid
            self.stranger = conn.execute(
                "INSERT INTO contacts(name,phone,tg_user_id) VALUES ('Мама','+79000000001',777)"
            ).lastrowid
            conn.execute("INSERT INTO campaign_contacts(campaign_id,contact_id,account_id,sent_at) "
                         "VALUES (?,?,?,datetime('now'))", (self.camp, self.friend, self.acc))
            database.add_message(conn, self.friend, 'out', 'Привет, Руслан) Это Василий Пельц. Как ты?',
                                 account_id=self.acc, tg_msg_ids=[100])
            # the knock went out a while ago
            conn.execute("UPDATE messages SET ts=datetime('now','-2 hours')")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        import gc
        gc.collect()
        self.tmp.cleanup()

    def event(self, msg_id, text, peer_id=555, phone='79858300020', when=None):
        peer = User(id=peer_id, phone=phone, first_name='Руслан')
        when = when or datetime.datetime.now(datetime.timezone.utc)
        return Event(is_private=True, peer=peer, raw_text=text, voice=False, video_note=False,
                     photo=False, message=SimpleNamespace(id=msg_id, date=when))

    def run_out(self, *a, **k):
        asyncio.run(ls._handle_own_outgoing(self.event(*a, **k), self.acc))

    def rows(self, contact):
        with database.get_conn() as conn:
            return [(r['text'], r['intent']) for r in conn.execute(
                "SELECT text, intent FROM messages WHERE contact_id=? ORDER BY id", (contact,))]

    def paused(self):
        with database.get_conn() as conn:
            return self.friend in database.paused_contact_ids(conn, self.camp)

    def test_manual_message_is_recorded_and_pauses_the_agent(self):
        self.run_out(101, 'привет))) да, норм')
        self.assertEqual(self.rows(self.friend)[-1], ('привет))) да, норм', 'owner'))
        self.assertTrue(self.paused())
        with database.get_conn() as conn:
            ev = conn.execute("SELECT title FROM events WHERE type='owner_took_over'").fetchall()
        self.assertEqual(len(ev), 1)

    def test_message_sent_by_the_system_is_not_a_takeover(self):
        self.run_out(100, 'Привет, Руслан) Это Василий Пельц. Как ты?')
        self.assertEqual(len(self.rows(self.friend)), 1)
        self.assertFalse(self.paused())

    def test_part_sent_right_next_to_a_system_record_is_not_a_takeover(self):
        with database.get_conn() as conn:
            database.add_message(conn, self.friend, 'out', 'Да вот, занимаюсь ИИ для бизнеса',
                                 account_id=self.acc)          # recorded without an id
        self.run_out(102, '[файл]')
        self.assertFalse(self.paused())

    def test_owner_stepping_in_a_minute_after_the_agent_is_a_takeover(self):
        with database.get_conn() as conn:
            database.add_message(conn, self.friend, 'out', 'Ссылку пришлю за час до созвона',
                                 account_id=self.acc, tg_msg_ids=[103])
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=90)
        self.run_out(104, 'ща тормозну бота)', when=later)
        self.assertTrue(self.paused())

    def test_second_manual_message_is_recorded_once_and_notifies_once(self):
        self.run_out(101, 'привет')
        self.run_out(105, 'эту кампанию допилю))')
        self.run_out(105, 'эту кампанию допилю))')            # the same update delivered twice
        self.assertEqual([t for t, i in self.rows(self.friend) if i == 'owner'],
                         ['привет', 'эту кампанию допилю))'])
        with database.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM events WHERE type='owner_took_over'").fetchone()['c']
        self.assertEqual(n, 1)

    def test_private_chat_outside_the_campaign_is_left_alone(self):
        self.run_out(200, 'мам, я позвоню вечером', peer_id=777, phone='79000000001')
        self.assertEqual(self.rows(self.stranger), [])

    def test_only_the_owners_numbers_are_watched(self):
        self.assertTrue(ls._owner_account({'protected': 1, 'acc_role': 'own'}))
        self.assertTrue(ls._owner_account({'protected': 0, 'acc_role': 'service'}))
        self.assertFalse(ls._owner_account({'protected': 0, 'acc_role': 'combat'}))


if __name__ == '__main__':
    unittest.main()
