import csv
import tempfile
import unittest
from pathlib import Path

from src.services.fish_audio_voice_catalog import (
    filter_voices,
    load_curated_voices,
    normalize_public_voice,
)


class FishAudioVoiceCatalogTests(unittest.TestCase):
    def test_normalizes_public_voice_metadata(self):
        voice = normalize_public_voice(
            {
                "_id": "voice-1",
                "title": "Warm narrator",
                "description": "A warm voice",
                "tags": ["female", "middle-aged", "narration", "warm", "clear"],
                "languages": ["en"],
                "samples": [{"audio": "https://example.com/sample.mp3"}],
            }
        )

        self.assertIsNotNone(voice)
        self.assertEqual(voice["voice_id"], "voice-1")
        self.assertEqual(voice["gender"], "女")
        self.assertEqual(voice["age_group"], "中年")
        self.assertEqual(voice["languages"], ["英语"])
        self.assertEqual(voice["voice_traits"], ["warm", "clear"])
        self.assertEqual(voice["preview_url"], "https://example.com/sample.mp3")

    def test_loads_curated_csv_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voices.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "voice_id",
                        "name",
                        "gender",
                        "age_group",
                        "languages",
                        "voice_traits",
                        "preview_url",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "voice_id": "voice-zh",
                        "name": "清晰女声",
                        "gender": "女",
                        "age_group": "青年",
                        "languages": "中文",
                        "voice_traits": "明亮|清晰",
                        "preview_url": "https://example.com/zh.mp3",
                    }
                )
                writer.writerow(
                    {
                        "voice_id": "voice-en",
                        "name": "Deep male",
                        "gender": "男",
                        "age_group": "中年",
                        "languages": "英语",
                        "voice_traits": "低沉|专业",
                        "preview_url": "https://example.com/en.mp3",
                    }
                )

            voices = load_curated_voices(path)
            selected = filter_voices(voices, language="zh", gender="female", query="明亮")

        self.assertEqual(len(voices), 2)
        self.assertEqual([item["voice_id"] for item in selected], ["voice-zh"])
        self.assertEqual(selected[0]["voice_traits"], ["明亮", "清晰"])


if __name__ == "__main__":
    unittest.main()
