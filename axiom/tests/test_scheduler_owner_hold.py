"""Offline: once the owner writes to a person himself, the scheduler sends them nothing,
and templates never address anyone as «Имя Фамилия»."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config
import scheduler
from channels import fio
from db import database


class OwnerHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows keeps the WAL open
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "DB_PATH", Path(self.tmp.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_db()
        with database.get_conn() as conn:
            self.camp = conn.execute(
                "INSERT INTO campaigns(name,status,channel,message_template) "
                "VALUES ('ИИ-Прорыв','running','telegram','Привет)')").lastrowid
            self.contact = conn.execute(
                "INSERT INTO contacts(name,person_name,tg_name,status,tg_user_id) "
                "VALUES ('Евгений Станкевич',NULL,'Станкевич Евгений Тест1','in_dialog',1)"
            ).lastrowid
            conn.execute("INSERT INTO campaign_contacts(campaign_id,contact_id,account_id,sent_at) "
                         "VALUES (?,?,3,datetime('now'))", (self.camp, self.contact))

    def due(self, kind, when):
        with database.get_conn() as conn:
            database.record_meeting(conn, self.contact, when.isoformat())
            return [a for a in scheduler.collect_due(conn) if a.kind == kind]

    def hold(self):
        with database.get_conn() as conn:
            database.pause_campaign_contacts(conn, self.camp, [self.contact])

    def test_noshow_addresses_by_first_name_not_name_and_surname(self):
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        texts = [a.text for a in self.due("noshow", past)]
        self.assertEqual(texts, ["Евгений, не получилось созвониться( давай перенесём? "
                                 "когда удобно на этой неделе?"])

    def test_patronymic_is_kept(self):
        with database.get_conn() as conn:
            conn.execute("UPDATE contacts SET person_name='Евгений Васильевич Станкевич'")
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        self.assertTrue(self.due("noshow", past)[0].text.startswith("Евгений Васильевич, "))

    def test_owner_took_over_no_noshow(self):
        self.hold()
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        self.assertEqual(self.due("noshow", past), [])

    def test_owner_took_over_no_reminder(self):
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        self.assertEqual(len(self.due("reminder", soon)), 1)
        self.hold()
        self.assertEqual(self.due("reminder", soon), [])

    def test_noshow_is_written_into_the_dialogue(self):
        past = datetime.now(timezone.utc) - timedelta(hours=3)
        action = self.due("noshow", past)[0]
        action.tg_msg_ids, action.account_id = [500], 3
        with database.get_conn() as conn:
            scheduler.apply(conn, action)
            row = conn.execute("SELECT text, tg_msg_id FROM messages WHERE contact_id=?",
                               (self.contact,)).fetchone()
        self.assertEqual(row["text"], action.text.replace("ё", "е"))   # book drops «ё»
        self.assertEqual(row["tg_msg_id"], "500")


class SurnameIsNotPatronymicTests(unittest.TestCase):
    def test_evich_surnames_are_surnames(self):
        for raw in ("Евгений Станкевич", "Станкевич Евгений", "Адам Мицкевич",
                    "Сергей Лукашевич"):
            self.assertEqual(fio.parse(raw).patronymic, "", raw)
            self.assertEqual(len(fio.address(raw).split()), 1, raw)

    def test_real_evich_patronymics_stay(self):
        for raw, want in (("Евгений Сергеевич Станкевич", "Евгений Сергеевич"),
                          ("Иван Николаевич", "Иван Николаевич"),
                          ("Петров Олег Игоревич", "Олег Игоревич"),
                          ("Антон Евгеньевич", "Антон Евгеньевич"),
                          ("Павел Дмитриевич", "Павел Дмитриевич")):
            self.assertEqual(fio.address(raw), want, raw)


if __name__ == "__main__":
    unittest.main()
