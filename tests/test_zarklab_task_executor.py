import unittest

from src.api.routes import _normalize_video_task_payload
from src.services.task_executor_types import NonPenalizedTaskError
from src.services.zarklab_task_executor import (
    ZARKLAB_PUBLIC_MODEL_ALIASES,
    _reference_url_entries,
    build_zarklab_quote_body,
    build_zarklab_tool_params,
    extract_zarklab_file_ids,
    extract_zarklab_video_urls,
    parse_zarklab_sse,
)


class ZarkLabPayloadTests(unittest.TestCase):
    def test_public_model_names_are_provider_scoped(self):
        self.assertEqual(
            set(ZARKLAB_PUBLIC_MODEL_ALIASES),
            {
                "zark-seedance-2.5",
                "zark-seedance-2.0",
                "zark-seedance-2.0-lite",
                "zark-seedance-2.0-mini",
                "zark-minimax-h3",
            },
        )

    def test_builds_seedance_25_params_and_quote(self):
        params = build_zarklab_tool_params(
            {
                "model": "zark-seedance-2.5",
                "duration": 30,
                "resolution": "720P",
                "aspect_ratio": "9:16",
                "sound": False,
            }
        )
        self.assertEqual(params["selected_model"], "fal-seedance-2-5")
        self.assertEqual(params["duration"], "30")
        self.assertEqual(params["resolution"], "720p")
        self.assertEqual(params["sound"], "off")
        self.assertEqual(
            build_zarklab_quote_body(params),
            {
                "target_media": "video",
                "action": "generate",
                "selected_model": "fal-seedance-2-5",
                "duration": 30,
                "resolution": "720p",
                "aspect_ratio": "9:16",
            },
        )

    def test_rejects_seedance_25_duration_above_catalog_limit(self):
        with self.assertRaises(NonPenalizedTaskError):
            build_zarklab_tool_params({"model": "zark-seedance-2.5", "duration": 31})

    def test_minimax_h3_normalizes_resolution_and_requires_sound(self):
        params = build_zarklab_tool_params(
            {"model": "zark-minimax-h3", "duration": 5, "resolution": "2k"}
        )
        self.assertEqual(params["resolution"], "2K")
        self.assertEqual(params["sound"], "on")
        with self.assertRaises(NonPenalizedTaskError):
            build_zarklab_tool_params({"model": "zark-minimax-h3", "sound": "off"})

    def test_existing_zark_file_ids_are_passed_through(self):
        params = build_zarklab_tool_params(
            {"model": "zark-seedance-2.0", "zark_file_ids": ["file-a", "file-b"]}
        )
        self.assertEqual(params["reference_file_ids"], ["file-a", "file-b"])

    def test_seconds_alias_is_supported(self):
        params = build_zarklab_tool_params(
            {"model": "zark-seedance-2.0", "seconds": 8}
        )
        self.assertEqual(params["duration"], "8")

    def test_collects_image_video_audio_urls_with_frame_roles(self):
        entries = _reference_url_entries(
            {
                "first_image_url": "https://cdn.example.com/start.png",
                "last_image_url": "https://cdn.example.com/end.png",
            }
        )
        self.assertEqual(
            entries,
            [
                {
                    "url": "https://cdn.example.com/start.png",
                    "role": "start_frame",
                    "media_type": "image",
                },
                {
                    "url": "https://cdn.example.com/end.png",
                    "role": "end_frame",
                    "media_type": "image",
                },
            ],
        )

        references = _reference_url_entries(
            {
                "reference_image_urls": ["https://cdn.example.com/ref.png"],
                "reference_video_urls": ["https://cdn.example.com/ref.mp4"],
                "reference_audio_urls": ["https://cdn.example.com/ref.mp3"],
            }
        )
        self.assertEqual([entry["media_type"] for entry in references], ["image", "video", "audio"])
        self.assertTrue(all(entry["role"] == "inspiration" for entry in references))

    def test_first_and_last_frames_select_interpolate_action(self):
        params = build_zarklab_tool_params(
            {
                "model": "zark-seedance-2.5",
                "first_image_url": "https://cdn.example.com/start.png",
                "last_image_url": "https://cdn.example.com/end.png",
            }
        )
        self.assertEqual(params["selected_action"], "interpolate")
        self.assertEqual(build_zarklab_quote_body(params)["action"], "interpolate")

    def test_frame_mode_rejects_inspiration_references(self):
        with self.assertRaises(NonPenalizedTaskError):
            build_zarklab_tool_params(
                {
                    "model": "zark-seedance-2.5",
                    "first_image_url": "https://cdn.example.com/start.png",
                    "reference_video_urls": ["https://cdn.example.com/ref.mp4"],
                }
            )

    def test_seedance_20_rejects_too_many_video_references(self):
        with self.assertRaises(NonPenalizedTaskError):
            build_zarklab_tool_params(
                {
                    "model": "zark-seedance-2.0",
                    "reference_video_urls": [f"https://cdn.example.com/{index}.mp4" for index in range(4)],
                }
            )


class ZarkLabSseTests(unittest.TestCase):
    def test_parses_file_ids_from_generation_events(self):
        raw = "\n".join(
            [
                'data: {"type":"creative_run_status","status":"generating","run_id":"run-1"}',
                'data: {"type":"agent_run_complete","data":{"generated_file_ids":["file-1"]}}',
                'data: {"type":"generation_complete","status":"saved","file_id":"file-2"}',
                "data: [DONE]",
            ]
        )
        events = parse_zarklab_sse(raw)
        self.assertEqual(extract_zarklab_file_ids(events), ["file-1", "file-2"])

    def test_extracts_video_url_without_returning_thumbnail(self):
        detail = {
            "file": {
                "thumbnail_url": "https://cdn.example.com/thumb.jpg",
                "download_url": "https://cdn.example.com/generated-video.mp4?token=abc",
            }
        }
        self.assertEqual(
            extract_zarklab_video_urls(detail),
            ["https://cdn.example.com/generated-video.mp4?token=abc"],
        )

    def test_extracts_extensionless_presigned_playback_url(self):
        detail = {
            "file": {"mimeType": "video/mp4"},
            "playback": {"presignedURL": "https://media.example.com/object?signature=abc"},
        }
        self.assertEqual(
            extract_zarklab_video_urls(detail),
            ["https://media.example.com/object?signature=abc"],
        )


class ZarkLabPublicRoutingTests(unittest.TestCase):
    def test_zark_model_routes_to_zark_task_type(self):
        task_type, payload = _normalize_video_task_payload(
            {"model": "zark-seedance-2.0", "prompt": "test"}
        )
        self.assertEqual(task_type, "zarklab_video")
        self.assertEqual(payload["zarklab_model"], "fal-seedance-2-pro")

    def test_existing_seedance_model_still_routes_to_leonardo(self):
        task_type, payload = _normalize_video_task_payload(
            {"model": "seedance-2", "prompt": "test"}
        )
        self.assertEqual(task_type, "leonardo_workflow")
        self.assertEqual(payload["model"], "seedance-2")


if __name__ == "__main__":
    unittest.main()
