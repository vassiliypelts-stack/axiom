"""A call is agreed only on a time the person saw; moves and cancellations keep reminders honest."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import config
import scheduler
from agent.agent import Reply, generate_reply
from db import database
from integrations import meetings


def reply(parts, agreed=False, slot=None):
    return Reply(reply_parts=parts, intent="agreed" if agreed else "question",
                 meeting_agreed=agreed, proposed_datetime=slot, notes="")


AGREED = reply(["Договорились, завтра в 12:00 наберу тебя здесь же"], True, "2026-10-10T12:00:00")
ASKED = reply(["Давай созвонимся, в понедельник в 12:00 по Москве удобно?"])


class AgreementTests(unittest.TestCase):
    def test_time_first_named_in_the_confirmation_is_rewritten_as_a_question(self):
        history = [{"role": "user", "content": "Какими судьбами?"},
                   {"role": "assistant", "content": "А ты сейчас что делаешь с ИИ?"},
                   {"role": "user", "content": "Давай, можно посмотреть"}]
        with patch("agent.llm.structured", side_effect=[AGREED, ASKED]) as mock:
            result = generate_reply(history, [])
        self.assertEqual(mock.call_count, 2)
        self.assertFalse(result.meeting_agreed)

    def test_repeated_self_appointment_is_not_sent(self):
        with patch("agent.llm.structured", return_value=AGREED):
            with self.assertRaises(ValueError):
                generate_reply([{"role": "user", "content": "Давай"}], [])

    def test_yes_to_our_proposed_time_is_an_agreement(self):
        history = [{"role": "user", "content": "Интересно"},
                   {"role": "assistant", "content": "Завтра в 12:00 по Москве удобно?"},
                   {"role": "user", "content": "Да, отлично"}]
        with patch("agent.llm.structured", return_value=AGREED) as mock:
            result = generate_reply(history, [])
        self.assertEqual(mock.call_count, 1)
        self.assertTrue(result.meeting_agreed)

    def test_time_named_by_the_person_is_an_agreement(self):
        for text in ("В понедельник с утра отлично", "набери через час", "давай в 15:30"):
            with patch("agent.llm.structured", return_value=AGREED) as mock:
                generate_reply([{"role": "user", "content": text}], [])
            self.assertEqual(mock.call_count, 1, text)


class MeetingBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows keeps the WAL open
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "DB_PATH", Path(self.tmp.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        self.soon = datetime.now(timezone.utc) + timedelta(hours=1)
        self.later = (datetime.now(ZoneInfo(config.MEETING_TZ)) + timedelta(days=3)).replace(
            hour=11, minute=0, second=0, microsecond=0)
        with database.get_conn() as conn:
            self.contact = conn.execute(
                "INSERT INTO contacts(name,tg_name,status,tg_user_id) "
                "VALUES ('lead','Руслан Хисматуллин Ппмск','in_dialog',1)").lastrowid

    def reminders(self, conn):
        return [a for a in scheduler.collect_due(conn) if a.kind == "reminder"]

    def test_moved_meeting_is_reminded_again(self):
        with database.get_conn() as conn:
            database.record_meeting(conn, self.contact, "2026-10-10T12:00:00+03:00")
            conn.execute("UPDATE deals SET reminder_sent=1")
            database.record_meeting(conn, self.contact, "2026-10-10T12:00:00+03:00")
            self.assertEqual(conn.execute("SELECT reminder_sent r FROM deals").fetchone()["r"], 1)
            database.record_meeting(conn, self.contact, self.soon.isoformat())
            self.assertEqual(len(self.reminders(conn)), 1)

    def test_cancelled_meeting_is_not_reminded(self):
        with database.get_conn() as conn:
            database.record_meeting(conn, self.contact, self.soon.isoformat())
            self.assertEqual(len(self.reminders(conn)), 1)
            self.assertTrue(database.cancel_meeting(conn, self.contact))
            self.assertEqual(self.reminders(conn), [])
            self.assertFalse(database.cancel_meeting(conn, self.contact))

    def test_same_time_is_not_a_new_meeting(self):
        with database.get_conn() as conn:
            database.record_meeting(conn, self.contact, self.later.isoformat())
        self.assertTrue(meetings.already_set(self.contact, f"{self.later:%Y-%m-%dT%H:%M:%S}"))
        self.assertFalse(meetings.already_set(
            self.contact, f"{self.later + timedelta(days=1):%Y-%m-%dT%H:%M:%S}"))

    def test_reminder_addresses_by_telegram_name_not_placeholder(self):
        with database.get_conn() as conn:
            database.record_meeting(conn, self.contact, self.soon.isoformat())
            self.assertTrue(self.reminders(conn)[0].text.startswith("Руслан, напоминаю про созвон"))
            conn.execute("UPDATE contacts SET tg_name=NULL")
            self.assertTrue(self.reminders(conn)[0].text.startswith("Напоминаю про созвон"))

    def test_voice_call_campaign_gets_no_link(self):
        with database.get_conn() as conn:
            cid = conn.execute("INSERT INTO campaigns(name,status,meeting_url) "
                               "VALUES ('Personal','running','-')").lastrowid
        self.assertTrue(meetings.call_without_link(cid))
        with patch("integrations.zoom.create_meeting") as zoom, \
                patch("integrations.calendar.create_event", return_value=None):
            result = meetings.arrange({"name": "Руслан"}, self.soon.isoformat(), cid)
        zoom.assert_not_called()
        self.assertIsNone(result.zoom_link)


if __name__ == "__main__":
    unittest.main()
