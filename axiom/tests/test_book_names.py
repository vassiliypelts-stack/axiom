"""Offline: a friend's name comes from the owner's address-book label, wherever it stands."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
from db import database
from channels import campaign_send as cs
from channels import fio


class LabelTests(unittest.TestCase):
    def test_name_is_found_after_a_surname(self):
        self.assertEqual(fio.first_in_label('Зырянов Сергей ИЮМ'), 'Сергей')
        self.assertEqual(fio.first_in_label('Балдин Павел ИЮМинт19 Балдин'), 'Павел')
        self.assertEqual(fio.first_in_label('Гришкевич Евгений Ппирк5'), 'Евгений')

    def test_first_of_several_names_wins(self):
        self.assertEqual(fio.first_in_label('Олег Ведров(Лоцман Сергей Борисович) ПпМСК'), 'Олег')

    def test_names_missing_from_the_old_list(self):
        self.assertEqual(fio.first_in_label('Розалия ЭпСочи'), 'Розалия')
        self.assertEqual(fio.first_in_label('Линар Мавлютов Хвала всевышнему!'), 'Линар')
        self.assertEqual(fio.first_in_label('Турсунай Делсвязи Турсунай'), 'Турсунай')
        self.assertEqual(fio.first_in_label('Костя Спартанец ПпСочи'), 'Костя')

    def test_no_name_in_the_label(self):
        self.assertEqual(fio.first_in_label('HappyPeopleSochi8'), '')
        self.assertEqual(fio.first_in_label('Ппалм Юрекегали'), '')
        self.assertEqual(fio.first_in_label('lead'), '')
        self.assertEqual(fio.first_in_label(None), '')

    def test_knock_takes_the_name_from_both_fields(self):
        entity = SimpleNamespace(first_name='Усольцев', last_name='Павел ЭСК2023')
        self.assertEqual(cs._contact_first_name(entity), 'Павел')
        self.assertEqual(cs._own_knock_parts('Привет, {name}) Это Василий Пельц. Как ты?',
                                             cs._contact_first_name(entity)),
                         ['Привет, Павел) Это Василий Пельц. Как ты?'])


class AdoptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(config, 'DB_PATH', Path(self.tmp.name) / 'test.db')
        self.patch.start()
        database.init_db()
        with database.get_conn() as conn:
            self.acc = conn.execute("INSERT INTO accounts(label,phone,status) "
                                    "VALUES ('702','+7702','active')").lastrowid

    def tearDown(self):
        self.patch.stop()
        import gc
        gc.collect()
        self.tmp.cleanup()

    def contact(self, phone, name=None, person=None, tg_name=None, pinned=True):
        with database.get_conn() as conn:
            return conn.execute(
                "INSERT INTO contacts(name,person_name,tg_name,phone,outreach_account_id) "
                "VALUES (?,?,?,?,?)",
                (name, person, tg_name, phone, self.acc if pinned else None)).lastrowid

    def card(self, cid):
        with database.get_conn() as conn:
            r = conn.execute("SELECT name, person_name FROM contacts WHERE id=?", (cid,)).fetchone()
        return r['name'], r['person_name']

    def adopt(self, cid, label):
        with database.get_conn() as conn:
            return database.adopt_book_name(conn, cid, label)

    def test_placeholder_is_replaced_by_the_label(self):
        cid = self.contact('+71', name='lead')
        self.assertTrue(self.adopt(cid, 'Зырянов Сергей ИЮМ'))
        self.assertEqual(self.card(cid), ('Зырянов Сергей ИЮМ', 'Сергей'))

    def test_own_name_is_kept(self):
        cid = self.contact('+72', name='Андрей Лидман Ппсочи', person='Андрей')
        self.assertFalse(self.adopt(cid, 'Андрюха'))
        self.assertEqual(self.card(cid), ('Андрей Лидман Ппсочи', 'Андрей'))

    def test_label_without_a_name_still_replaces_the_placeholder(self):
        cid = self.contact('+73')
        self.assertTrue(self.adopt(cid, 'HappyPeopleSochi8'))
        self.assertEqual(self.card(cid), ('HappyPeopleSochi8', None))

    def test_cold_contact_is_not_touched(self):
        cid = self.contact('+74', name='lead', pinned=False)
        self.assertFalse(self.adopt(cid, 'Мария'))
        self.assertEqual(self.card(cid), ('lead', None))

    def test_existing_cards_are_repaired_on_start(self):
        cid = self.contact('+75', name='lead', tg_name='Руслан Хисматуллин Ппмск')
        database.init_db()
        self.assertEqual(self.card(cid), ('Руслан Хисматуллин Ппмск', 'Руслан'))


if __name__ == '__main__':
    unittest.main()
