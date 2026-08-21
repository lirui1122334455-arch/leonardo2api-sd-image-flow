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


class FishAudioTestUiTests(unittest.TestCase):
    def test_fish_controls_exist_with_unique_ids(self):
        html = TEST_PAGE.read_text(encoding="utf-8")
        parser = _IdCollector()
        parser.feed(html)

        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        for element_id in (
            "fishAudioWrap",
            "fishVoiceSource",
            "fishVoiceLanguage",
            "fishVoiceGender",
            "fishVoiceAge",
            "fishVoiceSelect",
            "fishCustomVoiceId",
            "fishVoicePreview",
        ):
            self.assertIn(element_id, parser.ids)

    def test_fish_payload_and_audio_result_paths_are_present(self):
        html = TEST_PAGE.read_text(encoding="utf-8")

        self.assertIn('payload.reference_id = referenceId;', html)
        self.assertIn('const payload = audioWorkflow ? { text: prompt } : { prompt };', html)
        self.assertIn('.concat(d?.audio_url || [])', html)
        self.assertIn('<audio src="${escapeHtml(audioUrl)}"', html)
        self.assertIn('if (!audioWorkflow && !zarkLab && cardKeyIdRaw)', html)
        self.assertIn('if (!audioWorkflow && !zarkLab && duration !== undefined', html)


if __name__ == "__main__":
    unittest.main()
