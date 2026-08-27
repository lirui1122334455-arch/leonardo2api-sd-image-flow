import unittest
from io import BytesIO

from PIL import Image

from src.api.routes import _normalize_video_task_payload
from src.services.adobe_image2_task_executor import (
    _is_likely_image_url,
    extract_adobe_image_urls,
    is_adobe_generation_submission_request,
    parse_adobe_account_text,
    parse_adobe_effective_quota,
    resolve_adobe_image2_aspect_ratio,
    resolve_adobe_image2_quality,
    validate_adobe_image_bytes,
)
from src.services.task_executor_types import NonPenalizedTaskError
from src.services.task_handler_registry import (
    get_create_task_handler,
    get_refresh_quota_handler,
)


class AdobeImage2PayloadTests(unittest.TestCase):
    def test_public_model_routes_to_isolated_adobe_task_type(self):
        task_type, payload = _normalize_video_task_payload(
            {
                "model": "adobe-gpt-image2-high",
                "prompt": "a red paper sculpture",
                "aspect_ratio": "3:2",
            }
        )

        self.assertEqual(task_type, "adobe_image2_workflow")
        self.assertEqual(payload["quality"], "high")
        self.assertEqual(payload["provider_model"], "gpt-image@2")
        self.assertEqual(payload["workflow_kind"], "image")

    def test_existing_gpt_image2_route_is_unchanged(self):
        task_type, payload = _normalize_video_task_payload(
            {"model": "gpt-image2-2k", "prompt": "test"}
        )

        self.assertEqual(task_type, "gpt_workflow")
        self.assertEqual(payload["resolution"], "2k")

    def test_quality_and_ratio_validation(self):
        self.assertEqual(
            resolve_adobe_image2_quality({"model": "adobe-gpt-image2-low"}),
            ("adobe-gpt-image2-low", "low"),
        )
        self.assertEqual(resolve_adobe_image2_aspect_ratio({}), "auto")
        with self.assertRaises(NonPenalizedTaskError):
            resolve_adobe_image2_aspect_ratio({"aspect_ratio": "16:9"})
        with self.assertRaises(NonPenalizedTaskError):
            resolve_adobe_image2_quality({"quality": "ultra"})

    def test_handlers_are_registered_without_replacing_gpt(self):
        self.assertTrue(callable(get_create_task_handler("adobe_image2_workflow")))
        self.assertTrue(callable(get_refresh_quota_handler("adobe_firefly_credits")))
        self.assertTrue(callable(get_create_task_handler("gpt_workflow")))


class AdobeImage2AccountTests(unittest.TestCase):
    def test_parses_account_menu_credits(self):
        parsed = parse_adobe_account_text(
            [
                "Rubel Pata",
                "rubelpata7@gmail.com",
                "Premium features",
                "4000/4000 credits left",
                "Next reset: November 26, 2026",
            ]
        )

        self.assertEqual(parsed["platform_account"], "rubelpata7@gmail.com")
        self.assertEqual(parsed["remaining_quota"], 4000)
        self.assertEqual(parsed["provisioned_quota"], 4000)
        self.assertEqual(parsed["plan_title"], "Adobe Firefly Premium")

    def test_parses_bks_effective_quota(self):
        parsed = parse_adobe_effective_quota(
            {
                "effectiveQuotas": [
                    {
                        "resourceType": "firefly_credits",
                        "provisionedQuota": 4000.0,
                        "consumedQuota": 20.0,
                        "nextQuotaRefreshTimestamp": 1795759117,
                    }
                ]
            }
        )

        self.assertEqual(parsed["remaining_quota"], 3980)
        self.assertEqual(parsed["consumed_quota"], 20)
        self.assertEqual(parsed["next_reset_timestamp"], 1795759117)

    def test_extracts_image_assets_without_api_urls(self):
        urls = extract_adobe_image_urls(
            {
                "result": {
                    "imageUrl": "https://cdn.example.com/assets/result.webp?sig=1",
                    "statusUrl": "https://firefly-3p.ff.adobe.io/v2/status/job-1",
                }
            }
        )

        self.assertEqual(urls, ["https://cdn.example.com/assets/result.webp?sig=1"])


class AdobeImage2GenerationSafetyTests(unittest.TestCase):
    def test_rejects_known_tracking_pixel_url(self):
        self.assertFalse(_is_likely_image_url("https://alb.reddit.com/rp.gif"))

    def test_rejects_one_pixel_gif(self):
        tracking_gif = bytes.fromhex(
            "47494638396101000100800000000000ffffff21f90401000000002c000000000100010000020144003b"
        )

        with self.assertRaises(NonPenalizedTaskError):
            validate_adobe_image_bytes(tracking_gif)

    def test_accepts_generated_image_dimensions(self):
        output = BytesIO()
        Image.new("RGB", (256, 256), (210, 30, 45)).save(output, format="PNG")

        self.assertEqual(validate_adobe_image_bytes(output.getvalue())[:2], (256, 256))

    def test_identifies_adobe_generation_submission_request(self):
        self.assertTrue(
            is_adobe_generation_submission_request(
                url="https://firefly-3p.ff.adobe.io/v2/generate",
                method="POST",
                post_data='{"prompt":"a red paper sculpture","model":"gpt-image@2"}',
                prompt="a red paper sculpture",
            )
        )
        self.assertFalse(
            is_adobe_generation_submission_request(
                url="https://alb.reddit.com/rp.gif",
                method="GET",
                post_data=None,
                prompt="a red paper sculpture",
            )
        )


if __name__ == "__main__":
    unittest.main()
