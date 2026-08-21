import asyncio
import unittest

from src.services.browser_extension_bridge import ExtensionClient, _handle_client_message
from src.services.task_executor_types import NonPenalizedTaskError
from src.services.task_service import _task_error_allows_retry


class FlowRetrySafetyTests(unittest.TestCase):
    def test_submitted_poll_failure_is_not_retryable(self):
        exc = NonPenalizedTaskError(
            "VEO video poll failed",
            status_code=502,
            submitted=True,
            retryable=False,
            stage="polling",
        )

        self.assertTrue(exc.submitted)
        self.assertEqual(exc.stage, "polling")
        self.assertFalse(_task_error_allows_retry(exc))

    def test_legacy_errors_remain_retryable(self):
        self.assertTrue(_task_error_allows_retry(RuntimeError("temporary failure")))

    def test_submitted_flag_alone_disables_retry(self):
        exc = NonPenalizedTaskError("poll failed", submitted=True, retryable=True)
        self.assertFalse(_task_error_allows_retry(exc))


class BrowserExtensionBridgeErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_error_metadata_is_propagated(self):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        client = ExtensionClient(client_id="test-client", websocket=None)  # type: ignore[arg-type]
        client.pending["task-1"] = future

        await _handle_client_message(
            client,
            {
                "type": "task.error",
                "task_id": "task-1",
                "error": {
                    "message": "VEO video poll failed",
                    "status_code": 502,
                    "submitted": True,
                    "retryable": False,
                    "stage": "polling",
                },
            },
        )

        with self.assertRaises(NonPenalizedTaskError) as caught:
            await future
        self.assertEqual(caught.exception.status_code, 502)
        self.assertTrue(caught.exception.submitted)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.stage, "polling")


if __name__ == "__main__":
    unittest.main()
