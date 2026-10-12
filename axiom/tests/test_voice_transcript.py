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


if __name__ == "__main__":
    unittest.main()
