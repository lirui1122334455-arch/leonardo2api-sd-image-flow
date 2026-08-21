import unittest

from src.services.fish_audio_task_executor import _build_fish_task_body
from src.services.task_executor_types import NonPenalizedTaskError


class FishAudioTaskBodyTests(unittest.TestCase):
    def test_builds_web_task_payload(self):
        body = _build_fish_task_body(
            {
                "text": "hello",
                "backend": "s2.1-pro",
                "format": "mp3",
                "speed": 1.1,
                "normalize": False,
            },
            reference_id="voice-123",
            recaptcha="captcha-token",
        )

        self.assertEqual(body["type"], "tts")
        self.assertTrue(body["stream"])
        self.assertEqual(body["model"], "voice-123")
        self.assertEqual(body["parameters"]["text"], "hello")
        self.assertEqual(body["parameters"]["model_id"], "voice-123")
        self.assertEqual(body["parameters"]["prosody"]["speed"], 1.1)
        self.assertEqual(body["recaptcha"], "captcha-token")

    def test_accepts_prompt_alias_and_sampler(self):
        body = _build_fish_task_body(
            {"prompt": "test", "temperature": 0.7, "top_p": 0.9},
            reference_id="voice-456",
            recaptcha="token",
        )

        self.assertEqual(body["parameters"]["text"], "test")
        self.assertEqual(body["sampler"], {"temperature": 0.7, "top_p": 0.9})

    def test_rejects_missing_text(self):
        with self.assertRaises(NonPenalizedTaskError):
            _build_fish_task_body({}, reference_id="voice", recaptcha="token")

    def test_rejects_unknown_format(self):
        with self.assertRaises(NonPenalizedTaskError):
            _build_fish_task_body(
                {"text": "hello", "format": "aac"},
                reference_id="voice",
                recaptcha="token",
            )


if __name__ == "__main__":
    unittest.main()
