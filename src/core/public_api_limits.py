"""Shared defaults and validators for public task API limits."""

from __future__ import annotations


DEFAULT_PUBLIC_CREATE_TASK_MAX_INFLIGHT = 180
DEFAULT_SERVER_COUNT = 1


def normalize_public_create_task_max_inflight(value: int | None) -> int:
    """Normalize public create-task inflight limit.

    Rule:
    - must be a positive multiple of 3;
    - fallback to default when input is invalid.
    """
    try:
        num = int(value or 0)
    except Exception:
        num = DEFAULT_PUBLIC_CREATE_TASK_MAX_INFLIGHT
    if num <= 0:
        num = DEFAULT_PUBLIC_CREATE_TASK_MAX_INFLIGHT
    if num % 3 != 0:
        num = DEFAULT_PUBLIC_CREATE_TASK_MAX_INFLIGHT
    return max(3, num)


def normalize_server_count(value: int | None) -> int:
    """Normalize server count (must be >= 1)."""
    try:
        num = int(value or 0)
    except Exception:
        num = DEFAULT_SERVER_COUNT
    return max(1, num)


def calc_public_browser_pool_limit(
    create_task_max_inflight: int | None,
    server_count: int | None = None,
) -> int:
    """Window candidate pool size for task scheduling.

    Formula: create_task_max_inflight / server_count / 3
    """
    limit = normalize_public_create_task_max_inflight(create_task_max_inflight)
    sc = normalize_server_count(server_count)
    return max(1, limit // sc // 3)

