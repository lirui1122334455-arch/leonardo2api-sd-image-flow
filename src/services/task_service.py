"""Task scheduling + dispatch service."""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from datetime import datetime
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ..core.database import Database
from ..core.logger import logger
from ..core.models import Task
from ..core.public_api_limits import DEFAULT_PUBLIC_CREATE_TASK_MAX_INFLIGHT, calc_public_browser_pool_limit
from ..core.config import config as app_config
from .image_task_executor import simulate_image_task
from .playwright_broswer_context import (
    acquire_browser_open_slot,
    get_or_create_ctx as get_or_create_playwright_ctx,
)
from .video_task_executor import simulate_video_task
from .sora_task_executor import (
    get_or_create_sora_session,
    sora_gen_video,
    refresh_sora_balance_best_effort,
    force_refresh_sora_access_token,
    window_pool_guard_unknown_handler_page,
)
from .task_executor_types import NonPenalizedTaskError
from .window_human_activity import (
    perform_human_activity_for_window_mapping,
    random_human_activity_delay,
)
from .browser_extension_bridge import get_extension_client
from .browser_extension_interaction import ensure_extension_connected_via_window


def _sora_task_error_needs_forced_access_token_refresh(exc: BaseException) -> bool:
    """sora_gen_video 失败时：在 exception 路径触发一次窗口内重抓 token，供后续队列重试用。"""
    msg = str(exc or "")
    ml = msg.lower()
    if "token_expired" in ml or "token is expired" in ml:
        return True
    return False


def _db_bool(value: Any, *, default: bool = False) -> bool:
    """Parse sqlite/mysql-ish boolean values without treating string "0" as True."""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    return bool(value)


def _task_exception_message(exc: BaseException) -> str:
    msg = str(exc or "").strip()
    if msg:
        ml = msg.lower()
        if "public_error_minor_upload" in ml:
            return (
                "参考图上传被 Flow 拒绝：图片可能包含真人肖像、未成年人/儿童主体、"
                "未授权人像或其他不符合图片上传政策的内容。请更换已授权且合规的参考图，"
                "或改用非真人、非儿童主体图片。（PUBLIC_ERROR_MINOR_UPLOAD）"
            )
        return msg
    if isinstance(exc, asyncio.TimeoutError):
        return "Task timed out waiting for Flow/browser extension result"
    return exc.__class__.__name__ or "task failed"


def _task_error_allows_retry(exc: BaseException) -> bool:
    """A submitted generation must never be resubmitted after poll failure."""
    return not bool(getattr(exc, "submitted", False)) and getattr(exc, "retryable", True) is not False


class FlowAccountUnavailableError(NonPenalizedTaskError):
    """Flow account/session is not usable; retry may switch to another mapping."""


def _flow_is_extension_unavailable_error(exc: BaseException) -> bool:
    msg = str(exc or "").strip().lower()
    status_code = getattr(exc, "status_code", None)
    try:
        code = int(status_code) if status_code is not None else None
    except Exception:
        code = None
    return code == 503 or "浏览器插件未连接" in msg or "extension client" in msg


def _flow_is_account_unavailable_error(exc: BaseException) -> bool:
    if isinstance(exc, FlowAccountUnavailableError):
        return True
    msg = str(exc or "").strip().lower()
    status_code = getattr(exc, "status_code", None)
    try:
        code = int(status_code) if status_code is not None else None
    except Exception:
        code = None
    return (
        code == 401
        or "missing usable session_token" in msg
        or "missing usable short access_token" in msg
        or "google账号已登出" in msg
        or "账号被登出" in msg
        or "账号已登出" in msg
        or "unauthenticated" in msg
        or "invalid authentication credentials" in msg
    )


def _leonardo_is_switchable_error(exc: BaseException) -> bool:
    msg = str(exc or "").strip().lower()
    status_code = getattr(exc, "status_code", None)
    try:
        code = int(status_code) if status_code is not None else None
    except Exception:
        code = None
    switch_hints = (
        "login_required",
        "cloudflare_challenge",
        "cloudflare",
        "turnstile",
        "unauthorized",
        "unauthenticated",
        "jwt",
        "auth header capture timed out",
        "browser context is not initialized",
        "fingerprint window is not open",
        "leonardo page is not open",
        "missing better-auth",
        "session ping",
        "graphql probe",
        "window_not_open",
        "no_leonardo_page",
        "quota",
        "token",
        "insufficient",
    )
    return code in {401, 403, 429, 503, 504} or any(h in msg for h in switch_hints)


def _effective_browser_pure_mode_from_context(ctx: Dict[str, Any]) -> bool:
    """窗口池 browser_open 的 pure_mode：使用绑定 pure_mode 列；缺省保持旧行为 True。"""
    return _db_bool(ctx.get("pure_mode"), default=True)


def _effective_task_concurrency_from_context(ctx: Dict[str, Any]) -> int:
    """Flow/extension video work is safest as one active task per browser window."""
    raw = int((ctx or {}).get("task_concurrency") or 1)
    handler = str((ctx or {}).get("create_task_handler") or "").strip()
    code = str((ctx or {}).get("task_code") or "").strip()
    if handler == "veo_workflow" or code == "veo_workflow":
        return 1
    return max(1, raw)


from .sora_wm_remove_executor import sora_wm_remove
from .sora_plus_register_executor import sora_plus_register
from .grok_workflow_executor import (
    DEFAULT_GROK_TARGET,
    get_or_create_grok_session,
    grok_ref_url_count,
    grok_workflow,
)
from .veo_workflow_executor import (
    _veo_cached_access_still_valid,
    _veo_resolve_n_frames,
    get_or_create_veo_session,
    refresh_veo_balance_via_extension,
    veo_fetch_access_tokens_via_extension,
    veo_workflow,
    _veo_project_page_url,
)
from .jimeng_task_executor import (
    DEFAULT_DREAMINA_TARGET,
    get_or_create_dreamina_session,
    refresh_dreamina_balance,
    refresh_dreamina_balance_best_effort,
    dreamina_workflow,
    _DREAMINA_MIN_CREDIT,
    _DREAMINA_GIFT_CREDIT,
    _dreamina_resolve_model,
    _dreamina_is_seedance20_fast,
    _dreamina_is_seedance20_mini,
)
from .gpt_task_executor import gpt_workflow, refresh_gpt_balance_via_extension, DEFAULT_GPT_TARGET
from .leonardo_task_executor import DEFAULT_LEONARDO_TARGET, leonardo_workflow
from .fish_audio_task_executor import fish_audio_workflow
from .elevenlabs_task_executor import elevenlabs_workflow
from .zarklab_task_executor import zarklab_workflow


@dataclass
class PickedWindow:
    mapping_id: int
    window_pk: int
    window_key: str
    task_code: str
    task_concurrency: int
    threshold: int
    close_window_threshold: int
    timeout_seconds: int
    create_task_handler: Optional[str]
    browser_vendor: str
    browser_base_url: str
    browser_access_key: Optional[str]
    space_id: str
    sora_access_token: Optional[str] = None
    sora_access_expires: Optional[str] = None
    default_target_url: Optional[str] = None
    window_ip: Optional[str] = None
    headless: bool = False
    pure_mode: bool = True
    error_retry_count: int = 0
    project_id: Optional[str] = None

@dataclass
class QueuedTask:
    task_id: str
    task_type_code: str
    payload: Dict[str, Any]
    enqueued_at: float
    retry_attempt: int = 0
    required_window_pk: Optional[int] = None
    is_dedicated_window: bool = False
    allow_account_switch: bool = True


def _truthy_payload_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value or "").strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _is_no_submit_payload(payload: Optional[Dict[str, Any]]) -> bool:
    p = payload or {}
    return any(
        _truthy_payload_flag(p.get(key))
        for key in ("dry_run", "dryRun", "skip_submit", "skipSubmit", "preview_only", "previewOnly")
    )


_DREAMINA_MIN_DURATION_SECONDS = 4
_DREAMINA_MAX_DURATION_SECONDS = 15
_DREAMINA_COST_PER_SECOND_MINI = 31
_DREAMINA_COST_PER_SECOND_FAST = 35
_DREAMINA_COST_PER_SECOND_STANDARD = 43
_DREAMINA_MIN_RUN_CREDIT = _DREAMINA_MIN_DURATION_SECONDS * _DREAMINA_COST_PER_SECOND_MINI


def _dreamina_estimated_credit_cost(payload: Optional[Dict[str, Any]]) -> int:
    if payload is None:
        return _DREAMINA_MIN_RUN_CREDIT
    p = payload or {}
    raw_duration = p.get("duration")
    if raw_duration is None or str(raw_duration).strip() == "":
        seconds = _DREAMINA_MAX_DURATION_SECONDS
    else:
        try:
            seconds = int(float(str(raw_duration).strip()))
        except (TypeError, ValueError):
            seconds = _DREAMINA_MAX_DURATION_SECONDS
    seconds = max(_DREAMINA_MIN_DURATION_SECONDS, min(_DREAMINA_MAX_DURATION_SECONDS, seconds))

    model = _dreamina_resolve_model(p, has_image=False)
    raw_model = str(p.get("model_name") or p.get("model") or model or "").strip().lower()
    if _dreamina_is_seedance20_mini(model) or "mini" in raw_model:
        unit_cost = _DREAMINA_COST_PER_SECOND_MINI
    elif _dreamina_is_seedance20_fast(model) or "fast" in raw_model:
        unit_cost = _DREAMINA_COST_PER_SECOND_FAST
    else:
        unit_cost = _DREAMINA_COST_PER_SECOND_STANDARD
    return seconds * unit_cost


def _remaining_quota_exclusive_floor_for_pick(
    task_type_code: str, payload: Optional[Dict[str, Any]]
) -> Tuple[int, int]:
    credit_threthold = 1;
    """与 pick 时 remaining_quota >= floor 及预扣额度对齐（见 _consume_quota_after_window_pick）。"""
    code = (task_type_code or "").strip()
    if code == "sora_gen_video":
        return 3, credit_threthold
    if code == "veo_workflow":
        if _veo_resolve_n_frames(payload or {}) > 1:
            return 30,credit_threthold
        else:
            return 10,credit_threthold
    if code == "grok_workflow":
        if _veo_resolve_n_frames(payload or {}) > 1:
            return 30,credit_threthold
        else:
            return 10,credit_threthold
    if code == "dreamina_workflow":
        if _is_no_submit_payload(payload):
            return 0, 0
        credit_threthold = _dreamina_estimated_credit_cost(payload)
        return credit_threthold, credit_threthold
    if code == "leonardo_workflow":
        return 1, credit_threthold
    if code == "zarklab_video":
        if _is_no_submit_payload(payload):
            return 0, 0
        return 1, credit_threthold
    if code == "fish_audio_workflow":
        return 1, credit_threthold
    if code == "elevenlabs_workflow":
        return 1, credit_threthold
    if code == "gpt_workflow":
        return 1, credit_threthold
    return 3,credit_threthold


