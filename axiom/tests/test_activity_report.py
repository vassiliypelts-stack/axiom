"""Offline: the activity report merges every source by Moscow day and account."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from db import database


class ActivityReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(config, 'DB_PATH', Path(self.tmp.name) / 'test.db')
        self.patch.start()
        database.init_db()
        with database.get_conn() as conn:
            self.a = conn.execute("INSERT INTO accounts(label,tg_name,phone,status,spam_status) "
                                  "VALUES ('Альфия0930','Альфия0930','+1639','warming','limited')").lastrowid
            self.b = conn.execute("INSERT INTO accounts(label,tg_name,phone,status) "
                                  "VALUES ('Василий Валерьевич','Василий Валерьевич','+1289','active')").lastrowid
            database.log_activity(conn, self.a, "story_view", 1, "Мария")
            database.log_activity(conn, self.a, "story_like", 1, "Мария")
            database.log_activity(conn, self.a, "read", 5)
            database.log_activity(conn, self.a, "read", 0)          # пустое не пишем
            conn.execute("INSERT INTO boost_actions(channel,msg_id,account_id,kind,payload,due_at,status,done_at) "
                         "VALUES ('neiro24x7',10,?,'comment','А как это с CRM?',datetime('now'),'done',datetime('now'))",
                         (self.a,))
            conn.execute("INSERT INTO boost_actions(channel,msg_id,account_id,kind,payload,due_at,status) "
                         "VALUES ('neiro24x7',10,?,'react','👍',datetime('now'),'pending')", (self.b,))
            cid = conn.execute("INSERT INTO contacts(name,phone) VALUES ('Иван','+7900')").lastrowid
            conn.execute("INSERT INTO messages(contact_id,direction,text,account_id) VALUES (?,?,?,?)",
                         (cid, 'out', 'Добрый день, Иван?', self.b))
            conn.execute("INSERT INTO messages(contact_id,direction,text,account_id) VALUES (?,?,?,?)",
                         (cid, 'in', 'Да, слушаю', self.b))
            # старый журнал прогрева — должен переехать один раз
            conn.execute("INSERT INTO events(type,title,text,account_id,ts) VALUES "
                         "('warm_action','t','вступил в канал @winresult',?,datetime('now','-2 day'))", (self.b,))
            conn.execute("INSERT INTO events(type,title,text,account_id,ts) VALUES "
                         "('warm_action','t','прочитал ленту: 7 чатов',?,datetime('now','-2 day'))", (self.b,))
            conn.execute("DELETE FROM app_settings WHERE key='activity_backfill_v1'")
        database.init_db()                                         # перенос истории
        database.init_db()                                         # и второй раз — без дублей

    def tearDown(self):
        self.patch.stop()
        try:
            self.tmp.cleanup()
        except PermissionError:                                   # Windows держит файл базы
            pass

    def _report(self, days=7):
        from web.app import activity_report
        return json.loads(activity_report(days).body)

    def _totals(self, rep):
        t = {}
        for _d, acc, kind, n in rep["daily"]:
            t[(acc, kind)] = t.get((acc, kind), 0) + n
        return t

    def test_sources_are_merged_per_account_and_kind(self):
        t = self._totals(self._report())
        self.assertEqual(t[(self.a, "story_view")], 1)
        self.assertEqual(t[(self.a, "story_like")], 1)
        self.assertEqual(t[(self.a, "read")], 5)
        self.assertEqual(t[(self.a, "boost_comment")], 1)
        self.assertNotIn((self.b, "boost_react"), t)               # не выполнена — не считаем
        self.assertEqual(t[(self.b, "msg_out")], 1)
        self.assertEqual(t[(self.b, "msg_in")], 1)

    def test_warm_journal_backfilled_once(self):
        t = self._totals(self._report())
        self.assertEqual(t[(self.b, "join_channel")], 1)
        self.assertEqual(t[(self.b, "read")], 7)

    def test_account_meta_has_no_secrets(self):
        rep = self._report()
        acc = next(x for x in rep["accounts"] if x["id"] == self.a)
        self.assertEqual(acc["spam_status"], "limited")
        self.assertNotIn("tg_2fa", acc)
        self.assertNotIn("tg_session", acc)

    def test_feed_shows_texts_for_selected_accounts(self):
        from web.app import activity_feed
        items = json.loads(activity_feed(ids=str(self.a), days=7).body)["items"]
        self.assertTrue(items)
        self.assertTrue(all(i["account_id"] == self.a for i in items))
        comment = next(i for i in items if i["kind"] == "boost_comment")
        self.assertIn("А как это с CRM?", comment["detail"])


if __name__ == "__main__":
    unittest.main()
