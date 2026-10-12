"""Incoming voice becomes text for the agent; without a key it stays a plain marker."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
from agent import prompts
from integrations import stt


def deepgram(transcript):
    response = MagicMock()
    response.json.return_value = {"results": {"channels": [{"alternatives": [{"transcript": transcript}]}]}}
    return response


class VoiceTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.voice = Path(self.tmp.name) / "voice.oga"
        self.voice.write_bytes(b"OggS-audio")

    def test_voice_is_sent_to_deepgram_in_russian_and_returns_clean_text(self):
        with patch.object(stt, "api_key", return_value="k"), \
                patch("requests.post", return_value=deepgram("Привет,  занимаюсь\nстройкой.")) as post:
            self.assertEqual(stt.transcribe(self.voice, "audio/ogg"), "Привет, занимаюсь стройкой.")
        call = post.call_args
        self.assertTrue(call.args[0].endswith("/listen"))
        self.assertEqual(call.kwargs["params"]["language"], "ru")
        self.assertEqual(call.kwargs["headers"], {"Authorization": "Token k", "Content-Type": "audio/ogg"})
        self.assertEqual(call.kwargs["data"], b"OggS-audio")

    def test_no_key_silence_and_failures_leave_the_marker(self):
        with patch.object(stt, "api_key", return_value=""), patch("requests.post") as post:
            self.assertIsNone(stt.transcribe(self.voice))
            post.assert_not_called()
        with patch.object(stt, "api_key", return_value="k"):
            with patch("requests.post", return_value=deepgram("  ")):
                self.assertIsNone(stt.transcribe(self.voice))
            with patch("requests.post", side_effect=OSError("network")):
                self.assertIsNone(stt.transcribe(self.voice))
            self.assertIsNone(stt.transcribe(Path(self.tmp.name) / "missing.oga"))

    def test_pult_setting_wins_over_env_key(self):
        from db import database
        with patch.object(config, "DB_PATH", Path(self.tmp.name) / "test.db"), \
                patch.object(config, "DEEPGRAM_API_KEY", "from-env"):
            database.init_db()
            self.assertEqual(stt.api_key(), "from-env")
            with database.get_conn() as conn:
                database.set_setting(conn, stt.SETTING, " from-pult ")
            self.assertEqual(stt.api_key(), "from-pult")

    def test_agent_is_told_that_text_after_the_marker_is_what_was_said(self):
        for prompt in (prompts.build_system([]), prompts.build_system([], "Сценарий кампании")):
            self.assertIn("расшифровка", prompt)


class WhatsAppVoiceTests(unittest.TestCase):
    def setUp(self):
        from channels import listener, wa_bridge
        from db import database
        self.wa, self.database = wa_bridge, database
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        for target, name, value in ((config, "DB_PATH", Path(self.tmp.name) / "test.db"),
                                    (listener, "_MEDIA_DIR", Path(self.tmp.name) / "media")):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        database.init_db()
        with database.get_conn() as conn:
            self.acc = conn.execute("INSERT INTO accounts(label,status) VALUES ('wa','active')").lastrowid
            camp = conn.execute("INSERT INTO campaigns(name,status) VALUES ('c','running')").lastrowid
            self.ours = conn.execute("INSERT INTO contacts(name,phone,status) "
                                     "VALUES ('Илья','79990000001','messaged')").lastrowid
            self.personal = conn.execute("INSERT INTO contacts(name,phone,status) "
                                         "VALUES ('Мама','79990000002','new')").lastrowid
            conn.execute("INSERT INTO campaign_contacts(campaign_id,contact_id,account_id) VALUES (?,?,?)",
                         (camp, self.ours, self.acc))

    def incoming(self, phone):
        return self.wa.incoming(self.wa.Incoming(
            jid=f"{phone}@s.whatsapp.net", phone=phone, text="[голосовое]", account_id=self.acc,
            voice_b64="T2dnUw==", voice_mime="audio/ogg; codecs=opus"))

    def test_only_campaign_dialogs_are_offered_for_download(self):
        probe = lambda phone: self.wa.ours(self.wa.Probe(
            jid=f"{phone}@s.whatsapp.net", phone=phone, account_id=self.acc)).body
        self.assertIn(b"true", probe("79990000001"))
        self.assertIn(b"false", probe("79990000002"))
        self.assertIn(b"false", probe("79990000003"))

    def test_voice_is_stored_with_its_text_and_the_recording(self):
        with patch.object(stt, "transcribe", return_value="Привет, занимаюсь стройкой.") as said:
            self.incoming("79990000001")
        with self.database.get_conn() as conn:
            row = conn.execute("SELECT text, media_path, channel FROM messages").fetchone()
        self.assertEqual(row["text"], "[голосовое] Привет, занимаюсь стройкой.")
        self.assertEqual(row["channel"], "whatsapp")
        self.assertEqual((Path(self.tmp.name) / "media" / row["media_path"]).read_bytes(), b"OggS")
        self.assertEqual(said.call_args.args[1], "audio/ogg; codecs=opus")

    def test_untranscribed_voice_keeps_the_marker_and_personal_voice_is_not_touched(self):
        with patch.object(stt, "transcribe", return_value=None) as said:
            self.incoming("79990000001")
            self.assertEqual(said.call_count, 1)
            self.incoming("79990000002")
            self.assertEqual(said.call_count, 1)
        with self.database.get_conn() as conn:
            self.assertEqual([r["text"] for r in conn.execute("SELECT text FROM messages")], ["[голосовое]"])


if __name__ == "__main__":
    unittest.main()
