import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from src.api.routes import _add_absolute_public_asset_urls, _add_public_result_urls
from src.services.elevenlabs_task_executor import (
    _audio_extension,
    _build_sfx_request_body,
    _build_tts_request_body,
    _captured_auth,
    _generation_headers,
    _run_sound_effects,
    _run_tts,
    _subscription_summary,
    _workflow_mode,
)
from src.services.task_executor_types import NonPenalizedTaskError


class _FakeRequest:
    url = "https://api.elevenlabs.io/v1/user/subscription"
    headers = {
        "authorization": "Bearer test-token",
        "cookie": "must-not-be-captured",
        "x-posthog-session-id": "test-session",
    }


class ElevenLabsTaskExecutorTests(unittest.TestCase):
    def test_auth_capture_preserves_required_session_header_only(self):
        captured = _captured_auth(_FakeRequest())

        self.assertIsNotNone(captured)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(captured["headers"]["X-Posthog-Session-Id"], "test-session")
        self.assertNotIn("Cookie", captured["headers"])

    def test_generation_headers_match_web_app_requests(self):
        self.assertEqual(
            _generation_headers("Sound Effects"),
            {
                "X-Generation-Surface": "Sound Effects",
                "X-Generation-Actor": "User",
            },
        )

    def test_builds_sound_effect_request(self):
        body = _build_sfx_request_body(
            {
                "prompt": "short camera shutter",
                "duration": 1.5,
                "prompt_influence": 0.4,
                "loop": True,
                "number_of_generations": 1,
            }
        )

        self.assertEqual(body["text"], "short camera shutter")
        self.assertEqual(body["duration_seconds"], 1.5)
        self.assertEqual(body["model_id"], "eleven_text_to_sound_v2")
        self.assertTrue(body["loop"])
        self.assertEqual(body["number_of_generations"], 1)

    def test_sound_effects_default_to_four_web_variants(self):
        body = _build_sfx_request_body({"prompt": "footsteps on loose gravel"})

        self.assertEqual(body["number_of_generations"], 4)

    def test_rejects_invalid_sound_effect_duration(self):
        with self.assertRaises(NonPenalizedTaskError):
            _build_sfx_request_body({"text": "rain", "duration_seconds": 0.1})

    def test_builds_tts_request_and_mode_alias(self):
        body = _build_tts_request_body(
            {
                "text": "hello",
                "model": "eleven_multilingual_v2",
                "speed": 1.1,
                "use_speaker_boost": True,
            }
        )

        self.assertEqual(_workflow_mode({"mode": "text-to-speech"}), "tts")
        self.assertEqual(body["text"], "hello")
        self.assertEqual(body["voice_settings"]["speed"], 1.1)
        self.assertTrue(body["voice_settings"]["use_speaker_boost"])

    def test_normalizes_subscription_and_audio_type(self):
        summary = _subscription_summary(
            {"tier": "free", "character_count": 125, "character_limit": 10000}
        )

        self.assertEqual(summary["remaining_quota"], 9875)
        self.assertEqual(_audio_extension("audio/mpeg"), ".mp3")
        self.assertEqual(_audio_extension("application/octet-stream", "pcm_44100"), ".pcm")

    def test_public_audio_urls_are_made_absolute_without_image_fields(self):
        relative = "/public/elevenlabs-assets/task12345-0.mp3"
        result = {
            "type": "elevenlabs_sound_effects",
            "workflow_kind": "audio",
            "audio_url": relative,
            "url": relative,
            "urls": [relative],
            "public_urls": [relative],
        }
        payload = _add_public_result_urls({"task_id": "task12345", "result": dict(result)}, result)
        request = Request(
            {
                "type": "http",
                "scheme": "http",
                "server": ("example.test", 8000),
                "path": "/",
                "root_path": "",
                "query_string": b"",
                "headers": [],
            }
        )
        payload = _add_absolute_public_asset_urls(payload, request)

        self.assertEqual(payload["audio_url"], "http://example.test:8000" + relative)
        self.assertNotIn("image_url", payload)
        self.assertEqual(payload["result_urls"], ["http://example.test:8000" + relative])


class ElevenLabsTaskExecutorAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_sound_effect_returns_all_four_generated_variants(self):
        generation_rows = [
            {
                "sound_generation_history_item": {
                    "sound_generation_history_item_id": f"history-{index}"
                }
            }
            for index in range(4)
        ]
        progress = AsyncMock()
        with (
            patch(
                "src.services.elevenlabs_task_executor._page_fetch_json",
                new=AsyncMock(
                    return_value=(200, {"sound_generations_with_waveforms": generation_rows})
                ),
            ),
            patch(
                "src.services.elevenlabs_task_executor._page_fetch_audio",
                new=AsyncMock(return_value=(200, "audio/mpeg", b"audio", None)),
            ) as fetch_audio,
            patch(
                "src.services.elevenlabs_task_executor._save_audio_asset",
                side_effect=lambda task_id, index, audio, extension: (
                    f"/public/elevenlabs-assets/{task_id}-{index}{extension}"
                ),
            ),
        ):
            result = await _run_sound_effects(
                object(),
                auth={"api_base": "https://api.elevenlabs.io", "headers": {}},
                payload={"prompt": "footsteps on loose gravel"},
                task_id="task12345",
                progress_cb=progress,
            )

        self.assertEqual(fetch_audio.await_count, 4)
        self.assertEqual(len(result["urls"]), 4)
        self.assertEqual(result["public_urls"], result["urls"])
        self.assertEqual(result["audio_url"], result["urls"][0])
        self.assertEqual(result["elevenlabs_history_ids"], [f"history-{i}" for i in range(4)])

    async def test_sound_effect_401_preserves_provider_error(self):
        progress = AsyncMock()
        with patch(
            "src.services.elevenlabs_task_executor._page_fetch_json",
            new=AsyncMock(return_value=(401, {"detail": {"message": "generation denied"}})),
        ):
            with self.assertRaisesRegex(NonPenalizedTaskError, "generation denied"):
                await _run_sound_effects(
                    object(),
                    auth={"api_base": "https://api.elevenlabs.io", "headers": {}},
                    payload={"prompt": "camera shutter"},
                    task_id="task12345",
                    progress_cb=progress,
                )

    async def test_tts_401_preserves_provider_error(self):
        with (
            patch(
                "src.services.elevenlabs_task_executor._resolve_voice_id",
                new=AsyncMock(return_value="voice-id"),
            ),
            patch(
                "src.services.elevenlabs_task_executor._page_fetch_audio",
                new=AsyncMock(
                    return_value=(401, "application/json", b"", {"detail": {"message": "speech denied"}})
                ),
            ),
        ):
            with self.assertRaisesRegex(NonPenalizedTaskError, "speech denied"):
                await _run_tts(
                    object(),
                    auth={"api_base": "https://api.elevenlabs.io", "headers": {}},
                    payload={"text": "hello"},
                    task_id="task12345",
                )


if __name__ == "__main__":
    unittest.main()
