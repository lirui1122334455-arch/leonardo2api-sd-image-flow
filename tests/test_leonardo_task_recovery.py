import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.services import leonardo_task_executor as executor


class TargetClosedError(Exception):
    pass


class _FakeSession:
    def __init__(self):
        self.driver_lock = asyncio.Lock()
        self.cache_key = "test-session"
        self.ensure_open = AsyncMock()
        self.disconnect_playwright_only = AsyncMock()


class LeonardoTaskRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_reconnects_after_target_closed(self):
        calls = 0
        progress = []
        restored_page = object()

        async def graphql(page, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TargetClosedError("Target page, context or browser has been closed")
            self.assertIs(page, restored_page)
            return {
                "data": {
                    "generations_by_pk": {
                        "status": "COMPLETE",
                        "video_url": "https://cdn.leonardo.ai/example/video.mp4",
                    }
                }
            }

        async def progress_cb(value, payload):
            progress.append((value, payload))

        with (
            patch.object(executor, "_leonardo_graphql", side_effect=graphql),
            patch.object(executor.asyncio, "sleep", new=AsyncMock()),
            patch.object(executor, "append_log"),
        ):
            result = await executor._poll_generation_until_video(
                object(),
                auth_headers={"Authorization": "Bearer test"},
                generation_id="generation-1",
                timeout_seconds=60,
                log_file=Path("test.log"),
                progress_cb=progress_cb,
                reconnect_page=AsyncMock(return_value=restored_page),
            )

        self.assertEqual(result["video_url"], "https://cdn.leonardo.ai/example/video.mp4")
        self.assertEqual(calls, 2)
        self.assertTrue(any(item[1].get("stage") == "poll_reconnect" for item in progress))

    async def test_resume_generation_skips_upload_and_generate(self):
        session = _FakeSession()
        page = object()
        progress = []

        async def progress_cb(value, payload):
            progress.append((value, payload))

        poll_result = {
            "video_url": "https://cdn.leonardo.ai/example/resumed.mp4",
            "urls": ["https://cdn.leonardo.ai/example/resumed.mp4"],
        }
        with (
            patch.object(executor, "get_or_create_playwright_ctx", return_value=session),
            patch.object(executor, "_find_or_open_leonardo_page", new=AsyncMock(return_value=page)),
            patch.object(
                executor,
                "_capture_graphql_headers",
                new=AsyncMock(return_value={"Authorization": "Bearer test"}),
            ),
            patch.object(
                executor,
                "_build_reference_guidances",
                new=AsyncMock(side_effect=AssertionError("resume must not upload references")),
            ),
            patch.object(
                executor,
                "_leonardo_graphql",
                new=AsyncMock(side_effect=AssertionError("resume must not submit Generate")),
            ),
            patch.object(
                executor,
                "_poll_generation_until_video",
                new=AsyncMock(return_value=poll_result),
            ) as poll_mock,
        ):
            result = await executor.leonardo_workflow(
                {
                    "prompt": "resume test",
                    "model": "seedance-2.0",
                    "_leonardo_generation_id": "generation-existing",
                    "_leonardo_resume_meta": {
                        "duration": 13,
                        "aspect_ratio": "9:16",
                        "image_reference_count": 1,
                    },
                },
                progress_cb,
                browser_vendor="roxy",
                browser_base_url="http://127.0.0.1:50000",
                browser_access_key=None,
                space_id="space",
                window_key="window",
                timeout_seconds=300,
            )

        self.assertEqual(result["generation_id"], "generation-existing")
        self.assertEqual(result["video_url"], "https://cdn.leonardo.ai/example/resumed.mp4")
        self.assertEqual(result["video_mode"], "i2v")
        self.assertEqual(poll_mock.await_args.kwargs["generation_id"], "generation-existing")
        self.assertTrue(any(item[1].get("stage") == "resume_generation" for item in progress))


if __name__ == "__main__":
    unittest.main()