class TaskService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._browser_pool_limit: int = calc_public_browser_pool_limit(DEFAULT_PUBLIC_CREATE_TASK_MAX_INFLIGHT)
        # 任务 payload 仍保留一份内存副本供执行器使用；DB 侧仅保存一个“可查看/可检索”的 prompt 字符串
        self._task_payloads: dict[str, Dict[str, Any]] = {}
        # 1) payload["prompt"] 本身的长度上限（便于查看，也避免超长文本撑爆 DB）
        self._payload_prompt_max_chars: int = 1000
        # 2) 最终落库到 tasks.prompt 的总长度上限（兼容某些历史/自定义 schema 的较短字段）
        self._prompt_max_chars: int = 2000

        # ---- 专用窗口并发控制（generation_id + head_url 类任务） ----
        self._dedicated_window_inflight: int = 0
        self._dedicated_window_lock = asyncio.Lock()
        self._browser_open_concurrency: int = 3

        # ---- 排队机制：窗口满载时入队等待，窗口释放时自动派发 ----
        self._pending_queue: deque[QueuedTask] = deque()
        self._queue_lock = asyncio.Lock()
        self._dispatch_event = asyncio.Event()
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._queue_max_size: int = 1000
        self._queue_timeout_seconds: float = 300.0
        self._dispatch_poll_interval: float = 5.0
        # 从 DB 缓存读取排队配置（避免频繁读库）
        self._queue_config_cache: tuple[float, int, float] = (0.0, 1000, 300.0)
        self._queue_config_ttl: float = 30.0

        # ---- 窗口池（按任务类型 code 维护应预热的 mapping_id；不占 inflight_slots） ----
        self._window_pool_stop = asyncio.Event()
        self._window_pool_task: Optional[asyncio.Task] = None
        self._window_pool_lock = asyncio.Lock()
        self._window_pool_reconcile_serial = asyncio.Lock()
        self._window_pool_wake = asyncio.Event()
        self._window_pool_force_reconcile = False
        self._window_pool_targets: dict[str, set[int]] = {}
        # Cloudflare 巡检周期（较长，默认 30 分钟）
        self._window_pool_cf_interval: float = 1800.0
        # 与 DB 对齐窗口池目标的 reconcile 周期（较短，默认 10 分钟）
        self._window_pool_reconcile_interval: float = 600.0
        # supervisor 单次休眠上限，避免 stop 后长时间无响应
        self._window_pool_supervisor_poll_cap: float = 60.0
        # Dreamina 余额刷新独立任务：不放在 _window_pool_supervisor_loop，避免被 reconcile/wait 阻塞。
        self._dreamina_refresh_task: Optional[asyncio.Task] = None
        self._dreamina_refresh_wake = asyncio.Event()
        self._dreamina_refresh_timeout: float = 60.0
        self._dreamina_refresh_scan_interval: float = 300.0

        # Flow 账号健康检查：低频、单窗口、只读取 auth/session，不做登录/生成。
        self._flow_health_task: Optional[asyncio.Task] = None
        self._flow_extension_keepalive_task: Optional[asyncio.Task] = None
        self._flow_light_activity_task: Optional[asyncio.Task] = None
        self._gpt_keepalive_task: Optional[asyncio.Task] = None
        self._elevenlabs_health_task: Optional[asyncio.Task] = None
        self._leonardo_keepalive_task: Optional[asyncio.Task] = None
        self._flow_health_last_ok: dict[int, float] = {}
        self._flow_health_pick_cursor: int = 0
        self._flow_light_activity_due: dict[int, float] = {}
        self._gpt_keepalive_pick_cursor: int = 0
        self._elevenlabs_health_pick_cursor: int = 0
        self._elevenlabs_health_auth_failures: dict[int, int] = {}
        self._leonardo_keepalive_pick_cursor: int = 0
        self._leonardo_keepalive_next_due: dict[int, float] = {}
        self._leonardo_keepalive_graphql_next_due: dict[int, float] = {}
        self._leonardo_keepalive_auth_failures: dict[int, int] = {}
        self._leonardo_keepalive_state: dict[int, str] = {}
        self._leonardo_keepalive_last_reason: dict[int, str] = {}
        self._leonardo_keepalive_last_ok: dict[int, float] = {}
        self._leonardo_session_expires_at: dict[int, int] = {}
        self._leonardo_session_updated_at: dict[int, int] = {}
        self._leonardo_proxy_warning_logged: set[int] = set()

    def set_browser_pool_limit(self, limit: int) -> None:
        """Hot-update scheduling candidate pool size."""
        try:
            self._browser_pool_limit = max(1, int(limit))
        except Exception:
            pass

    def start_window_pool_maintainer(self) -> None:
        """在进程内启动窗口池协程（幂等）。"""
        self.start_dreamina_balance_refresher()
        self.start_flow_account_health_checker()
        self.start_gpt_keepalive_checker()
        self.start_elevenlabs_health_checker()
        self.start_leonardo_keepalive_checker()
        if self._window_pool_task is not None and not self._window_pool_task.done():
            return
        try:
            self._window_pool_stop.clear()
        except Exception:
            pass
        self._window_pool_task = asyncio.create_task(
            self._window_pool_supervisor_loop(), name="window_pool_maintainer"
        )

    def _flow_health_config(self) -> Dict[str, Any]:
        raw = app_config.get_raw_config().get("flow_health", {})
        return raw if isinstance(raw, dict) else {}

    def _flow_health_bool(self, key: str, default: bool) -> bool:
        raw = self._flow_health_config().get(key, default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        s = str(raw or "").strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)

    def _flow_health_float(self, key: str, default: float, *, minimum: float = 0.0) -> float:
        try:
            val = float(self._flow_health_config().get(key, default))
        except Exception:
            val = float(default)
        return max(float(minimum), val)

    def _flow_health_int(self, key: str, default: int, *, minimum: int = 0) -> int:
        try:
            val = int(self._flow_health_config().get(key, default))
        except Exception:
            val = int(default)
        return max(int(minimum), val)

    def _gpt_keepalive_config(self) -> Dict[str, Any]:
        raw = app_config.get_raw_config().get("gpt_keepalive", {})
        return raw if isinstance(raw, dict) else {}

    def _elevenlabs_health_config(self) -> Dict[str, Any]:
        raw = app_config.get_raw_config().get("elevenlabs_health", {})
        return raw if isinstance(raw, dict) else {}

    def _leonardo_keepalive_config(self) -> Dict[str, Any]:
        raw = app_config.get_raw_config().get("leonardo_keepalive", {})
        return raw if isinstance(raw, dict) else {}

    def _gpt_keepalive_bool(self, key: str, default: bool) -> bool:
        raw = self._gpt_keepalive_config().get(key, default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        s = str(raw or "").strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)

    def _gpt_keepalive_float(self, key: str, default: float, *, minimum: float = 0.0) -> float:
        try:
            val = float(self._gpt_keepalive_config().get(key, default))
        except Exception:
            val = float(default)
        return max(float(minimum), val)

    def _elevenlabs_health_bool(self, key: str, default: bool) -> bool:
        raw = self._elevenlabs_health_config().get(key, default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        value = str(raw or "").strip().lower()
        if value in {"1", "true", "yes", "y", "on"}:
            return True
        if value in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)

    def _elevenlabs_health_float(self, key: str, default: float, *, minimum: float = 0.0) -> float:
        try:
            val = float(self._elevenlabs_health_config().get(key, default))
        except Exception:
            val = float(default)
        return max(float(minimum), val)

    def _elevenlabs_health_int(self, key: str, default: int, *, minimum: int = 0) -> int:
        try:
            val = int(self._elevenlabs_health_config().get(key, default))
        except Exception:
            val = int(default)
        return max(int(minimum), val)

    def _leonardo_keepalive_bool(self, key: str, default: bool) -> bool:
        raw = self._leonardo_keepalive_config().get(key, default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        s = str(raw or "").strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)

    def _leonardo_keepalive_float(self, key: str, default: float, *, minimum: float = 0.0) -> float:
        try:
            val = float(self._leonardo_keepalive_config().get(key, default))
        except Exception:
            val = float(default)
        return max(float(minimum), val)

    def _leonardo_keepalive_int(self, key: str, default: int, *, minimum: int = 0) -> int:
        try:
            val = int(self._leonardo_keepalive_config().get(key, default))
        except Exception:
            val = int(default)
        return max(int(minimum), val)

    def _leonardo_keepalive_mapping_ids(self) -> set[int]:
        raw = self._leonardo_keepalive_config().get("mapping_ids", "")
        if raw is None:
            return set()
        if isinstance(raw, (list, tuple, set)):
            parts = list(raw)
        else:
            parts = str(raw or "").replace(";", ",").split(",")
        out: set[int] = set()
        for item in parts:
            try:
                mid = int(str(item).strip())
            except Exception:
                continue
            if mid > 0:
                out.add(mid)
        return out

    def _leonardo_keepalive_backoff_seconds(self, reason: str) -> float:
        reason_key = str(reason or "").strip().lower()
        key_by_reason = {
            "window_not_open": "window_not_open_backoff_seconds",
            "no_leonardo_page": "missing_page_backoff_seconds",
            "page_closed": "missing_page_backoff_seconds",
            "auth_capture_unavailable": "auth_unavailable_backoff_seconds",
            "graphql_probe_failed": "auth_unavailable_backoff_seconds",
            "login_required": "login_backoff_seconds",
            "session_missing": "login_backoff_seconds",
            "cloudflare_challenge": "cloudflare_backoff_seconds",
        }
        key = key_by_reason.get(reason_key, "auth_unavailable_backoff_seconds")
        base = self._leonardo_keepalive_float(key, 1800.0, minimum=60.0)
        jitter = random.uniform(0.85, 1.15)
        return max(60.0, base * jitter)

    def _gpt_keepalive_random_delay(self, min_key: str, max_key: str, default_min: float, default_max: float) -> float:
        lo = self._gpt_keepalive_float(min_key, default_min, minimum=0.0)
        hi = self._gpt_keepalive_float(max_key, default_max, minimum=0.0)
        if hi < lo:
            hi = lo
        if hi <= lo:
            return lo
        return random.uniform(lo, hi)

    def _leonardo_keepalive_random_delay(self, min_key: str, max_key: str, default_min: float, default_max: float) -> float:
        lo = self._leonardo_keepalive_float(min_key, default_min, minimum=0.0)
        hi = self._leonardo_keepalive_float(max_key, default_max, minimum=0.0)
        if hi < lo:
            hi = lo
        if hi <= lo:
            return lo
        return random.uniform(lo, hi)

    def _elevenlabs_health_random_delay(
        self,
        min_key: str,
        max_key: str,
        default_min: float,
        default_max: float,
    ) -> float:
        lo = self._elevenlabs_health_float(min_key, default_min, minimum=0.0)
        hi = self._elevenlabs_health_float(max_key, default_max, minimum=0.0)
        if hi < lo:
            hi = lo
        if hi <= lo:
            return lo
        return random.uniform(lo, hi)

    def _leonardo_set_state(self, mapping_id: int, state: str, *, reason: str = "") -> None:
        mid = int(mapping_id)
        new_state = str(state or "unknown").strip().lower() or "unknown"
        old_state = self._leonardo_keepalive_state.get(mid, "unknown")
        old_reason = self._leonardo_keepalive_last_reason.get(mid, "")
        self._leonardo_keepalive_state[mid] = new_state
        self._leonardo_keepalive_last_reason[mid] = str(reason or "").strip()
        if old_state != new_state or old_reason != self._leonardo_keepalive_last_reason[mid]:
            logger.info(
                "leonardo state: mapping=%s %s->%s reason=%s",
                mid,
                old_state,
                new_state,
                self._leonardo_keepalive_last_reason[mid] or "-",
            )

    def _leonardo_mark_online(self, mapping_id: int, *, degraded: bool = False, reason: str = "") -> None:
        mid = int(mapping_id)
        self._leonardo_keepalive_auth_failures.pop(mid, None)
        self._leonardo_keepalive_last_ok[mid] = time.monotonic()
        self._leonardo_set_state(mid, "online_degraded" if degraded else "online", reason=reason if degraded else "")

    def _leonardo_record_auth_failure(self, mapping_id: int, reason: str) -> tuple[int, bool]:
        mid = int(mapping_id)
        streak = int(self._leonardo_keepalive_auth_failures.get(mid, 0)) + 1
        self._leonardo_keepalive_auth_failures[mid] = streak
        required = self._leonardo_keepalive_int("auth_failure_confirmations", 2, minimum=1)
        confirmed = streak >= required
        self._leonardo_set_state(mid, "offline" if confirmed else "suspect", reason=reason)
        return streak, confirmed

    def _leonardo_graphql_probe_due(self, mapping_id: int) -> bool:
        return time.monotonic() >= self._leonardo_keepalive_graphql_next_due.get(int(mapping_id), 0.0)

    def _leonardo_schedule_next_graphql_probe(self, mapping_id: int) -> float:
        delay = self._leonardo_keepalive_random_delay(
            "graphql_interval_min_seconds",
            "graphql_interval_max_seconds",
            1200.0,
            1800.0,
        )
        self._leonardo_keepalive_graphql_next_due[int(mapping_id)] = time.monotonic() + delay
        return delay

    def start_flow_account_health_checker(self) -> None:
        """启动 Flow 低频账号健康检查协程（幂等）。"""
        if not self._flow_health_bool("enabled", True):
            return
        if self._flow_health_bool("extension_keepalive_enabled", True):
            if self._flow_extension_keepalive_task is None or self._flow_extension_keepalive_task.done():
                self._flow_extension_keepalive_task = asyncio.create_task(
                    self._flow_extension_keepalive_loop(), name="flow_extension_keepalive"
                )
        if self._flow_health_bool("light_activity_enabled", True):
            if self._flow_light_activity_task is None or self._flow_light_activity_task.done():
                self._flow_light_activity_task = asyncio.create_task(
                    self._flow_light_activity_loop(), name="flow_light_activity"
                )
        if self._flow_health_task is not None and not self._flow_health_task.done():
            return
        try:
            self._window_pool_stop.clear()
        except Exception:
            pass
        self._flow_health_task = asyncio.create_task(
            self._flow_account_health_loop(), name="flow_account_health_checker"
        )

    def start_gpt_keepalive_checker(self) -> None:
        """Start the low-frequency GPT auth/session keepalive loop."""
        if not self._gpt_keepalive_bool("enabled", True):
            return
        if self._gpt_keepalive_task is not None and not self._gpt_keepalive_task.done():
            return
        try:
            self._window_pool_stop.clear()
        except Exception:
            pass
        self._gpt_keepalive_task = asyncio.create_task(
            self._gpt_keepalive_loop(), name="gpt_keepalive_checker"
        )

    def start_elevenlabs_health_checker(self) -> None:
        """Start the low-frequency ElevenLabs subscription health check."""
        if not self._elevenlabs_health_bool("enabled", True):
            return
        if self._elevenlabs_health_task is not None and not self._elevenlabs_health_task.done():
            return
        try:
            self._window_pool_stop.clear()
        except Exception:
            pass
        self._elevenlabs_health_task = asyncio.create_task(
            self._elevenlabs_health_loop(), name="elevenlabs_health_checker"
        )

    def start_leonardo_keepalive_checker(self) -> None:
        """Start the low-frequency Leonardo auth/session keepalive loop."""
        if not self._leonardo_keepalive_bool("enabled", True):
            return
        if self._leonardo_keepalive_task is not None and not self._leonardo_keepalive_task.done():
            return
        try:
            self._window_pool_stop.clear()
        except Exception:
            pass
        self._leonardo_keepalive_task = asyncio.create_task(
            self._leonardo_keepalive_loop(), name="leonardo_keepalive_checker"
        )

    def start_dreamina_balance_refresher(self) -> None:
        """启动 Dreamina 余额刷新独立协程（幂等）。"""
        if self._dreamina_refresh_task is not None and not self._dreamina_refresh_task.done():
            return
        try:
            self._window_pool_stop.clear()
            self._dreamina_refresh_wake.set()  # 启动后立即扫描一次。
        except Exception:
            pass
        self._dreamina_refresh_task = asyncio.create_task(
            self._dreamina_balance_refresher_loop(), name="dreamina_balance_refresher"
        )

    async def refresh_window_pool_targets_now(self) -> None:
        """任务类型窗口池开关等变更后立即与 DB 对齐（不等 supervisor 周期）。"""
        self.start_window_pool_maintainer()
        try:
            await self._window_pool_reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("window_pool refresh_window_pool_targets_now failed")

    async def stop_window_pool_maintainer(self) -> None:
        """停止窗口池协程并尽量关闭池内会话。"""
        self._window_pool_stop.set()
        ft = self._flow_health_task
        self._flow_health_task = None
        if ft is not None and not ft.done():
            ft.cancel()
            try:
                await ft
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        kt = self._flow_extension_keepalive_task
        self._flow_extension_keepalive_task = None
        if kt is not None and not kt.done():
            kt.cancel()
            try:
                await kt
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        lt = self._flow_light_activity_task
        self._flow_light_activity_task = None
        if lt is not None and not lt.done():
            lt.cancel()
            try:
                await lt
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        gt = self._gpt_keepalive_task
        self._gpt_keepalive_task = None
        if gt is not None and not gt.done():
            gt.cancel()
            try:
                await gt
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        et = self._elevenlabs_health_task
        self._elevenlabs_health_task = None
        if et is not None and not et.done():
            et.cancel()
            try:
                await et
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        lt2 = self._leonardo_keepalive_task
        self._leonardo_keepalive_task = None
        if lt2 is not None and not lt2.done():
            lt2.cancel()
            try:
                await lt2
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        rt = self._dreamina_refresh_task
        self._dreamina_refresh_task = None
        if rt is not None and not rt.done():
            rt.cancel()
            try:
                await rt
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        t = self._window_pool_task
        self._window_pool_task = None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        async with self._window_pool_lock:
            codes = list(self._window_pool_targets.keys())
            all_mids: set[int] = set()
            for c in codes:
                all_mids |= set(self._window_pool_targets.get(c, set()))
            self._window_pool_targets.clear()
        for mid in all_mids:
            try:
                await self._window_pool_close_mapping(mid)
            except Exception:
                pass

    def _signal_window_pool_replenish(self) -> None:
        """空闲关闭等导致缺窗时唤醒 supervisor 尽快 reconcile；正在 reconcile 时忽略。"""
        try:
            if self._window_pool_reconcile_serial.locked():
                return
        except Exception:
            return
        self.start_window_pool_maintainer()
        try:
            self._window_pool_wake.set()
        except Exception:
            pass

    async def _window_pool_wait_interruptible(self, timeout: float) -> bool:
        """休眠最多 timeout 秒；若 stop 则返回 True。期间收到 wake 则清除事件并在未占用 reconcile 锁时置 force。"""
        if timeout <= 0:
            return self._window_pool_stop.is_set()
        deadline = time.monotonic() + timeout
        while True:
            if self._window_pool_stop.is_set():
                return True
            if self._window_pool_wake.is_set():
                self._window_pool_wake.clear()
                try:
                    if not self._window_pool_reconcile_serial.locked():
                        self._window_pool_force_reconcile = True
                except Exception:
                    pass
                return False
            rem = deadline - time.monotonic()
            if rem <= 0:
                return False
            try:
                await asyncio.wait_for(self._window_pool_stop.wait(), timeout=min(1.0, rem))
                return True
            except asyncio.TimeoutError:
                pass

    def _window_pool_random_human_activity_delay(self) -> float:
        """下一轮窗口池拟人操作延迟：在 reconcile_interval 与 cf_interval 之间随机。"""
        return random_human_activity_delay(
            self._window_pool_reconcile_interval,
            self._window_pool_cf_interval,
        )

    async def _window_pool_supervisor_loop(self) -> None:
        # 首次 Cloudflare 巡检在启动后满 cf_interval 再执行，避免与首轮 reconcile 抢浏览器打开槽位
        last_cf = time.monotonic()
        # 首轮尽快 reconcile 一次以预热池；之后按 _window_pool_reconcile_interval
        last_reconcile = time.monotonic() - self._window_pool_reconcile_interval
        # 空闲窗口拟人操作：启动后按 [_window_pool_reconcile_interval, _window_pool_cf_interval] 随机延迟执行
        while not self._window_pool_stop.is_set():
            try:
                r_sec, c_sec = await self.db.get_window_pool_maintainer_intervals_seconds()
                self._window_pool_reconcile_interval = float(r_sec)
                self._window_pool_cf_interval = float(c_sec)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            now = time.monotonic()
            reconcile_due = self._window_pool_force_reconcile or (
                now - last_reconcile >= self._window_pool_reconcile_interval
            )
            if reconcile_due:
                self._window_pool_force_reconcile = False
                last_reconcile = now
                try:
                    await self._window_pool_reconcile_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception("window_pool reconcile: %s", e)
            now = time.monotonic()
            now = time.monotonic()
            due_r = max(0.0, last_reconcile + self._window_pool_reconcile_interval - now)
            due_c = max(0.0, last_cf + self._window_pool_cf_interval - now)
            wait = min(due_r, due_c, self._window_pool_supervisor_poll_cap)
            wait = max(0.1, wait)
            if await self._window_pool_wait_interruptible(wait):
                break

    async def _dreamina_balance_refresher_loop(self) -> None:
        """Dreamina 余额刷新独立循环。

        启动后立即扫描所有 enabled dreamina_workflow 窗口：
        - 已到期/即将到期（cooldown_until <= now + 1 minute）的先刷新；
        - 未到期的计算最近 cooldown_until，并睡到该时间前 1 分钟再刷新；
        - 不设 80 个上限，符合条件的有多少刷多少。
        """
        while not self._window_pool_stop.is_set():
            try:
                due_rows = await self._dreamina_refresh_list_due_candidates()
                if due_rows:
                    await self._dreamina_refresh_rows(due_rows)
                    continue
                next_wait = await self._dreamina_refresh_seconds_until_next_due()
                wait = min(max(1.0, next_wait), self._dreamina_refresh_scan_interval)
                self._dreamina_refresh_wake.clear()
                try:
                    await asyncio.wait_for(self._dreamina_refresh_wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("dreamina balance refresher loop: %s", e)
                try:
                    await asyncio.wait_for(self._window_pool_stop.wait(), timeout=30.0)
                    return
                except asyncio.TimeoutError:
                    pass

    async def _dreamina_refresh_rows(self, rows: list[Dict[str, Any]]) -> None:
        if not rows:
            return
        rows = sorted(rows, key=lambda r: str(r.get("cooldown_until") or ""))
        logger.info("dreamina balance refresh: due=%d", len(rows))
        ok = 0
        fail = 0
        for row in rows:
            if self._window_pool_stop.is_set():
                return
            try:
                picked = PickedWindow(
                    mapping_id=int(row.get("mapping_id") or row.get("id")),
                    window_pk=int(row.get("window_pk") or 0),
                    window_key=str(row.get("window_key") or ""),
                    task_code=str(row.get("task_code") or ""),
                    task_concurrency=int(row.get("task_concurrency") or 1),
                    threshold=int(row.get("continuous_error_threshold") or 3),
                    close_window_threshold=int(row.get("continuous_error_close_window_threshold") or 3),
                    timeout_seconds=int(row.get("timeout_seconds") or 1800),
                    create_task_handler=str(row.get("create_task_handler") or ""),
                    browser_vendor=str(row.get("browser_vendor") or "generic"),
                    browser_base_url=str(row.get("browser_base_url") or ""),
                    browser_access_key=row.get("browser_access_key"),
                    space_id=str(row.get("space_id") or ""),
                    sora_access_token=row.get("sora_access_token"),
                    sora_access_expires=row.get("sora_access_expires"),
                    default_target_url=row.get("default_target_url"),
                    window_ip=row.get("window_ip"),
                    headless=_db_bool(row.get("headless"), default=False),
                    pure_mode=_db_bool(row.get("pure_mode"), default=True),
                    error_retry_count=int(row.get("error_retry_count") or 0),
                )
                if not picked.sora_access_token:
                    continue
                await asyncio.wait_for(
                    refresh_dreamina_balance(
                        db=self.db,
                        picked=picked,
                        refresh_timeout_seconds=self._dreamina_refresh_timeout,
                        signal_window_pool_replenish=self._signal_window_pool_replenish,
                    ),
                    timeout=self._dreamina_refresh_timeout,
                )
                ok += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fail += 1
                logger.warning("dreamina balance refresh mapping=%s err=%s", row.get("mapping_id") or row.get("id"), e)
            try:
                await asyncio.wait_for(self._window_pool_stop.wait(), timeout=random.uniform(0.1, 0.5))
                return
            except asyncio.TimeoutError:
                pass
        logger.info("dreamina balance refresh done: ok=%d fail=%d", ok, fail)

    async def _dreamina_refresh_list_due_candidates(self) -> list[Dict[str, Any]]:
        return await self._dreamina_refresh_list_candidates(due_only=True)

    async def _dreamina_refresh_seconds_until_next_due(self) -> float:
        threshold = int(_DREAMINA_MIN_CREDIT - _DREAMINA_GIFT_CREDIT)
        async with self.db._read_conn() as db:  # type: ignore[attr-defined]
            import aiosqlite
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT CAST((julianday(MIN(m.cooldown_until)) - julianday(datetime('now','localtime', '+1 minute'))) * 86400.0 AS REAL) AS wait_seconds
                FROM task_type_windows m
                JOIN task_types t ON t.id = m.task_type_id
                JOIN windows w ON w.id = m.window_pk
                JOIN spaces s ON s.id = w.space_pk
                JOIN browsers b ON b.id = s.browser_id
                WHERE t.deleted = 0 AND t.enabled = 1
                  AND m.deleted = 0 AND m.enabled = 1
                  AND w.deleted = 0 AND w.enabled = 1
                  AND b.deleted = 0
                  AND t.create_task_handler = 'dreamina_workflow'
                  AND TRIM(COALESCE(m.sora_access_token, '')) <> ''
                  AND COALESCE(m.remaining_quota, 0) >= ?
                  AND m.cooldown_until IS NOT NULL
                  AND m.cooldown_until > datetime('now','localtime', '+1 minute')
                """,
                (threshold,),
            )
            row = await cur.fetchone()
            if not row or row["wait_seconds"] is None:
                return self._dreamina_refresh_scan_interval
            try:
                return max(1.0, float(row["wait_seconds"]))
            except Exception:
                return self._dreamina_refresh_scan_interval

    async def _dreamina_refresh_list_candidates(self, *, due_only: bool) -> list[Dict[str, Any]]:
        threshold = int(_DREAMINA_MIN_CREDIT - _DREAMINA_GIFT_CREDIT)
        async with self.db._read_conn() as db:  # type: ignore[attr-defined]
            import aiosqlite
            db.row_factory = aiosqlite.Row
            due_clause = "AND m.cooldown_until <= datetime('now','localtime', '+1 minute')" if due_only else ""
            cur = await db.execute(
                f"""
                SELECT
                  m.id AS mapping_id,
                  m.window_pk,
                  m.remaining_quota,
                  m.sora_remaining_count,
                  m.sora_access_token,
                  m.sora_access_expires,
                  m.cooldown_until,
                  m.headless,
                  m.pure_mode,
                  t.code AS task_code,
                  t.concurrency AS task_concurrency,
                  t.continuous_error_threshold,
                  t.continuous_error_close_window_threshold,
                  t.timeout_seconds,
                  t.create_task_handler,
                  t.error_retry_count,
                  t.default_target_url,
                  w.window_key,
                  w.proxy_addr AS window_ip,
                  s.space_id,
                  b.vendor AS browser_vendor,
                  b.lan_addr AS browser_base_url,
                  b.access_key AS browser_access_key
                FROM task_type_windows m
                JOIN task_types t ON t.id = m.task_type_id
                JOIN windows w ON w.id = m.window_pk
                JOIN spaces s ON s.id = w.space_pk
                JOIN browsers b ON b.id = s.browser_id
                WHERE t.deleted = 0 AND t.enabled = 1
                  AND m.deleted = 0 AND m.enabled = 1
                  AND w.deleted = 0 AND w.enabled = 1
                  AND b.deleted = 0
                  AND t.create_task_handler = 'dreamina_workflow'
                  AND TRIM(COALESCE(m.sora_access_token, '')) <> ''
                  AND COALESCE(m.remaining_quota, 0) >= ?
                  AND m.cooldown_until IS NOT NULL
                  {due_clause}
                ORDER BY m.cooldown_until ASC, m.remaining_quota ASC, m.updated_at ASC
                """,
                (threshold,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


    async def _flow_sleep_or_stop(self, seconds: float) -> bool:
        """Sleep until timeout or service stop; returns True when stopping."""
        try:
            await asyncio.wait_for(self._window_pool_stop.wait(), timeout=max(0.1, float(seconds)))
            return True
        except asyncio.TimeoutError:
            return False

    def _flow_random_delay(self, min_key: str, max_key: str, default_min: float, default_max: float) -> float:
        lo = self._flow_health_float(min_key, default_min, minimum=0.0)
        hi = self._flow_health_float(max_key, default_max, minimum=0.0)
        if hi < lo:
            lo, hi = hi, lo
        if hi <= 0:
            return 0.0
        return random.uniform(lo, hi)

    async def _flow_list_mapping_rows(self, *, include_disabled: bool) -> list[Dict[str, Any]]:
        enabled_clause = "" if include_disabled else "AND m.enabled = 1"
        async with self.db._read_conn() as db:  # type: ignore[attr-defined]
            import aiosqlite
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"""
                SELECT
                  m.*,
                  t.code AS task_code,
                  t.default_target_url,
                  w.window_key,
                  w.window_name,
                  w.proxy_addr AS window_ip,
                  s.space_id,
                  b.vendor,
                  b.lan_addr,
                  b.access_key,
                  (
                    SELECT v.project_id FROM veo_flow_projects v
                    WHERE v.task_type_window_id = m.id AND v.deleted = 0
                    ORDER BY v.updated_at DESC, v.id DESC
                    LIMIT 1
                  ) AS current_project_id
                FROM task_type_windows m
                JOIN task_types t ON t.id = m.task_type_id
                JOIN windows w ON w.id = m.window_pk
                JOIN spaces s ON s.id = w.space_pk
                JOIN browsers b ON b.id = s.browser_id
                WHERE t.deleted = 0 AND t.enabled = 1
                  AND t.create_task_handler = 'veo_workflow'
                  AND m.deleted = 0
                  {enabled_clause}
                  AND w.deleted = 0 AND w.enabled = 1
                  AND b.deleted = 0
                  AND TRIM(COALESCE(w.window_key, '')) <> ''
                ORDER BY COALESCE(w.window_sort_num, 999999), m.id
                """
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def _flow_extension_keepalive_loop(self) -> None:
        startup_delay = self._flow_random_delay(
            "extension_keepalive_startup_min_seconds",
            "extension_keepalive_startup_max_seconds",
            30.0,
            90.0,
        )
        logger.info("flow extension keepalive scheduled in %.0fs", startup_delay)
        if await self._flow_sleep_or_stop(startup_delay):
            return

        while not self._window_pool_stop.is_set():
            try:
                if self._flow_health_bool("extension_keepalive_enabled", True):
                    await self._flow_extension_keepalive_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("flow extension keepalive tick skipped: %s", e)

            interval = self._flow_random_delay(
                "extension_keepalive_interval_min_seconds",
                "extension_keepalive_interval_max_seconds",
                600.0,
                900.0,
            )
            if await self._flow_sleep_or_stop(interval):
                return

    async def _flow_extension_keepalive_once(self) -> None:
        rows = await self._flow_list_mapping_rows(
            include_disabled=self._flow_health_bool("extension_keepalive_include_disabled", True)
        )
        if not rows:
            return
        timeout = self._flow_health_float("extension_keepalive_timeout_seconds", 8.0, minimum=1.0)
        for row in rows:
            if self._window_pool_stop.is_set():
                return
            mid = int(row.get("mapping_id") or row.get("id") or 0)
            window_key = str(row.get("window_key") or "").strip()
            space_id = str(row.get("space_id") or "").strip()
            if not window_key or not space_id:
                continue
            if await get_extension_client(space_id, window_key) is not None:
                continue
            target_url = str(row.get("default_target_url") or "").strip() or "https://labs.google/fx/tools/flow"
            project_id = str(row.get("current_project_id") or "").strip() or None
            target_url = _veo_project_page_url(project_id=project_id, hint_url=target_url)
            sess = get_or_create_veo_session(
                vendor=str(row.get("vendor") or "generic"),
                base_url=str(row.get("lan_addr") or ""),
                access_key=row.get("access_key"),
                space_id=space_id,
                window_key=window_key,
            )
            sess.browser_headless = _db_bool(row.get("headless"), default=False)
            sess.browser_pure_mode = _effective_browser_pure_mode_from_context(row)
            try:
                client = await ensure_extension_connected_via_window(
                    sess=sess,
                    target_url=target_url,
                    space_id=space_id,
                    window_key=window_key,
                    wait_seconds=timeout,
                    force_open=False,
                    headless=sess.browser_headless,
                    pure_mode=sess.browser_pure_mode,
                    log_file=sess._log_file,
                )
                if client is not None:
                    logger.info("flow extension keepalive connected mapping=%s", mid)
                else:
                    logger.info("flow extension keepalive skipped; still offline mapping=%s", mid)
            except Exception as e:
                logger.warning("flow extension keepalive mapping=%s failed: %s", mid, e)
            jitter = random.uniform(2.0, 8.0)
            if await self._flow_sleep_or_stop(jitter):
                return

    def _flow_light_activity_delay(self) -> float:
        return self._flow_random_delay(
            "light_activity_interval_min_seconds",
            "light_activity_interval_max_seconds",
            1500.0,
            2700.0,
        )

    async def _flow_light_activity_loop(self) -> None:
        startup_delay = self._flow_random_delay(
            "light_activity_startup_min_seconds",
            "light_activity_startup_max_seconds",
            300.0,
            900.0,
        )
        logger.info("flow light activity scheduled in %.0fs", startup_delay)
        if await self._flow_sleep_or_stop(startup_delay):
            return

        while not self._window_pool_stop.is_set():
            try:
                if self._flow_health_bool("light_activity_enabled", True):
                    await self._flow_light_activity_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("flow light activity tick skipped: %s", e)

            tick = self._flow_health_float("light_activity_tick_seconds", 60.0, minimum=5.0)
            if await self._flow_sleep_or_stop(tick):
                return

    async def _flow_light_activity_once(self) -> None:
        rows = await self._flow_list_mapping_rows(
            include_disabled=self._flow_health_bool("light_activity_include_disabled", False)
        )
        rows = [r for r in rows if _db_bool(r.get("enabled"), default=False)]
        if not rows:
            return

        now = time.monotonic()
        known: set[int] = set()
        random.shuffle(rows)
        for row in rows:
            mid = int(row.get("mapping_id") or row.get("id") or 0)
            if mid <= 0:
                continue
            known.add(mid)
            if mid not in self._flow_light_activity_due:
                self._flow_light_activity_due[mid] = now + self._flow_light_activity_delay()
        for mid in list(self._flow_light_activity_due.keys()):
            if mid not in known:
                self._flow_light_activity_due.pop(mid, None)

        due_rows = [
            r for r in rows
            if now >= float(self._flow_light_activity_due.get(int(r.get("mapping_id") or r.get("id") or 0), now + 999999.0))
        ]
        if not due_rows:
            return

        for row in due_rows:
            if self._window_pool_stop.is_set():
                return
            mid = int(row.get("mapping_id") or row.get("id") or 0)
            if mid <= 0:
                continue
            if int(row.get("inflight_slots") or 0) > 0:
                self._flow_light_activity_due[mid] = now + self._flow_random_delay(
                    "light_activity_busy_retry_min_seconds",
                    "light_activity_busy_retry_max_seconds",
                    300.0,
                    600.0,
                )
                continue
            try:
                logger.info("flow light activity start mapping=%s", mid)
                await asyncio.wait_for(
                    perform_human_activity_for_window_mapping(
                        self.db,
                        mid,
                        max_refreshes=0,
                        mouse_moves=random.randint(1, 2),
                        scrolls=random.randint(1, 2),
                        input_attempts=0,
                    ),
                    timeout=self._flow_health_float("light_activity_timeout_seconds", 60.0, minimum=10.0),
                )
                logger.info("flow light activity ok mapping=%s", mid)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("flow light activity mapping=%s failed: %s", mid, e)
            finally:
                self._flow_light_activity_due[mid] = time.monotonic() + self._flow_light_activity_delay()

            gap = self._flow_random_delay(
                "light_activity_between_windows_min_seconds",
                "light_activity_between_windows_max_seconds",
                20.0,
                90.0,
            )
            if await self._flow_sleep_or_stop(gap):
                return
            return

    async def _flow_account_health_loop(self) -> None:
        """Low-frequency Flow auth/session checker.

        The background path intentionally checks only one Flow mapping per tick and,
        by default, can trigger extension reconnects. It does not log in,
        generate, click buttons, or refresh target pages.
        """
        startup_delay = self._flow_random_delay(
            "startup_delay_min_seconds",
            "startup_delay_max_seconds",
            1800.0,
            3600.0,
        )
        logger.info("flow account health checker scheduled in %.0fs", startup_delay)
        if await self._flow_sleep_or_stop(startup_delay):
            return

        while not self._window_pool_stop.is_set():
            try:
                row = await self._flow_health_pick_candidate_row()
                if row:
                    timeout = self._flow_health_float("background_timeout_seconds", 20.0, minimum=3.0)
                    await self._flow_probe_mapping_session(
                        row,
                        source="background_health",
                        auto_trigger_connection=self._flow_health_bool("background_auto_trigger_connection", False),
                        timeout_seconds=timeout,
                        disable_on_auth_failure=True,
                        raise_on_auth_failure=False,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("flow account health checker tick skipped: %s", e)

            interval = self._flow_random_delay(
                "interval_min_seconds",
                "interval_max_seconds",
                5400.0,
                9000.0,
            )
            if await self._flow_sleep_or_stop(interval):
                return

    async def _gpt_keepalive_loop(self) -> None:
        """Low-frequency GPT auth/session keepalive.

        This only reads ChatGPT /api/auth/session through the already-bound
        fingerprint window and stores the returned access token. It does not
        submit generation work or disable mappings on failure.
        """
        startup_delay = self._gpt_keepalive_random_delay(
            "startup_delay_min_seconds",
            "startup_delay_max_seconds",
            120.0,
            300.0,
        )
        logger.info("gpt keepalive scheduled in %.0fs", startup_delay)
        if await self._flow_sleep_or_stop(startup_delay):
            return

        while not self._window_pool_stop.is_set():
            try:
                row = await self._gpt_keepalive_pick_candidate_row()
                if row:
                    await self._gpt_keepalive_probe_mapping(row)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("gpt keepalive tick skipped: %s", e)

            interval = self._gpt_keepalive_random_delay(
                "interval_min_seconds",
                "interval_max_seconds",
                1200.0,
                1800.0,
            )
            if await self._flow_sleep_or_stop(interval):
                return

    async def _gpt_keepalive_pick_candidate_row(self) -> Optional[Dict[str, Any]]:
        rows = await self._gpt_keepalive_list_mapping_rows(
            include_disabled=self._gpt_keepalive_bool("include_disabled", False)
        )
        if not rows:
            return None
        idx = self._gpt_keepalive_pick_cursor % len(rows)
        self._gpt_keepalive_pick_cursor += 1
        return rows[idx]

    async def _gpt_keepalive_list_mapping_rows(self, *, include_disabled: bool) -> list[Dict[str, Any]]:
        enabled_clause = "" if include_disabled else "AND m.enabled = 1"
        async with self.db._read_conn() as db:  # type: ignore[attr-defined]
            import aiosqlite
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"""
                SELECT
                  m.*,
                  m.id AS mapping_id,
                  t.code AS task_code,
                  t.default_target_url,
                  w.window_key,
                  w.window_name,
                  w.platform_account,
                  s.space_id,
                  b.vendor,
                  b.lan_addr,
                  b.access_key
                FROM task_type_windows m
                JOIN task_types t ON t.id = m.task_type_id
                JOIN windows w ON w.id = m.window_pk
                JOIN spaces s ON s.id = w.space_pk
                JOIN browsers b ON b.id = s.browser_id
                WHERE t.deleted = 0 AND t.enabled = 1
                  AND t.create_task_handler = 'gpt_workflow'
                  AND m.deleted = 0
                  {enabled_clause}
                  AND w.deleted = 0 AND w.enabled = 1
                  AND b.deleted = 0
                  AND TRIM(COALESCE(w.window_key, '')) <> ''
                  AND TRIM(COALESCE(w.platform_account, '')) <> ''
                ORDER BY COALESCE(w.window_sort_num, 999999), m.id
                """
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def _gpt_keepalive_probe_mapping(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mid = int(row.get("mapping_id") or row.get("id") or 0)
        if mid <= 0:
            return None
        window_key = str(row.get("window_key") or "").strip()
        base_url = str(row.get("lan_addr") or row.get("browser_base_url") or "").strip()
        space_id = str(row.get("space_id") or "").strip()
        if not window_key or not base_url or not space_id:
            return None

        from .gpt_task_executor import (
            DEFAULT_GPT_TARGET,
            gpt_fetch_access_token_via_extension,
        )  # type: ignore

        timeout = self._gpt_keepalive_float("timeout_seconds", 45.0, minimum=5.0)
        target_url = str(row.get("default_target_url") or "").strip() or DEFAULT_GPT_TARGET
        vendor = str(row.get("vendor") or row.get("browser_vendor") or "generic").strip() or "generic"
        access_key = row.get("access_key") if row.get("access_key") is not None else row.get("browser_access_key")
        sess = get_or_create_veo_session(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            space_id=space_id,
            window_key=window_key,
        )
        sess.browser_headless = _db_bool(row.get("headless"), default=False)
        sess.browser_pure_mode = _effective_browser_pure_mode_from_context(row)
        try:
            token_info = await asyncio.wait_for(
                gpt_fetch_access_token_via_extension(
                    sess=sess,
                    space_id=space_id,
                    window_key=window_key,
                    target_url=target_url,
                    connect_wait_seconds=self._gpt_keepalive_float("connect_wait_seconds", 1.0, minimum=0.1),
                    token_timeout_seconds=timeout,
                    auto_triger_connection=self._gpt_keepalive_bool("auto_trigger_connection", False),
                ),
                timeout=max(8.0, timeout + 5.0),
            )
            access_token = str((token_info or {}).get("access_token") or "").strip()
            if not access_token:
                raise FlowAccountUnavailableError("GPT auth/session did not return access_token", status_code=401)
            await self.db.update_task_type_window(
                mapping_id=mid,
                sora_access_token=access_token,
                sora_access_expires=str((token_info or {}).get("expires") or "").strip() or None,
                consecutive_errors=0,
            )
            logger.info("gpt keepalive ok: mapping=%s", mid)
            return {"access_token": access_token, "expires": str((token_info or {}).get("expires") or "")}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("gpt keepalive skipped: mapping=%s err=%s", mid, e)
            return None

    async def _elevenlabs_health_loop(self) -> None:
        """Periodically read ElevenLabs subscription state without generating audio."""
        startup_delay = self._elevenlabs_health_random_delay(
            "startup_delay_min_seconds",
            "startup_delay_max_seconds",
            300.0,
            600.0,
        )
        logger.info("elevenlabs health check scheduled in %.0fs", startup_delay)
        if await self._flow_sleep_or_stop(startup_delay):
            return

        while not self._window_pool_stop.is_set():
            try:
                row = await self._elevenlabs_health_pick_candidate_row()
                if row:
                    await self._elevenlabs_health_probe_mapping(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("elevenlabs health check tick skipped: %s", exc)

            interval = self._elevenlabs_health_random_delay(
                "interval_min_seconds",
                "interval_max_seconds",
                2400.0,
                3000.0,
            )
            if await self._flow_sleep_or_stop(interval):
                return

    async def _elevenlabs_health_pick_candidate_row(self) -> Optional[Dict[str, Any]]:
        rows = await self._elevenlabs_health_list_mapping_rows()
        if not rows:
            return None
        idx = self._elevenlabs_health_pick_cursor % len(rows)
        self._elevenlabs_health_pick_cursor += 1
        return rows[idx]

    async def _elevenlabs_health_list_mapping_rows(self) -> list[Dict[str, Any]]:
        async with self.db._read_conn() as db:  # type: ignore[attr-defined]
            import aiosqlite

            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT
                  m.*,
                  m.id AS mapping_id,
                  t.code AS task_code,
                  t.default_target_url,
                  w.window_key,
                  w.window_name,
                  s.space_id,
                  b.vendor,
                  b.lan_addr,
                  b.access_key
                FROM task_type_windows m
                JOIN task_types t ON t.id = m.task_type_id
                JOIN windows w ON w.id = m.window_pk
                JOIN spaces s ON s.id = w.space_pk
                JOIN browsers b ON b.id = s.browser_id
                WHERE t.deleted = 0 AND t.enabled = 1
                  AND t.create_task_handler = 'elevenlabs_workflow'
                  AND m.deleted = 0 AND m.enabled = 1
                  AND w.deleted = 0 AND w.enabled = 1
                  AND b.deleted = 0
                  AND TRIM(COALESCE(w.window_key, '')) <> ''
                ORDER BY COALESCE(w.window_sort_num, 999999), m.id
                """
            )
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _elevenlabs_health_auth_failure_kind(exc: BaseException) -> str:
        message = str(exc or "").strip().lower()
        try:
            status_code = int(getattr(exc, "status_code", 0) or 0)
        except Exception:
            status_code = 0
        capture_timeout = "authorization capture timed out" in message
        if status_code == 401 and not capture_timeout:
            return "confirmed"
        if capture_timeout or any(
            marker in message
            for marker in (
                "login expired",
                "make sure the fingerprint window is logged in",
                "sign in",
                "sign-in",
            )
        ):
            return "suspect"
        return ""

    async def _elevenlabs_health_probe_mapping(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mid = int(row.get("mapping_id") or row.get("id") or 0)
        window_key = str(row.get("window_key") or "").strip()
        base_url = str(row.get("lan_addr") or row.get("browser_base_url") or "").strip()
        space_id = str(row.get("space_id") or "").strip()
        if mid <= 0 or not window_key or not base_url or not space_id:
            return None

        from .elevenlabs_task_executor import (
            DEFAULT_ELEVENLABS_TARGET,
            elevenlabs_fetch_subscription,
        )

        timeout = self._elevenlabs_health_float("timeout_seconds", 30.0, minimum=5.0)
        target_url = str(row.get("default_target_url") or "").strip() or DEFAULT_ELEVENLABS_TARGET
        try:
            info = await asyncio.wait_for(
                elevenlabs_fetch_subscription(
                    browser_vendor=str(row.get("vendor") or "roxy"),
                    browser_base_url=base_url,
                    browser_access_key=row.get("access_key"),
                    space_id=space_id,
                    window_key=window_key,
                    target_url=target_url,
                    headless=_db_bool(row.get("headless"), default=False),
                    pure_mode=_effective_browser_pure_mode_from_context(row),
                    timeout_seconds=timeout,
                ),
                timeout=max(10.0, timeout + 10.0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure_kind = self._elevenlabs_health_auth_failure_kind(exc)
            if not failure_kind:
                logger.warning("elevenlabs health check transient failure: mapping=%s err=%s", mid, exc)
                return None

            streak = int(self._elevenlabs_health_auth_failures.get(mid, 0)) + 1
            self._elevenlabs_health_auth_failures[mid] = streak
            required = self._elevenlabs_health_int("auth_failure_confirmations", 2, minimum=1)
            confirmed = failure_kind == "confirmed" or streak >= required
            disable = confirmed and self._elevenlabs_health_bool("disable_on_auth_failure", True)
            if disable:
                await self.db.update_task_type_window(mapping_id=mid, enabled=False)
            logger.warning(
                "elevenlabs auth health failure: mapping=%s kind=%s streak=%s disabled=%s err=%s",
                mid,
                failure_kind,
                streak,
                disable,
                exc,
            )
            return None

        self._elevenlabs_health_auth_failures.pop(mid, None)
        remaining = int((info or {}).get("remaining_quota") or 0)
        limit = int((info or {}).get("character_limit") or 0)
        update_kwargs: Dict[str, Any] = {
            "mapping_id": mid,
            "remaining_quota": remaining,
            "sora_remaining_count": remaining,
            "consecutive_errors": 0,
        }
        if limit > 0:
            update_kwargs["daily_quota"] = limit
        tier = str((info or {}).get("tier") or "").strip()
        if tier:
            update_kwargs["sora_plan_title"] = tier
        await self.db.update_task_type_window(**update_kwargs)
        logger.info(
            "elevenlabs health check ok: mapping=%s tier=%s remaining=%s limit=%s",
            mid,
            tier or "-",
            remaining,
            limit,
        )
        return dict(info or {})

    async def _leonardo_keepalive_loop(self) -> None:
        """Low-frequency Leonardo auth/session warmer."""
        startup_delay = self._leonardo_keepalive_random_delay(
            "startup_delay_min_seconds",
            "startup_delay_max_seconds",
            30.0,
            90.0,
        )
        logger.info("leonardo keepalive scheduled in %.0fs", startup_delay)
        if await self._flow_sleep_or_stop(startup_delay):
            return

        while not self._window_pool_stop.is_set():
            try:
                limit = self._leonardo_keepalive_int("max_mappings_per_tick", 3, minimum=1)
                rows = await self._leonardo_keepalive_pick_candidate_rows(limit=limit)
                for idx, row in enumerate(rows):
                    await self._leonardo_keepalive_probe_mapping(row)
                    if idx < len(rows) - 1:
                        delay = self._leonardo_keepalive_random_delay(
                            "between_mappings_min_seconds",
                            "between_mappings_max_seconds",
                            10.0,
                            25.0,
                        )
                        if await self._flow_sleep_or_stop(delay):
                            return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("leonardo keepalive tick skipped: %s", e)

            interval = self._leonardo_keepalive_random_delay(
                "interval_min_seconds",
                "interval_max_seconds",
                60.0,
                120.0,
            )
            if await self._flow_sleep_or_stop(interval):
                return

    async def _leonardo_keepalive_pick_candidate_row(self) -> Optional[Dict[str, Any]]:
        rows = await self._leonardo_keepalive_pick_candidate_rows(limit=1)
        return rows[0] if rows else None

    async def _leonardo_keepalive_pick_candidate_rows(self, *, limit: int) -> list[Dict[str, Any]]:
        rows = await self._leonardo_keepalive_list_mapping_rows(
            include_disabled=self._leonardo_keepalive_bool("include_disabled", False)
        )
        allowed_ids = self._leonardo_keepalive_mapping_ids()
        if allowed_ids:
            rows = [
                r
                for r in rows
                if int(r.get("mapping_id") or r.get("id") or 0) in allowed_ids
            ]
        now = time.monotonic()
        rows = [
            r
            for r in rows
            if now >= self._leonardo_keepalive_next_due.get(int(r.get("mapping_id") or r.get("id") or 0), 0.0)
        ]
        if not rows:
            return []
        count = min(max(1, int(limit or 1)), len(rows))
        urgent_states = {"suspect", "starting", "offline", "recovering"}
        urgent = [
            r
            for r in rows
            if self._leonardo_keepalive_state.get(int(r.get("mapping_id") or r.get("id") or 0), "") in urgent_states
        ]
        urgent.sort(
            key=lambda r: self._leonardo_keepalive_next_due.get(
                int(r.get("mapping_id") or r.get("id") or 0), 0.0
            )
        )
        if len(urgent) >= count:
            return urgent[:count]

        urgent_ids = {int(r.get("mapping_id") or r.get("id") or 0) for r in urgent}
        normal = [r for r in rows if int(r.get("mapping_id") or r.get("id") or 0) not in urgent_ids]
        if normal:
            idx = self._leonardo_keepalive_pick_cursor % len(normal)
            self._leonardo_keepalive_pick_cursor += max(1, count - len(urgent))
            normal = normal[idx:] + normal[:idx]
        return (urgent + normal)[:count]

    async def _leonardo_keepalive_list_mapping_rows(self, *, include_disabled: bool) -> list[Dict[str, Any]]:
        enabled_clause = "" if include_disabled else "AND m.enabled = 1"
        async with self.db._read_conn() as db:  # type: ignore[attr-defined]
            import aiosqlite
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"""
                SELECT
                  m.*,
                  m.id AS mapping_id,
                  t.code AS task_code,
                  t.default_target_url,
                  w.window_key,
                  w.window_name,
                  w.platform_account,
                  w.proxy_id,
                  s.space_id,
                  b.vendor,
                  b.lan_addr,
                  b.access_key
                FROM task_type_windows m
                JOIN task_types t ON t.id = m.task_type_id
                JOIN windows w ON w.id = m.window_pk
                JOIN spaces s ON s.id = w.space_pk
                JOIN browsers b ON b.id = s.browser_id
                WHERE t.deleted = 0 AND t.enabled = 1
                  AND t.create_task_handler = 'leonardo_workflow'
                  AND m.deleted = 0
                  {enabled_clause}
                  AND w.deleted = 0 AND w.enabled = 1
                  AND b.deleted = 0
                  AND TRIM(COALESCE(w.window_key, '')) <> ''
                  AND TRIM(COALESCE(w.platform_account, '')) <> ''
                ORDER BY COALESCE(w.window_sort_num, 999999), m.id
                """
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def _leonardo_open_closed_mapping(self, row: Dict[str, Any]) -> Dict[str, Any]:
        from .fp_browser_client import FPBrowserClient

        vendor = str(row.get("vendor") or row.get("browser_vendor") or "roxy").strip() or "roxy"
        base_url = str(row.get("lan_addr") or row.get("browser_base_url") or "").strip()
        access_key = row.get("access_key") if row.get("access_key") is not None else row.get("browser_access_key")
        space_id = str(row.get("space_id") or "").strip()
        window_key = str(row.get("window_key") or "").strip()
        if not base_url or not space_id or not window_key:
            return {"success": False, "error": "mapping missing lan_addr/space_id/window_key"}

        try:
            rsp = await asyncio.wait_for(
                FPBrowserClient().browser_open(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                    args=[],
                    force_open=False,
                    headless=_db_bool(row.get("headless"), default=False),
                    pure_mode=_effective_browser_pure_mode_from_context(row),
                ),
                timeout=self._leonardo_keepalive_float("window_open_timeout_seconds", 60.0, minimum=10.0),
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        code = (rsp or {}).get("code")
        data = (rsp or {}).get("data") or {}
        success = code == 0 or bool(data.get("http") or data.get("ws"))
        return {"success": bool(success), "response": rsp}

    async def _leonardo_keepalive_probe_mapping(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mid = int(row.get("mapping_id") or row.get("id") or 0)
        if mid <= 0:
            return None
        window_key = str(row.get("window_key") or "").strip()
        base_url = str(row.get("lan_addr") or row.get("browser_base_url") or "").strip()
        space_id = str(row.get("space_id") or "").strip()
        if not window_key or not base_url or not space_id:
            return None

        from .leonardo_task_executor import (
            LEONARDO_DEFAULT_AUTH_CACHE_SECONDS,
            LEONARDO_DEFAULT_CF_REFRESH_TTL_SECONDS,
            leonardo_keepalive,
            leonardo_restart_and_login_mapping,
        )  # type: ignore

        if int(row.get("proxy_id") or 0) <= 0 and mid not in self._leonardo_proxy_warning_logged:
            self._leonardo_proxy_warning_logged.add(mid)
            logger.warning(
                "leonardo mapping has no local fixed proxy binding: mapping=%s; verify the fingerprint profile itself uses a stable IP",
                mid,
            )

        timeout = self._leonardo_keepalive_float("timeout_seconds", 25.0, minimum=5.0)
        cache_seconds = self._leonardo_keepalive_float(
            "auth_cache_seconds", LEONARDO_DEFAULT_AUTH_CACHE_SECONDS, minimum=0.0
        )
        cf_refresh_ttl = self._leonardo_keepalive_float(
            "cf_refresh_ttl_seconds", LEONARDO_DEFAULT_CF_REFRESH_TTL_SECONDS, minimum=0.0
        )
        target_url = str(row.get("default_target_url") or "").strip() or DEFAULT_LEONARDO_TARGET
        vendor = str(row.get("vendor") or row.get("browser_vendor") or "generic").strip() or "generic"
        access_key = row.get("access_key") if row.get("access_key") is not None else row.get("browser_access_key")
        graphql_due = self._leonardo_keepalive_bool("probe_graphql", True) and self._leonardo_graphql_probe_due(mid)

        try:
            result = await asyncio.wait_for(
                leonardo_keepalive(
                    browser_vendor=vendor,
                    browser_base_url=base_url,
                    browser_access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                    target_url=target_url,
                    headless=_db_bool(row.get("headless"), default=False),
                    pure_mode=_effective_browser_pure_mode_from_context(row),
                    auth_cache_seconds=cache_seconds,
                    auth_capture_timeout_seconds=timeout,
                    probe_graphql=graphql_due,
                    active_auth_capture=self._leonardo_keepalive_bool("active_auth_capture", False),
                    auth_session_probe_enabled=self._leonardo_keepalive_bool("auth_session_probe_enabled", True),
                    force_server_session_refresh=self._leonardo_keepalive_bool(
                        "force_server_session_refresh", False
                    ),
                    session_ping_enabled=self._leonardo_keepalive_bool("session_ping_enabled", True),
                    cf_refresh_ttl_seconds=cf_refresh_ttl,
                    ui_mode_toggle=self._leonardo_keepalive_bool("ui_mode_toggle", False),
                    active_page_refresh=self._leonardo_keepalive_bool("active_page_refresh", False),
                    disconnect_after=self._leonardo_keepalive_bool("disconnect_after_keepalive", True),
                ),
                timeout=max(45.0, timeout * 3.0 + 45.0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delay = self._leonardo_keepalive_random_delay(
                "transient_retry_min_seconds", "transient_retry_max_seconds", 120.0, 300.0
            )
            self._leonardo_keepalive_next_due[mid] = time.monotonic() + delay
            self._leonardo_set_state(mid, "unknown", reason="probe_exception")
            logger.warning("leonardo keepalive probe error: mapping=%s retry=%.0fs err=%s", mid, delay, exc)
            return None

        cookie_state = (result or {}).get("cookie_state")
        auth_session = (result or {}).get("auth_session")
        auth_session = auth_session if isinstance(auth_session, dict) else {}
        try:
            server_expires_at = int(auth_session.get("session_expires_at") or 0)
        except Exception:
            server_expires_at = 0
        try:
            server_updated_at = int(auth_session.get("session_updated_at") or 0)
        except Exception:
            server_updated_at = 0
        if server_expires_at > 0:
            self._leonardo_session_expires_at[mid] = server_expires_at
        if server_updated_at > 0:
            self._leonardo_session_updated_at[mid] = server_updated_at
        if isinstance(cookie_state, dict) and cookie_state.get("cookies_checked"):
            logger.info(
                "leonardo keepalive state: mapping=%s cf_present=%s cf_ttl_seconds=%s "
                "better_auth=%s session_ttl_seconds=%s session_data=%s",
                mid,
                bool(cookie_state.get("cf_access_token_present")),
                cookie_state.get("cf_access_token_ttl_seconds"),
                bool(cookie_state.get("better_auth_session_present")),
                cookie_state.get("better_auth_session_ttl_seconds"),
                int(cookie_state.get("better_auth_session_data_count") or 0),
            )
        if auth_session:
            logger.info(
                "leonardo server session: mapping=%s authenticated=%s status=%s "
                "expires_at=%s expires_in_seconds=%s updated_at=%s updated_age_seconds=%s",
                mid,
                auth_session.get("authenticated"),
                auth_session.get("status"),
                auth_session.get("session_expires_at"),
                auth_session.get("session_expires_in_seconds"),
                auth_session.get("session_updated_at"),
                auth_session.get("session_updated_age_seconds"),
            )

        if bool((result or {}).get("ok")):
            session_state = str((result or {}).get("session_state") or "online").strip().lower()
            degraded = session_state == "online_degraded"
            self._leonardo_mark_online(
                mid,
                degraded=degraded,
                reason=str((result or {}).get("reason") or "") if degraded else "",
            )
            success_delay = self._leonardo_keepalive_random_delay(
                "success_interval_min_seconds", "success_interval_max_seconds", 600.0, 900.0
            )
            self._leonardo_keepalive_next_due[mid] = time.monotonic() + success_delay
            if graphql_due:
                self._leonardo_schedule_next_graphql_probe(mid)
            balance = (result or {}).get("balance")
            update_kwargs: Dict[str, Any] = {"consecutive_errors": 0, "error_cooldown_until": ""}
            if isinstance(balance, dict) and "remaining_quota" in balance:
                remaining = int(balance.get("remaining_quota") or 0)
                update_kwargs.update(
                    remaining_quota=remaining,
                    sora_remaining_count=remaining,
                    sora_purchased_remaining_count=int(balance.get("paid_tokens") or 0),
                    sora_plan_title=str(balance.get("plan") or "").strip(),
                    sora_subscription_end=str(balance.get("token_renewal_date") or "").strip(),
                    sora_rate_limit_reached=False,
                    sora_access_resets_in_seconds=0,
                )
            try:
                await self.db.update_task_type_window(mapping_id=mid, **update_kwargs)
            except Exception as update_exc:
                logger.warning("leonardo keepalive state update failed: mapping=%s err=%s", mid, update_exc)
            logger.info(
                "leonardo keepalive ok: mapping=%s remaining_quota=%s graphql=%s next_due=%.0fs",
                mid,
                (balance or {}).get("remaining_quota") if isinstance(balance, dict) else "unchanged",
                bool(graphql_due),
                success_delay,
            )
            return result

        reason = str((result or {}).get("reason") or "unknown").strip().lower()
        session_state = str((result or {}).get("session_state") or "unknown").strip().lower()
        if reason == "window_not_open":
            if self._leonardo_keepalive_bool("auto_open_closed_windows", True):
                self._leonardo_set_state(mid, "starting", reason=reason)
                opened = await self._leonardo_open_closed_mapping(row)
                if bool(opened.get("success")):
                    delay = self._leonardo_keepalive_float("window_open_recheck_seconds", 45.0, minimum=10.0)
                    self._leonardo_keepalive_next_due[mid] = time.monotonic() + delay
                    logger.info("leonardo closed window opened: mapping=%s recheck=%.0fs", mid, delay)
                    merged = dict(result or {})
                    merged["window_open"] = opened
                    return merged
                logger.warning("leonardo closed window open failed: mapping=%s err=%s", mid, opened.get("error") or opened)
            backoff = self._leonardo_keepalive_backoff_seconds(reason)
            self._leonardo_keepalive_next_due[mid] = time.monotonic() + backoff
            self._leonardo_set_state(mid, "closed", reason=reason)
            return result

        if reason == "cloudflare_challenge":
            self._leonardo_set_state(mid, "manual_verification", reason=reason)
            backoff = self._leonardo_keepalive_backoff_seconds(reason)
            self._leonardo_keepalive_next_due[mid] = time.monotonic() + backoff
            logger.warning("leonardo manual Cloudflare verification required: mapping=%s backoff=%.0fs", mid, backoff)
            return result

        auth_like = session_state == "offline" and reason in {"login_required", "session_missing"}
        if not auth_like:
            delay = self._leonardo_keepalive_random_delay(
                "transient_retry_min_seconds", "transient_retry_max_seconds", 120.0, 300.0
            )
            self._leonardo_keepalive_next_due[mid] = time.monotonic() + delay
            self._leonardo_set_state(mid, "unknown", reason=reason)
            logger.info(
                "leonardo probe inconclusive: mapping=%s state=%s reason=%s retry=%.0fs",
                mid,
                session_state,
                reason,
                delay,
            )
            return result

        streak, confirmed = self._leonardo_record_auth_failure(mid, reason)
        last_server_expiry = self._leonardo_session_expires_at.get(mid)
        seconds_from_server_expiry = (
            int(time.time()) - int(last_server_expiry) if last_server_expiry else None
        )
        if not confirmed:
            delay = self._leonardo_keepalive_random_delay(
                "suspect_recheck_min_seconds", "suspect_recheck_max_seconds", 45.0, 90.0
            )
            self._leonardo_keepalive_next_due[mid] = time.monotonic() + delay
            logger.warning(
                "leonardo auth suspect: mapping=%s reason=%s streak=%s retry=%.0fs "
                "last_server_expiry=%s seconds_from_server_expiry=%s",
                mid,
                reason,
                streak,
                delay,
                last_server_expiry,
                seconds_from_server_expiry,
            )
            return result

        try:
            task_cd = self._leonardo_keepalive_int("recovery_task_cooldown_seconds", 900, minimum=60)
            await self.db.mark_mapping_error(
                mapping_id=mid,
                threshold=int(row.get("continuous_error_threshold") or 3),
                cooldown_seconds=task_cd,
                cooldown_seconds_short=task_cd,
                reset_on_threshold=False,
            )
        except Exception as cool_exc:
            logger.warning("leonardo recovery cooldown failed: mapping=%s err=%s", mid, cool_exc)

        if not self._leonardo_keepalive_bool("auto_relogin_enabled", True):
            backoff = self._leonardo_keepalive_backoff_seconds("login_required")
            self._leonardo_keepalive_next_due[mid] = time.monotonic() + backoff
            return result

        self._leonardo_set_state(mid, "recovering", reason=reason)
        try:
            ctx_row = await self.db.get_task_type_window_context(mid)
        except Exception:
            ctx_row = None
        recover_result: Dict[str, Any]
        if not ctx_row:
            recover_result = {"success": False, "errors": ["mapping context not found"]}
        else:
            try:
                recover_result = await asyncio.wait_for(
                    leonardo_restart_and_login_mapping(
                        ctx_row=ctx_row,
                        headless=_db_bool(row.get("headless"), default=False),
                        wait_after_open_seconds=self._leonardo_keepalive_float(
                            "auto_relogin_wait_after_open_seconds", 3.0, minimum=0.0
                        ),
                    ),
                    timeout=self._leonardo_keepalive_float("auto_relogin_timeout_seconds", 300.0, minimum=60.0),
                )
            except Exception as recover_exc:
                recover_result = {"success": False, "errors": [str(recover_exc)]}

        merged = dict(result or {})
        merged["auto_relogin"] = recover_result
        if bool(recover_result.get("manual_verification_required")):
            self._leonardo_set_state(mid, "manual_verification", reason="cloudflare_challenge")
            backoff = self._leonardo_keepalive_float("manual_verification_backoff_seconds", 1800.0, minimum=300.0)
            self._leonardo_keepalive_next_due[mid] = time.monotonic() + backoff
            logger.warning("leonardo recovery waiting for manual verification: mapping=%s backoff=%.0fs", mid, backoff)
            return merged

        if bool(recover_result.get("success")):
            self._leonardo_mark_online(mid)
            balance = recover_result.get("balance")
            update_kwargs = {"consecutive_errors": 0, "error_cooldown_until": ""}
            if isinstance(balance, dict) and "remaining_quota" in balance:
                remaining = int(balance.get("remaining_quota") or 0)
                update_kwargs.update(
                    remaining_quota=remaining,
                    sora_remaining_count=remaining,
                    sora_purchased_remaining_count=int(balance.get("paid_tokens") or 0),
                    sora_plan_title=str(balance.get("plan") or "").strip(),
                    sora_subscription_end=str(balance.get("token_renewal_date") or "").strip(),
                    sora_rate_limit_reached=False,
                    sora_access_resets_in_seconds=0,
                )
            try:
                await self.db.update_task_type_window(mapping_id=mid, **update_kwargs)
            except Exception as update_exc:
                logger.warning("leonardo recovery balance update failed: mapping=%s err=%s", mid, update_exc)
            self._leonardo_schedule_next_graphql_probe(mid)
            next_probe = self._leonardo_keepalive_float("post_relogin_probe_seconds", 60.0, minimum=10.0)
            self._leonardo_keepalive_next_due[mid] = time.monotonic() + next_probe
            logger.info("leonardo recovery completed: mapping=%s next_probe=%.0fs", mid, next_probe)
            return merged

        if bool(recover_result.get("preflight_inconclusive")):
            self._leonardo_keepalive_auth_failures.pop(mid, None)
            self._leonardo_set_state(mid, "unknown", reason="recovery_preflight_inconclusive")
            delay = self._leonardo_keepalive_random_delay(
                "transient_retry_min_seconds", "transient_retry_max_seconds", 120.0, 300.0
            )
            self._leonardo_keepalive_next_due[mid] = time.monotonic() + delay
            logger.warning("leonardo recovery cancelled by inconclusive preflight: mapping=%s retry=%.0fs", mid, delay)
            return merged

        self._leonardo_set_state(mid, "offline", reason=reason)
        backoff = self._leonardo_keepalive_backoff_seconds("login_required")
        self._leonardo_keepalive_next_due[mid] = time.monotonic() + backoff
        errs = "; ".join(str(x) for x in (recover_result.get("errors") or []) if str(x).strip())
        logger.warning("leonardo recovery failed: mapping=%s backoff=%.0fs err=%s", mid, backoff, errs or "unknown")
        return merged

    async def _flow_health_pick_candidate_row(self) -> Optional[Dict[str, Any]]:
        rows = await self._flow_list_mapping_rows(
            include_disabled=self._flow_health_bool("background_include_disabled", True)
        )
        if not rows:
            return None
        idx = self._flow_health_pick_cursor % len(rows)
        self._flow_health_pick_cursor += 1
        return rows[idx]

    async def _flow_enabled_mapping_count(self, task_type_code: str) -> int:
        code = (task_type_code or "").strip()
        if not code:
            return 0
        async with self.db._read_conn() as db:  # type: ignore[attr-defined]
            cur = await db.execute(
                """
                SELECT COUNT(1)
                FROM task_type_windows m
                JOIN task_types t ON t.id = m.task_type_id
                JOIN windows w ON w.id = m.window_pk
                WHERE t.deleted = 0 AND t.enabled = 1
                  AND t.code = ?
                  AND m.deleted = 0 AND m.enabled = 1
                  AND w.deleted = 0 AND w.enabled = 1
                """,
                (code,),
            )
            row = await cur.fetchone()
            try:
                return int((row or [0])[0] or 0)
            except Exception:
                return 0

    async def _flow_disable_mapping_after_auth_failure(self, mapping_id: int, reason: str) -> None:
        mid = int(mapping_id)
        self._flow_health_last_ok.pop(mid, None)
        try:
            await self.db.update_task_type_window(
                mapping_id=mid,
                enabled=False,
                sora_access_token="",
                sora_access_expires="",
                error_cooldown_until="",
            )
            logger.warning("flow mapping disabled after auth failure: mapping=%s reason=%s", mid, reason)
        except Exception as e:
            logger.warning("flow disable mapping=%s after auth failure failed: %s", mid, e)

    async def _flow_probe_mapping_session(
        self,
        row: Dict[str, Any],
        *,
        source: str,
        auto_trigger_connection: bool,
        timeout_seconds: float,
        disable_on_auth_failure: bool,
        raise_on_auth_failure: bool,
    ) -> Optional[Dict[str, Any]]:
        mid = int(row.get("mapping_id") or row.get("id") or 0)
        if mid <= 0:
            return None
        window_key = str(row.get("window_key") or "").strip()
        base_url = str(row.get("lan_addr") or row.get("browser_base_url") or "").strip()
        if not window_key or not base_url:
            return None

        target_url = str(row.get("default_target_url") or "").strip() or "https://labs.google/fx/tools/flow"
        project_id = str(row.get("current_project_id") or row.get("project_id") or "").strip() or None
        target_url = _veo_project_page_url(project_id=project_id, hint_url=target_url)
        space_id = str(row.get("space_id") or "").strip()
        vendor = str(row.get("vendor") or row.get("browser_vendor") or "generic").strip() or "generic"
        access_key = row.get("access_key") if row.get("access_key") is not None else row.get("browser_access_key")
        current_token = str(row.get("sora_access_token") or "").strip() or None
        current_expires = str(row.get("sora_access_expires") or "").strip() or None

        sess = get_or_create_veo_session(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            space_id=space_id,
            window_key=window_key,
        )
        sess.browser_headless = _db_bool(row.get("headless"), default=False)
        sess.browser_pure_mode = _effective_browser_pure_mode_from_context(row)

        try:
            token_info = await asyncio.wait_for(
                veo_fetch_access_tokens_via_extension(
                    sess=sess,
                    target_url=target_url,
                    space_id=space_id,
                    window_key=window_key,
                    connect_wait_seconds=min(8.0, max(0.5, float(timeout_seconds))),
                    token_timeout_seconds=max(5.0, float(timeout_seconds)),
                    auto_triger_connection=bool(auto_trigger_connection),
                    access_token=current_token,
                    access_expires=current_expires,
                    session_token=current_token,
                    short_access_token=current_token,
                    short_expires=current_expires,
                ),
                timeout=max(6.0, float(timeout_seconds) + 3.0),
            )
            session_token = str((token_info or {}).get("session_token") or (token_info or {}).get("access_token") or "").strip()
            short_token = str((token_info or {}).get("short_access_token") or session_token or "").strip()
            expires = str((token_info or {}).get("expires") or (token_info or {}).get("short_expires") or "").strip() or None
            short_expires = str((token_info or {}).get("short_expires") or expires or "").strip() or None
            if not session_token or not short_token:
                raise FlowAccountUnavailableError("Flow账号会话不可用：auth/session 未返回可用 token", status_code=401)
            if not _veo_cached_access_still_valid(short_token, short_expires, margin_seconds=30):
                raise FlowAccountUnavailableError("Flow账号会话不可用或已过期，请手动打开对应窗口确认登录", status_code=401)

            reenable = self._flow_health_bool("reenable_on_probe_ok", True)
            was_disabled = not _db_bool(row.get("enabled"), default=True)
            await self.db.update_task_type_window(
                mapping_id=mid,
                enabled=True if reenable else None,
                sora_access_token=session_token,
                sora_access_expires=expires,
            )
            self._flow_health_last_ok[mid] = time.monotonic()
            if reenable and was_disabled:
                logger.info("flow session probe ok: mapping=%s source=%s re-enabled", mid, source)
            else:
                logger.info("flow session probe ok: mapping=%s source=%s", mid, source)
            return {"access_token": session_token, "expires": expires or ""}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if _flow_is_extension_unavailable_error(e) and not auto_trigger_connection:
                logger.info("flow session probe skipped; extension not connected: mapping=%s source=%s", mid, source)
                return None
            if _flow_is_account_unavailable_error(e):
                if disable_on_auth_failure:
                    await self._flow_disable_mapping_after_auth_failure(mid, str(e)[:200])
                if raise_on_auth_failure:
                    raise FlowAccountUnavailableError(
                        "Flow账号会话不可用或已登出，已跳过该账号并尝试切换其它账号",
                        status_code=401,
                    ) from e
                return None
            logger.warning("flow session probe skipped: mapping=%s source=%s err=%s", mid, source, e)
            return None

    async def _maybe_precheck_flow_account_for_task(
        self,
        picked: PickedWindow,
        *,
        project_page: str,
        task_id: str,
    ) -> None:
        if (picked.create_task_handler or "").strip() != "veo_workflow":
            return
        if not self._flow_health_bool("task_precheck_enabled", True):
            return
        mid = int(picked.mapping_id)
        ttl = self._flow_health_float("task_precheck_cache_seconds", 900.0, minimum=0.0)
        last_ok = float(self._flow_health_last_ok.get(mid) or 0.0)
        if ttl > 0 and last_ok > 0 and (time.monotonic() - last_ok) < ttl:
            return

        row = await self.db.get_task_type_window_context(mid)
        if not row:
            return
        row["default_target_url"] = project_page or row.get("default_target_url")
        timeout = self._flow_health_float("task_precheck_timeout_seconds", 30.0, minimum=5.0)
        token = await self._flow_probe_mapping_session(
            row,
            source=f"task_precheck:{task_id}",
            auto_trigger_connection=self._flow_health_bool("task_precheck_auto_trigger_connection", True),
            timeout_seconds=timeout,
            disable_on_auth_failure=True,
            raise_on_auth_failure=True,
        )
        if token:
            picked.sora_access_token = str(token.get("access_token") or "").strip() or picked.sora_access_token
            picked.sora_access_expires = str(token.get("expires") or "").strip() or picked.sora_access_expires


    async def _window_pool_reconcile_once(self) -> None:
        async with self._window_pool_reconcile_serial:
            await self._window_pool_reconcile_once_impl()

    async def _window_pool_reconcile_once_impl(self) -> None:
        try:
            all_types = await self.db.list_task_types()
        except Exception as e:
            logger.warning("window_pool list_task_types: %s", e)
            return
        if self._window_pool_stop.is_set():
            return

        new_targets: dict[str, set[int]] = {}
        # 任务类型仍存在、但被禁用或关闭了窗口池时，只应从窗口池管理集合中移除，
        # 不能主动关闭已经由窗口池/用户打开的指纹浏览器窗口。
        #
        # 之前这里把这类 code 直接从 new_targets 里略过，后面的 diff 逻辑会把
        # prev[code] 全部视为「需要关闭」，导致后台保存“关闭窗口池”后整批窗口
        # 被 _window_pool_close_mapping 调度 idle close。
        inactive_existing_codes: set[str] = set()

        for t in all_types:
            if self._window_pool_stop.is_set():
                return
            code = (t.code or "").strip()
            if not code:
                continue
            if not t.enabled or not bool(getattr(t, "window_pool_enabled", False)):
                inactive_existing_codes.add(code)
                continue
            handler = (t.create_task_handler or "").strip()
            credit_threthold = 1;
            if handler in ("veo_workflow", "grok_workflow", "gpt_workflow"):
                hi = await self.db.task_type_has_mapping_remaining_quota_above(code, 30)
                floor = 30 if hi else 10
            else:
                floor,credit_threthold = _remaining_quota_exclusive_floor_for_pick(code, None)
            try:
                ids = await self.db.list_window_pool_target_mapping_ids(
                    code, self._browser_pool_limit, floor,credit_threthold
                )
            except Exception as e:
                logger.warning("window_pool targets %s: %s", code, e)
                continue
            new_targets[code] = {int(x) for x in ids}

        async with self._window_pool_lock:
            prev = {k: set(v) for k, v in self._window_pool_targets.items()}
            self._window_pool_targets = {k: set(v) for k, v in new_targets.items()}

        to_close: list[int] = []
        for code, old_set in prev.items():
            if code not in new_targets:
                if code in inactive_existing_codes:
                    logger.info(
                        "window_pool disabled for task_type=%s; detach %d managed windows without closing",
                        code,
                        len(old_set),
                    )
                    continue
                to_close.extend(old_set)
            else:
                to_close.extend(old_set - new_targets[code])
        for mid in to_close:
            if self._window_pool_stop.is_set():
                return
            await self._window_pool_close_mapping(mid)
            await asyncio.sleep(0)

        to_open: list[tuple[str, int]] = []
        for code, new_set in new_targets.items():
            old_set = prev.get(code, set())
            for mid in new_set - old_set:
                to_open.append((code, mid))
        for code, mid in to_open:
            if self._window_pool_stop.is_set():
                return
            ok = await self._window_pool_open_mapping(mid)
            if not ok:
                async with self._window_pool_lock:
                    s = self._window_pool_targets.get(code)
                    if s is not None:
                        s.discard(mid)
                logger.warning(
                    "window_pool open mapping=%s failed; keep mapping enabled", mid
                )
            await asyncio.sleep(0)

    async def _window_pool_open_mapping(self, mapping_id: int) -> bool:
        if self._window_pool_stop.is_set():
            return True
        ctx = await self.db.get_task_type_window_context(mapping_id)
        if not ctx:
            return True
        handler = (ctx.get("create_task_handler") or "").strip()
        base_url = str(ctx.get("lan_addr") or "").strip()
        window_key = str(ctx.get("window_key") or "").strip()
        if not base_url or not window_key:
            return True
        vendor = str(ctx.get("vendor") or "generic")
        access_key = ctx.get("access_key")
        space_id = str(ctx.get("space_id") or "")
        headless = bool(ctx.get("headless"))
        pure_mode = _effective_browser_pure_mode_from_context(ctx)
        target_url = (str(ctx.get("default_target_url") or "").strip() or None)

        try:
            async with acquire_browser_open_slot(base_url):
                if handler == "veo_workflow":
                    picked_pid = await self.db.get_random_veo_flow_project_id(mapping_id)
                    tu = target_url or "https://labs.google/fx"
                    if picked_pid is not None:
                        tu = f"https://labs.google/fx/tools/flow/project/{picked_pid}"

                    sess = get_or_create_veo_session(
                        vendor=vendor,
                        base_url=base_url,
                        access_key=access_key,
                        space_id=space_id,
                        window_key=window_key,
                    )
                    sess.browser_headless = headless
                    sess.browser_pure_mode = pure_mode
                    sess.idle_close_disabled = True
                    sess._cancel_idle_close()

                    try:
                        # VEO 窗口池只打开/唤起目标窗口，不连接 CDP，降低 Playwright 暴露面。
                        await sess.pw_ctx.open_fingerprint_window_only(
                            args=[*sess.browser_open_args, tu],
                            force_open=sess.browser_force_open,
                            headless=headless,
                            pure_mode=pure_mode,
                        )
                        await asyncio.sleep(3.0)
                    except Exception as e:
                        logger.warning("window_pool open VEO mapping=%s by open-only failed: %s", mapping_id, e)
                        return False

                    token_info = None
                    try:
                        token_info = await veo_fetch_access_tokens_via_extension(
                            sess=sess,
                            target_url=tu,
                            space_id=space_id,
                            window_key=window_key,
                            connect_wait_seconds=8.0,
                            token_timeout_seconds=45.0,
                            log_file=sess._log_file,
                        )
                    except Exception as e:
                        logger.warning("window_pool VEO extension token mapping=%s failed: %s", mapping_id, e)
                        try:
                            await self.db.update_task_type_window(mapping_id=mapping_id, enabled=False)
                        except Exception:
                            pass
                        return True

                    long_session_token = str((token_info or {}).get("session_token") or (token_info or {}).get("access_token") or "").strip()
                    if long_session_token:
                        await self.db.update_task_type_window(
                            mapping_id=mapping_id,
                            sora_access_token=long_session_token,
                            sora_access_expires=str((token_info or {}).get("expires") or "").strip() or None,
                        )
                    return True
                elif handler == "grok_workflow":
                    tu = target_url or DEFAULT_GROK_TARGET
                    gs = get_or_create_grok_session(
                        vendor=vendor,
                        base_url=base_url,
                        access_key=access_key,
                        space_id=space_id,
                        window_key=window_key,
                    )
                    gs.browser_headless = headless
                    gs.browser_pure_mode = pure_mode
                    gs.idle_close_disabled = True
                    gs._cancel_idle_close()
                    await gs.ensure_open(
                        args=gs.browser_open_args,
                        force_open=gs.browser_force_open,
                        headless=headless,
                        pure_mode=pure_mode,
                    )
                    await gs._bring_target_page_to_front(refresh_target=False, drafts_url=tu)
                    try:
                        await gs.disconnect_playwright_under_bring_lock()
                    except Exception:
                        pass
                    return True
                elif handler == "dreamina_workflow":
                    tu = target_url or DEFAULT_DREAMINA_TARGET
                    ds = get_or_create_dreamina_session(
                        vendor=vendor,
                        base_url=base_url,
                        access_key=access_key,
                        space_id=space_id,
                        window_key=window_key,
                    )
                    ds.browser_headless = headless
                    ds.browser_pure_mode = pure_mode
                    ds.idle_close_disabled = True
                    ds._cancel_idle_close()
                    await ds.ensure_open(
                        args=ds.browser_open_args,
                        force_open=ds.browser_force_open,
                        headless=headless,
                        pure_mode=pure_mode,
                    )
                    await ds._bring_target_page_to_front(refresh_target=False, drafts_url=tu)
                    try:
                        await ds.disconnect_playwright_under_bring_lock()
                    except Exception:
                        pass
                    return True
                elif handler == "gpt_workflow":
                    from .gpt_task_executor import DEFAULT_GPT_TARGET, gpt_fetch_access_token_in_window  # type: ignore

                    tu = target_url or DEFAULT_GPT_TARGET
                    sess = get_or_create_veo_session(
                        vendor=vendor,
                        base_url=base_url,
                        access_key=access_key,
                        space_id=space_id,
                        window_key=window_key,
                    )
                    sess.browser_headless = headless
                    sess.browser_pure_mode = pure_mode
                    sess.idle_close_disabled = True
                    sess._cancel_idle_close()
                    await sess.ensure_open(args=sess.browser_open_args, force_open=False, headless=headless, pure_mode=pure_mode)
                    await sess._bring_target_page_to_front(refresh_target=False, drafts_url=tu)
                    try:
                        tok_info = await gpt_fetch_access_token_in_window(
                            browser_vendor=vendor,
                            browser_base_url=base_url,
                            browser_access_key=access_key,
                            space_id=space_id,
                            window_key=window_key,
                            target_url=tu,
                            headless=headless,
                            pure_mode=pure_mode,
                            timeout_seconds=45.0,
                        )
                        access_token = str((tok_info or {}).get("access_token") or "").strip()
                        if access_token:
                            await self.db.update_task_type_window(
                                mapping_id=mapping_id,
                                sora_access_token=access_token,
                                sora_access_expires=str((tok_info or {}).get("expires") or "").strip() or None,
                            )
                    except Exception as e:
                        logger.warning("window_pool gpt token refresh mapping=%s failed: %s", mapping_id, e)
                    try:
                        await sess.disconnect_playwright_under_bring_lock()
                    except Exception:
                        pass
                    return True
                elif handler in ("sora_gen_video", "sora_wm_remove", "sora_plus_register"):
                    tu = target_url or "https://sora.chatgpt.com/drafts"
                    sess = get_or_create_sora_session(
                        vendor=vendor,
                        base_url=base_url,
                        access_key=access_key,
                        space_id=space_id,
                        window_key=window_key,
                    )
                    tok = str(ctx.get("sora_access_token") or "").strip()
                    if tok:
                        sess.set_access_token(tok, str(ctx.get("sora_access_expires") or "").strip() or None)
                    sess.browser_headless = headless
                    sess.browser_pure_mode = pure_mode
                    sess.idle_close_disabled = True
                    sess._cancel_idle_close()
                    await sess.ensure_open(
                        args=sess.browser_open_args,
                        force_open=sess.browser_force_open,
                        headless=headless,
                        pure_mode=pure_mode,
                    )
                    await sess._bring_sora_drafts_to_front(refresh_target=False, drafts_url=tu)
                    try:
                        await sess.disconnect_playwright_under_bring_lock()
                    except Exception:
                        pass
                    return True
                else:
                    tu = target_url
                    if not tu:
                        return True
                    pw = get_or_create_playwright_ctx(
                        vendor=vendor,
                        base_url=base_url,
                        access_key=access_key,
                        space_id=space_id,
                        window_key=window_key,
                    )
                    await pw.ensure_open(
                        args=[],
                        force_open=False,
                        headless=headless,
                        require_page=False,
                        pure_mode=pure_mode,
                    )
                    try:
                        async with pw.driver_lock:
                            if pw.context is None:
                                return True
                            if pw.page is None:
                                try:
                                    pages = list(getattr(pw.context, "pages", []) or [])
                                except Exception:
                                    pages = []
                                pw.page = pages[0] if pages else await pw.context.new_page()
                            try:
                                await pw.page.goto(tu, wait_until="domcontentloaded", timeout=60_000)
                            except Exception:
                                pass
                    finally:
                        try:
                            await pw.disconnect_playwright_only_under_driver_lock()
                        except Exception:
                            pass
                    return True
        except Exception as e:
            logger.warning("window_pool open mapping=%s err=%s", mapping_id, e)
            return False

    async def _window_pool_close_mapping(self, mapping_id: int) -> None:
        ctx = await self.db.get_task_type_window_context(mapping_id)
        if not ctx:
            return
        handler = (ctx.get("create_task_handler") or "").strip()
        base_url = str(ctx.get("lan_addr") or "").strip()
        window_key = str(ctx.get("window_key") or "").strip()
        if not base_url or not window_key:
            return
        vendor = str(ctx.get("vendor") or "generic")
        access_key = ctx.get("access_key")
        space_id = str(ctx.get("space_id") or "")
        try:
            if handler == "veo_workflow":
                sess = get_or_create_veo_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                sess.idle_close_disabled = False
                sess._schedule_idle_close()
            elif handler == "grok_workflow":
                gs = get_or_create_grok_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                gs.idle_close_disabled = False
                gs._schedule_idle_close()
            elif handler == "dreamina_workflow":
                ds = get_or_create_dreamina_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                ds.idle_close_disabled = False
                ds._schedule_idle_close()
            elif handler in ("sora_gen_video", "sora_wm_remove", "sora_plus_register"):
                sess = get_or_create_sora_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                sess.idle_close_disabled = False
                sess._schedule_idle_close()
            else:
                pw = get_or_create_playwright_ctx(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                await pw.close_and_drop()
        except Exception as e:
            logger.debug("window_pool close mapping=%s err=%s", mapping_id, e)

    async def _window_pool_drop_sessions_for_mapping(self, mapping_id: int) -> None:
        """CF 仍失败时丢弃会话，由下次 reconcile 重新打开。"""
        ctx = await self.db.get_task_type_window_context(mapping_id)
        if not ctx:
            return
        handler = (ctx.get("create_task_handler") or "").strip()
        base_url = str(ctx.get("lan_addr") or "").strip()
        window_key = str(ctx.get("window_key") or "").strip()
        if not base_url or not window_key:
            return
        vendor = str(ctx.get("vendor") or "generic")
        access_key = ctx.get("access_key")
        space_id = str(ctx.get("space_id") or "")
        try:
            if handler == "veo_workflow":
                sess = get_or_create_veo_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                await sess.close_and_drop()
            elif handler == "grok_workflow":
                gs = get_or_create_grok_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                await gs.close_and_drop()
            elif handler == "dreamina_workflow":
                ds = get_or_create_dreamina_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                await ds.close_and_drop()
            elif handler in ("sora_gen_video", "sora_wm_remove", "sora_plus_register"):
                sess = get_or_create_sora_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                await sess.close_and_drop()
            else:
                pw = get_or_create_playwright_ctx(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                await pw.close_and_drop()
        except Exception as e:
            logger.debug("window_pool drop mapping=%s err=%s", mapping_id, e)

    async def _window_pool_cloudflare_tick(self) -> None:
        async with self._window_pool_lock:
            snapshot = {k: set(v) for k, v in self._window_pool_targets.items()}
        for _code, mids in snapshot.items():
            for mid in mids:
                if self._window_pool_stop.is_set():
                    return
                try:
                    await self._window_pool_cloudflare_one(mid)
                except Exception as e:
                    logger.warning("window_pool cf mapping=%s err=%s", mid, e)

    async def _window_pool_cloudflare_one(self, mapping_id: int) -> None:
        ctx = await self.db.get_task_type_window_context(mapping_id)
        if not ctx:
            return
        handler = (ctx.get("create_task_handler") or "").strip()
        base_url = str(ctx.get("lan_addr") or "").strip()
        window_key = str(ctx.get("window_key") or "").strip()
        if not base_url or not window_key:
            return
        vendor = str(ctx.get("vendor") or "generic")
        access_key = ctx.get("access_key")
        space_id = str(ctx.get("space_id") or "")
        target_url = (str(ctx.get("default_target_url") or "").strip() or None)

        try:
            if handler == "veo_workflow":
                picked_pid = await self.db.get_random_veo_flow_project_id(mapping_id)
                tu = target_url or "https://labs.google/fx"
                if picked_pid is not None:
                    tu = f"https://labs.google/fx/tools/flow/project/{picked_pid}"
                sess = get_or_create_veo_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                if not sess.idle_close_disabled:
                    return
                wpk = int(ctx.get("window_pk") or 0)
                try:
                    gl_ms = int(float(ctx.get("task_timeout_seconds") or 120) * 1000)
                except Exception:
                    gl_ms = 120_000
                gl_ms = max(45_000, min(gl_ms, 240_000))
                page = getattr(sess.pw_ctx, "page", None)
                await sess.raise_if_cloudflare_page_nonpenalized(
                    page,
                    stage="window_pool",
                    target_url=tu,
                    window_pool_google_relogin_db=self.db if wpk > 0 else None,
                    window_pool_google_relogin_window_pk=wpk if wpk > 0 else None,
                    window_pool_google_relogin_timeout_ms=gl_ms,
                )
                try:
                    await sess.disconnect_playwright_under_bring_lock()
                except Exception:
                    pass
            elif handler == "grok_workflow":
                tu = target_url or DEFAULT_GROK_TARGET
                gs = get_or_create_grok_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                if not gs.idle_close_disabled:
                    return
                page = getattr(gs.pw_ctx, "page", None)
                await window_pool_guard_unknown_handler_page(page, stage="window_pool", target_url=tu)
                try:
                    await gs.disconnect_playwright_under_bring_lock()
                except Exception:
                    pass
            elif handler == "dreamina_workflow":
                tu = target_url or DEFAULT_DREAMINA_TARGET
                ds = get_or_create_dreamina_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                if not ds.idle_close_disabled:
                    return
                page = getattr(ds.pw_ctx, "page", None)
                await window_pool_guard_unknown_handler_page(page, stage="window_pool", target_url=tu)
                try:
                    await ds.disconnect_playwright_under_bring_lock()
                except Exception:
                    pass
            elif handler in ("sora_gen_video", "sora_wm_remove", "sora_plus_register"):
                tu = target_url or "https://sora.chatgpt.com/drafts"
                sess = get_or_create_sora_session(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                if not sess.idle_close_disabled:
                    return
                page = getattr(sess.pw_ctx, "page", None)
                await sess._raise_if_cloudflare_page_nonpenalized(
                    page, stage="window_pool", drafts_url=tu
                )
                try:
                    await sess.disconnect_playwright_under_bring_lock()
                except Exception:
                    pass
            else:
                tu = target_url
                if not tu:
                    return
                pw = get_or_create_playwright_ctx(
                    vendor=vendor,
                    base_url=base_url,
                    access_key=access_key,
                    space_id=space_id,
                    window_key=window_key,
                )
                page = getattr(pw, "page", None)
                await window_pool_guard_unknown_handler_page(
                    page, stage="window_pool", target_url=tu
                )
                try:
                    await pw.disconnect_playwright_only_under_driver_lock()
                except Exception:
                    pass
        except NonPenalizedTaskError:
            logger.warning(
                "window_pool cloudflare persists, reset session mapping_id=%s", mapping_id
            )
            await self._window_pool_drop_sessions_for_mapping(mapping_id)
        except Exception:
            pass

    def _truncate_text(self, s: str, max_chars: int, *, label: str) -> str:
        s = str(s or "")
        max_chars = int(max_chars or 0)
        if max_chars <= 0:
            return ""
        if len(s) <= max_chars:
            return s
        suffix = f"…({label} truncated, orig_chars={len(s)}, max_chars={max_chars})"
        keep = max(0, max_chars - len(suffix))
        if keep <= 0:
            return suffix[:max_chars]
        return s[:keep] + suffix

    @staticmethod
    def _task_created_at_for_sql(v: Any) -> Optional[str]:
        """将任务行的 created_at 转为 SQLite 可接受的本地时间字符串（用于 INSERT 覆盖）。"""
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        s = str(v).strip()
        return s or None

    def _payload_to_prompt_text(self, payload: Dict[str, Any]) -> str:
        """把 payload 序列化成可落库的 prompt 文本（尽量是 JSON，且控制长度）。"""

        def _dumps(obj: Any) -> str:
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)

        total_max = max(64, int(self._prompt_max_chars or 0))
        prompt_max = max(0, int(self._payload_prompt_max_chars or 0))

        base_payload: Dict[str, Any]
        if isinstance(payload, dict):
            base_payload = dict(payload or {})
        else:
            base_payload = {"payload": payload}

        # 先对 payload["prompt"] 做“字段级”限长（<=1000）
        orig_prompt = str(base_payload.get("prompt") or "")
        if "prompt" in base_payload or orig_prompt:
            base_payload["prompt"] = self._truncate_text(orig_prompt, prompt_max, label="prompt")

        try:
            s = _dumps(base_payload)
        except Exception:
            # 极端兜底：保证永远能落库
            s = self._truncate_text(str(payload or {}), total_max, label="payload")

        if len(s) <= total_max:
            return s

        # 若整段 JSON 仍超长：降级为最小可查看 JSON（保证总长度 <= 2000 且尽量保持可解析）
        minimal_flag_key = "_payload_trimmed"
        prompt_text = str(base_payload.get("prompt") or "")

        def _minimal_json(prompt_val: str) -> str:
            return _dumps({"prompt": prompt_val, minimal_flag_key: True})

        # 二分裁剪 prompt（在不超过字段级上限的前提下），直到 minimal JSON 满足 total_max
        hi = len(prompt_text)
        lo = 0
        best = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            cand_prompt = self._truncate_text(prompt_text, mid, label="prompt_db")
            cand = _minimal_json(cand_prompt)
            if len(cand) <= total_max:
                best = cand
                lo = mid + 1
            else:
                hi = mid - 1

        if best:
            return best

        # 最后兜底：即使 prompt 为空也要可落库
        empty = _minimal_json("")
        if len(empty) <= total_max:
            return empty
        return empty[:total_max]

    async def submit_task(
        self,
        task_type_code: str,
        payload: Dict[str, Any],
        *,
        mapping_id: Optional[int] = None,
        window_pk: Optional[int] = None,
    ) -> str:
        task_type_code = (task_type_code or "").strip()
        if not task_type_code:
            raise ValueError("task_type_code 不能为空")
        payload = payload or {}

        # Sora 角色创建分支：payload.generation_id + payload.head_url
        # 需求：若能走该分支，则优先复用 generation_id 对应历史任务的窗口
        payload_generation_id = str(payload.get("generation_id") or "").strip() or None
        payload_head_url = str(payload.get("head_url") or "").strip() or None

        picked: Optional[PickedWindow] = None
        _is_dedicated_window = False
        # 指定窗口优先级：mapping_id > window_pk > 默认自动挑选
        if mapping_id is not None:
            picked = await self._pick_window_by_mapping(
                task_type_code, mapping_id=int(mapping_id), payload=payload
            )
        elif window_pk is not None:
            picked = await self._pick_window_by_window_pk(
                task_type_code, window_pk=int(window_pk), payload=payload
            )
        else:
            # 若 payload 满足“基于 generation_id 创建角色”分支，则尝试按 generation_id 绑定窗口
            if payload_generation_id and payload_head_url:
                try:
                    win_pk = await self.db.get_task_window_pk_by_generation_id(payload_generation_id)
                except Exception:
                    win_pk = None

                if win_pk is None:
                    raise RuntimeError("该视频不属于我们的账号，请先生成视频再使用返回的generation_id创建角色")

                # 并发控制：专用窗口任务受 browser_open_concurrency 限制
                await self._refresh_queue_config()
                _over_limit = False
                async with self._dedicated_window_lock:
                    if self._dedicated_window_inflight >= self._browser_open_concurrency:
                        _over_limit = True
                    else:
                        self._dedicated_window_inflight += 1
                if _over_limit:
                    return await self._enqueue_task(
                        task_type_code, payload,
                        required_window_pk=win_pk,
                        is_dedicated_window=True,
                    )
                _is_dedicated_window = True

                picked = await self._pick_window_by_window_pk(task_type_code, win_pk, payload=payload)
                if not picked:
                    async with self._dedicated_window_lock:
                        self._dedicated_window_inflight = max(0, self._dedicated_window_inflight - 1)
                    raise RuntimeError("该视频不属于我们的账号，请先生成视频再使用返回的generation_id创建角色")
            if not picked:
                picked = await self._pick_window(task_type_code, payload=payload)
        if not picked:
            if mapping_id is not None or window_pk is not None:
                raise RuntimeError("指定窗口不可用：请确认该窗口已绑定该任务类型、未删除、已启用")
            return await self._enqueue_task(task_type_code, payload)

        task_id = uuid.uuid4().hex
        try:
            # 把 payload 序列化落库到 prompt 里，便于管理台查看/检索（控制长度，避免字段溢出）
            prompt_text = self._payload_to_prompt_text(payload)
            await self.db.create_task(
                Task(
                    task_id=task_id,
                    task_type_code=task_type_code,
                    generation_id=None,
                    status="queued",
                    progress=0,
                    prompt=prompt_text,
                    image_path=None,
                    window_pk=picked.window_pk,
                    window_ip=picked.window_ip,
                )
            )
            self._task_payloads[task_id] = payload
            asyncio.create_task(
                self._run_task(
                    task_id,
                    picked,
                    _is_dedicated_window=_is_dedicated_window,
                    _allow_account_switch=(
                        mapping_id is None and window_pk is None and not _is_dedicated_window
                    ),
                )
            )
            return task_id
        except Exception:
            if _is_dedicated_window:
                async with self._dedicated_window_lock:
                    self._dedicated_window_inflight = max(0, self._dedicated_window_inflight - 1)
            # 兜底：若创建任务失败，释放预占槽位避免泄漏，并撤销挑选时标记的 window_status=1
            try:
                await self.db.release_mapping_slot(picked.mapping_id)
            except Exception:
                pass
            raise

    async def _consume_quota_after_window_pick(
        self, picked: PickedWindow, payload: Optional[Dict[str, Any]] = None
    ) -> None:
        """挑选窗口成功后按 handler 预扣 mapping 额度（与真实消耗对齐）。"""
        handler = (picked.create_task_handler or "").strip()
        if handler == "sora_gen_video":
            try:
                await self.db.consume_mapping_quota(picked.mapping_id, amount=2)
            except Exception:
                pass
        elif handler == "veo_workflow":
            try:
                if _veo_resolve_n_frames(payload or {}) > 1:
                    await self.db.consume_mapping_quota(picked.mapping_id, amount=20)
            except Exception:
                pass
        elif handler == "grok_workflow":
            try:
                n = grok_ref_url_count(payload or {})
                if n > 1:
                    await self.db.consume_mapping_quota(picked.mapping_id, amount=20)
                elif n == 1:
                    await self.db.consume_mapping_quota(picked.mapping_id, amount=10)
            except Exception:
                pass
        elif handler == "dreamina_workflow":
            try:
                if not _is_no_submit_payload(payload):
                    await self.db.consume_mapping_quota(
                        picked.mapping_id,
                        amount=_dreamina_estimated_credit_cost(payload),
                    )
            except Exception:
                pass

    async def _finalize_picked_window(
        self, r: Dict[str, Any], payload: Optional[Dict[str, Any]] = None
    ) -> Optional[PickedWindow]:
        """由 reserve / pick 返回的行构造 PickedWindow，并处理 window_key 缺失与预扣额度。"""
        mid = int(r["id"])
        picked = PickedWindow(
            mapping_id=mid,
            window_pk=int(r["window_pk"]),
            window_key=str(r.get("window_key") or "").strip(),
            task_code=str(r["task_code"]),
            task_concurrency=_effective_task_concurrency_from_context(r),
            threshold=int(r.get("continuous_error_threshold") or 3),
            close_window_threshold=int(r.get("continuous_error_close_window_threshold") or 3),
            timeout_seconds=int(r.get("timeout_seconds") or 600),
            create_task_handler=(str(r.get("create_task_handler") or "").strip() or None),
            window_ip=(str(r.get("window_ip") or "").strip() or None),
            browser_vendor=str(r.get("vendor") or "generic"),
            browser_base_url=str(r.get("lan_addr") or ""),
            browser_access_key=r.get("access_key"),
            space_id=str(r.get("space_id") or ""),
            sora_access_token=(str(r.get("sora_access_token") or "").strip() or None),
            sora_access_expires=(str(r.get("sora_access_expires") or "").strip() or None),
            default_target_url=(str(r.get("default_target_url") or "").strip() or None),
            headless=bool(r.get("headless")),
            pure_mode=_effective_browser_pure_mode_from_context(r),
            error_retry_count=int(r.get("error_retry_count") or 0),
            project_id=str(r.get("current_project_id") or 0)
        )
        if not picked.window_key:
            try:
                await self.db.release_mapping_slot(mid)
            except Exception:
                pass
            return None
        if (picked.create_task_handler or "").strip() == "veo_workflow":
            try:
                client = await get_extension_client(picked.space_id, picked.window_key)
                if client is not None and len(client.pending) > 0:
                    logger.info(
                        "skip VEO window with extension pending tasks: mapping=%s window=%s pending=%d",
                        picked.mapping_id,
                        picked.window_pk,
                        len(client.pending),
                    )
                    await self.db.release_mapping_slot(mid)
                    return None
            except Exception:
                pass
        await self._consume_quota_after_window_pick(picked, payload)
        return picked

    async def _window_pool_pin_selected_mapping(self, task_type_code: str, mapping_id: int) -> None:
        """显式选窗成功后钉入窗口池集合（与 DB 推导目标合并），便于 reconcile / CF 统一管理。"""
        code = (task_type_code or "").strip()
        if not code:
            return
        try:
            tt = await self.db.get_task_type_by_code(code)
        except Exception:
            return
        if not tt or not bool(getattr(tt, "window_pool_enabled", False)):
            return
        mid = int(mapping_id)
        async with self._window_pool_lock:
            self._window_pool_targets.setdefault(code, set()).add(mid)

    async def _pick_window(self, task_type_code: str, payload: Optional[Dict[str, Any]] = None) -> Optional[PickedWindow]:
        """从 DB 候选中挑选窗口，并在 DB 中原子预占并发槽位。

        说明：
        - 预占由 DB 字段 inflight_slots 完成（支持多进程/多实例，避免超卖）
        - 预占成功同时将 windows.window_status 置 1，使单浏览器窗口池上限在打开指纹前即计数
        - 挑选排序由 DB 决定（consecutive_errors 最低优先，其次 remaining_quota 最少优先）
        - 若任务类型开启窗口池：仅从 `_window_pool_targets` 内由 DB 单事务 `pick_and_reserve_window_from_pool` 原子挑选（与全局 pick 相同：+60s error_cooldown_until，避免高并发下多任务盯上同一 mapping）；池为空或无可用则返回 None（不回退全局 pick）
        """
        floor,credit_threthold = _remaining_quota_exclusive_floor_for_pick(task_type_code, payload)
        try:
            tt = await self.db.get_task_type_by_code(task_type_code)
        except Exception:
            tt = None
        if tt and bool(getattr(tt, "window_pool_enabled", False)):
            async with self._window_pool_lock:
                pool_ids = list(self._window_pool_targets.get(task_type_code, set()))
            if not pool_ids:
                return None
            r = await self.db.pick_and_reserve_window_from_pool(
                task_type_code,
                pool_ids,
                remaining_quota_exclusive_floor=floor,
                credit_threthold=credit_threthold
            )
            if not r:
                return None
            return await self._finalize_picked_window(r, payload)

        r = await self.db.pick_and_reserve_window_for_task(
            task_type_code=task_type_code,
            browser_pool_limit=self._browser_pool_limit,
            remaining_quota_exclusive_floor=floor,
            credit_threthold=credit_threthold
        )
        if not r:
            return None
        return await self._finalize_picked_window(r, payload)

    async def _pick_window_by_mapping(
        self, task_type_code: str, mapping_id: int, payload: Optional[Dict[str, Any]] = None
    ) -> Optional[PickedWindow]:
        """Reserve an explicitly selected task_type_windows mapping."""
        r = await self.db.force_reserve_mapping_for_task(task_type_code=task_type_code, mapping_id=int(mapping_id))
        if not r:
            return None
        picked = await self._finalize_picked_window(r, payload)
        if not picked:
            return None
        await self._window_pool_pin_selected_mapping(task_type_code, picked.mapping_id)
        return picked

    async def _pick_window_by_window_pk(
        self, task_type_code: str, window_pk: int, payload: Optional[Dict[str, Any]] = None
    ) -> Optional[PickedWindow]:
        """Reserve an explicitly selected browser window."""
        r = await self.db.force_reserve_window_for_task(task_type_code=task_type_code, window_pk=int(window_pk))
        if not r:
            return None
        picked = await self._finalize_picked_window(r, payload)
        if not picked:
            return None
        await self._window_pool_pin_selected_mapping(task_type_code, picked.mapping_id)
        return picked

    # ---- 排队与调度 ----

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._dispatcher_task = asyncio.create_task(self._dispatcher_loop())

    async def _refresh_queue_config(self) -> None:
        now = time.monotonic()
        expire_at, _, _ = self._queue_config_cache
        if now < expire_at:
            return
        try:
            syscfg = await self.db.get_system_config()
            max_size = max(1, int(getattr(syscfg, "task_queue_max_size", 0) or 1000))
            timeout = max(10.0, float(getattr(syscfg, "task_queue_timeout_seconds", 0) or 300.0))
            browser_open_concurrency = max(1, int(getattr(syscfg, "browser_open_concurrency", 0) or 3))
        except Exception:
            max_size, timeout = self._queue_max_size, self._queue_timeout_seconds
            browser_open_concurrency = self._browser_open_concurrency
        self._queue_config_cache = (now + self._queue_config_ttl, max_size, timeout)
        self._queue_max_size = max_size
        self._queue_timeout_seconds = timeout
        self._browser_open_concurrency = browser_open_concurrency

    async def _enqueue_task(
        self,
        task_type_code: str,
        payload: Dict[str, Any],
        *,
        required_window_pk: Optional[int] = None,
        is_dedicated_window: bool = False,
        allow_account_switch: Optional[bool] = None,
    ) -> str:
        self._ensure_dispatcher()
        await self._refresh_queue_config()

        if len(self._pending_queue) >= self._queue_max_size:
            raise RuntimeError("任务队列已满，请稍后重试")

        task_id = uuid.uuid4().hex
        prompt_text = self._payload_to_prompt_text(payload)
        await self.db.create_task(
            Task(
                task_id=task_id,
                task_type_code=task_type_code,
                generation_id=None,
                status="queued",
                progress=0,
                prompt=prompt_text,
                image_path=None,
                window_pk=None,
                window_ip=None,
            )
        )
        self._task_payloads[task_id] = payload

        async with self._queue_lock:
            self._pending_queue.append(
                QueuedTask(
                    task_id=task_id,
                    task_type_code=task_type_code,
                    payload=payload,
                    enqueued_at=time.monotonic(),
                    required_window_pk=required_window_pk,
                    is_dedicated_window=is_dedicated_window,
                    allow_account_switch=(
                        bool(allow_account_switch)
                        if allow_account_switch is not None
                        else (required_window_pk is None and not is_dedicated_window)
                    ),
                )
            )
        self._dispatch_event.set()
        logger.info(
            "task queued: %s type=%s queue_size=%d",
            task_id,
            task_type_code,
            len(self._pending_queue),
        )
        return task_id

    async def _dispatcher_loop(self) -> None:
        while True:
            try:
                try:
                    await asyncio.wait_for(
                        self._dispatch_event.wait(),
                        timeout=self._dispatch_poll_interval,
                    )
                except asyncio.TimeoutError:
                    pass
                self._dispatch_event.clear()

                if not self._pending_queue:
                    continue

                await self._refresh_queue_config()
                await self._try_dispatch_all()
            except Exception as e:
                logger.exception("dispatcher_loop error: %s", e)
                await asyncio.sleep(1.0)

    async def _try_dispatch_all(self) -> None:
        async with self._queue_lock:
            still_pending: deque[QueuedTask] = deque()
            exhausted_type_floors: dict[str, int] = {}
            now = time.monotonic()

            while self._pending_queue:
                item = self._pending_queue.popleft()

                if now - item.enqueued_at > self._queue_timeout_seconds:
                    try:
                        await self.db.update_task(
                            item.task_id,
                            status="failed",
                            error_message="排队超时，请稍后重试",
                            set_completed=True,
                        )
                    except Exception:
                        pass
                    self._task_payloads.pop(item.task_id, None)
                    logger.warning("task queue timeout: %s", item.task_id)
                    continue

                queue_floor, _queue_credit_threshold = _remaining_quota_exclusive_floor_for_pick(
                    item.task_type_code, item.payload
                )
                if item.required_window_pk is None:
                    exhausted_floor = exhausted_type_floors.get(item.task_type_code)
                    if exhausted_floor is not None and queue_floor >= exhausted_floor:
                        still_pending.append(item)
                        continue

                # 专用窗口任务：先检查并发限制
                _dedicated_acquired = False
                if item.is_dedicated_window:
                    async with self._dedicated_window_lock:
                        if self._dedicated_window_inflight >= self._browser_open_concurrency:
                            still_pending.append(item)
                            continue
                        self._dedicated_window_inflight += 1
                        _dedicated_acquired = True

                if item.required_window_pk is not None:
                    picked = await self._pick_window_by_window_pk(
                        item.task_type_code, item.required_window_pk, payload=item.payload
                    )
                else:
                    picked = await self._pick_window(item.task_type_code, payload=item.payload)
                if picked:
                    try:
                        await self.db.update_task(
                            item.task_id,
                            window_pk=picked.window_pk,
                            window_ip=picked.window_ip,
                        )
                    except Exception:
                        pass
                    asyncio.create_task(self._run_task(
                        item.task_id, picked,
                        _retry_attempt=item.retry_attempt,
                        _is_dedicated_window=item.is_dedicated_window,
                        _allow_account_switch=item.allow_account_switch,
                    ))
                    logger.info(
                        "task dispatched from queue: %s type=%s window=%s retry=%d (waited %.1fs)",
                        item.task_id,
                        item.task_type_code,
                        picked.window_pk,
                        item.retry_attempt,
                        now - item.enqueued_at,
                    )
                else:
                    if _dedicated_acquired:
                        async with self._dedicated_window_lock:
                            self._dedicated_window_inflight = max(0, self._dedicated_window_inflight - 1)
                    if item.required_window_pk is None:
                        prev_floor = exhausted_type_floors.get(item.task_type_code)
                        if prev_floor is None or queue_floor < prev_floor:
                            exhausted_type_floors[item.task_type_code] = queue_floor
                    still_pending.append(item)

            self._pending_queue = still_pending

    async def get_queue_info(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "queue_size": len(self._pending_queue),
            "queue_max_size": self._queue_max_size,
            "queue_timeout_seconds": self._queue_timeout_seconds,
            "dispatcher_running": self._dispatcher_task is not None and not self._dispatcher_task.done(),
        }
        try:
            info["task_stats"] = await self.db.task_status_summary()
        except Exception:
            info["task_stats"] = {}
        return info

    async def _run_task(
        self,
        task_id: str,
        picked: PickedWindow,
        *,
        _retry_attempt: int = 0,
        _is_dedicated_window: bool = False,
        _allow_account_switch: bool = True,
    ) -> None:
        _need_retry = False
        _retry_error_msg = ""
        try:
            await self.db.update_task(task_id, status="running", progress=0, set_started=True)
            logger.info("task started: %s type=%s window=%s mapping=%s attempt=%d", task_id, picked.task_code, picked.window_pk, picked.mapping_id, _retry_attempt)

            payload = self._task_payloads.get(task_id) or {}
            if not isinstance(payload, dict):
                payload = {}
            if picked.create_task_handler == "leonardo_workflow" and not str(
                payload.get("_leonardo_generation_id") or ""
            ).strip():
                try:
                    task_row = await self.db.get_task(task_id)
                    persisted_generation_id = str(getattr(task_row, "generation_id", None) or "").strip()
                    if persisted_generation_id:
                        payload["_leonardo_generation_id"] = persisted_generation_id
                        self._task_payloads[task_id] = payload
                except Exception:
                    pass

            _last_saved_progress = -1
            _last_saved_stage = ""
            _last_saved_generation_id = ""
            _last_progress_payload: Dict[str, Any] = {}

            async def progress_cb(p: int, _payload: Optional[Dict[str, Any]]):
                nonlocal _last_saved_progress, _last_saved_stage, _last_saved_generation_id, _last_progress_payload
                pi = int(p)
                payload_info: Dict[str, Any] = {}
                if isinstance(_payload, dict) and _payload:
                    try:
                        payload_info = json.loads(json.dumps(_payload, ensure_ascii=False, default=str))
                    except Exception:
                        payload_info = {"raw": str(_payload)[:1000]}
                    _last_progress_payload = payload_info
                stage = str(payload_info.get("stage") or "").strip()
                progress_due = pi != _last_saved_progress
                # 只在关键节点或变化 >=5 时写库，大幅减少写频率
                if progress_due and pi not in (0, 100) and abs(pi - _last_saved_progress) < 5:
                    progress_due = False
                stage_due = (
                    picked.create_task_handler == "veo_workflow"
                    and bool(stage)
                    and stage != _last_saved_stage
                )
                runtime_progress_due = (
                    picked.create_task_handler in {"veo_workflow", "leonardo_workflow"}
                    and bool(payload_info)
                    and (stage_due or progress_due)
                )
                leonardo_generation_id = ""
                zarklab_run_id = ""
                if picked.create_task_handler == "leonardo_workflow":
                    leonardo_generation_id = str(payload_info.get("generation_id") or "").strip()
                    if leonardo_generation_id:
                        payload["_leonardo_generation_id"] = leonardo_generation_id
                        resume_meta = {
                            str(k): v
                            for k, v in payload_info.items()
                            if str(k) not in {"stage", "generation_id", "status", "video_url_found", "attempt"}
                        }
                        if resume_meta:
                            payload["_leonardo_resume_meta"] = resume_meta
                        self._task_payloads[task_id] = payload
                elif picked.create_task_handler == "zarklab_workflow":
                    zarklab_run_id = str(payload_info.get("run_id") or "").strip()
                    if zarklab_run_id:
                        payload["_zark_run_id"] = zarklab_run_id
                        self._task_payloads[task_id] = payload
                provider_generation_id = leonardo_generation_id or zarklab_run_id
                generation_due = bool(
                    provider_generation_id and provider_generation_id != _last_saved_generation_id
                )
                if not progress_due and not runtime_progress_due and not generation_due:
                    return
                try:
                    update_kwargs: Dict[str, Any] = {}
                    if progress_due:
                        update_kwargs["progress"] = pi
                    if runtime_progress_due:
                        update_kwargs["result"] = {
                            "runtime_progress": {
                                "progress": pi,
                                "stage": stage,
                                "data": payload_info,
                                "updated_at": datetime.now().isoformat(timespec="seconds"),
                            }
                        }
                    if generation_due:
                        update_kwargs["generation_id"] = provider_generation_id
                    if update_kwargs:
                        await self.db.update_task(task_id, **update_kwargs)
                    if progress_due:
                        _last_saved_progress = pi
                    if stage_due:
                        _last_saved_stage = stage
                    if generation_due:
                        _last_saved_generation_id = provider_generation_id
                except Exception:
                    pass

            prompt = str(payload.get("prompt") or "").strip()
            target_url = str(payload.get("sora_url") or "https://sora.chatgpt.com/drafts").strip()
            try:
                refresh_timeout_seconds = max(1.0, float(payload.get("sora_balance_refresh_timeout_seconds") or 60.0))
            except Exception:
                refresh_timeout_seconds = 60.0

            try:
                # 执行分发：优先按 task_type 配置的 create_task_handler 决定执行器
                if picked.create_task_handler == "sora_gen_video":
                    result = await asyncio.wait_for(
                        sora_gen_video(
                            payload,
                            progress_cb,
                            browser_vendor=picked.browser_vendor,
                            browser_base_url=picked.browser_base_url,
                            browser_access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                            timeout_seconds=float(picked.timeout_seconds),
                            access_token=picked.sora_access_token,
                            access_expires=picked.sora_access_expires,
                            headless=picked.headless,
                        ),
                        timeout=float(picked.timeout_seconds),
                    )
                elif picked.create_task_handler == "veo_workflow":
                    veo_payload = dict(payload or {})
                    if picked.default_target_url and not str(
                        veo_payload.get("veo_url") or veo_payload.get("target_url") or ""
                    ).strip():
                        veo_payload["veo_url"] = picked.default_target_url
                    project_id = picked.project_id
                    project_page = _veo_project_page_url(project_id=project_id, hint_url=picked.default_target_url)
                    picked.default_target_url = project_page;
                    await self._maybe_precheck_flow_account_for_task(
                        picked,
                        project_page=project_page,
                        task_id=task_id,
                    )
                    result,project_page = await asyncio.wait_for(
                        veo_workflow(
                            veo_payload,
                            progress_cb,
                            browser_vendor=picked.browser_vendor,
                            browser_base_url=picked.browser_base_url,
                            browser_access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                            timeout_seconds=float(picked.timeout_seconds),
                            access_token=picked.sora_access_token,
                            access_expires=picked.sora_access_expires,
                            headless=picked.headless,
                            pure_mode=picked.pure_mode,
                            db=self.db,
                            task_type_window_id=picked.mapping_id,
                        ),
                        timeout=float(picked.timeout_seconds),
                    )
                    picked.default_target_url = project_page;
                    print(f"default_target_url:{project_page}");
                elif picked.create_task_handler == "grok_workflow":
                    grok_payload = dict(payload or {})
                    result = await asyncio.wait_for(
                        grok_workflow(
                            grok_payload,
                            progress_cb,
                            browser_vendor=picked.browser_vendor,
                            browser_base_url=picked.browser_base_url,
                            browser_access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                            timeout_seconds=float(picked.timeout_seconds),
                            default_target_url=picked.default_target_url,
                            headless=picked.headless,
                            access_token=picked.sora_access_token,
                            access_expires=picked.sora_access_expires,
                            db=self.db,
                            task_type_window_id=picked.mapping_id,
                        ),
                        timeout=float(picked.timeout_seconds),
                    )
                elif picked.create_task_handler == "dreamina_workflow":
                    dreamina_payload = dict(payload or {})
                    result = await asyncio.wait_for(
                        dreamina_workflow(
                            dreamina_payload,
                            progress_cb,
                            browser_vendor=picked.browser_vendor,
                            browser_base_url=picked.browser_base_url,
                            browser_access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                            timeout_seconds=float(picked.timeout_seconds),
                            default_target_url=picked.default_target_url,
                            headless=picked.headless,
                            access_token=picked.sora_access_token,
                            access_expires=picked.sora_access_expires,
                            pure_mode=picked.pure_mode,
                            db=self.db,
                            task_type_window_id=picked.mapping_id,
                        ),
                        timeout=float(picked.timeout_seconds),
                    )
                elif picked.create_task_handler == "leonardo_workflow":
                    leonardo_payload = dict(payload or {})
                    result = await asyncio.wait_for(
                        leonardo_workflow(
                            leonardo_payload,
                            progress_cb,
                            browser_vendor=picked.browser_vendor,
                            browser_base_url=picked.browser_base_url,
                            browser_access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                            timeout_seconds=float(picked.timeout_seconds),
                            default_target_url=picked.default_target_url or DEFAULT_LEONARDO_TARGET,
                            headless=picked.headless,
                            access_token=picked.sora_access_token,
                            access_expires=picked.sora_access_expires,
                            pure_mode=picked.pure_mode,
                            db=self.db,
                            task_type_window_id=picked.mapping_id,
                        ),
                        timeout=float(picked.timeout_seconds),
                    )
                elif picked.create_task_handler == "zarklab_workflow":
                    zarklab_payload = dict(payload or {})
                    result = await asyncio.wait_for(
                        zarklab_workflow(
                            zarklab_payload,
                            progress_cb,
                            browser_vendor=picked.browser_vendor,
                            browser_base_url=picked.browser_base_url,
                            browser_access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                            timeout_seconds=float(picked.timeout_seconds),
                            default_target_url=picked.default_target_url,
                            headless=picked.headless,
                            pure_mode=picked.pure_mode,
                            db=self.db,
                            task_type_window_id=picked.mapping_id,
                        ),
                        timeout=float(picked.timeout_seconds),
                    )
                elif picked.create_task_handler == "gpt_workflow":
                    gpt_payload = dict(payload or {})
                    result = await asyncio.wait_for(
                        gpt_workflow(
                            gpt_payload,
                            progress_cb,
                            browser_vendor=picked.browser_vendor,
                            browser_base_url=picked.browser_base_url,
                            browser_access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                            timeout_seconds=float(picked.timeout_seconds),
                            access_token=picked.sora_access_token,
                            access_expires=picked.sora_access_expires,
                            default_target_url=picked.default_target_url,
                            headless=picked.headless,
                            pure_mode=picked.pure_mode,
                            db=self.db,
                            task_type_window_id=picked.mapping_id,
                        ),
                        timeout=float(picked.timeout_seconds),
                    )
                elif picked.create_task_handler == "fish_audio_workflow":
                    fish_payload = dict(payload or {})
                    result = await asyncio.wait_for(
                        fish_audio_workflow(
                            fish_payload,
                            progress_cb,
                            browser_vendor=picked.browser_vendor,
                            browser_base_url=picked.browser_base_url,
                            browser_access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                            timeout_seconds=float(picked.timeout_seconds),
                            default_target_url=picked.default_target_url,
                            headless=picked.headless,
                            pure_mode=picked.pure_mode,
                            db=self.db,
                            task_type_window_id=picked.mapping_id,
                        ),
                        timeout=float(picked.timeout_seconds),
                    )
                elif picked.create_task_handler == "elevenlabs_workflow":
                    elevenlabs_payload = dict(payload or {})
                    result = await asyncio.wait_for(
                        elevenlabs_workflow(
                            elevenlabs_payload,
                            progress_cb,
                            browser_vendor=picked.browser_vendor,
                            browser_base_url=picked.browser_base_url,
                            browser_access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                            timeout_seconds=float(picked.timeout_seconds),
                            task_id=task_id,
                            default_target_url=picked.default_target_url,
                            headless=picked.headless,
                            pure_mode=picked.pure_mode,
                            db=self.db,
                            task_type_window_id=picked.mapping_id,
                        ),
                        timeout=float(picked.timeout_seconds),
                    )
                elif picked.create_task_handler == "sora_wm_remove":
                    result = await asyncio.wait_for(
                        sora_wm_remove(
                            payload,
                            progress_cb,
                            browser_vendor=picked.browser_vendor,
                            browser_base_url=picked.browser_base_url,
                            browser_access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                            timeout_seconds=float(picked.timeout_seconds),
                        ),
                        timeout=float(picked.timeout_seconds),
                    )
                elif picked.create_task_handler == "sora_plus_register":
                    result = await asyncio.wait_for(
                        sora_plus_register(
                            payload,
                            progress_cb,
                            db=self.db,
                            window_pk=picked.window_pk,
                            browser_vendor=picked.browser_vendor,
                            browser_base_url=picked.browser_base_url,
                            browser_access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                            timeout_seconds=float(picked.timeout_seconds),
                        ),
                        timeout=float(picked.timeout_seconds),
                    )
                elif picked.task_code == "gen_video":
                    result = await asyncio.wait_for(simulate_video_task(prompt, None, progress_cb), timeout=float(picked.timeout_seconds))
                else:
                    # 默认按图片模拟（包括 gen_image 以及其它未实现类型）
                    result = await asyncio.wait_for(simulate_image_task(prompt, None, progress_cb), timeout=float(picked.timeout_seconds))

                # Sora：单独把 generation_id 落库（用于后续按 generation_id 绑定窗口）
                try:
                    if isinstance(result, dict):
                        gid = str(result.get("generation_id") or "").strip() or None
                        if gid:
                            await self.db.update_task(task_id, generation_id=gid)
                except Exception:
                    pass

                if picked.create_task_handler == "veo_workflow":
                    await refresh_veo_balance_via_extension(
                        db=self.db,
                        picked=picked,
                        refresh_timeout_seconds=refresh_timeout_seconds,
                        signal_window_pool_replenish=self._signal_window_pool_replenish,
                        force_refresh_token=False,
                    )
                elif picked.create_task_handler == "gpt_workflow":
                    await refresh_gpt_balance_via_extension(
                        db=self.db,
                        picked=picked,
                        refresh_timeout_seconds=refresh_timeout_seconds,
                        signal_window_pool_replenish=self._signal_window_pool_replenish,
                        force_refresh_token=False,
                    )
                elif picked.create_task_handler == "dreamina_workflow":
                    await refresh_dreamina_balance_best_effort(
                        db=self.db,
                        picked=picked,
                        refresh_timeout_seconds=refresh_timeout_seconds,
                        signal_window_pool_replenish=self._signal_window_pool_replenish,
                        task_id=task_id,
                    )
                elif picked.create_task_handler == "grok_workflow":
                    pass
                elif picked.create_task_handler == "leonardo_workflow":
                    pass
                elif picked.create_task_handler == "sora_gen_video":
                    await refresh_sora_balance_best_effort(
                        db=self.db,
                        picked=picked,
                        target_url=target_url,
                        refresh_timeout_seconds=refresh_timeout_seconds,
                        signal_window_pool_replenish=self._signal_window_pool_replenish,
                        task_id=task_id,
                    )
                try:
                    if isinstance(result, dict) and result.get("drafts_count") is not None:
                        await self.db.update_task_type_window(
                            mapping_id=picked.mapping_id,
                            sora_drafts_count=int(result.get("drafts_count") or 0),
                        )
                except Exception:
                    pass
                # 清空一下result中的nf_check，避免敏感信息泄露
                if isinstance(result, dict):
                    result["nf_check"] = None
                await self.db.update_task(task_id, status="completed", progress=100, result=result, set_completed=True)
                #await self.db.consume_mapping_quota(picked.mapping_id, amount=1)
                await self.db.mark_mapping_success(picked.mapping_id)
                logger.info("task completed: %s", task_id)
            except Exception as e:
                flow_account_unavailable = (
                    picked.create_task_handler == "veo_workflow"
                    and _flow_is_account_unavailable_error(e)
                )
                leonardo_switchable_error = (
                    picked.create_task_handler == "leonardo_workflow"
                    and _leonardo_is_switchable_error(e)
                )
                if flow_account_unavailable:
                    await self._flow_disable_mapping_after_auth_failure(
                        picked.mapping_id,
                        str(e)[:200],
                    )
                elif picked.create_task_handler == "veo_workflow":
                    await refresh_veo_balance_via_extension(
                        db=self.db,
                        picked=picked,
                        refresh_timeout_seconds=refresh_timeout_seconds,
                        signal_window_pool_replenish=self._signal_window_pool_replenish,
                        force_refresh_token=False,
                    )
                elif picked.create_task_handler == "dreamina_workflow":
                    await refresh_dreamina_balance_best_effort(
                        db=self.db,
                        picked=picked,
                        refresh_timeout_seconds=refresh_timeout_seconds,
                        signal_window_pool_replenish=self._signal_window_pool_replenish,
                        task_id=task_id,
                    )
                elif picked.create_task_handler == "grok_workflow":
                    pass
                elif picked.create_task_handler == "leonardo_workflow":
                    pass
                elif picked.create_task_handler == "sora_gen_video":
                    await refresh_sora_balance_best_effort(
                        db=self.db,
                        picked=picked,
                        target_url=target_url,
                        refresh_timeout_seconds=refresh_timeout_seconds,
                        signal_window_pool_replenish=self._signal_window_pool_replenish,
                        task_id=task_id,
                    )
                    if _sora_task_error_needs_forced_access_token_refresh(e):
                        await force_refresh_sora_access_token(
                            db=self.db,
                            picked=picked,
                            target_url=target_url,
                            refresh_timeout_seconds=refresh_timeout_seconds,
                            task_id=task_id,
                        )
                # 失败：尽量把“是否不扣罚(no_penalty)”等信息写入 result_json，便于上游做退款/分类。
                err_msg = _task_exception_message(e)
                if _last_progress_payload and isinstance(e, asyncio.TimeoutError):
                    stage = str(_last_progress_payload.get("stage") or "").strip()
                    if stage:
                        err_msg = f"{err_msg}; last Flow stage: {stage}"
                no_penalty = bool(getattr(e, "no_penalty", False))
                status_code = getattr(e, "status_code", None)
                submitted = bool(getattr(e, "submitted", False))
                retryable = _task_error_allows_retry(e)
                err_result: Dict[str, Any] = {
                    "error_type": e.__class__.__name__,
                    "no_penalty": no_penalty,
                    "submitted": submitted,
                    "retryable": retryable,
                }
                error_stage = str(getattr(e, "stage", None) or "").strip()
                if error_stage:
                    err_result["error_stage"] = error_stage
                if _last_progress_payload:
                    err_result["last_progress"] = _last_progress_payload
                if "public_error_minor_upload" in err_msg.lower():
                    err_result["policy_reason"] = "PUBLIC_ERROR_MINOR_UPLOAD"
                if status_code is not None:
                    try:
                        err_result["status_code"] = int(status_code)
                    except Exception:
                        err_result["status_code"] = str(status_code)
                _err_lower = err_msg.lower()
                _is_violation = int(
                    "sora_content_violation" in _err_lower
                    or "cameo_not_found" in _err_lower
                    or "cameo_permission_denied" in _err_lower
                    or "包含违禁画面" in err_msg
                    or "包含违规内容" in err_msg
                    or "参考图中包含未成年" in err_msg
                    or "分辨率过高" in err_msg
                    or "public_error_minor_upload" in _err_lower
                    or "未成年人/儿童主体" in err_msg
                    or "未授权人像" in err_msg
                    or "不能超过 4k" in _err_lower
                    or "不能超过4k" in _err_lower
                    or bool(getattr(e, "content_violation", False))
                )
                # ---- 错误重试逻辑 ----
                max_retries = picked.error_retry_count
                leonardo_resume_generation_id = (
                    str(payload.get("_leonardo_generation_id") or "").strip()
                    if picked.create_task_handler == "leonardo_workflow"
                    else ""
                )
                flow_switch_retry_limit = self._flow_health_int(
                    "account_switch_max_retries",
                    3,
                    minimum=0,
                )
                leonardo_switch_retry_limit = self._leonardo_keepalive_int(
                    "task_switch_max_retries",
                    3,
                    minimum=0,
                )
                flow_remaining_accounts = 0
                if flow_account_unavailable and _allow_account_switch:
                    try:
                        flow_remaining_accounts = await self._flow_enabled_mapping_count(picked.task_code)
                    except Exception:
                        flow_remaining_accounts = 0
                leonardo_remaining_accounts = 0
                if leonardo_switchable_error and _allow_account_switch:
                    try:
                        leonardo_remaining_accounts = await self._flow_enabled_mapping_count(picked.task_code)
                    except Exception:
                        leonardo_remaining_accounts = 0
                can_retry = (
                    retryable
                    and bool(leonardo_resume_generation_id)
                    and _retry_attempt < leonardo_switch_retry_limit
                ) or (
                    retryable
                    and flow_account_unavailable
                    and _allow_account_switch
                    and flow_remaining_accounts > 0
                    and _retry_attempt < flow_switch_retry_limit
                ) or (
                    retryable
                    and leonardo_switchable_error
                    and _allow_account_switch
                    and leonardo_remaining_accounts > 1
                    and _retry_attempt < leonardo_switch_retry_limit
                    and not _is_violation
                ) or (
                    retryable
                    and (not flow_account_unavailable)
                    and (not leonardo_switchable_error)
                    and max_retries > 0
                    and _retry_attempt < max_retries
                    and not _is_violation
                )
                if leonardo_resume_generation_id:
                    retry_limit_label = leonardo_switch_retry_limit
                elif flow_account_unavailable:
                    retry_limit_label = flow_switch_retry_limit
                elif leonardo_switchable_error:
                    retry_limit_label = leonardo_switch_retry_limit
                else:
                    retry_limit_label = max_retries
                if can_retry:
                    archive_id = uuid.uuid4().hex
                    try:
                        _orig_row = await self.db.get_task(task_id)
                        _archive_created_at = self._task_created_at_for_sql(
                            getattr(_orig_row, "created_at", None) if _orig_row else None
                        )
                        prompt_text = self._payload_to_prompt_text(payload)
                        await self.db.create_task(
                            Task(
                                task_id=archive_id,
                                task_type_code=picked.task_code,
                                generation_id=(
                                    str(_last_progress_payload.get("generation_id") or "").strip()
                                    or str(payload.get("_leonardo_generation_id") or "").strip()
                                    or None
                                ),
                                status="failed",
                                progress=0,
                                prompt=prompt_text,
                                image_path=None,
                                window_pk=picked.window_pk,
                                window_ip=picked.window_ip,
                            ),
                            insert_created_at=_archive_created_at,
                        )
                        await self.db.update_task(
                            archive_id,
                            status="failed",
                            error_message=f"[{_retry_attempt + 1}|{retry_limit_label}]{err_msg}",
                            result=err_result,
                            content_violation=_is_violation if _is_violation else None,
                            set_completed=True,
                        )
                    except Exception:
                        pass
                    try:
                        await self.db.update_task(
                            task_id, status="queued", progress=0, touch_created_at=True
                        )
                    except Exception:
                        pass
                    _need_retry = True
                    _retry_error_msg = err_msg
                    logger.warning(
                        "task will retry %d/%d: %s err=%s, enqueue for dispatch",
                        _retry_attempt + 1, retry_limit_label, task_id, err_msg,
                    )
                else:
                    await self.db.update_task(
                        task_id,
                        status="failed",
                        error_message=err_msg,
                        result=err_result,
                        content_violation=_is_violation if _is_violation else None,
                        set_completed=True,
                    )
                # 某些错误不应计入“窗口连续错误”（例如：Sora create 400 invalid_request、未抓到 POST 等环境/请求错误）
                # 执行器侧会抛出带 no_penalty=true 的异常（或同名属性），这里做兼容判断。
                if leonardo_switchable_error and not leonardo_resume_generation_id:
                    try:
                        short_cd = self._leonardo_keepalive_int(
                            "task_switch_cooldown_seconds",
                            180,
                            minimum=10,
                        )
                        long_cd = self._leonardo_keepalive_int(
                            "task_switch_long_cooldown_seconds",
                            1800,
                            minimum=60,
                        )
                        await self.db.mark_mapping_error(
                            picked.mapping_id,
                            threshold=picked.threshold,
                            cooldown_seconds=long_cd,
                            cooldown_seconds_short=short_cd,
                            reset_on_threshold=False,
                        )
                        logger.warning(
                            "leonardo mapping cooled after switchable task error: mapping=%s short_cooldown=%ss err=%s",
                            picked.mapping_id,
                            short_cd,
                            err_msg,
                        )
                        self._signal_window_pool_replenish()
                    except Exception as cool_exc:
                        logger.warning(
                            "leonardo mapping cooldown after switchable error failed: mapping=%s err=%s",
                            picked.mapping_id,
                            cool_exc,
                        )
                elif picked.create_task_handler == "leonardo_workflow" and leonardo_resume_generation_id:
                    logger.warning(
                        "leonardo generation recovery keeps original mapping active: mapping=%s task=%s generation=%s err=%s",
                        picked.mapping_id,
                        task_id,
                        leonardo_resume_generation_id,
                        err_msg,
                    )
                elif not no_penalty and not picked.create_task_handler == "sora_wm_remove":
                    await self.db.mark_mapping_error(
                        picked.mapping_id,
                        threshold=picked.threshold,
                        cooldown_seconds=3600,
                        reset_on_threshold=False,
                    )
                    # 连续错误达到“关闭窗口阈值”的整数倍时，启动倒计时关闭窗口（不重置连续错误）
                    try:
                        st = await self.db.get_mapping_runtime_state(mapping_id=picked.mapping_id)
                        ce = int((st or {}).get("consecutive_errors") or 0)
                    except Exception:
                        ce = 0
                    close_thr = max(1, int(getattr(picked, "close_window_threshold", 1) or 1))
                    should_close = ce > 0 and (ce % close_thr == 0)
                    # Sora / Veo 等真实浏览器会话：达阈值后调度空闲关闭，窗口池协程会再补开
                    if should_close:
                        try:
                            if (picked.create_task_handler or "").strip() == "veo_workflow":
                                v_sess = get_or_create_veo_session(
                                    vendor=picked.browser_vendor,
                                    base_url=picked.browser_base_url,
                                    access_key=picked.browser_access_key,
                                    space_id=picked.space_id,
                                    window_key=picked.window_key,
                                )
                                v_sess._schedule_idle_close()
                            elif (picked.create_task_handler or "").strip() == "grok_workflow":
                                g_sess = get_or_create_grok_session(
                                    vendor=picked.browser_vendor,
                                    base_url=picked.browser_base_url,
                                    access_key=picked.browser_access_key,
                                    space_id=picked.space_id,
                                    window_key=picked.window_key,
                                )
                                g_sess._schedule_idle_close()
                            elif (picked.create_task_handler or "").strip() == "dreamina_workflow":
                                d_sess = get_or_create_dreamina_session(
                                    vendor=picked.browser_vendor,
                                    base_url=picked.browser_base_url,
                                    access_key=picked.browser_access_key,
                                    space_id=picked.space_id,
                                    window_key=picked.window_key,
                                )
                                d_sess._schedule_idle_close()
                            elif (picked.create_task_handler or "").strip() == "leonardo_workflow":
                                logger.info(
                                    "leonardo browser close skipped after task error: mapping=%s task=%s",
                                    picked.mapping_id,
                                    task_id,
                                )
                            else:
                                sess = get_or_create_sora_session(
                                    vendor=picked.browser_vendor,
                                    base_url=picked.browser_base_url,
                                    access_key=picked.browser_access_key,
                                    space_id=picked.space_id,
                                    window_key=picked.window_key,
                                )
                                sess._schedule_idle_close()
                        except Exception:
                            pass
                        self._signal_window_pool_replenish()
                if not _need_retry:
                    logger.exception("task failed: %s err=%s", task_id, e)
            finally:
                # 专用窗口任务：无论成败都调度关闭窗口
                if _is_dedicated_window:
                    try:
                        sess = get_or_create_sora_session(
                            vendor=picked.browser_vendor,
                            base_url=picked.browser_base_url,
                            access_key=picked.browser_access_key,
                            space_id=picked.space_id,
                            window_key=picked.window_key,
                        )
                        sess._schedule_idle_close()
                    except Exception:
                        pass
                    self._signal_window_pool_replenish()
                if not _need_retry:
                    self._task_payloads.pop(task_id, None)
        finally:
            try:
                await self.db.release_mapping_slot(picked.mapping_id)
            except Exception:
                pass
            # 专用窗口任务：释放并发计数（重试时也先释放，重新派发时再获取）
            if _is_dedicated_window:
                async with self._dedicated_window_lock:
                    self._dedicated_window_inflight = max(0, self._dedicated_window_inflight - 1)
            self._dispatch_event.set()

            if _need_retry:
                try:
                    payload = self._task_payloads.get(task_id) or {}
                    _retry_gen_id = str(payload.get("generation_id") or "").strip() or None
                    _retry_head_url = str(payload.get("head_url") or "").strip() or None
                    _leonardo_retry_generation_id = str(
                        payload.get("_leonardo_generation_id") or ""
                    ).strip() or None
                    _bind_window_pk: Optional[int] = None
                    if _retry_gen_id and _retry_head_url:
                        _bind_window_pk = picked.window_pk
                    if picked.create_task_handler == "leonardo_workflow" and _leonardo_retry_generation_id:
                        _bind_window_pk = picked.window_pk

                    self._ensure_dispatcher()
                    await self._refresh_queue_config()
                    _retry_enqueued = False
                    _retry_queue_size = 0
                    async with self._queue_lock:
                        if len(self._pending_queue) >= self._queue_max_size:
                            try:
                                await self.db.update_task(
                                    task_id,
                                    status="failed",
                                    error_message=f"任务重试时队列已满，请稍后重试。原错误: {_retry_error_msg}",
                                    set_completed=True,
                                )
                            except Exception:
                                pass
                            self._task_payloads.pop(task_id, None)
                            logger.warning(
                                "task retry dropped (queue full): %s attempt=%d/%d",
                                task_id,
                                _retry_attempt + 1,
                                picked.error_retry_count,
                            )
                        else:
                            self._pending_queue.append(
                                QueuedTask(
                                    task_id=task_id,
                                    task_type_code=picked.task_code,
                                    payload=payload,
                                    enqueued_at=time.monotonic(),
                                    retry_attempt=_retry_attempt + 1,
                                    required_window_pk=_bind_window_pk,
                                    is_dedicated_window=_is_dedicated_window,
                                    allow_account_switch=(
                                        False
                                        if picked.create_task_handler == "leonardo_workflow"
                                        and _leonardo_retry_generation_id
                                        else _allow_account_switch
                                    ),
                                )
                            )
                            _retry_queue_size = len(self._pending_queue)
                            _retry_enqueued = True
                    if _retry_enqueued:
                        self._dispatch_event.set()
                        logger.info(
                            "task retry enqueued: %s attempt=%d/%d queue_size=%d bind_window=%s",
                            task_id,
                            _retry_attempt + 1,
                            retry_limit_label,
                            _retry_queue_size,
                            _bind_window_pk,
                        )
                except Exception as retry_err:
                    try:
                        await self.db.update_task(
                            task_id,
                            status="failed",
                            error_message=f"retry exception ({_retry_attempt + 1}/{picked.error_retry_count}): {retry_err}. original error: {_retry_error_msg}",
                            set_completed=True,
                        )
                    except Exception:
                        pass
                    self._task_payloads.pop(task_id, None)
                    logger.exception("task retry error: %s err=%s", task_id, retry_err)

