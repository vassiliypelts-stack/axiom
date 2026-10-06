"""Personal outreach stays concise; other campaign styles keep their formatting."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import config
from channels import deslop
from db import database
from agent.agent import Reply, generate_reply


class PersonalStyleTests(unittest.TestCase):
    def test_paragraphs_become_separate_short_messages(self):
        parts = deslop.personal_parts(["**Привет** — как дела?\n\nЯ — на связи)"])
        self.assertEqual(parts, ["Привет, как дела?", "Я, на связи)"])
        self.assertIsNone(deslop.personal_problem(parts, first_reply=True))

    def test_spaced_hyphen_is_punctuation_but_words_and_numbers_keep_it(self):
        self.assertEqual(deslop.personal_parts(["ИИ - это помощник. Что-то на 15-20 минут"]),
                         ["ИИ, это помощник. Что-то на 15-20 минут"])

    def test_long_reply_and_multiple_questions_require_rewrite(self):
        self.assertIsNotNone(deslop.personal_problem(["а" * 161], first_reply=True))
        self.assertIsNotNone(deslop.personal_problem(["Как дела? Чем занят?"]))
        self.assertIsNotNone(deslop.personal_problem(["а" * 180, "б" * 180]))
        self.assertIsNotNone(deslop.personal_problem(["Раз", "Два", "Три"]))

    def test_other_campaigns_keep_intentional_paragraphs(self):
        self.assertEqual(deslop.clean("Первое.\n\nВторое."), "Первое.\n\nВторое.")

    @staticmethod
    def reply(parts):
        return Reply(reply_parts=parts, intent="question", meeting_agreed=False,
                     proposed_datetime=None, notes="")

    def test_bad_generation_is_rewritten_without_truncating(self):
        with patch("agent.llm.structured", side_effect=[self.reply(["а" * 300]),
                   self.reply(["Занимаюсь ИИ для бизнеса)", "Ты уже что-то пробовал у себя?"])]) as mock:
            result = generate_reply([{"role": "user", "content": "Что расскажешь?"}], [],
                                    campaign_prompt=deslop.SHORT_PERSONAL_MARKER)
        self.assertEqual(mock.call_count, 2)
        self.assertEqual(result.reply_parts[-1], "Ты уже что-то пробовал у себя?")

    def test_second_invalid_generation_is_not_returned_for_sending(self):
        with patch("agent.llm.structured", return_value=self.reply(["а" * 300])):
            with self.assertRaises(ValueError):
                generate_reply([{"role": "user", "content": "Привет"}], [],
                               campaign_prompt=deslop.SHORT_PERSONAL_MARKER)

    def test_other_campaign_does_not_get_length_limit(self):
        with patch("agent.llm.structured", return_value=self.reply(["а" * 300])) as mock:
            result = generate_reply([{"role": "user", "content": "Привет"}], [],
                                    campaign_prompt="Other campaign")
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(len(result.reply_parts[0]), 300)

    def test_answered_personal_contact_does_not_get_sales_followups(self):
        import scheduler
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "DB_PATH", Path(tmp) / "test.db"):
            database.init_db()
            with database.get_conn() as conn:
                cid = conn.execute("INSERT INTO campaigns(name,status,agent_prompt,extra_followup_template) "
                                   "VALUES ('Personal','running',?,'Pitch')",
                                   (deslop.SHORT_PERSONAL_MARKER,)).lastrowid
                contact = conn.execute("INSERT INTO contacts(name,status,outreach_campaign_id) "
                                       "VALUES ('Test','in_dialog',?)", (cid,)).lastrowid
                conn.execute("INSERT INTO campaign_contacts(campaign_id,contact_id) VALUES (?,?)",
                             (cid, contact))
                now = datetime.now(timezone.utc)
                for direction, hours in [('out', 22), ('in', 21), ('out', 20)]:
                    conn.execute("INSERT INTO messages(contact_id,direction,text,ts) VALUES (?,?,?,?)",
                                 (contact, direction, 'Hello', (now - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')))
                with patch.object(database, "outreach_allowed", return_value=True):
                    self.assertFalse(any(a.contact_id == contact and a.kind == 'followup'
                                         for a in scheduler.collect_due(conn)))
                    conn.execute("UPDATE campaigns SET agent_prompt='Other' WHERE id=?", (cid,))
                    self.assertTrue(any(a.contact_id == contact and a.kind == 'followup'
                                        for a in scheduler.collect_due(conn)))


if __name__ == "__main__":
    unittest.main()
