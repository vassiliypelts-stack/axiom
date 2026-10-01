"""Offline: one campaign, own contacts pinned to their number and messenger."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from db import database
from channels import campaign_send as cs


class PinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(config, 'DB_PATH', Path(self.tmp.name) / 'test.db')
        self.patch.start()
        database.init_db()
        with database.get_conn() as conn:
            self.cid = conn.execute(
                "INSERT INTO campaigns(name,status,audience_tag,channel,message_template,"
                "quiet_opener_template,daily_limit,tg_verified_only) VALUES (?,?,?,?,?,?,?,0)",
                ('ИИ‑Прорыв', 'running', 'ИИ-Прорыв', 'telegram,whatsapp',
                 'Привет, {name}!', 'Привет, {name}! Как поживаешь?', 50)).lastrowid
            self.a = conn.execute("INSERT INTO accounts(label,phone,status,wa_authed) "
                                  "VALUES ('988','+7988','active','no')").lastrowid
            self.b = conn.execute("INSERT INTO accounts(label,phone,status,wa_authed) "
                                  "VALUES ('702','+7702','active','yes')").lastrowid
            for acc in (self.a, self.b):
                conn.execute("INSERT INTO campaign_accounts(campaign_id,account_id,daily_limit) "
                             "VALUES (?,?,15)", (self.cid, acc))

            def contact(phone, tag, acc, ch, has_tg='yes'):
                return conn.execute(
                    "INSERT INTO contacts(name,phone,status,tags,has_tg,outreach_campaign_id,"
                    "outreach_account_id,outreach_channel) VALUES (?,?,?,?,?,?,?,?)",
                    (phone, phone, 'new', tag, has_tg, self.cid, acc, ch)).lastrowid
            self.tg_a = contact('+79000000001', 'ИИ-Прорыв 988 TG', self.a, 'telegram')
            self.tg_b = contact('+79000000002', 'ИИ-Прорыв 702 TG', self.b, 'telegram')
            self.wa_b = contact('+77000000003', 'ИИ-Прорыв 702 WA', self.b, 'whatsapp')

    def tearDown(self):
        self.patch.stop()
        import gc
        gc.collect()
        self.tmp.cleanup()

    def ids(self, rows):
        return {r['id'] for r in rows}

    def test_telegram_audience_skips_whatsapp_people(self):
        rows = cs._audience(self.cid, 'ИИ-Прорыв', 'telegram,whatsapp', 100)
        self.assertEqual(self.ids(rows), {self.tg_a, self.tg_b})

    def test_number_knocks_only_its_own(self):
        rows = cs._audience(self.cid, 'ИИ-Прорыв', 'telegram', 100, sender_id=self.a)
        self.assertEqual(self.ids(rows), {self.tg_a})

    def test_pinned_contact_waits_for_its_number(self):
        live = [{'id': self.a, 'remaining': 3}, {'id': self.b, 'remaining': 0}]
        rows = {r['id']: r for r in cs._audience(self.cid, 'ИИ-Прорыв', 'telegram', 100)}
        self.assertEqual(cs._pick_for(rows[self.tg_a], live, 1)['id'], self.a)
        self.assertIsNone(cs._pick_for(rows[self.tg_b], live, 0))

    def test_whatsapp_queue_takes_only_whatsapp_people_from_their_number(self):
        camp = cs._load_campaign(self.cid)
        queued = cs.queue_whatsapp(self.cid, camp, 10, tg_too=True)
        self.assertEqual(queued, 1)
        with database.get_conn() as conn:
            row = conn.execute("SELECT contact_id, account_id FROM wa_outbox").fetchone()
        self.assertEqual((row['contact_id'], row['account_id']), (self.wa_b, self.b))


if __name__ == '__main__':
    unittest.main()
