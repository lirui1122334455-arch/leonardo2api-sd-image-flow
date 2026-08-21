import unittest
from unittest.mock import AsyncMock, patch

from src.services.task_executor_types import NonPenalizedTaskError
from src.services.task_service import TaskService


class _FakeDatabase:
    def __init__(self):
        self.updates = []

    async def update_task_type_window(self, **kwargs):
        self.updates.append(kwargs)


def _mapping_row():
    return {
        "mapping_id": 57,
        "window_key": "window-key",
        "lan_addr": "http://127.0.0.1:50000",
        "space_id": "113289",
        "vendor": "roxy",
        "default_target_url": "https://elevenlabs.io/app/sound-effects",
        "headless": 0,
        "pure_mode": 1,
    }


class ElevenLabsHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_updates_quota_and_clears_auth_failure_streak(self):
        db = _FakeDatabase()
        service = TaskService(db)  # type: ignore[arg-type]
        service._elevenlabs_health_auth_failures[57] = 1

        with patch(
            "src.services.elevenlabs_task_executor.elevenlabs_fetch_subscription",
            new=AsyncMock(
                return_value={
                    "tier": "free",
                    "character_limit": 10000,
                    "remaining_quota": 9800,
                }
            ),
        ):
            result = await service._elevenlabs_health_probe_mapping(_mapping_row())

        self.assertEqual(result["remaining_quota"], 9800)
        self.assertNotIn(57, service._elevenlabs_health_auth_failures)
        self.assertEqual(
            db.updates,
            [
                {
                    "mapping_id": 57,
                    "remaining_quota": 9800,
                    "sora_remaining_count": 9800,
                    "consecutive_errors": 0,
                    "daily_quota": 10000,
                    "sora_plan_title": "free",
                }
            ],
        )

    async def test_capture_timeout_requires_two_failures_before_disabling(self):
        db = _FakeDatabase()
        service = TaskService(db)  # type: ignore[arg-type]
        error = NonPenalizedTaskError(
            "ElevenLabs authorization capture timed out; make sure the fingerprint window is logged in",
            status_code=401,
        )

        with patch(
            "src.services.elevenlabs_task_executor.elevenlabs_fetch_subscription",
            new=AsyncMock(side_effect=error),
        ):
            await service._elevenlabs_health_probe_mapping(_mapping_row())
            self.assertEqual(db.updates, [])
            await service._elevenlabs_health_probe_mapping(_mapping_row())

        self.assertEqual(db.updates, [{"mapping_id": 57, "enabled": False}])

    async def test_confirmed_401_disables_immediately(self):
        db = _FakeDatabase()
        service = TaskService(db)  # type: ignore[arg-type]
        error = NonPenalizedTaskError("ElevenLabs login expired", status_code=401)

        with patch(
            "src.services.elevenlabs_task_executor.elevenlabs_fetch_subscription",
            new=AsyncMock(side_effect=error),
        ):
            await service._elevenlabs_health_probe_mapping(_mapping_row())

        self.assertEqual(db.updates, [{"mapping_id": 57, "enabled": False}])

    async def test_transient_error_does_not_disable_mapping(self):
        db = _FakeDatabase()
        service = TaskService(db)  # type: ignore[arg-type]

        with patch(
            "src.services.elevenlabs_task_executor.elevenlabs_fetch_subscription",
            new=AsyncMock(side_effect=RuntimeError("temporary network error")),
        ):
            await service._elevenlabs_health_probe_mapping(_mapping_row())

        self.assertEqual(db.updates, [])
        self.assertNotIn(57, service._elevenlabs_health_auth_failures)


if __name__ == "__main__":
    unittest.main()
