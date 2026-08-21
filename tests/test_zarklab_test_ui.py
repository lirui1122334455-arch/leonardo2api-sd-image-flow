import unittest
from html.parser import HTMLParser
from pathlib import Path


TEST_PAGE = Path(__file__).resolve().parents[1] / "static" / "test.html"


class _IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])


class ZarkLabTestUiTests(unittest.TestCase):
    def test_zark_controls_exist_with_unique_ids(self):
        html = TEST_PAGE.read_text(encoding="utf-8")
        parser = _IdCollector()
        parser.feed(html)

        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        for element_id in (
            "zarklabModelWrap",
            "zarklabModel",
            "zarklabDuration",
            "zarklabResolution",
            "zarklabAspectRatio",
            "zarklabSound",
            "zarklabStartFrameUrl",
            "zarklabEndFrameUrl",
            "zarklabReferenceImageUrls",
            "zarklabReferenceVideoUrls",
            "zarklabReferenceAudioUrls",
            "zarklabFileIds",
            "zarklabDryRun",
        ):
            self.assertIn(element_id, parser.ids)

    def test_all_public_models_and_dynamic_capabilities_are_present(self):
        html = TEST_PAGE.read_text(encoding="utf-8")

        for model in (
            "zark-seedance-2.5",
            "zark-seedance-2.0-lite",
            "zark-seedance-2.0-mini",
            "zark-seedance-2.0",
            "zark-minimax-h3",
        ):
            self.assertIn(f'value="{model}"', html)
            self.assertIn(f'"{model}": {{', html)
        self.assertIn("function syncZarklabCapabilities()", html)
        self.assertIn("maxImages: 30, maxVideos: 10, maxAudios: 10, maxTotal: 50", html)
        self.assertIn("maxImages: 9, maxVideos: 3, maxAudios: 3, maxTotal: 12", html)

    def test_zark_payload_contains_model_parameters_and_references(self):
        html = TEST_PAGE.read_text(encoding="utf-8")

        for assignment in (
            "payload.model = zarklabModel;",
            "payload.duration = zarklabDuration;",
            "payload.resolution = zarklabResolution;",
            "payload.aspect_ratio = zarklabAspectRatio;",
            "payload.sound = zarklabSound;",
            "payload.first_image_url = startFrameUrl;",
            "payload.last_image_url = endFrameUrl;",
            "payload.reference_image_urls = referenceImageUrls;",
            "payload.reference_video_urls = referenceVideoUrls;",
            "payload.reference_audio_urls = referenceAudioUrls;",
            "payload.zark_file_ids = zarkFileIds;",
        ):
            self.assertIn(assignment, html)

        self.assertIn('isZarkLabType(type)', html)
        self.assertIn('selectedZarklabReferenceMode() === "frames"', html)


if __name__ == "__main__":
    unittest.main()
