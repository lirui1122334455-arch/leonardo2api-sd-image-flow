import unittest

from src.api.routes import _normalize_video_task_payload
from src.services.task_executor_types import NonPenalizedTaskError
from src.services.veo_workflow_executor import (
    VIDEO_ASPECT_RATIO_LANDSCAPE,
    _veo_collect_ingredients_image_urls,
    _veo_resolve_i2v_model_key,
    _veo_resolve_r2v_model,
    _veo_resolve_t2v_model,
    _veo_resolve_video_input_urls,
)


class OmniPublicRoutingTests(unittest.TestCase):
    def test_omni_11_alias_routes_to_veo(self):
        task_type, payload = _normalize_video_task_payload(
            {"model": "omni-1.1-flash", "prompt": "test", "duration": 6}
        )
        self.assertEqual(task_type, "veo_workflow")
        self.assertEqual(payload["model"], "gemini-omni")
        self.assertEqual(payload["duration"], 6)

    def test_display_name_alias_is_accepted(self):
        task_type, payload = _normalize_video_task_payload(
            {"model": "Omni 1.1 Flash", "prompt": "test", "duration": 8}
        )
        self.assertEqual(task_type, "veo_workflow")
        self.assertEqual(payload["model"], "gemini-omni")


class OmniModelKeyTests(unittest.TestCase):
    def test_text_to_video_uses_abra_key(self):
        key, aspect_ratio = _veo_resolve_t2v_model(
            {"model": "omni-1.1-flash", "duration": 4, "aspect_ratio": "9:16"}
        )
        self.assertEqual(key, "abra_t2v_4s")
        self.assertNotEqual(aspect_ratio, VIDEO_ASPECT_RATIO_LANDSCAPE)

    def test_reference_video_supports_360p(self):
        key, _ = _veo_resolve_r2v_model(
            {"model": "omni-1.1-flash", "duration": 10, "resolution": "360p"}
        )
        self.assertEqual(key, "abra_r2v_10s_360p")

    def test_single_image_uses_native_omni_i2v_key(self):
        key = _veo_resolve_i2v_model_key(
            {"model": "omni-1.1-flash", "duration": 6},
            VIDEO_ASPECT_RATIO_LANDSCAPE,
            image_count=1,
        )
        self.assertEqual(key, "abra_i2v_6s")

    def test_first_last_images_use_native_omni_key(self):
        key = _veo_resolve_i2v_model_key(
            {"model": "omni-1.1-flash", "duration": 8, "video_resolution": 360},
            VIDEO_ASPECT_RATIO_LANDSCAPE,
            image_count=2,
        )
        self.assertEqual(key, "omni_flash_i2v_8s_first_last_360p")

    def test_invalid_resolution_is_rejected(self):
        with self.assertRaises(NonPenalizedTaskError) as caught:
            _veo_resolve_t2v_model(
                {"model": "omni-1.1-flash", "duration": 8, "resolution": "1080p"}
            )
        self.assertEqual(caught.exception.status_code, 400)


class OmniInputRoutingTests(unittest.TestCase):
    def setUp(self):
        self.urls = [f"https://example.com/ref-{i}.jpg" for i in range(1, 8)]

    def test_images_default_to_i2v_for_omni(self):
        ingredients, frames = _veo_resolve_video_input_urls(
            {"model": "omni-1.1-flash", "images": self.urls[:2]},
            omni_mode=True,
        )
        self.assertEqual(ingredients, [])
        self.assertEqual(frames, self.urls[:2])

    def test_ingredients_support_seven_references_for_omni(self):
        ingredients, frames = _veo_resolve_video_input_urls(
            {"model": "omni-1.1-flash", "Ingredients_images": self.urls},
            omni_mode=True,
        )
        self.assertEqual(ingredients, self.urls)
        self.assertEqual(frames, [])

    def test_explicit_r2v_supports_images_alias(self):
        ingredients, frames = _veo_resolve_video_input_urls(
            {
                "model": "omni-1.1-flash",
                "video_mode": "r2v",
                "images": self.urls,
            },
            omni_mode=True,
        )
        self.assertEqual(ingredients, self.urls)
        self.assertEqual(frames, [])

    def test_omni_rejects_more_than_seven_references(self):
        with self.assertRaises(NonPenalizedTaskError) as caught:
            _veo_resolve_video_input_urls(
                {
                    "model": "omni-1.1-flash",
                    "Ingredients_images": self.urls + ["https://example.com/ref-8.jpg"],
                },
                omni_mode=True,
            )
        self.assertEqual(caught.exception.status_code, 400)

    def test_veo31_ingredients_limit_remains_three(self):
        with self.assertRaises(NonPenalizedTaskError):
            _veo_collect_ingredients_image_urls({"Ingredients_images": self.urls[:4]})


if __name__ == "__main__":
    unittest.main()
