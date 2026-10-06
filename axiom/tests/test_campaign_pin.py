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

    def test_shared_test_contact_ignores_previous_campaign(self):
        with database.get_conn() as conn:
            other = conn.execute("INSERT INTO campaigns(name) VALUES ('Other')").lastrowid
            conn.execute("UPDATE contacts SET is_test=1, outreach_campaign_id=? WHERE id=?",
                         (other, self.tg_a))
            conn.execute("UPDATE contacts SET is_test=1, test_campaign_id=? WHERE id=?",
                         (other, self.tg_b))
        rows = cs._audience(self.cid, 'unused', 'telegram', 100, test=True,
                            only_contacts=[self.tg_a, self.tg_b])
        self.assertEqual(self.ids(rows), {self.tg_a})
        self.assertEqual(cs._audience(self.cid, 'unused', 'telegram', 100, test=True,
                                     only_contacts=[self.wa_b]), [])

    def test_whatsapp_shared_test_ignores_previous_campaign(self):
        with database.get_conn() as conn:
            other = conn.execute("INSERT INTO campaigns(name) VALUES ('Other')").lastrowid
            conn.execute("UPDATE contacts SET is_test=1, outreach_campaign_id=? WHERE id=?",
                         (other, self.wa_b))
            conn.execute("UPDATE contacts SET is_test=1, test_campaign_id=? WHERE id=?",
                         (other, self.tg_b))
        queued = cs.queue_whatsapp(self.cid, cs._load_campaign(self.cid), 10, test=True,
                                  test_account=self.b,
                                  test_contacts=[self.wa_b, self.tg_a, self.tg_b], tg_too=True)
        self.assertEqual(queued, 1)
        with database.get_conn() as conn:
            row = conn.execute("SELECT contact_id, account_id, is_test FROM wa_outbox").fetchone()
        self.assertEqual(tuple(row), (self.wa_b, self.b, 1))

    def test_production_still_excludes_other_campaign(self):
        with database.get_conn() as conn:
            other = conn.execute("INSERT INTO campaigns(name) VALUES ('Other')").lastrowid
            conn.execute("UPDATE contacts SET outreach_campaign_id=?", (other,))
        self.assertEqual(cs._audience(self.cid, 'ИИ-Прорыв', 'telegram', 100), [])
        self.assertEqual(cs.queue_whatsapp(self.cid, cs._load_campaign(self.cid), 10), 0)

    def test_window_follows_contacts_own_timezone(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        h = datetime.now(ZoneInfo('Asia/Almaty')).hour
        with database.get_conn() as conn:
            # Окно — текущий час по Алматы; в Москве сейчас на 2 часа раньше.
            conn.execute("UPDATE campaigns SET work_hours_start=?, work_hours_end=?, "
                         "work_hours_tz='Europe/Moscow' WHERE id=?",
                         (f"{h:02d}:00", f"{h:02d}:59", self.cid))
            conn.execute("UPDATE contacts SET work_tz='Asia/Almaty' WHERE id=?", (self.wa_b,))
            camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (self.cid,)).fetchone()
            kz = conn.execute("SELECT * FROM contacts WHERE id=?", (self.wa_b,)).fetchone()
            ru = conn.execute("SELECT * FROM contacts WHERE id=?", (self.tg_a,)).fetchone()
            self.assertEqual(database.campaign_tzs(conn, camp), [None, 'Asia/Almaty'])
        self.assertTrue(database.in_work_hours(camp, database.contact_tz(kz)))
        self.assertFalse(database.in_work_hours(camp, database.contact_tz(ru)))

    def test_whatsapp_queue_takes_only_whatsapp_people_from_their_number(self):
        camp = cs._load_campaign(self.cid)
        queued = cs.queue_whatsapp(self.cid, camp, 10, tg_too=True)
        self.assertEqual(queued, 1)
        with database.get_conn() as conn:
            row = conn.execute("SELECT contact_id, account_id FROM wa_outbox").fetchone()
        self.assertEqual((row['contact_id'], row['account_id']), (self.wa_b, self.b))


if __name__ == '__main__':
    unittest.main()
