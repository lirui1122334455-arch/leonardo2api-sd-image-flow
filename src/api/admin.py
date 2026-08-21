"""Admin API routes (management console)."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import secrets
import shlex
import subprocess
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel, Field, field_validator

from ..core.auth import AuthManager
from ..core.database import Database
from ..core.logger import logger, setup_logging
from ..core.paths import APP_ROOT, PID_FILE
from ..core.public_api_limits import normalize_public_create_task_max_inflight
from ..services.fp_browser_client import FPBrowserClient
from ..services.fish_audio_voice_catalog import list_fish_audio_voices

router = APIRouter()

# dependency injection (set in src/main.py)
db: Database | None = None

active_admin_tokens: dict[str, str] = {}  # token -> username


def _admin_manual_open_target_url(ctx_row: Dict[str, Any]) -> str:
    """manual-open 非 browser_only 模式使用的目标页。

    优先使用任务类型配置的 default_target_url；未配置时按工作流给一个默认目标页。
    对 Veo：若该窗口已绑定 Flow project_id，则优先打开对应 project_page。
    """
    default_target_url = str(ctx_row.get("default_target_url") or "").strip()
    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
    if handler == "veo_workflow":
        try:
            from ..services.veo_workflow_executor import _veo_project_page_url  # type: ignore

            project_id = str(ctx_row.get("current_project_id") or ctx_row.get("veo_project_id") or "").strip()
            if project_id:
                return _veo_project_page_url(project_id=project_id, hint_url=default_target_url or "https://labs.google/fx")
        except Exception:
            pass
    if default_target_url:
        return default_target_url
    if handler == "veo_workflow":
        return "https://veo.google.com"
    if handler == "grok_workflow":
        try:
            from ..services.grok_workflow_executor import DEFAULT_GROK_TARGET  # type: ignore

            return str(DEFAULT_GROK_TARGET or "").strip() or "https://grok.com"
        except Exception:
            return "https://grok.com"
    if handler == "dreamina_workflow":
        try:
            from ..services.jimeng_task_executor import DEFAULT_DREAMINA_TARGET  # type: ignore

            return str(DEFAULT_DREAMINA_TARGET or "").strip() or "https://dreamina.capcut.com/ai-tool/video/generate"
        except Exception:
            return "https://dreamina.capcut.com/ai-tool/video/generate"
    if handler == "leonardo_workflow":
        try:
            from ..services.leonardo_task_executor import DEFAULT_LEONARDO_TARGET  # type: ignore

            return str(DEFAULT_LEONARDO_TARGET or "").strip() or "https://app.leonardo.ai/generate?model=seedance-2.0-fast"
        except Exception:
            return "https://app.leonardo.ai/generate?model=seedance-2.0-fast"
    if handler == "gpt_workflow":
        try:
            from ..services.gpt_task_executor import DEFAULT_GPT_TARGET  # type: ignore

            return str(DEFAULT_GPT_TARGET or "").strip() or "https://chatgpt.com/"
        except Exception:
            return "https://chatgpt.com/"
    if handler == "fish_audio_workflow":
        try:
            from ..services.fish_audio_task_executor import DEFAULT_FISH_AUDIO_TARGET  # type: ignore

            return str(DEFAULT_FISH_AUDIO_TARGET or "").strip() or "https://fish.audio/zh-CN/app/playground/"
        except Exception:
            return "https://fish.audio/zh-CN/app/playground/"
    if handler == "elevenlabs_workflow":
        try:
            from ..services.elevenlabs_task_executor import DEFAULT_ELEVENLABS_TARGET  # type: ignore

            return str(DEFAULT_ELEVENLABS_TARGET or "").strip() or "https://elevenlabs.io/app/sound-effects"
        except Exception:
            return "https://elevenlabs.io/app/sound-effects"
    if handler == "zarklab_workflow":
        try:
            from ..services.zarklab_task_executor import DEFAULT_ZARKLAB_TARGET  # type: ignore

            return str(DEFAULT_ZARKLAB_TARGET or "").strip() or "https://www.zarklab.ai/"
        except Exception:
            return "https://www.zarklab.ai/"
    return "https://sora.chatgpt.com/drafts"


def _safe_hostname(url: str) -> str:
    try:
        return str(urlparse(str(url or "").strip()).hostname or "").strip().lower()
    except Exception:
        return ""


def _main_domain(hostname: str) -> str:
    host = str(hostname or "").strip().lower().strip(".")
    if not host:
        return ""
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return host
    second_level_markers = {"ac", "co", "com", "edu", "gov", "net", "org"}
    country_tlds = {"au", "br", "cn", "hk", "jp", "kr", "mx", "nz", "sg", "tw", "uk", "za"}
    if len(parts[-1]) == 2 and parts[-1] in country_tlds and parts[-2] in second_level_markers and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _same_main_domain(url_a: str, url_b: str) -> bool:
    host_a = _safe_hostname(url_a)
    host_b = _safe_hostname(url_b)
    if not host_a or not host_b:
        return False
    return _main_domain(host_a) == _main_domain(host_b)


def _account_platform_kind_from_url(url: str) -> str:
    host = _safe_hostname(url)
    if not host:
        return ""
    if host == "leonardo.ai" or host.endswith(".leonardo.ai"):
        return "leonardo"
    if host == "capcut.com" or host.endswith(".capcut.com"):
        return "dreamina"
    return ""


async def _expected_account_platform_kind_for_space(space: Any, browser: Any) -> str:
    """Infer the account platform that belongs to a workspace/project."""
    if not db or not browser:
        return ""
    project_id = int(getattr(browser, "project_id", 0) or 0)
    if project_id > 0:
        for code, kind in (("dreamina_workflow", "dreamina"), ("leonardo_workflow", "leonardo")):
            try:
                task_type = await db.get_task_type_by_code(code)
            except Exception:
                task_type = None
            if task_type and int(getattr(task_type, "project_id", 0) or 0) == project_id:
                return kind

    hay = " ".join(
        [
            str(getattr(space, "name", "") or ""),
            str(getattr(browser, "name", "") or ""),
        ]
    ).strip().lower()
    if any(x in hay for x in ("即梦", "dreamina", "jimeng", "capcut")):
        return "dreamina"
    if "leonardo" in hay:
        return "leonardo"
    return ""


async def _ensure_manual_open_has_target_page(pw_ctx: Any, target_url: str) -> str:
    """打开/唤起指纹窗口后，确保窗口内存在目标页或其子页面。

    规则：先找目标 URL 前缀一致页面；再找主域名一致页面并复用导航；
    最后才复用其它可用页/新建 page，避免同主域名页面重复打开。
    """
    from ..services.playwright_broswer_context import pick_working_page_from_context  # type: ignore

    target = str(target_url or "").strip()
    if not target:
        return ""

    await pw_ctx.ensure_open(args=[], force_open=False, headless=bool(getattr(pw_ctx, "headless", False)), require_page=False)
    ctx = getattr(pw_ctx, "context", None)
    if ctx is None:
        raise RuntimeError("CDP 已连接但未获取到浏览器上下文")

    def _base(u: str) -> str:
        s = str(u or "").strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
        return s or str(u or "").strip().rstrip("/")

    target_base = _base(target)
    pages = list(getattr(ctx, "pages", []) or [])
    same_domain_page = None
    for p in pages:
        try:
            if bool(getattr(p, "is_closed", lambda: False)()):
                continue
            u = str(getattr(p, "url", "") or "")
            ub = _base(u)
            if ub == target_base or ub.startswith(target_base + "/"):
                try:
                    await p.bring_to_front()
                except Exception:
                    pass
                return u
            if same_domain_page is None and _same_main_domain(u, target):
                same_domain_page = p
        except Exception:
            continue

    page = same_domain_page or await pick_working_page_from_context(ctx)
    await page.goto(target, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.bring_to_front()
    except Exception:
        pass
    return str(getattr(page, "url", "") or target)


def _remote_response_code(rsp: Optional[Dict[str, Any]], default: int = -1) -> int:
    """安全解析指纹浏览器 API 的 code。

    注意：不能写成 int(rsp.get("code") or -1)，因为 JSON 数字 0 会被 Python
    当作 False，导致成功响应被误判为 -1。
    """
    if not isinstance(rsp, dict):
        return default
    if "code" not in rsp:
        return default
    code = rsp.get("code")
    if code is None or code == "":
        return default
    try:
        return int(code)
    except Exception:
        return default


def _remote_response_msg(rsp: Optional[Dict[str, Any]], default: str = "") -> str:
    if not isinstance(rsp, dict):
        return default
    return str(rsp.get("msg") or default).strip()


def _platform_account_key(account: Dict[str, Any]) -> str:
    if not isinstance(account, dict):
        return ""
    url = str(account.get("platformUrl") or account.get("platform_url") or "").strip().lower()
    user = str(account.get("platformUserName") or account.get("platform_username") or "").strip().lower()
    return f"{url}||{user}" if user else ""


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _remote_msg_matches(msg: str, hints: tuple[str, ...]) -> bool:
    msg_low = str(msg or "").lower()
    return any(str(h or "").lower() in msg_low for h in hints if str(h or "").strip())


REMOTE_WINDOW_NOT_FOUND_HINTS: tuple[str, ...] = (
    "待删除的窗口不存在",
    "窗口不存在",
    "window not found",
    "browser not found",
    "dirid not found",
    "not found",
)

REMOTE_ACCOUNT_NOT_FOUND_HINTS: tuple[str, ...] = (
    "待删除的账号不存在",
    "账号不存在",
    "账户不存在",
    "account not found",
    "account does not exist",
    "not found",
)
REMOTE_ACCOUNT_MISSING_OR_NO_PERMISSION_HINTS: tuple[str, ...] = (
    # RoxyBrowser 对删除“已不在当前 workspace 账号列表中的 id”也会返回这个文案。
    # 不能直接当作不存在处理，需再用 account/list 校验该 id 是否确实缺失。
    "当前账号无该空间操作权限",
)

REMOTE_PROXY_NOT_FOUND_HINTS: tuple[str, ...] = (
    "待删除的代理不存在",
    "代理不存在",
    "proxy not found",
    "proxy does not exist",
    "id not found",
    "not found",
)


def _build_delete_account_remark(old_remark: str, reason: str, *, window_name: str, window_key: str, window_sort_num: Any) -> str:
    base = str(old_remark or "").strip()
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        return base
    sort_text = str(window_sort_num or "-").strip() or "-"
    name_text = str(window_name or "").strip() or "-"
    msg = f"[删除账号原因] {clean_reason}（窗口sort:{sort_text} / 窗口:{name_text} / dirId:{str(window_key or '').strip() or '-'}）"
    return f"{base}\n{msg}" if base else msg

PAGE_KEYS: Set[str] = {
    "system",
    "projects",
    "task_types",
    "tasks",
    "test",
    "agent",
    "paypal",
    "image_resources",
    "card_keys",
    "logs",
    "users",
}


def set_dependencies(database: Database) -> None:
    global db
    db = database


def _effective_browser_pure_mode(ctx_row: Dict[str, Any], query_pure_mode: Optional[bool]) -> bool:
    """指纹 browser_open 的 pure_mode：查询参数优先，否则用绑定 pure_mode 列（True=纯净）。"""
    if query_pure_mode is not None:
        return bool(query_pure_mode)
    raw = ctx_row.get("pure_mode")
    if raw is None:
        return True
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return bool(raw)


async def _get_user_by_token(token: str):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    username = active_admin_tokens.get(token)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid session")
    user = await db.get_admin_user(username)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def _user_is_admin(user) -> bool:
    return bool(getattr(user, "is_admin", False))


async def _user_can_switch_proxy(user) -> bool:
    """是否允许切换窗口代理；管理员默认允许。"""
    if await _user_is_admin(user):
        return True
    return bool(getattr(user, "can_switch_proxy", False))


async def _get_allowed_page_keys(user) -> Set[str]:
    if await _user_is_admin(user):
        return set(PAGE_KEYS)
    if not db:
        return set()
    rows = await db.get_user_page_permissions(int(user.id or 0))
    if not rows:
        return set(PAGE_KEYS)
    return {x for x in rows if x in PAGE_KEYS}


async def _ensure_page_access(token: str, page_key: str):
    user = await _get_user_by_token(token)
    pages = await _get_allowed_page_keys(user)
    if page_key not in pages:
        raise HTTPException(status_code=403, detail="无权访问该页面")
    return user


async def _ensure_any_page_access(token: str, page_keys: Set[str]):
    user = await _get_user_by_token(token)
    pages = await _get_allowed_page_keys(user)
    if not any((k in pages) for k in page_keys):
        raise HTTPException(status_code=403, detail="无权访问该页面")
    return user


async def _get_allowed_project_ids(user) -> Optional[List[int]]:
    if await _user_is_admin(user):
        return None
    if not db:
        return []
    rows = await db.get_user_project_permissions(int(user.id or 0))
    # 未设置则默认全可见
    return rows or None


async def _get_allowed_task_type_ids(user) -> Optional[List[int]]:
    if await _user_is_admin(user):
        return None
    if not db:
        return []
    rows = await db.get_user_task_type_permissions(int(user.id or 0))
    # 未设置则默认全可见
    return rows or None


async def _ensure_admin_user(token: str):
    user = await _get_user_by_token(token)
    if not await _user_is_admin(user):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user


# -------------------- models --------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    username: Optional[str] = None
    old_password: str
    new_password: str = Field(min_length=4)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=4, max_length=128)
    is_admin: bool = False
    can_switch_proxy: bool = False
    page_permissions: List[str] = Field(default_factory=list)
    project_ids: List[int] = Field(default_factory=list)
    task_type_ids: List[int] = Field(default_factory=list)


class UpdateUserPermissionsRequest(BaseModel):
    is_admin: bool = False
    can_switch_proxy: bool = False
    page_permissions: List[str] = Field(default_factory=list)
    project_ids: List[int] = Field(default_factory=list)
    task_type_ids: List[int] = Field(default_factory=list)


class UpdateUserPasswordRequest(BaseModel):
    new_password: str = Field(min_length=4, max_length=128)


class UpdateAPIKeyRequest(BaseModel):
    api_key: str = Field(min_length=6)


class UpdateSystemConfigRequest(BaseModel):
    proxy_enabled: bool
    proxy_url: Optional[str] = None
    debug_enabled: bool
    log_to_file: bool
    stop_accepting_tasks: bool = False
    public_create_task_max_inflight: int = Field(default=180, ge=3)
    server_count: int = Field(default=1, ge=1)
    browser_open_concurrency: Optional[int] = Field(default=None, ge=1, le=50)
    browser_open_queue_timeout: Optional[float] = Field(default=None, ge=10, le=600)
    task_queue_max_size: Optional[int] = Field(default=None, ge=1, le=100000)
    task_queue_timeout_seconds: Optional[float] = Field(default=None, ge=10, le=3600)

    @field_validator("public_create_task_max_inflight")
    @classmethod
    def _validate_public_create_task_max_inflight(cls, v: int) -> int:
        vv = int(v or 0)
        if vv % 3 != 0:
            raise ValueError("并发上限必须是 3 的整数倍")
        return vv


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class UpdateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class CreateBrowserRequest(BaseModel):
    project_id: int
    name: str = Field(min_length=1, max_length=100)
    lan_addr: str = Field(min_length=3, max_length=255)
    vendor: str = Field(default="roxy", max_length=50)
    access_key: Optional[str] = Field(default=None, max_length=255)
    browser_pool_limit: int = Field(default=0, ge=0)


class UpdateBrowserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    lan_addr: str = Field(min_length=3, max_length=255)
    vendor: str = Field(default="roxy", max_length=50)
    access_key: Optional[str] = Field(default=None, max_length=255)
    browser_pool_limit: int = Field(default=0, ge=0)


class CreateSpaceRequest(BaseModel):
    browser_id: int
    name: str = Field(min_length=1, max_length=100)
    space_id: str = Field(min_length=1, max_length=128)
    # 可选：RoxyBrowser /browser/list_v3 的 projectIds（格式 "10,11"）
    project_ids: Optional[str] = Field(default=None, max_length=512)


class UpdateSpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    space_id: str = Field(min_length=1, max_length=128)
    project_ids: Optional[str] = Field(default=None, max_length=512)


class CreateTaskTypeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    project_id: Optional[int] = Field(default=None, ge=1)
    concurrency: int = Field(default=1, ge=1, le=999)
    continuous_error_threshold: int = Field(default=3, ge=1, le=999)
    continuous_error_close_window_threshold: int = Field(default=3, ge=1, le=999999)
    timeout_seconds: int = Field(default=1800, ge=10, le=24 * 3600)
    window_call_cooldown_seconds: int = Field(default=30, ge=0, le=3600)
    create_task_handler: Optional[str] = None
    refresh_quota_handler: Optional[str] = None
    error_retry_count: int = Field(default=0, ge=0, le=10)
    default_target_url: Optional[str] = Field(default=None, max_length=2048)
    window_pool_enabled: bool = False
    window_pool_reconcile_interval_sec: int = Field(default=600, ge=10, le=7200)
    window_pool_cloudflare_interval_sec: int = Field(default=1800, ge=30, le=86400)


class UpdateTaskTypeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    project_id: Optional[int] = Field(default=None, ge=1)
    concurrency: int = Field(default=1, ge=1, le=999)
    continuous_error_threshold: int = Field(default=3, ge=1, le=999)
    continuous_error_close_window_threshold: int = Field(default=3, ge=1, le=999999)
    timeout_seconds: int = Field(default=1800, ge=10, le=24 * 3600)
    window_call_cooldown_seconds: int = Field(default=30, ge=0, le=3600)
    create_task_handler: Optional[str] = None
    refresh_quota_handler: Optional[str] = None
    error_retry_count: int = Field(default=0, ge=0, le=10)
    default_target_url: Optional[str] = Field(default=None, max_length=2048)
    window_pool_enabled: bool = False
    window_pool_reconcile_interval_sec: int = Field(default=600, ge=10, le=7200)
    window_pool_cloudflare_interval_sec: int = Field(default=1800, ge=30, le=86400)
    enabled: bool = True


class AddTaskTypeWindowsRequest(BaseModel):
    window_pks: List[int] = Field(min_length=1)
    daily_quota: int = Field(default=0, ge=0, le=100000)
    remaining_quota: int = Field(default=0, ge=0, le=100000)
    enabled: bool = True


class UpdateTaskTypeWindowRequest(BaseModel):
    enabled: Optional[bool] = None
    headless: Optional[bool] = None
    pure_mode: Optional[bool] = None
    deleted: Optional[bool] = None
    task_type_id: Optional[int] = Field(default=None, ge=1)
    daily_quota: Optional[int] = Field(default=None, ge=0, le=100000)
    remaining_quota: Optional[int] = Field(default=None, ge=0, le=100000)
    cooldown_until: Optional[str] = None  # ISO or empty
    error_cooldown_until: Optional[str] = None  # ISO or empty
    total_errors: Optional[int] = Field(default=None, ge=0, le=1000000)
    consecutive_errors: Optional[int] = Field(default=None, ge=0, le=1000000)


class UpsertSoraAccessTokenRequest(BaseModel):
    access_token: Optional[str] = None
    expires: Optional[str] = None


class UpdateWindowProxyRequest(BaseModel):
    """在指纹浏览器侧修改某个窗口的代理配置（通过本地已保存的 proxy_id 选择）。"""

    proxy_id: int = Field(ge=0, description="本地代理列表中的 proxy_id；0 表示不使用代理")


class SetWindowCoreVersionRequest(BaseModel):
    """在指纹浏览器侧修改窗口内核版本（Roxy POST /browser/mdf 的 coreVersion，字符串，如 125、138）。"""

    core_version: str = Field(min_length=1, max_length=16, description="内核主版本号，数字字符串")


class UpdateWindowAccountRequest(BaseModel):
    """在指纹浏览器侧修改某个窗口的平台账号（通过本地 account_id 选择）。"""

    account_id: int = Field(ge=0, description="本地账号列表中的 account_id；0 表示清空窗口账号")


class SetPureModeRequest(BaseModel):
    """切换纯净模式：修改窗口的 platformUrl 和 openWorkbench。"""

    pure_mode: bool = Field(description="True=纯净模式（platformUrl 清空），False=恢复（platformUrl 设为 Google 登录页）")
    mapping_id: Optional[int] = Field(default=None, description="task_type_windows 映射 ID，用于持久化 pure_mode 状态")


class DeleteWindowRequest(BaseModel):
    """删除窗口请求。"""

    delete_remote: bool = Field(default=True, description="是否同时删除指纹浏览器远程窗口；False 则仅删除本地数据")
    delete_account: bool = Field(default=False, description="是否同时删除该窗口绑定的平台账号")
    account_id: Optional[int] = Field(default=None, ge=0, description="要删除的平台账号 account_id；为空时尝试从窗口绑定解析")
    account_delete_reason: Optional[str] = Field(default="", description="删除账号原因；可选，仅写入本地备注")


class MoveWindowRequest(BaseModel):
    """把本地窗口转移到另一个空间。"""

    target_space_pk: int = Field(ge=1, description="目标空间主键")


class UpdateWindowRemarkRequest(BaseModel):
    """仅更新本地窗口备注。"""

    window_remark: Optional[str] = Field(default="", description="窗口备注")


class ImportAccountsRequest(BaseModel):
    content: str = Field(min_length=1, description="批量导入文本")
    platform_url: Optional[str] = Field(default=None, max_length=2048, description="平台 URL；为空默认 Google 登录页")


class ImportBindAccountsRequest(BaseModel):
    content: str = Field(min_length=1)
    platform_url: Optional[str] = Field(default=None, max_length=2048)
    task_type_code: str = Field(default="leonardo_workflow", min_length=2, max_length=64)
    low_credit_threshold: int = Field(default=800, ge=0, le=100000)
    replace_low_credit: bool = True
    bind_empty_windows: bool = True
    add_to_task_pool: bool = True
    open_after_bind: bool = True
    open_account_after_bind: bool = False
    refresh_quota_after_bind: bool = True
    daily_quota: int = Field(default=9999, ge=0, le=100000)
    remaining_quota: int = Field(default=0, ge=0, le=100000)
    max_bind_count: int = Field(default=0, ge=0, le=10000)
    open_browser_only: bool = False
    headless: bool = False
    pure_mode: Optional[bool] = None
    auto_create_windows: bool = False
    auto_create_window_max: int = Field(default=10, ge=0, le=100)


class BatchDreaminaLogoutRequest(BaseModel):
    mapping_ids: List[int] = Field(default_factory=list)
    local_only: bool = False


class UpdateAccountRemarkRequest(BaseModel):
    platform_remarks: Optional[str] = Field(default="", description="本地备注")


class UpdateAccountPasswordRequest(BaseModel):
    platform_password: Optional[str] = Field(default="", description="????")


class SyncAccountsRequest(BaseModel):
    keep_local_deleted: bool = Field(default=True, description="同步时保留本地已删除账号状态")


class UpdateProxyRemarkRequest(BaseModel):
    remark: Optional[str] = Field(default="", description="本地代理备注")


class DeleteProxyRequest(BaseModel):
    hard_delete: bool = Field(default=False, description="True=从本地 DB 物理删除；False=仅标记 deleted=1")


class SyncProxiesRequest(BaseModel):
    keep_local_deleted: bool = Field(default=True, description="同步时保留本地已删除代理状态")
    keep_local_remark: bool = Field(default=True, description="同步时保留本地代理备注")


class ImportProxiesRequest(BaseModel):
    content: str = Field(min_length=1, description="批量导入代理文本，每行格式：IP:端口:账号:密码")
    protocol: Optional[str] = Field(default=None, description="代理协议；未传默认 SOCKS5")


class ImportCardKeysRequest(BaseModel):
    content: str = Field(min_length=1, description="每行一个卡密")


class UpdateCardKeyRequest(BaseModel):
    card_key: str = Field(min_length=1, max_length=256)


class BatchDeleteCardKeysRequest(BaseModel):
    ids: List[int] = Field(default_factory=list, description="要删除的卡密 ID 列表")


class ImportPaypalAccountsRequest(BaseModel):
    content: str = Field(min_length=1, description="每行一条 PayPal 开户资料")


class UpsertPaypalAccountRequest(BaseModel):
    label: Optional[str] = Field(default=None, max_length=255)
    paypal_email: Optional[str] = Field(default=None, max_length=255)
    paypal_password: Optional[str] = Field(default=None, max_length=512)
    account_status: Optional[str] = Field(default="new", max_length=64)
    card_number: Optional[str] = Field(default=None, max_length=64)
    card_expiry_raw: Optional[str] = Field(default=None, max_length=32)
    card_expiry_year: Optional[int] = Field(default=None, ge=2000, le=2200)
    card_expiry_month: Optional[int] = Field(default=None, ge=1, le=12)
    card_cvv: Optional[str] = Field(default=None, max_length=8)
    phone: Optional[str] = Field(default=None, max_length=64)
    sms_api_url: Optional[str] = Field(default=None, max_length=2048)
    first_name: Optional[str] = Field(default=None, max_length=128)
    last_name: Optional[str] = Field(default=None, max_length=128)
    full_name: Optional[str] = Field(default=None, max_length=255)
    address_line1: Optional[str] = Field(default=None, max_length=255)
    address_line2: Optional[str] = Field(default=None, max_length=255)
    city: Optional[str] = Field(default=None, max_length=128)
    state: Optional[str] = Field(default=None, max_length=64)
    postal_code: Optional[str] = Field(default=None, max_length=64)
    country: Optional[str] = Field(default=None, max_length=64)
    raw_line: Optional[str] = None
    notes: Optional[str] = None


class BatchDeletePaypalAccountsRequest(BaseModel):
    ids: List[int] = Field(default_factory=list, description="要删除的 PayPal 资料 ID 列表")
    hard_delete: bool = Field(default=False, description="True=物理删除；False=软删除")


class PaypalWindowActionRequest(BaseModel):
    target_url: Optional[str] = Field(default=None, max_length=2048)
    register_url: Optional[str] = Field(default=None, max_length=2048)
    paypal_account_id: Optional[int] = Field(default=None, ge=1)
    headless: bool = False
    pure_mode: Optional[bool] = None
    auto_submit: bool = False
    ai_model: Optional[str] = Field(default=None, max_length=64)
    ai_api_key: Optional[str] = Field(default=None, max_length=4096)
    ai_prompt: Optional[str] = Field(default=None, max_length=8000)
    max_steps: int = Field(default=14, ge=1, le=30)


class AIAgentChatMessage(BaseModel):
    role: str = Field(default="user", max_length=32)
    content: str = Field(default="", max_length=200000)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        role = str(v or "user").strip().lower()
        if role not in {"system", "user", "assistant"}:
            return "user"
        return role


class AIAgentChatRequest(BaseModel):
    model: Optional[str] = Field(default=None, max_length=64)
    api_key: Optional[str] = Field(default=None, max_length=4096)
    messages: List[AIAgentChatMessage] = Field(default_factory=list)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=128, le=12000)


class AIAgentConfigUpdateRequest(BaseModel):
    api_key: Optional[str] = Field(default=None, max_length=4096)
    default_model: Optional[str] = Field(default=None, max_length=64)


class AIBrowserAgentRunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    target_url: Optional[str] = Field(default=None, max_length=2048)
    model: Optional[str] = Field(default=None, max_length=64)
    api_key: Optional[str] = Field(default=None, max_length=4096)
    data: Dict[str, Any] = Field(default_factory=dict)
    headless: bool = False
    pure_mode: Optional[bool] = None
    auto_submit: bool = False
    max_steps: int = Field(default=14, ge=1, le=30)


# -------------------- auth helper --------------------
def _normalize_import_platform_url(platform_url: Optional[str], default_url: str = "https://accounts.google.com/") -> str:
    url = str(platform_url or "").strip()
    if not url:
        return str(default_url or "").strip() or "https://accounts.google.com/"
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url
    return url


def _parse_batch_account_lines(content: str, platform_url: Optional[str] = None, default_platform_url: str = "https://accounts.google.com/") -> List[Dict[str, Any]]:
    """解析批量账号文本，支持多种分隔符格式：
    - 邮箱----密码（EFA 可省略）
    - 邮箱——密码——忽略——EFA（中文长横线）
    - 邮箱----密码----忽略----EFA----忽略（四连字符，取第1/2/4字段）
    """
    out: List[Dict[str, Any]] = []
    url = _normalize_import_platform_url(platform_url, default_platform_url)
    lines = [str(x or "").strip() for x in str(content or "").splitlines()]
    for ln in lines:
        if not ln:
            continue
        # 兼容：四连字符 / 中文长横线 / 双连字符 / 单长破折号 / 空格连字符
        parts = [x.strip() for x in re.split(r"(?:----|——|--|—| - )", ln) if str(x or "").strip()]
        if len(parts) < 2:
            continue
        email = str(parts[0] or "").strip()
        password = str(parts[1] or "").strip()
        efa = str(parts[3] or "").strip() if len(parts) >= 4 else ""
        if not email or not password:
            continue
        out.append(
            {
                "platformUrl": url,
                "platformUserName": email,
                "platformPassword": password,
                "platformEfa": efa,
                "platformRemarks": "batch-import",
            }
        )
    return out


PROXY_IMPORT_CHECK_CHANNEL = "IPRust.io"
PROXY_IMPORT_DEFAULT_PROTOCOL = "SOCKS5"


def _normalize_proxy_protocol(v: Any, default: str = PROXY_IMPORT_DEFAULT_PROTOCOL) -> str:
    s = str(v or "").strip().upper()
    if s in {"HTTP", "HTTPS", "SOCKS5"}:
        return s
    return default


def _normalize_proxy_ip_type(v: Any, host: str = "") -> str:
    s = str(v or "").strip().upper()
    if s in {"IPV4", "IPV6"}:
        return s
    try:
        return "IPV6" if ipaddress.ip_address(str(host or "").strip()).version == 6 else "IPV4"
    except Exception:
        return "IPV4"


def _parse_batch_proxy_lines(content: str) -> tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
    """解析代理导入文本：每行 IP:端口:账号:密码。"""
    items: List[Dict[str, str]] = []
    invalid: List[Dict[str, Any]] = []
    for idx, raw in enumerate(str(content or "").splitlines(), start=1):
        line = str(raw or "").strip()
        if not line:
            continue
        parts = [x.strip() for x in line.split(":")]
        if len(parts) != 4:
            invalid.append({"line": idx, "content": line, "reason": "格式应为 IP:端口:账号:密码"})
            continue
        host, port, username, password = parts
        try:
            ip_obj = ipaddress.ip_address(host)
            # 当前导入格式使用冒号分隔，不支持未加括号的 IPv6；按用户格式限定为 IPv4。
            if ip_obj.version != 4:
                invalid.append({"line": idx, "content": line, "reason": "当前导入格式仅支持 IPv4"})
                continue
        except Exception:
            invalid.append({"line": idx, "content": line, "reason": "IP 地址无效"})
            continue
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            invalid.append({"line": idx, "content": line, "reason": "端口无效"})
            continue
        if not username or not password:
            invalid.append({"line": idx, "content": line, "reason": "账号/密码不能为空"})
            continue
        items.append({"host": host, "port": port, "proxyUserName": username, "proxyPassword": password, "line": str(idx)})

    # 按 IP 去重，后面的行覆盖前面的行，符合“以 IP 更新”的语义。
    by_ip: Dict[str, Dict[str, str]] = {}
    for item in items:
        by_ip[str(item["host"]).strip()] = item
    return list(by_ip.values()), invalid


def _extract_proxy_id_from_response(rsp: Dict[str, Any]) -> int:
    """兼容提取 create/modify 返回中的代理 ID（官方文档未承诺返回 ID）。"""
    def _to_int(v: Any) -> int:
        try:
            i = int(v)
            return i if i > 0 else 0
        except Exception:
            return 0

    def walk(obj: Any) -> int:
        if isinstance(obj, dict):
            for k in ("id", "proxyId", "proxy_id"):
                val = _to_int(obj.get(k))
                if val > 0:
                    return val
            for v in obj.values():
                val = walk(v)
                if val > 0:
                    return val
        elif isinstance(obj, list):
            for it in obj:
                val = walk(it)
                if val > 0:
                    return val
        return 0

    return walk(rsp or {})


def _proxy_row_matches_import(row: Dict[str, Any], item: Dict[str, str], *, require_port: bool = True) -> bool:
    host = str(row.get("host") or row.get("lastIp") or row.get("last_ip") or "").strip()
    last_ip = str(row.get("lastIp") or row.get("last_ip") or "").strip()
    if str(item.get("host") or "").strip() not in {host, last_ip}:
        return False
    if require_port and str(row.get("port") or "").strip() != str(item.get("port") or "").strip():
        return False
    return True


def _pick_proxy_row_from_remote(rows: List[Dict[str, Any]], item: Dict[str, str]) -> Optional[Dict[str, Any]]:
    candidates = [r for r in (rows or []) if isinstance(r, dict) and _proxy_row_matches_import(r, item, require_port=True)]
    if not candidates:
        candidates = [r for r in (rows or []) if isinstance(r, dict) and _proxy_row_matches_import(r, item, require_port=False)]
    if not candidates:
        return None

    def _row_id(row: Dict[str, Any]) -> int:
        try:
            return int(row.get("id") if row.get("id") is not None else row.get("proxy_id") or 0)
        except Exception:
            return 0

    candidates.sort(key=_row_id, reverse=True)
    return candidates[0]


def _build_proxy_payload(
    item: Dict[str, str],
    *,
    protocol: str,
    ip_type: str = "IPV4",
    refresh_url: str = "",
    remark: str = "",
) -> Dict[str, Any]:
    return {
        "checkChannel": PROXY_IMPORT_CHECK_CHANNEL,
        "ipType": _normalize_proxy_ip_type(ip_type, str(item.get("host") or "")),
        "protocol": _normalize_proxy_protocol(protocol),
        "host": str(item.get("host") or "").strip(),
        "port": str(item.get("port") or "").strip(),
        "proxyUserName": str(item.get("proxyUserName") or "").strip(),
        "proxyPassword": str(item.get("proxyPassword") or "").strip(),
        "refreshUrl": str(refresh_url or "").strip(),
        "remark": str(remark or "").strip(),
    }


def _extract_accounts_from_batch_create_response(
    rsp: Dict[str, Any], requested_accounts: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """从 batch_create 返回体中提取可直接入库的账号数据（不再额外回查 account/list）。"""

    req_key_map: Dict[str, Dict[str, Any]] = {}
    for a in (requested_accounts or []):
        if not isinstance(a, dict):
            continue
        k = f"{str(a.get('platformUrl') or '').strip().lower()}||{str(a.get('platformUserName') or '').strip().lower()}"
        if k and k not in req_key_map:
            req_key_map[k] = a

    def _to_int(v: Any) -> Optional[int]:
        try:
            i = int(v)
            return i if i > 0 else None
        except Exception:
            return None

    extracted: List[Dict[str, Any]] = []
    seen_ids: set[int] = set()
    id_keys = {"ids", "accountids", "idlist"}
    id_pool: List[int] = []

    def walk(obj: Any):
        if isinstance(obj, dict):
            # 先尝试“完整账号对象”提取
            aid = _to_int(obj.get("id") if obj.get("id") is not None else (obj.get("accountId") if obj.get("accountId") is not None else obj.get("account_id")))
            if aid:
                url = obj.get("platformUrl") if obj.get("platformUrl") is not None else obj.get("platform_url")
                user = obj.get("platformUserName") if obj.get("platformUserName") is not None else obj.get("platform_username")
                pwd = obj.get("platformPassword") if obj.get("platformPassword") is not None else obj.get("platform_password")
                efa = obj.get("platformEfa") if obj.get("platformEfa") is not None else obj.get("platform_efa")
                remarks = obj.get("platformRemarks") if obj.get("platformRemarks") is not None else obj.get("platform_remarks")
                key = f"{str(url or '').strip().lower()}||{str(user or '').strip().lower()}"
                base = req_key_map.get(key) or {}
                if aid not in seen_ids:
                    extracted.append(
                        {
                            "id": int(aid),
                            "platformUrl": str(url).strip() if url is not None else (str(base.get("platformUrl") or "").strip() or None),
                            "platformUserName": str(user).strip() if user is not None else (str(base.get("platformUserName") or "").strip() or None),
                            "platformPassword": str(pwd).strip() if pwd is not None else (str(base.get("platformPassword") or "").strip() or None),
                            "platformEfa": str(efa).strip() if efa is not None else (str(base.get("platformEfa") or "").strip() or None),
                            "platformRemarks": str(remarks).strip() if remarks is not None else (str(base.get("platformRemarks") or "").strip() or None),
                        }
                    )
                    seen_ids.add(aid)

            # 再提取“仅 ID 列表”
            for k, v in obj.items():
                kl = str(k or "").strip().lower()
                if kl in id_keys and isinstance(v, list):
                    for item in v:
                        iid = _to_int(item)
                        if iid and iid not in seen_ids and iid not in id_pool:
                            id_pool.append(iid)
                walk(v)
            return

        if isinstance(obj, list):
            for it in obj:
                walk(it)

    walk(rsp or {})

    # 如果返回体只给了 ids，则按请求顺序与 ids 对齐回写本地
    if (not extracted) and id_pool:
        for idx, aid in enumerate(id_pool):
            if idx >= len(requested_accounts):
                break
            a = requested_accounts[idx] if isinstance(requested_accounts[idx], dict) else {}
            extracted.append(
                {
                    "id": int(aid),
                    "platformUrl": str(a.get("platformUrl") or "").strip() or None,
                    "platformUserName": str(a.get("platformUserName") or "").strip() or None,
                    "platformPassword": str(a.get("platformPassword") or "").strip() or None,
                    "platformEfa": str(a.get("platformEfa") or "").strip() or None,
                    "platformRemarks": str(a.get("platformRemarks") or "").strip() or None,
                }
            )

    return extracted


async def verify_admin_token(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization")
    token = authorization[7:]
    if token not in active_admin_tokens:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")
    return token


# -------------------- auth endpoints --------------------
@router.post("/api/admin/login")
async def admin_login(req: LoginRequest):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    user = await db.get_admin_user(req.username.strip())
    if not user or not AuthManager.verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    session_token = f"admin-{secrets.token_urlsafe(32)}"
    active_admin_tokens[session_token] = user.username
    return {
        "success": True,
        "token": session_token,
        "username": user.username,
        "is_admin": bool(getattr(user, "is_admin", False)),
        "can_switch_proxy": bool(getattr(user, "is_admin", False)) or bool(getattr(user, "can_switch_proxy", False)),
    }


@router.post("/api/login")
async def login_alias(req: LoginRequest):
    """前端兼容：/api/login -> /api/admin/login"""
    return await admin_login(req)


@router.post("/api/admin/logout")
async def admin_logout(token: str = Depends(verify_admin_token)):
    active_admin_tokens.pop(token, None)
    return {"success": True, "message": "退出登录成功"}


@router.post("/api/logout")
async def logout_alias(token: str = Depends(verify_admin_token)):
    """前端兼容：/api/logout -> /api/admin/logout"""
    return await admin_logout(token)


@router.post("/api/admin/change-password")
async def change_password(req: ChangePasswordRequest, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    current_username = active_admin_tokens.get(token)
    if not current_username:
        raise HTTPException(status_code=401, detail="Invalid session")

    user = await db.get_admin_user(current_username)
    if not user:
        raise HTTPException(status_code=400, detail="管理员账号不存在")

    if not AuthManager.verify_password(req.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码错误")

    # 更新密码
    await db.update_admin_password(user.username, AuthManager.hash_password(req.new_password))

    # 可选更新用户名
    if req.username and req.username.strip() and req.username.strip() != user.username:
        await db.update_admin_username(user.username, req.username.strip())

    # 安全起见：清空全部会话，强制重登
    active_admin_tokens.clear()
    return {"success": True, "message": "密码修改成功，请重新登录"}


@router.get("/api/admin/me")
async def admin_me(token: str = Depends(verify_admin_token)):
    user = await _get_user_by_token(token)
    page_permissions = sorted(list(await _get_allowed_page_keys(user)))
    project_ids = await _get_allowed_project_ids(user)
    task_type_ids = await _get_allowed_task_type_ids(user)
    return {
        "success": True,
        "user": {
            "id": user.id,
            "username": user.username,
            "is_admin": bool(getattr(user, "is_admin", False)),
            "can_switch_proxy": bool(getattr(user, "is_admin", False)) or bool(getattr(user, "can_switch_proxy", False)),
            "page_permissions": page_permissions,
            "project_ids": [] if project_ids is None else project_ids,
            "task_type_ids": [] if task_type_ids is None else task_type_ids,
            "all_projects": project_ids is None,
            "all_task_types": task_type_ids is None,
        },
        "page_options": sorted(list(PAGE_KEYS)),
    }


@router.get("/api/admin/users")
async def list_users(token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "users")
    await _ensure_admin_user(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    users = await db.list_admin_users()
    out: List[Dict[str, Any]] = []
    for u in users:
        page_keys = await db.get_user_page_permissions(int(u.id or 0))
        project_ids = await db.get_user_project_permissions(int(u.id or 0))
        task_type_ids = await db.get_user_task_type_permissions(int(u.id or 0))
        out.append(
            {
                "id": u.id,
                "username": u.username,
                "is_admin": bool(getattr(u, "is_admin", False)),
                "can_switch_proxy": bool(getattr(u, "can_switch_proxy", False)),
                # 密码不明文展示；保留固定占位，避免泄露 hash
                "password_display": "******",
                "page_permissions": page_keys,
                "project_ids": project_ids,
                "task_type_ids": task_type_ids,
                "all_pages": len(page_keys) == 0 or bool(getattr(u, "is_admin", False)),
                "all_projects": len(project_ids) == 0 or bool(getattr(u, "is_admin", False)),
                "all_task_types": len(task_type_ids) == 0 or bool(getattr(u, "is_admin", False)),
            }
        )
    return {"success": True, "items": out, "page_options": sorted(list(PAGE_KEYS))}


@router.post("/api/admin/users")
async def create_user(req: CreateUserRequest, token: str = Depends(verify_admin_token)):
    await _ensure_admin_user(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    username = req.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    if await db.get_admin_user(username):
        raise HTTPException(status_code=400, detail="用户名已存在")

    invalid_pages = [x for x in (req.page_permissions or []) if x not in PAGE_KEYS]
    if invalid_pages:
        raise HTTPException(status_code=400, detail=f"无效页面权限: {', '.join(invalid_pages)}")

    uid = await db.create_admin_user(
        username=username,
        password_hash=AuthManager.hash_password(req.password),
        is_admin=bool(req.is_admin),
        can_switch_proxy=bool(req.can_switch_proxy),
    )
    await db.set_user_page_permissions(uid, list(req.page_permissions or []))
    await db.set_user_project_permissions(uid, [int(x) for x in (req.project_ids or [])])
    await db.set_user_task_type_permissions(uid, [int(x) for x in (req.task_type_ids or [])])
    return {"success": True, "user_id": uid}


@router.put("/api/admin/users/{user_id}/password")
async def update_user_password(user_id: int, req: UpdateUserPasswordRequest, token: str = Depends(verify_admin_token)):
    await _ensure_admin_user(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    user = await db.get_admin_user_by_id(int(user_id))
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    await db.update_admin_password_by_id(int(user_id), AuthManager.hash_password(req.new_password))
    return {"success": True}


@router.put("/api/admin/users/{user_id}/permissions")
async def update_user_permissions(user_id: int, req: UpdateUserPermissionsRequest, token: str = Depends(verify_admin_token)):
    current = await _ensure_admin_user(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    target = await db.get_admin_user_by_id(int(user_id))
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 避免把自己降权导致系统无管理员可维护
    if int(current.id or 0) == int(user_id) and not bool(req.is_admin):
        raise HTTPException(status_code=400, detail="不能取消当前登录管理员的管理员权限")

    invalid_pages = [x for x in (req.page_permissions or []) if x not in PAGE_KEYS]
    if invalid_pages:
        raise HTTPException(status_code=400, detail=f"无效页面权限: {', '.join(invalid_pages)}")

    await db.update_admin_user_role(int(user_id), bool(req.is_admin))
    await db.update_admin_user_proxy_switch(int(user_id), bool(req.can_switch_proxy))
    await db.set_user_page_permissions(int(user_id), list(req.page_permissions or []))
    await db.set_user_project_permissions(int(user_id), [int(x) for x in (req.project_ids or [])])
    await db.set_user_task_type_permissions(int(user_id), [int(x) for x in (req.task_type_ids or [])])
    return {"success": True}


# -------------------- system config --------------------
@router.get("/api/admin/system-config")
async def get_system_config(token: str = Depends(verify_admin_token)):
    await _ensure_any_page_access(token, {"system", "test"})
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    syscfg = await db.get_system_config()
    admin_user = await db.get_first_admin_user()
    return {
        "success": True,
        "config": {
            "proxy_enabled": syscfg.proxy_enabled,
            "proxy_url": syscfg.proxy_url or "",
            "api_key": syscfg.api_key,
            "debug_enabled": syscfg.debug_enabled,
            "log_to_file": syscfg.log_to_file,
            "stop_accepting_tasks": bool(getattr(syscfg, "stop_accepting_tasks", False)),
            "public_create_task_max_inflight": normalize_public_create_task_max_inflight(
                getattr(syscfg, "public_create_task_max_inflight", None)
            ),
            "server_count": max(1, int(getattr(syscfg, "server_count", 1) or 1)),
            "browser_open_concurrency": int(getattr(syscfg, "browser_open_concurrency", 3) or 3),
            "browser_open_queue_timeout": float(getattr(syscfg, "browser_open_queue_timeout", 120.0) or 120.0),
            "task_queue_max_size": int(getattr(syscfg, "task_queue_max_size", 1000) or 1000),
            "task_queue_timeout_seconds": float(getattr(syscfg, "task_queue_timeout_seconds", 300.0) or 300.0),
            "admin_username": admin_user.username if admin_user else "admin",
        },
    }


@router.get("/api/admin/ui-defaults")
async def get_ui_defaults(token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "task_types")
    """给管理台前端提供的默认值（避免页面写死 magic number）。"""
    try:
        # dataclass 字段默认值，无需实例化（SoraSession 需要 pw_ctx 参数）
        from ..services.sora_task_executor import SoraSession  # type: ignore

        idle_close_seconds_default = float(SoraSession.__dataclass_fields__["idle_close_seconds"].default)  # type: ignore[attr-defined]
    except Exception:
        idle_close_seconds_default = 30.0

    return {
        "success": True,
        "defaults": {
            # 点击“关闭”后，会触发 _schedule_idle_close；这里的默认倒计时提示应与执行器默认值一致
            "idle_close_seconds": idle_close_seconds_default,
        },
    }


@router.post("/api/admin/system-config")
async def update_system_config(req: UpdateSystemConfigRequest, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "system")
    await _ensure_admin_user(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    proxy_url = (req.proxy_url or "").strip() or None
    await db.update_system_config(
        proxy_enabled=req.proxy_enabled,
        proxy_url=proxy_url,
        debug_enabled=req.debug_enabled,
        log_to_file=req.log_to_file,
        stop_accepting_tasks=req.stop_accepting_tasks,
        public_create_task_max_inflight=req.public_create_task_max_inflight,
        server_count=req.server_count,
        browser_open_concurrency=req.browser_open_concurrency,
        browser_open_queue_timeout=req.browser_open_queue_timeout,
        task_queue_max_size=req.task_queue_max_size,
        task_queue_timeout_seconds=req.task_queue_timeout_seconds,
    )
    await db.reload_config_to_memory()
    setup_logging()

    return {"success": True, "message": "系统配置已更新"}


@router.get("/api/admin/queue-info")
async def get_queue_info(token: str = Depends(verify_admin_token)):
    await _ensure_any_page_access(token, {"system", "tasks"})
    from ..api.routes import task_service as _ts
    if not _ts:
        return {"success": True, "queue": {}}
    return {"success": True, "queue": await _ts.get_queue_info()}


@router.post("/api/admin/api-key")
async def update_api_key(req: UpdateAPIKeyRequest, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "system")
    await _ensure_admin_user(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    await db.update_system_config(api_key=req.api_key.strip())
    await db.reload_config_to_memory()
    return {"success": True, "message": "API Key 已更新"}


def _project_root_dir() -> Path:
    return APP_ROOT


def _fpbrowser2api_pid_file_path() -> Path:
    raw = (os.environ.get("PID_FILE") or "").strip()
    if raw:
        return Path(raw)
    return PID_FILE


def _sync_pid_file_for_service_restart() -> None:
    """写入当前进程 PID，使脚本能 stop 到正在响应请求的 uvicorn（含未用 service 脚本、直接 python main.py 启动）。"""
    try:
        pf = _fpbrowser2api_pid_file_path()
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass


@router.post("/api/admin/service-restart")
async def service_restart(token: str = Depends(verify_admin_token)):
    """调度执行服务脚本 restart（Linux: fpbrowser2api_service.sh；Windows: fpbrowser2api_service.ps1）。"""
    await _ensure_page_access(token, "system")
    await _ensure_admin_user(token)
    root = _project_root_dir()
    _sync_pid_file_for_service_restart()

    if os.name == "nt":
        script = root / "fpbrowser2api_service.ps1"
        if not script.is_file():
            raise HTTPException(status_code=503, detail="未找到 fpbrowser2api_service.ps1，无法通过此接口重启")
        # 与 Linux 一致：先 sleep 再 restart，避免当前请求未返回就 stop
        ps1_quoted = str(script.resolve()).replace("'", "''")
        inner = f"Start-Sleep -Seconds 2; & '{ps1_quoted}' restart"
        try:
            # 勿用 Popen 直接挂 powershell 为 uvicorn 子进程：stop 会结束 Python，子 PowerShell 常被一并终止，restart 无法跑完。
            # cmd /c start 拉起独立进程树；CREATE_BREAKAWAY_FROM_JOB 减轻 IDE 作业对象连带结束调度进程的情况。
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            creationflags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
            subprocess.Popen(
                [
                    "cmd.exe",
                    "/c",
                    "start",
                    "",
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    inner,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(script.parent),
                creationflags=creationflags,
            )
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"无法启动重启进程: {e}") from e
        logger.info(
            "service restart scheduled (Windows): pid=%s pid_file=%s",
            os.getpid(),
            _fpbrowser2api_pid_file_path(),
        )
        return {
            "success": True,
            "message": "已调度服务重启，约数秒后进程将重新启动，页面可能暂时无法访问",
        }

    script = root / "fpbrowser2api_service.sh"
    if not script.is_file():
        raise HTTPException(status_code=503, detail="未找到 fpbrowser2api_service.sh，无法通过此接口重启")
    if not os.access(script, os.X_OK):
        raise HTTPException(status_code=503, detail="fpbrowser2api_service.sh 无执行权限，请在服务器上 chmod +x")
    # 勿仅 Popen 单层 bash 为 uvicorn 子进程：stop 结束 Python 时，子 shell 可能被 cgroup/会话一并带走，restart 跑不完。
    # nohup + setsid 后台：外层 bash 立刻结束，真正执行 sleep+restart 的进程被 init/systemd 收养，与 uvicorn 解耦。
    restart_inner = f"sleep 2 && exec bash {shlex.quote(str(script))} restart"
    launcher = (
        f"nohup setsid bash -c {shlex.quote(restart_inner)} </dev/null >/dev/null 2>&1 &"
    )
    try:
        subprocess.Popen(
            ["/bin/bash", "-c", launcher],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(script.parent),
        )
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"无法启动重启进程: {e}") from e
    logger.info(
        "service restart scheduled (Linux): pid=%s pid_file=%s",
        os.getpid(),
        _fpbrowser2api_pid_file_path(),
    )
    return {
        "success": True,
        "message": "已调度服务重启，约数秒后进程将重新启动，页面可能暂时无法访问",
    }


# -------------------- project management --------------------
@router.get("/api/admin/projects")
async def list_projects(token: str = Depends(verify_admin_token)):
    user = await _ensure_page_access(token, "projects")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    allowed_ids = await _get_allowed_project_ids(user)
    return {"success": True, "projects": [p.model_dump() for p in await db.list_projects(allowed_project_ids=allowed_ids)]}


@router.post("/api/admin/projects")
async def create_project(req: CreateProjectRequest, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "projects")
    await _ensure_admin_user(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    pid = await db.create_project(req.name)
    return {"success": True, "project_id": pid}


@router.put("/api/admin/projects/{project_id}")
async def update_project(project_id: int, req: UpdateProjectRequest, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "projects")
    await _ensure_admin_user(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    await db.update_project(project_id, req.name)
    return {"success": True}


@router.delete("/api/admin/projects/{project_id}")
async def delete_project(project_id: int, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "projects")
    await _ensure_admin_user(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    await db.delete_project(project_id)
    return {"success": True}


# -------------------- browsers & spaces --------------------
@router.get("/api/admin/projects/{project_id}/browsers")
async def list_browsers(project_id: int, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {"success": True, "browsers": [b.model_dump() for b in await db.list_browsers(project_id)]}


@router.post("/api/admin/browsers")
async def create_browser(req: CreateBrowserRequest, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    bid = await db.create_browser(req.project_id, req.name, req.lan_addr, req.vendor, req.access_key, req.browser_pool_limit)
    return {"success": True, "browser_id": bid}


@router.put("/api/admin/browsers/{browser_id}")
async def update_browser(browser_id: int, req: UpdateBrowserRequest, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    await db.update_browser(browser_id, req.name, req.lan_addr, req.vendor, req.access_key, req.browser_pool_limit)
    return {"success": True}


@router.delete("/api/admin/browsers/{browser_id}")
async def delete_browser(browser_id: int, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    await db.delete_browser(browser_id)
    return {"success": True}


@router.get("/api/admin/browsers/{browser_id}/spaces")
async def list_spaces(browser_id: int, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {"success": True, "spaces": [s.model_dump() for s in await db.list_spaces(browser_id)]}


@router.post("/api/admin/spaces")
async def create_space(req: CreateSpaceRequest, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    sid = await db.create_space(req.browser_id, req.name, req.space_id, req.project_ids)
    return {"success": True, "space_pk": sid}


@router.put("/api/admin/spaces/{space_pk}")
async def update_space(space_pk: int, req: UpdateSpaceRequest, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    await db.update_space(space_pk, req.name, req.space_id, req.project_ids)
    return {"success": True}


@router.delete("/api/admin/spaces/{space_pk}")
async def delete_space(space_pk: int, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    await db.delete_space(space_pk)
    return {"success": True}


@router.get("/api/admin/spaces/{space_pk}/windows")
async def list_windows(space_pk: int, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {"success": True, "windows": [w.model_dump() for w in await db.list_windows(space_pk)]}


@router.get("/api/admin/spaces/{space_pk}/proxies")
async def list_local_proxies(
    space_pk: int,
    include_deleted: bool = False,
    token: str = Depends(verify_admin_token),
):
    """查看本地 DB 保存的代理列表（按空间/工作空间维度）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {
        "success": True,
        "proxies": [p.model_dump(exclude={"raw"}) for p in await db.list_proxies(space_pk, include_deleted=include_deleted)],
    }


@router.get("/api/admin/proxies")
async def list_all_proxies(
    include_deleted: bool = False,
    token: str = Depends(verify_admin_token),
):
    """返回所有空间的代理列表（按 proxy_id 去重），代理是跨空间共用的。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {
        "success": True,
        "proxies": [p.model_dump(exclude={"raw"}) for p in await db.list_all_proxies(include_deleted=include_deleted)],
    }


@router.get("/api/admin/spaces/{space_pk}/proxy-bindings")
async def list_proxy_bindings(space_pk: int, token: str = Depends(verify_admin_token)):
    """返回代理绑定数：proxy_id -> 被多少个本地未删除窗口绑定。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {"success": True, "counts": await db.count_proxy_bindings(space_pk)}


@router.get("/api/admin/spaces/{space_pk}/proxies/{proxy_id}/bound-windows")
async def list_proxy_bound_windows(space_pk: int, proxy_id: int, token: str = Depends(verify_admin_token)):
    """返回该代理绑定的窗口详情（跨所有空间）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {"success": True, "items": await db.list_proxy_bound_windows(proxy_id)}


@router.get("/api/admin/spaces/{space_pk}/proxy-success-counts")
async def list_proxy_success_counts(space_pk: int, token: str = Depends(verify_admin_token)):
    """返回代理成功任务数：proxy_id -> tasks 表中该 IP 的 completed 数。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {"success": True, "counts": await db.count_proxy_success_tasks(space_pk)}


@router.get("/api/admin/spaces/{space_pk}/proxy-task-stats")
async def list_proxy_task_stats(space_pk: int, token: str = Depends(verify_admin_token)):
    """返回代理任务统计：proxy_id -> {completed, failed}（按 tasks.window_ip 聚合）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {"success": True, "stats": await db.count_proxy_task_stats(space_pk)}


@router.post("/api/admin/spaces/{space_pk}/proxies/{proxy_id}/delete")
async def delete_local_proxy(
    space_pk: int,
    proxy_id: int,
    req: DeleteProxyRequest = DeleteProxyRequest(),
    token: str = Depends(verify_admin_token),
):
    """删除代理（默认先删除指纹浏览器侧，再删除本地）。

    说明：
    - 默认调用 RoxyBrowser `/proxy/delete` 成功后，再把本地记录标记 deleted=1。
    - hard_delete=True 时仅从本地 DB 物理删除该 proxy_id 的代理记录（用于清理本地已软删除记录）。
    - 后续“同步该空间代理到本地”默认会保留本地删除标记，不会自动恢复。
    - 代理列表是全局去重展示；若按 space_pk+proxy_id 未命中，按 proxy_id 全局兜底删除。
    - 如果本地也已不存在，按幂等成功返回，避免“远端/本地没有该代理”时影响操作体验。
    """
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    if int(proxy_id) <= 0:
        raise HTTPException(status_code=400, detail="invalid proxy_id")

    if bool(req.hard_delete):
        affected = await db.hard_delete_proxy_by_proxy_id(proxy_id=int(proxy_id))
        if affected <= 0:
            return {
                "success": True,
                "message": "本地代理记录已不存在，视为删除完成",
                "affected": 0,
                "already_missing": True,
                "hard_deleted": True,
            }
        return {"success": True, "message": "已从本地数据库彻底删除该代理记录", "affected": affected, "hard_deleted": True}

    # 先删除指纹浏览器侧代理。只有远端删除成功（或远端已不存在）后，才删除本地记录。
    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    remote_err = ""
    remote_already_missing = False
    rsp: Dict[str, Any] = {}
    try:
        rsp = await client.delete_proxies(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
            proxy_ids=[int(proxy_id)],
        )
    except Exception as e:
        remote_err = str(e)
    if not remote_err:
        code = _remote_response_code(rsp)
        msg = _remote_response_msg(rsp)
        remote_already_missing = code != 0 and _remote_msg_matches(msg, REMOTE_PROXY_NOT_FOUND_HINTS)
        if code != 0 and not remote_already_missing:
            remote_err = msg or "删除代理失败"

    if remote_err:
        raise HTTPException(status_code=400, detail=f"远端代理删除失败，本地未删除：{remote_err}")

    affected = await db.delete_proxy(space_pk=int(space_pk), proxy_id=int(proxy_id))
    fallback_affected = 0
    if affected <= 0 and int(proxy_id) > 0:
        fallback_affected = await db.delete_proxy_by_proxy_id(proxy_id=int(proxy_id))
    total_affected = int(affected or 0) + int(fallback_affected or 0)
    if total_affected <= 0:
        msg = "远端代理已不存在，本地代理记录也已不存在" if remote_already_missing else "远端代理已删除，本地代理记录已不存在"
        return {
            "success": True,
            "message": msg,
            "affected": 0,
            "already_missing": True,
            "remote_deleted": (not remote_already_missing),
            "remote_already_missing": remote_already_missing,
        }
    msg = "远端代理已不存在，已完成本地删除" if remote_already_missing else "代理已删除并同步到指纹浏览器"
    return {
        "success": True,
        "message": msg,
        "affected": total_affected,
        "remote_deleted": (not remote_already_missing),
        "remote_already_missing": remote_already_missing,
    }


@router.post("/api/admin/spaces/{space_pk}/proxies/{proxy_id}/remark")
async def update_local_proxy_remark(
    space_pk: int,
    proxy_id: int,
    req: UpdateProxyRemarkRequest,
    token: str = Depends(verify_admin_token),
):
    """仅更新本地代理备注，不影响指纹浏览器侧。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    affected = await db.update_proxy_remark(space_pk=int(space_pk), proxy_id=int(proxy_id), remark=str(req.remark or "").strip())
    if affected <= 0:
        raise HTTPException(status_code=404, detail="proxy not found")
    return {"success": True, "message": "代理备注已更新", "affected": affected}


@router.post("/api/admin/spaces/{space_pk}/proxies/{proxy_id}/analyze-ip")
async def analyze_local_proxy_ip(space_pk: int, proxy_id: int, token: str = Depends(verify_admin_token)):
    """检测代理 IP 风险并回写到本地 proxies 表。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    proxy = await db.get_proxy(space_pk=int(space_pk), proxy_id=int(proxy_id))
    if not proxy:
        raise HTTPException(status_code=404, detail="proxy not found")

    def _pick_ip(*candidates: Optional[str]) -> Optional[str]:
        for c in candidates:
            v = str(c or "").strip()
            if not v:
                continue
            try:
                ipaddress.ip_address(v)
                return v
            except Exception:
                continue
        return None

    ip = _pick_ip(proxy.last_ip, proxy.host)
    if not ip:
        raise HTTPException(status_code=400, detail="该代理缺少可检测的 IP（last_ip/host）")

    syscfg = await db.get_system_config()
    url = "https://getgpt.pro/api/analyze-ip"
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)
    proxy_url = str(syscfg.proxy_url or "").strip()
    use_proxy = bool(syscfg.proxy_enabled) and bool(proxy_url)

    def _build_client_kwargs(enable_proxy: bool) -> Dict[str, Any]:
        # 禁用环境变量代理，避免其他机器上 HTTP(S)_PROXY 导致意外失败。
        kwargs: Dict[str, Any] = {"timeout": timeout, "trust_env": False}
        if enable_proxy:
            try:
                kwargs["proxy"] = proxy_url
            except TypeError:
                kwargs["proxies"] = proxy_url
        return kwargs

    try:
        async with httpx.AsyncClient(**_build_client_kwargs(use_proxy)) as client:
            rsp = await client.get(url, params={"ip": ip})
    except Exception as first_err:
        if use_proxy:
            # 配了代理但请求失败时，回退直连重试一次。
            try:
                async with httpx.AsyncClient(**_build_client_kwargs(False)) as client:
                    rsp = await client.get(url, params={"ip": ip})
            except Exception as second_err:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "调用 IP 检测接口失败（代理+直连均失败）："
                        f"proxy_err={first_err.__class__.__name__}: {first_err}; "
                        f"direct_err={second_err.__class__.__name__}: {second_err}"
                    ),
                )
        else:
            raise HTTPException(
                status_code=502,
                detail=f"调用 IP 检测接口失败（直连）：{first_err.__class__.__name__}: {first_err}",
            )

    try:
        payload = rsp.json()
    except Exception:
        payload = None
    if rsp.status_code >= 400 or not isinstance(payload, dict):
        body_preview = ""
        try:
            body_preview = (rsp.text or "")[:200]
        except Exception:
            body_preview = ""
        raise HTTPException(
            status_code=502,
            detail=f"IP 检测接口返回异常（status={rsp.status_code}, body={body_preview}）",
        )

    if not payload.get("success"):
        raise HTTPException(status_code=400, detail=str(payload.get("message") or "IP 检测失败"))

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    risk_level = str(data.get("riskLevel") or "").strip() or None
    asn_type = str(data.get("asnType") or "").strip() or None

    await db.update_proxy_ip_profile(
        space_pk=int(space_pk),
        proxy_id=int(proxy_id),
        risk_level=risk_level,
        asn_type=asn_type,
    )

    return {
        "success": True,
        "space_pk": int(space_pk),
        "proxy_id": int(proxy_id),
        "ip": ip,
        "risk_level": risk_level,
        "asn_type": asn_type,
        "data": data,
    }


@router.post("/api/admin/spaces/{space_pk}/sync-proxies")
async def sync_space_proxies(
    space_pk: int,
    req: Optional[SyncProxiesRequest] = None,
    token: str = Depends(verify_admin_token),
):
    """同步某个空间的代理列表（从指纹浏览器拉取后写入本地 DB）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    try:
        proxies = await client.list_proxies(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    keep_local_deleted = True if req is None else bool(req.keep_local_deleted)
    keep_local_remark = True if req is None else bool(req.keep_local_remark)
    affected = await db.upsert_proxies(
        space_pk=space_pk,
        proxies=proxies,
        restore_deleted=(not keep_local_deleted),
        overwrite_remark=(not keep_local_remark),
    )
    return {"success": True, "message": f"同步完成，写入/更新 {affected} 条代理记录", "affected": affected}


@router.post("/api/admin/spaces/{space_pk}/proxies/import")
async def import_space_proxies(space_pk: int, req: ImportProxiesRequest, token: str = Depends(verify_admin_token)):
    """批量导入代理到指纹浏览器，并按 IP 更新/创建本地 DB。

    格式：IP:端口:账号:密码。
    - 本地已存在相同 IP（host 或 last_ip）：先调用 Roxy /proxy/modify，再更新本地端口/账号/密码与创建时间。
    - 本地不存在：先调用 Roxy /proxy/create，再回查代理列表并写入本地 DB。
    """
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    current_space = await db.get_space(space_pk)
    if not current_space:
        raise HTTPException(status_code=404, detail="space not found")
    current_browser = await db.get_browser(current_space.browser_id)
    if not current_browser:
        raise HTTPException(status_code=404, detail="browser not found")

    parsed, invalid = _parse_batch_proxy_lines(req.content)
    if not parsed:
        raise HTTPException(status_code=400, detail="未解析到有效代理，请检查格式：IP地址:端口:账号:密码")

    protocol_default = _normalize_proxy_protocol(req.protocol)
    syscfg = await db.get_system_config()
    client = FPBrowserClient()

    updated = 0
    created = 0
    failed = 0
    results: List[Dict[str, Any]] = []
    pending_creates: List[Dict[str, Any]] = []

    async def _load_space_browser(target_space_pk: int):
        sp = await db.get_space(int(target_space_pk))
        if not sp:
            raise RuntimeError(f"space not found: {target_space_pk}")
        br = await db.get_browser(sp.browser_id)
        if not br:
            raise RuntimeError(f"browser not found: {sp.browser_id}")
        return sp, br

    for item in parsed:
        host = str(item.get("host") or "").strip()
        existing = await db.find_proxy_by_ip_global(host)
        try:
            if existing:
                target_space, target_browser = await _load_space_browser(int(existing.space_pk))
                proxy_id = int(existing.proxy_id or 0)
                if proxy_id <= 0:
                    raise RuntimeError("本地代理缺少 proxy_id，无法修改指纹浏览器代理")
                payload = _build_proxy_payload(
                    item,
                    protocol=_normalize_proxy_protocol(existing.protocol, protocol_default),
                    ip_type=_normalize_proxy_ip_type(existing.ip_type, host),
                    refresh_url=str(existing.refresh_url or ""),
                    remark=str(existing.remark or ""),
                )
                rsp = await client.update_proxy(
                    vendor=target_browser.vendor,
                    base_url=target_browser.lan_addr,
                    access_key=target_browser.access_key,
                    space_id=target_space.space_id,
                    proxy_id=proxy_id,
                    proxy=payload,
                )
                if _remote_response_code(rsp) != 0:
                    raise RuntimeError(_remote_response_msg(rsp, "修改代理失败"))

                raw = {**payload, "id": proxy_id, "proxy_id": proxy_id, "workspaceId": str(target_space.space_id or "").strip()}
                affected = await db.update_imported_proxy_local(
                    space_pk=int(existing.space_pk),
                    proxy_id=proxy_id,
                    host=host,
                    port=str(item.get("port") or ""),
                    proxy_username=str(item.get("proxyUserName") or ""),
                    proxy_password=str(item.get("proxyPassword") or ""),
                    protocol=str(payload.get("protocol") or protocol_default),
                    ip_type=str(payload.get("ipType") or "IPV4"),
                    check_channel=PROXY_IMPORT_CHECK_CHANNEL,
                    refresh_url=str(payload.get("refreshUrl") or ""),
                    remark=str(existing.remark or ""),
                    raw=raw,
                    overwrite_remark=False,
                )
                if affected <= 0:
                    raise RuntimeError("远端已修改，但本地 DB 更新失败：proxy not found")
                updated += 1
                results.append({"host": host, "proxy_id": proxy_id, "action": "updated", "space_pk": int(existing.space_pk)})
                continue

            payload = _build_proxy_payload(item, protocol=protocol_default, remark="batch-import")
            rsp = await client.create_proxy(
                vendor=current_browser.vendor,
                base_url=current_browser.lan_addr,
                access_key=current_browser.access_key,
                space_id=current_space.space_id,
                proxy=payload,
            )
            remote_created = _remote_response_code(rsp) == 0
            pending_creates.append(
                {
                    "item": item,
                    "payload": payload,
                    "rsp": rsp or {},
                    "remote_created": remote_created,
                    "proxy_id": _extract_proxy_id_from_response(rsp or {}) if remote_created else 0,
                }
            )
        except Exception as e:
            failed += 1
            results.append({"host": host, "action": "failed", "error": str(e)})

    if pending_creates:
        remote_list_failed = False
        try:
            remote_rows = await client.list_proxies(
                vendor=current_browser.vendor,
                base_url=current_browser.lan_addr,
                access_key=current_browser.access_key,
                space_id=current_space.space_id,
            )
        except Exception as e:
            remote_list_failed = True
            remote_rows = []
            for pending in pending_creates:
                failed += 1
                item = pending.get("item") or {}
                results.append(
                    {
                        "host": str(item.get("host") or ""),
                        "action": "failed",
                        "error": f"远端创建后回查代理列表失败：{e}",
                    }
                )

        for pending in pending_creates:
            item = pending.get("item") or {}
            host = str(item.get("host") or "").strip()
            payload = pending.get("payload") or {}
            try:
                if remote_list_failed:
                    continue
                remote_created = bool(pending.get("remote_created"))
                rsp = pending.get("rsp") or {}
                proxy_id = int(pending.get("proxy_id") or 0)
                remote_row = _pick_proxy_row_from_remote(remote_rows, item)
                if remote_row and proxy_id <= 0:
                    proxy_id = _extract_proxy_id_from_response(remote_row)
                if not remote_created and not remote_row:
                    raise RuntimeError(_remote_response_msg(rsp, "创建代理失败"))
                if proxy_id <= 0:
                    raise RuntimeError("远端已创建，但未能获取新代理 ID，请稍后手动同步代理列表")
                if (not remote_created) and remote_row:
                    # 远端可能已存在同 IP/端口但本地未同步：按导入内容补一次 modify，确保远端数据也被更新。
                    rsp_modify = await client.update_proxy(
                        vendor=current_browser.vendor,
                        base_url=current_browser.lan_addr,
                        access_key=current_browser.access_key,
                        space_id=current_space.space_id,
                        proxy_id=proxy_id,
                        proxy=payload,
                    )
                    if _remote_response_code(rsp_modify) != 0:
                        raise RuntimeError(_remote_response_msg(rsp_modify, "创建失败后尝试修改远端已有代理也失败"))

                row_for_db = {
                    **(remote_row or {}),
                    **payload,
                    "id": proxy_id,
                    "proxy_id": proxy_id,
                    "host": host,
                    "port": str(item.get("port") or ""),
                    "proxyUserName": str(item.get("proxyUserName") or ""),
                    "proxyPassword": str(item.get("proxyPassword") or ""),
                    "checkChannel": PROXY_IMPORT_CHECK_CHANNEL,
                    "lastIp": host,
                    "purchase_type": (remote_row or {}).get("purchase_type") or (remote_row or {}).get("purchaseType") or "外部购买",
                }
                await db.upsert_proxies(
                    space_pk=int(space_pk),
                    proxies=[row_for_db],
                    restore_deleted=True,
                    overwrite_remark=True,
                    mark_missing_deleted=False,
                )
                affected = await db.update_imported_proxy_local(
                    space_pk=int(space_pk),
                    proxy_id=proxy_id,
                    host=host,
                    port=str(item.get("port") or ""),
                    proxy_username=str(item.get("proxyUserName") or ""),
                    proxy_password=str(item.get("proxyPassword") or ""),
                    protocol=str(payload.get("protocol") or protocol_default),
                    ip_type=str(payload.get("ipType") or "IPV4"),
                    check_channel=PROXY_IMPORT_CHECK_CHANNEL,
                    refresh_url=str(payload.get("refreshUrl") or ""),
                    remark=str(payload.get("remark") or ""),
                    raw=row_for_db,
                    overwrite_remark=True,
                )
                if affected <= 0:
                    # 兜底：upsert_proxies 可能因历史 proxy_id 跨空间去重写入了原空间。
                    local_row = await db.find_proxy_by_ip_global(host)
                    if local_row:
                        affected = await db.update_imported_proxy_local(
                            space_pk=int(local_row.space_pk),
                            proxy_id=proxy_id,
                            host=host,
                            port=str(item.get("port") or ""),
                            proxy_username=str(item.get("proxyUserName") or ""),
                            proxy_password=str(item.get("proxyPassword") or ""),
                            protocol=str(payload.get("protocol") or protocol_default),
                            ip_type=str(payload.get("ipType") or "IPV4"),
                            check_channel=PROXY_IMPORT_CHECK_CHANNEL,
                            refresh_url=str(payload.get("refreshUrl") or ""),
                            remark=str(payload.get("remark") or ""),
                            raw=row_for_db,
                            overwrite_remark=True,
                        )
                if affected <= 0:
                    raise RuntimeError("远端已创建，但本地 DB 写入失败")

                created += 1
                results.append(
                    {
                        "host": host,
                        "proxy_id": proxy_id,
                        "action": "created" if remote_created else "synced_remote_existing",
                        "space_pk": int(space_pk),
                    }
                )
            except Exception as e:
                failed += 1
                results.append({"host": host, "action": "failed", "error": str(e)})

    skipped_duplicate = max(0, len([x for x in str(req.content or "").splitlines() if str(x or "").strip()]) - len(parsed) - len(invalid))
    skipped = len(invalid) + skipped_duplicate
    msg = f"导入完成：更新 {updated} 条，新增 {created} 条，失败 {failed} 条"
    if skipped:
        msg += f"，跳过 {skipped} 行"
    return {
        "success": failed == 0,
        "message": msg,
        "parsed": len(parsed),
        "updated": updated,
        "created": created,
        "failed": failed,
        "skipped": skipped,
        "invalid": invalid[:50],
        "results": results[:200],
    }


@router.get("/api/admin/accounts")
async def list_all_accounts_global(
    include_deleted: bool = True,
    token: str = Depends(verify_admin_token),
):
    """返回所有空间的平台账号列表（按 account_id 去重），账号是跨空间共用的。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    rows = [a.model_dump(exclude={"raw"}) for a in await db.list_all_platform_accounts(include_deleted=include_deleted)]
    bindings = await db.list_account_bindings()
    merged: List[Dict[str, Any]] = []
    for x in rows:
        aid = int(x.get("account_id") or 0)
        b = bindings.get(aid) or {}
        merged.append(
            {
                **x,
                "binding_count": int(b.get("count") or 0),
                "binding_windows": str(b.get("windows") or "").strip(),
            }
        )
    return {"success": True, "accounts": merged}


@router.get("/api/admin/spaces/{space_pk}/accounts")
async def list_local_accounts(space_pk: int, token: str = Depends(verify_admin_token)):
    """查看本地 DB 保存的平台账号列表（按空间维度）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    rows = [a.model_dump(exclude={"raw"}) for a in await db.list_platform_accounts(space_pk, include_deleted=True)]
    bindings = await db.list_account_bindings(space_pk)
    merged: List[Dict[str, Any]] = []
    for x in rows:
        aid = int(x.get("account_id") or 0)
        b = bindings.get(aid) or {}
        merged.append(
            {
                **x,
                "binding_count": int(b.get("count") or 0),
                "binding_windows": str(b.get("windows") or "").strip(),
            }
        )
    return {"success": True, "accounts": merged}


@router.post("/api/admin/spaces/{space_pk}/sync-accounts")
async def sync_space_accounts(
    space_pk: int,
    req: Optional[SyncAccountsRequest] = None,
    token: str = Depends(verify_admin_token),
):
    """同步某个空间的平台账号列表（从指纹浏览器拉取后写入本地 DB）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    try:
        accounts = await client.list_accounts(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    keep_local_deleted = True if req is None else bool(req.keep_local_deleted)
    affected = await db.upsert_platform_accounts(
        space_pk=space_pk,
        accounts=accounts,
        restore_deleted=(not keep_local_deleted),
    )
    return {"success": True, "message": f"同步完成，写入/更新 {affected} 条账号记录", "affected": affected}


@router.post("/api/admin/spaces/{space_pk}/accounts/import")
async def import_space_accounts(space_pk: int, req: ImportAccountsRequest, token: str = Depends(verify_admin_token)):
    """批量导入账号到指纹浏览器，并回写本地 DB。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    try:
        leonardo_type = await db.get_task_type_by_code("leonardo_workflow")
        if (
            leonardo_type
            and int(getattr(leonardo_type, "project_id", 0) or 0) > 0
            and int(getattr(browser, "project_id", 0) or 0) == int(getattr(leonardo_type, "project_id", 0) or 0)
        ):
            return await import_bind_space_accounts(
                space_pk,
                ImportBindAccountsRequest(content=req.content, platform_url=req.platform_url),
                token=token,
            )
    except HTTPException:
        raise
    except Exception:
        logger.warning("leonardo import auto-bind fallback failed; using plain account import", exc_info=True)

    parsed = _parse_batch_account_lines(req.content, req.platform_url)
    if not parsed:
        raise HTTPException(status_code=400, detail="未解析到有效账号，请检查格式：邮箱----密码 或 邮箱----密码----忽略----EFA")

    # 避免覆盖/重复：按 (platformUrl, platformUserName) 去重
    dedup_map: Dict[str, Dict[str, Any]] = {}
    for a in parsed:
        k = f"{str(a.get('platformUrl') or '').strip().lower()}||{str(a.get('platformUserName') or '').strip().lower()}"
        if k and k not in dedup_map:
            dedup_map[k] = a
    parsed_unique = list(dedup_map.values())

    local_accounts = await db.list_platform_accounts(space_pk)
    existing_keys = {
        f"{str(x.platform_url or '').strip().lower()}||{str(x.platform_username or '').strip().lower()}"
        for x in local_accounts
    }
    to_create = []
    for a in parsed_unique:
        k = f"{str(a.get('platformUrl') or '').strip().lower()}||{str(a.get('platformUserName') or '').strip().lower()}"
        if k in existing_keys:
            continue
        to_create.append(a)

    if to_create:
        syscfg = await db.get_system_config()
        client = FPBrowserClient()
        try:
            rsp = await client.create_accounts_batch(
                vendor=browser.vendor,
                base_url=browser.lan_addr,
                access_key=browser.access_key,
                space_id=space.space_id,
                account_list=to_create,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if _remote_response_code(rsp) != 0:
            raise HTTPException(status_code=400, detail=str((rsp or {}).get("msg") or "批量创建账号失败"))

    # 不再额外回查 account/list，直接使用 batch_create 返回结果落本地。
    affected = 0
    found_count = 0
    if to_create:
        created_rows = _extract_accounts_from_batch_create_response(rsp or {}, to_create)
        found_count = len(created_rows or [])
        # 仅增量写入，不做 full_replace（避免清空本地其他账号）
        affected = await db.upsert_platform_accounts(space_pk=space_pk, accounts=created_rows or [], full_replace=False)

    missing_count = max(0, len(to_create) - int(found_count or 0))
    tip = f"，本地写入 {found_count} 条"
    if missing_count > 0:
        tip += f"（{missing_count} 条未从创建响应中拿到ID，可稍后手动同步补齐）"
    return {
        "success": True,
        "message": f"导入完成：新增 {len(to_create)} 条，跳过 {len(parsed_unique) - len(to_create)} 条（重复账号）{tip}",
        "parsed": len(parsed_unique),
        "created": len(to_create),
        "skipped": len(parsed_unique) - len(to_create),
        "synced": int(affected or 0),
        "found": int(found_count or 0),
    }


async def _dreamina_direct_login_for_mapping(
    mapping_id: int,
    *,
    headless: bool = False,
    pure_mode: Optional[bool] = None,
) -> Dict[str, Any]:
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")
    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
    if handler != "dreamina_workflow":
        raise HTTPException(status_code=400, detail="当前绑定不是 dreamina_workflow")

    window_pk = int(ctx_row.get("window_pk") or 0)
    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if window_pk <= 0 or not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing window/browser context")

    timeout_seconds = float(ctx_row.get("task_timeout_seconds") or 600)
    timeout_seconds = max(120.0, min(timeout_seconds, 3600.0))
    effective_pure = _effective_browser_pure_mode(ctx_row, pure_mode)

    async def _progress_cb(_pct: int, _meta: Optional[Dict[str, Any]] = None) -> None:
        return

    try:
        from ..services.jimeng_task_executor import dreamina_admin_direct_login_page  # type: ignore

        result = await dreamina_admin_direct_login_page(
            _progress_cb,
            db=db,
            window_pk=window_pk,
            browser_vendor=vendor,
            browser_base_url=base_url,
            browser_access_key=access_key,
            space_id=space_id,
            window_key=window_key,
            headless=headless,
            default_target_url=str(ctx_row.get("default_target_url") or "").strip(),
            pure_mode=effective_pure,
            timeout_seconds=timeout_seconds,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Dreamina 直接上号失败：{e}")

    access_token = str((result or {}).get("access_token") or "").strip() or None
    expires = str((result or {}).get("expires") or "").strip() or None
    if not access_token:
        raise HTTPException(status_code=400, detail="Dreamina 直接上号失败：未返回 sessionid")

    await db.update_task_type_window(mapping_id=mapping_id, sora_access_token=access_token, sora_access_expires=expires)
    return result


def _extract_window_keys_from_create_response(rsp: Optional[Dict[str, Any]]) -> List[str]:
    keys: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("dirId", "dir_id", "windowKey", "window_key"):
                raw = value.get(key)
                text = str(raw or "").strip()
                if text:
                    keys.append(text)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(rsp or {})
    out: List[str] = []
    seen: Set[str] = set()
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


async def _auto_create_empty_windows_for_binding(
    *,
    client: FPBrowserClient,
    browser: Any,
    space: Any,
    space_pk: int,
    task_code: str,
    default_platform_url: str,
    count: int,
) -> Dict[str, Any]:
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    create_count = max(0, int(count or 0))
    if create_count <= 0:
        return {"created_windows": [], "sync_results": []}

    target_url = _normalize_import_platform_url(default_platform_url, default_platform_url)
    code = str(task_code or "").strip()
    if code == "dreamina_workflow":
        prefix = "Dreamina"
    elif code == "leonardo_workflow":
        prefix = "Leonardo"
    else:
        prefix = "Auto"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    created_windows: List[Dict[str, Any]] = []
    created_keys: Set[str] = set()

    for idx in range(create_count):
        window_name = f"{prefix} auto {stamp}-{idx + 1}"
        rsp = await client.create_window(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
            window_name=window_name,
            open_urls=[target_url] if target_url else [],
        )
        if _remote_response_code(rsp) != 0:
            raise RuntimeError(_remote_response_msg(rsp, "fingerprint window create failed"))
        keys = _extract_window_keys_from_create_response(rsp)
        for key in keys:
            created_keys.add(key)
        created_windows.append(
            {
                "window_name": window_name,
                "window_keys": keys,
                "message": _remote_response_msg(rsp, "created"),
            }
        )

    sync_results: List[Dict[str, Any]] = []
    for attempt in range(5):
        if attempt > 0:
            await asyncio.sleep(0.8 * attempt)
        remote_windows = await client.list_windows(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
            project_ids=getattr(space, "project_ids", None),
        )
        sync_result = await db.upsert_windows(space_pk=space_pk, windows=remote_windows)
        sync_results.append(sync_result)
        local_windows = await db.list_windows(space_pk)
        if created_keys:
            local_keys = {str(getattr(w, "window_key", "") or "").strip() for w in local_windows}
            if created_keys.issubset(local_keys):
                break
        elif int(sync_result.get("affected") or 0) >= create_count:
            break

    return {
        "created_windows": created_windows,
        "created_window_keys": sorted(created_keys),
        "sync_results": sync_results,
    }


@router.post("/api/admin/spaces/{space_pk}/accounts/import-bind")
async def import_bind_space_accounts(space_pk: int, req: ImportBindAccountsRequest, token: str = Depends(verify_admin_token)):
    """Import accounts, bind them to workflow windows, and optionally open windows."""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    task_code = str(req.task_type_code or "leonardo_workflow").strip()
    task_type = await db.get_task_type_by_code(task_code)
    if not task_type or not bool(getattr(task_type, "enabled", True)):
        raise HTTPException(status_code=404, detail=f"task_type not found or disabled: {task_code}")
    task_type_id = int(getattr(task_type, "id", 0) or 0)
    if task_type_id <= 0:
        raise HTTPException(status_code=400, detail="task_type id missing")

    if task_code == "leonardo_workflow":
        default_platform_url = "https://app.leonardo.ai/"
    elif task_code == "dreamina_workflow":
        default_platform_url = "https://dreamina.capcut.com/"
    else:
        default_platform_url = "https://accounts.google.com/"
    refresh_quota_after_bind = bool(req.refresh_quota_after_bind)
    if task_code == "leonardo_workflow":
        # Leonardo accounts must pass the login/Cloudflare page manually first.
        # Do not run GraphQL quota probing immediately after binding a fresh account.
        refresh_quota_after_bind = False
    initial_remaining_quota = int(req.remaining_quota)
    if task_code == "leonardo_workflow" and initial_remaining_quota < int(req.low_credit_threshold):
        initial_remaining_quota = max(int(req.low_credit_threshold), int(req.daily_quota))
    initial_mapping_enabled = task_code != "leonardo_workflow"
    parsed = _parse_batch_account_lines(req.content, req.platform_url, default_platform_url)
    if not parsed:
        raise HTTPException(status_code=400, detail="no valid accounts parsed; expected email----password or email----password----ignore----EFA")

    dedup_map: Dict[str, Dict[str, Any]] = {}
    for account in parsed:
        key = _platform_account_key(account)
        if key and key not in dedup_map:
            dedup_map[key] = account
    parsed_unique = list(dedup_map.values())
    if req.max_bind_count > 0:
        parsed_unique = parsed_unique[: int(req.max_bind_count)]

    client = FPBrowserClient()
    local_accounts = await db.list_platform_accounts(space_pk)
    existing_keys = {
        f"{str(x.platform_url or '').strip().lower()}||{str(x.platform_username or '').strip().lower()}"
        for x in local_accounts
    }
    to_create = [a for a in parsed_unique if _platform_account_key(a) not in existing_keys]
    create_error = ""
    if to_create:
        try:
            rsp = await client.create_accounts_batch(
                vendor=browser.vendor,
                base_url=browser.lan_addr,
                access_key=browser.access_key,
                space_id=space.space_id,
                account_list=to_create,
            )
            if _remote_response_code(rsp) != 0:
                raise RuntimeError(_remote_response_msg(rsp, "account batch create failed"))
            created_rows = _extract_accounts_from_batch_create_response(rsp or {}, to_create)
            if created_rows:
                await db.upsert_platform_accounts(space_pk=space_pk, accounts=created_rows, full_replace=False)
        except Exception as e:
            create_error = str(e)

    remote_rows: List[Dict[str, Any]] = []
    try:
        remote_rows = await client.find_accounts_by_keys(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
            keys=[_platform_account_key(a) for a in parsed_unique],
        )
    except Exception as e:
        if not create_error:
            create_error = f"account lookup failed: {e}"
    if remote_rows:
        await db.upsert_platform_accounts(space_pk=space_pk, accounts=remote_rows, full_replace=False)

    accounts_by_key: Dict[str, Any] = {}
    for account in await db.list_platform_accounts(space_pk):
        key = f"{str(account.platform_url or '').strip().lower()}||{str(account.platform_username or '').strip().lower()}"
        if key and key not in accounts_by_key:
            accounts_by_key[key] = account

    import_accounts: List[Any] = []
    missing_accounts: List[Dict[str, Any]] = []
    for requested in parsed_unique:
        key = _platform_account_key(requested)
        account = accounts_by_key.get(key)
        if account:
            import_accounts.append(account)
        else:
            missing_accounts.append({
                "username": str(requested.get("platformUserName") or ""),
                "platform_url": str(requested.get("platformUrl") or ""),
                "reason": "missing account_id after import",
            })

    skipped_bound_accounts: List[Dict[str, Any]] = []
    try:
        bindings = await db.list_account_bindings()
    except Exception:
        bindings = {}
    bindable_accounts: List[Any] = []
    for account in import_accounts:
        aid = int(getattr(account, "account_id", 0) or 0)
        binding = bindings.get(aid) or {}
        if int(binding.get("count") or 0) > 0:
            skipped_bound_accounts.append({
                "account_id": aid,
                "username": str(getattr(account, "platform_username", "") or "").strip(),
                "windows": str(binding.get("windows") or "").strip(),
            })
            continue
        bindable_accounts.append(account)
    import_accounts = bindable_accounts

    if not import_accounts:
        return {
            "success": False,
            "message": "No unbound imported accounts are bindable.",
            "parsed": len(parsed_unique),
            "created": len(to_create),
            "bindable": 0,
            "missing_accounts": missing_accounts,
            "skipped_bound_accounts": skipped_bound_accounts,
            "create_error": create_error,
            "items": [],
        }

    windows = await db.list_windows(space_pk)
    windows_by_pk = {int(getattr(w, "id", 0) or 0): w for w in windows if int(getattr(w, "id", 0) or 0) > 0}
    mappings = await db.list_task_type_windows(task_type_id)
    mappings_by_window = {int(m.get("window_pk") or 0): m for m in mappings if int(m.get("window_pk") or 0) > 0}
    used_window_pks: Set[int] = set()

    targets: List[Dict[str, Any]] = []
    if req.replace_low_credit:
        for m in sorted(mappings, key=lambda row: (_safe_int(row.get("remaining_quota")), _safe_int(row.get("id")))):
            wpk = _safe_int(m.get("window_pk"))
            if wpk <= 0 or wpk in used_window_pks or wpk not in windows_by_pk:
                continue
            if not bool(getattr(windows_by_pk[wpk], "enabled", True)):
                continue
            if _safe_int(m.get("remaining_quota")) < int(req.low_credit_threshold):
                targets.append({"window": windows_by_pk[wpk], "mapping": m, "reason": "low_credit"})
                used_window_pks.add(wpk)

    if req.bind_empty_windows:
        for w in windows:
            wpk = int(getattr(w, "id", 0) or 0)
            if wpk <= 0 or wpk in used_window_pks:
                continue
            if not bool(getattr(w, "enabled", True)):
                continue
            account_id = int(getattr(w, "platform_account_id", 0) or 0)
            account_name = str(getattr(w, "platform_account", "") or "").strip()
            if account_id > 0 or account_name:
                continue
            targets.append({"window": w, "mapping": mappings_by_window.get(wpk), "reason": "empty_window"})
            used_window_pks.add(wpk)

    auto_created_windows: List[Dict[str, Any]] = []
    auto_create_error = ""
    auto_create_sync_results: List[Dict[str, Any]] = []
    effective_auto_create_windows = bool(req.auto_create_windows) or (
        task_code == "dreamina_workflow" and bool(req.open_account_after_bind)
    )
    if effective_auto_create_windows and len(targets) < len(import_accounts):
        missing_target_count = len(import_accounts) - len(targets)
        create_count = min(missing_target_count, int(req.auto_create_window_max or 0))
        if create_count > 0:
            try:
                auto_create_result = await _auto_create_empty_windows_for_binding(
                    client=client,
                    browser=browser,
                    space=space,
                    space_pk=space_pk,
                    task_code=task_code,
                    default_platform_url=default_platform_url,
                    count=create_count,
                )
                auto_created_windows = list(auto_create_result.get("created_windows") or [])
                auto_create_sync_results = list(auto_create_result.get("sync_results") or [])
                auto_created_keys = {
                    str(x or "").strip()
                    for x in list(auto_create_result.get("created_window_keys") or [])
                    if str(x or "").strip()
                }
                auto_created_names = {
                    str((x or {}).get("window_name") or "").strip()
                    for x in auto_created_windows
                    if isinstance(x, dict) and str((x or {}).get("window_name") or "").strip()
                }

                windows = await db.list_windows(space_pk)
                windows_by_pk = {int(getattr(w, "id", 0) or 0): w for w in windows if int(getattr(w, "id", 0) or 0) > 0}
                mappings = await db.list_task_type_windows(task_type_id)
                mappings_by_window = {int(m.get("window_pk") or 0): m for m in mappings if int(m.get("window_pk") or 0) > 0}
                used_window_pks = set()
                targets = []
                if req.replace_low_credit:
                    for m in sorted(mappings, key=lambda row: (_safe_int(row.get("remaining_quota")), _safe_int(row.get("id")))):
                        wpk = _safe_int(m.get("window_pk"))
                        if wpk <= 0 or wpk in used_window_pks or wpk not in windows_by_pk:
                            continue
                        if not bool(getattr(windows_by_pk[wpk], "enabled", True)):
                            continue
                        if _safe_int(m.get("remaining_quota")) < int(req.low_credit_threshold):
                            targets.append({"window": windows_by_pk[wpk], "mapping": m, "reason": "low_credit"})
                            used_window_pks.add(wpk)
                for w in windows:
                    wpk = int(getattr(w, "id", 0) or 0)
                    if wpk <= 0 or wpk in used_window_pks:
                        continue
                    if not bool(getattr(w, "enabled", True)):
                        continue
                    account_id = int(getattr(w, "platform_account_id", 0) or 0)
                    account_name = str(getattr(w, "platform_account", "") or "").strip()
                    if account_id > 0 or account_name:
                        continue
                    wkey = str(getattr(w, "window_key", "") or "").strip()
                    wname = str(getattr(w, "window_name", "") or "").strip()
                    is_auto_created = (wkey in auto_created_keys) or (wname in auto_created_names)
                    if (not bool(req.bind_empty_windows)) and not is_auto_created:
                        continue
                    reason = "auto_created_empty_window" if is_auto_created else "empty_window"
                    targets.append({"window": w, "mapping": mappings_by_window.get(wpk), "reason": reason})
                    used_window_pks.add(wpk)
            except Exception as e:
                auto_create_error = str(e)
                logger.warning("auto-create fingerprint windows failed for import-bind: space_pk=%s err=%s", space_pk, e, exc_info=True)

    bind_count = min(len(import_accounts), len(targets))
    if bind_count <= 0:
        return {
            "success": False,
            "message": (
                "No target windows available for binding."
                + (f" Auto-create failed: {auto_create_error}" if auto_create_error else "")
            ),
            "parsed": len(parsed_unique),
            "created": len(to_create),
            "bindable": len(import_accounts),
            "missing_accounts": missing_accounts,
            "skipped_bound_accounts": skipped_bound_accounts,
            "create_error": create_error,
            "auto_created_windows": auto_created_windows,
            "auto_create_error": auto_create_error,
            "auto_create_sync_results": auto_create_sync_results,
            "items": [],
        }

    items: List[Dict[str, Any]] = []
    bound = 0
    opened = 0
    refreshed = 0
    for idx in range(bind_count):
        account = import_accounts[idx]
        target = targets[idx]
        window = target["window"]
        wpk = int(getattr(window, "id", 0) or 0)
        window_key = str(getattr(window, "window_key", "") or "").strip()
        item: Dict[str, Any] = {
            "account_id": int(getattr(account, "account_id", 0) or 0),
            "username": str(getattr(account, "platform_username", "") or "").strip(),
            "window_pk": wpk,
            "window_key": window_key,
            "window_name": str(getattr(window, "window_name", "") or "").strip(),
            "reason": str(target.get("reason") or ""),
            "status": "pending",
            "mapping_id": 0,
            "remaining_quota": None,
            "errors": [],
        }
        try:
            item["bind_result"] = await _mdf_window_account_and_proxy_from_local(
                space_pk=space_pk,
                window_key=window_key,
                selected_account=account,
                clear_account=False,
                require_local_account=False,
            )
            bound += 1

            mapping_id = _safe_int((target.get("mapping") or {}).get("id"))
            if req.add_to_task_pool:
                await db.add_task_type_windows(
                    task_type_id=task_type_id,
                    window_pks=[wpk],
                    daily_quota=int(req.daily_quota),
                    remaining_quota=initial_remaining_quota,
                    enabled=initial_mapping_enabled,
                )
                fresh_mappings = await db.list_task_type_windows(task_type_id)
                for m in fresh_mappings:
                    if _safe_int(m.get("window_pk")) == wpk:
                        mapping_id = _safe_int(m.get("id"))
                        break
            item["mapping_id"] = mapping_id

            if mapping_id > 0 and req.open_after_bind:
                try:
                    if task_code == "dreamina_workflow" and bool(req.open_account_after_bind):
                        item["open_result"] = await _dreamina_direct_login_for_mapping(
                            mapping_id,
                            headless=bool(req.headless),
                            pure_mode=req.pure_mode,
                        )
                    else:
                        item["open_result"] = await manual_open_mapping_window(
                            mapping_id,
                            headless=bool(req.headless),
                            pure_mode=req.pure_mode,
                            browser_only=bool(req.open_browser_only),
                            token=token,
                        )
                    opened += 1
                except Exception as e:
                    item["errors"].append(f"open failed: {e}")

            if task_code == "leonardo_workflow" and _is_leonardo_account(account):
                try:
                    item["clear_session_result"] = await _clear_leonardo_window_site_session(
                        client=client,
                        browser=browser,
                        space=space,
                        window_key=window_key,
                    )
                except Exception as e:
                    item["errors"].append(f"clear old Leonardo session failed: {e}")

            if mapping_id > 0 and refresh_quota_after_bind:
                try:
                    refresh_result = await refresh_mapping_remaining_quota(
                        mapping_id,
                        source=None,
                        headless=bool(req.headless),
                        token=token,
                    )
                    item["refresh_result"] = refresh_result
                    item["remaining_quota"] = int(refresh_result.get("remaining_quota") or 0)
                    refreshed += 1
                except Exception as e:
                    item["errors"].append(f"refresh quota failed: {e}")
            elif mapping_id > 0 and task_code == "leonardo_workflow":
                item["refresh_deferred"] = True
                item["remaining_quota"] = initial_remaining_quota

            item["status"] = "ok" if not item["errors"] else "partial"
        except Exception as e:
            item["status"] = "failed"
            item["errors"].append(str(e))
        items.append(item)
        if task_code == "leonardo_workflow" and idx < bind_count - 1:
            await asyncio.sleep(5.0)

    skipped_accounts = len(import_accounts) - bind_count
    auto_created_count = len(auto_created_windows)
    return {
        "success": bound > 0,
        "message": (
            f"import-bind done: bound={bound}, opened={opened}, refreshed={refreshed}, "
            f"auto_created_windows={auto_created_count}, skipped_accounts={skipped_accounts}"
            + (f", auto_create_error={auto_create_error}" if auto_create_error else "")
        ),
        "task_type_id": task_type_id,
        "task_type_code": task_code,
        "parsed": len(parsed_unique),
        "created": len(to_create),
        "bindable": len(import_accounts),
        "bound": bound,
        "opened": opened,
        "refreshed": refreshed,
        "skipped_accounts": skipped_accounts,
        "unused_target_windows": max(0, len(targets) - bind_count),
        "missing_accounts": missing_accounts,
        "skipped_bound_accounts": skipped_bound_accounts,
        "create_error": create_error,
        "auto_created_windows": auto_created_windows,
        "auto_create_error": auto_create_error,
        "auto_create_sync_results": auto_create_sync_results,
        "items": items,
    }


@router.post("/api/admin/spaces/{space_pk}/accounts/{account_id}/delete")
async def delete_space_account(space_pk: int, account_id: int, token: str = Depends(verify_admin_token)):
    """删除平台账号（远端 + 本地）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    if int(account_id) <= 0:
        raise HTTPException(status_code=400, detail="invalid account_id")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    account_row = await db.get_platform_account(space_pk=space_pk, account_id=int(account_id))
    account_space_pk = int(getattr(account_row, "space_pk", 0) or space_pk)
    if account_space_pk != int(space_pk):
        target_space = await db.get_space(account_space_pk)
        if target_space:
            target_browser = await db.get_browser(target_space.browser_id)
            if target_browser:
                space = target_space
                browser = target_browser

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    remote_err = ""
    remote_already_missing = False
    rsp: Dict[str, Any] = {}
    try:
        rsp = await client.delete_accounts(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
            account_ids=[int(account_id)],
        )
    except Exception as e:
        remote_err = str(e)
    if not remote_err:
        code = _remote_response_code(rsp)
        msg = _remote_response_msg(rsp)
        remote_already_missing = code != 0 and await _is_remote_account_already_missing(
            client,
            browser=browser,
            space=space,
            account_id=int(account_id),
            msg=msg,
        )
        if code != 0 and not remote_already_missing:
            remote_err = msg or "删除账号失败"

    if remote_err:
        raise HTTPException(status_code=400, detail=f"远端账号删除失败，本地未删除：{remote_err}")

    account_affected = await db.delete_platform_account_by_account_id(account_id=int(account_id))
    binding_cleared = await db.clear_window_platform_binding_by_account_global(account_id=int(account_id))
    msg = "远端账号已不存在，已完成本地删除" if remote_already_missing else "账号已删除并同步到指纹浏览器"
    return {
        "success": True,
        "message": msg,
        "remote_deleted": (not remote_already_missing),
        "remote_already_missing": remote_already_missing,
        "account_affected": int(account_affected or 0),
        "binding_cleared": int(binding_cleared or 0),
    }


@router.post("/api/admin/spaces/{space_pk}/accounts/{account_id}/hard-delete")
async def hard_delete_space_account(space_pk: int, account_id: int, token: str = Depends(verify_admin_token)):
    """账号管理（全局）专用删除：删除远端账号后，物理删除本地账号记录。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    if int(account_id) <= 0:
        raise HTTPException(status_code=400, detail="invalid account_id")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    # 全局账号列表按 account_id 去重展示，账号本地记录可能归属其他 space_pk。
    # 删除远端时优先使用账号记录所属空间，避免用当前打开面板的空间调用时触发 Roxy 权限错误。
    account_row = await db.get_platform_account(space_pk=space_pk, account_id=int(account_id))
    account_space_pk = int(getattr(account_row, "space_pk", 0) or space_pk)
    if account_space_pk != int(space_pk):
        target_space = await db.get_space(account_space_pk)
        if target_space:
            target_browser = await db.get_browser(target_space.browser_id)
            if target_browser:
                space = target_space
                browser = target_browser

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    remote_err = ""
    remote_already_missing = False
    rsp: Dict[str, Any] = {}
    try:
        rsp = await client.delete_accounts(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
            account_ids=[int(account_id)],
        )
    except Exception as e:
        remote_err = str(e)
    if not remote_err:
        code = _remote_response_code(rsp)
        msg = _remote_response_msg(rsp)
        remote_already_missing = code != 0 and await _is_remote_account_already_missing(
            client,
            browser=browser,
            space=space,
            account_id=int(account_id),
            msg=msg,
        )
        if code != 0 and not remote_already_missing:
            remote_err = msg or "删除账号失败"

    if remote_err:
        raise HTTPException(status_code=400, detail=f"远端账号删除失败，本地未物理删除：{remote_err}")

    # 先清空窗口引用，再物理删除账号记录。这里不设置 deleted=1。
    binding_cleared = await db.clear_window_platform_binding_by_account_global(account_id=int(account_id))
    account_affected = await db.hard_delete_platform_account_by_account_id(account_id=int(account_id))
    msg = "远端账号已不存在，已物理删除本地账号" if remote_already_missing else "账号已删除并同步到指纹浏览器，本地账号已物理删除"
    return {
        "success": True,
        "message": msg,
        "remote_deleted": (not remote_already_missing),
        "remote_already_missing": remote_already_missing,
        "hard_deleted": True,
        "account_affected": int(account_affected or 0),
        "binding_cleared": int(binding_cleared or 0),
    }


@router.post("/api/admin/spaces/{space_pk}/accounts/{account_id}/remark")
async def update_space_account_remark(
    space_pk: int,
    account_id: int,
    req: UpdateAccountRemarkRequest,
    token: str = Depends(verify_admin_token),
):
    """仅更新本地账号备注，不同步到指纹浏览器。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    if int(account_id) <= 0:
        raise HTTPException(status_code=400, detail="invalid account_id")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")

    row = await db.get_platform_account(space_pk=space_pk, account_id=int(account_id))
    if not row:
        raise HTTPException(status_code=404, detail="account not found")

    remark = str(req.platform_remarks or "").strip()
    affected = await db.update_platform_account_remark(
        space_pk=space_pk,
        account_id=int(account_id),
        remark=remark,
    )
    if affected <= 0:
        raise HTTPException(status_code=404, detail="account not found")
    return {"success": True, "message": "本地备注已更新", "affected": int(affected)}


@router.post("/api/admin/spaces/{space_pk}/accounts/{account_id}/password")
async def update_space_account_password(
    space_pk: int,
    account_id: int,
    req: UpdateAccountPasswordRequest,
    token: str = Depends(verify_admin_token),
):
    """????????????????????"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    if int(account_id) <= 0:
        raise HTTPException(status_code=400, detail="invalid account_id")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")

    row = await db.get_platform_account(space_pk=space_pk, account_id=int(account_id))
    if not row:
        raise HTTPException(status_code=404, detail="account not found")

    password = str(req.platform_password or "").strip()
    affected = await db.update_platform_account_password(
        space_pk=space_pk,
        account_id=int(account_id),
        password=password,
    )
    if affected <= 0:
        raise HTTPException(status_code=404, detail="account not found")
    return {"success": True, "message": "???????", "affected": int(affected)}


def _build_mdf_proxy_info(proxy_id: int) -> Dict[str, Any]:
    """按本地窗口 proxy_id 构造 /browser/mdf 的 proxyInfo。"""
    pid = int(proxy_id or 0)
    if pid > 0:
        return {"moduleId": pid, "proxyMethod": "choose"}
    return {"moduleId": 0, "proxyMethod": "custom", "proxyCategory": "noproxy"}


def _account_to_window_platform_payload(account: Any) -> Dict[str, Any]:
    """把本地 platform_accounts 记录转换为 Roxy windowPlatformList 项。"""
    return {
        "id": int(getattr(account, "account_id", 0) or 0),
        "platformUrl": str(getattr(account, "platform_url", "") or "").strip(),
        "platformUserName": str(getattr(account, "platform_username", "") or "").strip(),
        "platformPassword": str(getattr(account, "platform_password", "") or "").strip(),
        "platformEfa": str(getattr(account, "platform_efa", "") or "").strip(),
        "platformRemarks": str(getattr(account, "platform_remarks", "") or "").strip(),
    }


def _account_response_payload(account: Any) -> Optional[Dict[str, Any]]:
    if not account:
        return None
    return {
        "account_id": int(getattr(account, "account_id", 0) or 0),
        "platform_url": str(getattr(account, "platform_url", "") or "").strip(),
        "platform_username": str(getattr(account, "platform_username", "") or "").strip(),
        "platform_password": str(getattr(account, "platform_password", "") or "").strip(),
        "platform_efa": str(getattr(account, "platform_efa", "") or "").strip(),
        "platform_remarks": str(getattr(account, "platform_remarks", "") or "").strip(),
    }


def _same_account_url(a: str, b: str) -> bool:
    aa = str(a or "").strip().rstrip("/").lower()
    bb = str(b or "").strip().rstrip("/").lower()
    return (not aa) or (not bb) or aa == bb


def _is_leonardo_platform_url(value: Any) -> bool:
    host = _safe_hostname(str(value or ""))
    return host == "leonardo.ai" or host.endswith(".leonardo.ai")


def _is_leonardo_account(account: Any) -> bool:
    return bool(account) and _is_leonardo_platform_url(getattr(account, "platform_url", ""))


LEONARDO_LOGIN_URL = "https://app.leonardo.ai/auth/login"
LEONARDO_AUTH_COOKIE_NAMES = {
    "__Secure-better-auth.session_token",
}
LEONARDO_AUTH_COOKIE_PREFIXES = (
    "__Secure-better-auth.session_data",
    "better-auth.",
)
LEONARDO_CF_COOKIE_NAMES = {
    "CF_Access_Token",
    "cf_clearance",
    "__cf_bm",
    "__cflb",
    "__cfseq",
}
LEONARDO_CF_COOKIE_PREFIXES = (
    "cf_",
    "__cf",
)
LEONARDO_AUTH_STORAGE_HINTS = (
    "better-auth",
    "auth",
    "session",
    "user",
    "jwt",
    "token",
)


def _is_leonardo_cf_cookie_name(name: Any) -> bool:
    n = str(name or "").strip()
    nl = n.lower()
    if n in LEONARDO_CF_COOKIE_NAMES:
        return True
    return any(nl.startswith(prefix.lower()) for prefix in LEONARDO_CF_COOKIE_PREFIXES)


def _is_leonardo_auth_cookie_name(name: Any) -> bool:
    n = str(name or "").strip()
    nl = n.lower()
    if not n or _is_leonardo_cf_cookie_name(n):
        return False
    if n in LEONARDO_AUTH_COOKIE_NAMES:
        return True
    return any(nl.startswith(prefix.lower()) for prefix in LEONARDO_AUTH_COOKIE_PREFIXES)


async def _clear_leonardo_window_site_session(
    *,
    client: FPBrowserClient,
    browser: Any,
    space: Any,
    window_key: str,
) -> Dict[str, Any]:
    """Clear Leonardo site state before rebinding a fingerprint window to another account."""
    wk = str(window_key or "").strip()
    out: Dict[str, Any] = {
        "success": False,
        "window_key": wk,
        "mode": "",
        "closed_duplicate_pages": 0,
        "errors": [],
    }
    if not wk:
        out["errors"].append("missing window_key")
        return out

    conn = None
    try:
        conn = await client.get_open_window_connection_info(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            window_key=wk,
        )
    except Exception as e:
        out["errors"].append(f"connection_info failed: {e}")

    if conn:
        endpoint = str((conn or {}).get("http") or (conn or {}).get("ws") or "").strip()
        if endpoint and not endpoint.startswith(("http://", "https://", "ws://", "wss://")):
            endpoint = f"http://{endpoint}"
        try:
            from playwright.async_api import async_playwright  # type: ignore

            async with async_playwright() as pw:
                br = await pw.chromium.connect_over_cdp(endpoint)
                try:
                    ctx = br.contexts[0] if br.contexts else None
                    if ctx is None:
                        raise RuntimeError("no browser context")
                    leonardo_pages = [
                        p
                        for p in list(ctx.pages or [])
                        if "app.leonardo.ai" in str(getattr(p, "url", "") or "")
                    ]
                    for page in leonardo_pages[1:]:
                        try:
                            await page.close()
                            out["closed_duplicate_pages"] += 1
                        except Exception as e:
                            out["errors"].append(f"close duplicate page failed: {e}")
                    page = leonardo_pages[0] if leonardo_pages else (ctx.pages[0] if ctx.pages else await ctx.new_page())
                    try:
                        session = await ctx.new_cdp_session(page)
                        try:
                            await session.send("Network.enable")
                        except Exception:
                            pass
                        deleted_cookie_count = 0
                        try:
                            cookies = await ctx.cookies(
                                [
                                    "https://app.leonardo.ai",
                                    "https://leonardo.ai",
                                    "https://api.leonardo.ai",
                                    "https://auth.leonardo.ai",
                                ]
                            )
                        except Exception as e:
                            cookies = []
                            out["errors"].append(f"list cookies failed: {e}")
                        for cookie in cookies:
                            cname = str((cookie or {}).get("name") or "").strip()
                            if not _is_leonardo_auth_cookie_name(cname):
                                continue
                            try:
                                await session.send(
                                    "Network.deleteCookies",
                                    {
                                        "name": cname,
                                        "domain": str((cookie or {}).get("domain") or "").strip(),
                                        "path": str((cookie or {}).get("path") or "/").strip() or "/",
                                    },
                                )
                                deleted_cookie_count += 1
                            except Exception as e:
                                out["errors"].append(f"delete auth cookie failed: {cname}: {e}")
                        out["deleted_auth_cookies"] = deleted_cookie_count
                        for origin in ("https://app.leonardo.ai", "https://leonardo.ai"):
                            try:
                                await session.send(
                                    "Storage.clearDataForOrigin",
                                    {
                                        "origin": origin,
                                        "storageTypes": "local_storage,session_storage,indexeddb,cache_storage,service_workers",
                                    },
                                )
                            except Exception as e:
                                out["errors"].append(f"cdp app storage clear failed for {origin}: {e}")
                    except Exception as e:
                        out["errors"].append(f"cdp soft clear failed: {e}")
                    try:
                        await page.goto("about:blank", wait_until="domcontentloaded", timeout=10_000)
                    except Exception as e:
                        out["errors"].append(f"blank navigation failed: {e}")
                    try:
                        await page.goto(LEONARDO_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                        await page.evaluate(
                            """(hints) => {
                                const shouldDrop = (key, value) => {
                                    const text = String(key || "") + " " + String(value || "");
                                    const lower = text.toLowerCase();
                                    return hints.some((hint) => lower.includes(hint));
                                };
                                for (const store of [localStorage, sessionStorage]) {
                                    try {
                                        for (const key of Object.keys(store)) {
                                            const value = store.getItem(key);
                                            if (shouldDrop(key, value)) store.removeItem(key);
                                        }
                                    } catch (e) {}
                                }
                            }""",
                            list(LEONARDO_AUTH_STORAGE_HINTS),
                        )
                    except Exception as e:
                        out["errors"].append(f"page auth storage clear failed: {e}")
                    out["mode"] = "cdp_soft_auth_clear"
                    out["success"] = True
                finally:
                    try:
                        await br.close()
                    except Exception:
                        pass
        except Exception as e:
            out["errors"].append(f"cdp clear failed: {e}")

    if not out["success"]:
        out["mode"] = out["mode"] or "not_open"
        out["errors"].append("window is not open; skipped hard local-cache clear to preserve Cloudflare state")

    return out


async def _recover_leonardo_login_page(
    *,
    client: FPBrowserClient,
    browser: Any,
    window_key: str,
    wait_seconds: float = 3.0,
) -> Dict[str, Any]:
    """Reload the Leonardo login page when the visible Turnstile token has gone stale."""
    wk = str(window_key or "").strip()
    out: Dict[str, Any] = {
        "success": False,
        "window_key": wk,
        "mode": "login_page_reload",
        "detected_captcha_failure": False,
        "page_url": "",
        "title": "",
        "errors": [],
    }
    if not wk:
        out["errors"].append("missing window_key")
        return out

    try:
        conn = await client.get_open_window_connection_info(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            window_key=wk,
        )
    except Exception as e:
        out["errors"].append(f"connection_info failed: {e}")
        return out
    if not conn:
        out["errors"].append("window is not open")
        return out

    endpoint = str((conn or {}).get("http") or (conn or {}).get("ws") or "").strip()
    if endpoint and not endpoint.startswith(("http://", "https://", "ws://", "wss://")):
        endpoint = f"http://{endpoint}"
    try:
        from playwright.async_api import async_playwright  # type: ignore

        async with async_playwright() as pw:
            br = await pw.chromium.connect_over_cdp(endpoint)
            try:
                ctx = br.contexts[0] if br.contexts else None
                if ctx is None:
                    raise RuntimeError("no browser context")
                pages = list(ctx.pages or [])
                page = next((p for p in pages if "app.leonardo.ai" in str(getattr(p, "url", "") or "")), None)
                if page is None:
                    page = pages[0] if pages else await ctx.new_page()
                try:
                    body_text = await page.locator("body").inner_text(timeout=3000)
                except Exception:
                    body_text = ""
                lower = body_text.lower()
                out["detected_captcha_failure"] = (
                    "captcha verification failed" in lower
                    or "verify you are a human" in lower
                    or "please verify you are a human" in lower
                )
                try:
                    await page.goto("about:blank", wait_until="domcontentloaded", timeout=10_000)
                except Exception as e:
                    out["errors"].append(f"blank navigation failed: {e}")
                await page.goto(LEONARDO_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(max(0.0, float(wait_seconds or 0.0)))
                out["page_url"] = str(getattr(page, "url", "") or "")
                try:
                    out["title"] = str(await page.title() or "")
                except Exception:
                    out["title"] = ""
                out["success"] = True
            finally:
                try:
                    await br.close()
                except Exception:
                    pass
    except Exception as e:
        out["errors"].append(f"recover login page failed: {e}")
    return out


async def _leonardo_page_needs_manual_verification(page: Any) -> bool:
    try:
        challenge = page.locator(
            "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']"
        ).first
        if await challenge.is_visible(timeout=800):
            return True
    except Exception:
        pass
    try:
        body_text = await page.locator("body").inner_text(timeout=2500)
    except Exception:
        body_text = ""
    lower = str(body_text or "").lower()
    return any(
        hint in lower
        for hint in (
            "captcha verification failed",
            "verify you are a human",
            "please verify you are a human",
            "checking if the site connection is secure",
            "just a moment",
            "cf-ray",
        )
    )


async def _click_leonardo_button(page: Any, patterns: List[str], *, timeout_ms: int = 1500) -> bool:
    for pattern in patterns:
        try:
            btn = page.get_by_role("button", name=re.compile(pattern, re.I)).first
            if callable(btn):
                btn = btn()
            if await btn.is_visible(timeout=timeout_ms):
                await btn.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


async def _fill_first_visible_input(page: Any, selectors: List[str], value: str, *, timeout_ms: int = 5000) -> bool:
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if callable(loc):
                loc = loc()
            if await loc.is_visible(timeout=timeout_ms):
                await loc.fill(value, timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


async def _restart_and_login_leonardo_mapping(
    *,
    ctx_row: Dict[str, Any],
    headless: bool = False,
    wait_after_open_seconds: float = 3.0,
) -> Dict[str, Any]:
    """Restart a Leonardo fingerprint window and submit the normal email/password login form.

    This intentionally does not solve or bypass Cloudflare/Turnstile. If a manual
    verification screen is detected, the browser is left open and the result says so.
    """
    vendor = str(ctx_row.get("vendor") or "roxy").strip()
    base_url = str(ctx_row.get("lan_addr") or "").strip()
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "").strip()
    window_key = str(ctx_row.get("window_key") or "").strip()
    username = str(ctx_row.get("platform_username") or ctx_row.get("platform_account") or "").strip()
    password = str(ctx_row.get("platform_password") or "").strip()
    pure_mode = _effective_browser_pure_mode(ctx_row, None)

    out: Dict[str, Any] = {
        "success": False,
        "window_key": window_key,
        "closed": False,
        "reopened": False,
        "login_attempted": False,
        "login_submitted": False,
        "already_logged_in": False,
        "manual_verification_required": False,
        "missing_credentials": False,
        "page_url": "",
        "title": "",
        "errors": [],
    }
    if not base_url or not space_id or not window_key:
        out["errors"].append("mapping missing lan_addr/space_id/window_key")
        return out

    client = FPBrowserClient()
    try:
        await client.browser_close(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            window_key=window_key,
        )
        out["closed"] = True
    except Exception as e:
        out["errors"].append(f"browser_close failed: {e}")

    for _ in range(20):
        try:
            conn = await client.get_open_window_connection_info(
                vendor=vendor,
                base_url=base_url,
                access_key=access_key,
                window_key=window_key,
            )
            if not conn:
                break
        except Exception:
            break
        await asyncio.sleep(0.5)

    raw_endpoint = ""
    try:
        rsp = await client.browser_open(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            space_id=space_id,
            window_key=window_key,
            args=[],
            force_open=True,
            headless=bool(headless),
            pure_mode=bool(pure_mode),
        )
        if (rsp or {}).get("code") == 0:
            out["reopened"] = True
        data = (rsp or {}).get("data") or {}
        raw_endpoint = str(data.get("http") or data.get("ws") or "").strip()
    except Exception as e:
        out["errors"].append(f"browser_open failed: {e}")

    if not raw_endpoint:
        try:
            conn = await client.get_open_window_connection_info(
                vendor=vendor,
                base_url=base_url,
                access_key=access_key,
                window_key=window_key,
            )
            raw_endpoint = str((conn or {}).get("http") or (conn or {}).get("ws") or "").strip()
            if raw_endpoint:
                out["reopened"] = True
        except Exception as e:
            out["errors"].append(f"connection_info after open failed: {e}")
    if not raw_endpoint:
        out["errors"].append("missing CDP endpoint after browser_open")
        return out

    try:
        from playwright.async_api import async_playwright  # type: ignore
        from ..services.playwright_broswer_context import normalize_cdp_endpoint  # type: ignore

        endpoint = normalize_cdp_endpoint(raw_endpoint, base_url=base_url)
        if wait_after_open_seconds > 0:
            await asyncio.sleep(float(wait_after_open_seconds))
        async with async_playwright() as pw:
            br = await pw.chromium.connect_over_cdp(endpoint)
            try:
                ctx = br.contexts[0] if br.contexts else await br.new_context()
                pages = list(ctx.pages or [])
                page = next((p for p in pages if "leonardo.ai" in str(getattr(p, "url", "") or "")), None)
                page = page or (pages[0] if pages else await ctx.new_page())
                await page.goto(LEONARDO_LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
                await asyncio.sleep(2.0)

                if await _leonardo_page_needs_manual_verification(page):
                    out["manual_verification_required"] = True
                    return out

                out["page_url"] = str(getattr(page, "url", "") or "")
                try:
                    out["title"] = str(await page.title() or "")
                except Exception:
                    out["title"] = ""
                if "/auth/" not in out["page_url"]:
                    out["already_logged_in"] = True
                    out["success"] = True
                    return out

                if not username or not password:
                    out["missing_credentials"] = True
                    out["errors"].append("missing Leonardo username/password for this mapping")
                    return out

                out["login_attempted"] = True
                await _click_leonardo_button(page, [r"continue\s+with\s+email", r"email"], timeout_ms=1200)
                await asyncio.sleep(0.8)

                if await _leonardo_page_needs_manual_verification(page):
                    out["manual_verification_required"] = True
                    return out

                email_filled = await _fill_first_visible_input(
                    page,
                    [
                        "input[type='email']",
                        "input[name='email']",
                        "input[autocomplete='email']",
                        "input[placeholder*='@']",
                        "input[placeholder*='name']",
                    ],
                    username,
                    timeout_ms=7000,
                )
                if email_filled:
                    await _click_leonardo_button(page, [r"continue", r"next"], timeout_ms=2500)
                    await asyncio.sleep(1.5)

                if await _leonardo_page_needs_manual_verification(page):
                    out["manual_verification_required"] = True
                    return out

                password_filled = await _fill_first_visible_input(
                    page,
                    ["input[type='password']", "input[name='password']", "input[autocomplete='current-password']"],
                    password,
                    timeout_ms=12_000,
                )
                if not password_filled:
                    if await _leonardo_page_needs_manual_verification(page):
                        out["manual_verification_required"] = True
                    else:
                        out["errors"].append("password input not found")
                    return out

                if await _leonardo_page_needs_manual_verification(page):
                    out["manual_verification_required"] = True
                    return out

                clicked = await _click_leonardo_button(
                    page,
                    [r"log\s*in", r"sign\s*in", r"continue"],
                    timeout_ms=4000,
                )
                if not clicked:
                    try:
                        await page.keyboard.press("Enter")
                        clicked = True
                    except Exception as e:
                        out["errors"].append(f"submit login failed: {e}")
                out["login_submitted"] = bool(clicked)
                await asyncio.sleep(5.0)
                if await _leonardo_page_needs_manual_verification(page):
                    out["manual_verification_required"] = True

                out["page_url"] = str(getattr(page, "url", "") or "")
                try:
                    out["title"] = str(await page.title() or "")
                except Exception:
                    out["title"] = ""
                out["success"] = bool(out["login_submitted"]) and not bool(out["manual_verification_required"])
            finally:
                try:
                    await br.close()
                except Exception:
                    pass
    except Exception as e:
        out["errors"].append(f"restart login failed: {e}")

    return out


@router.post("/api/admin/task-type-windows/{mapping_id}/leonardo-recover-login")
async def recover_leonardo_mapping_login(mapping_id: int, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")
    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
    if handler != "leonardo_workflow":
        raise HTTPException(status_code=400, detail="mapping is not Leonardo")
    window_key = str(ctx_row.get("window_key") or "").strip()
    browser = SimpleNamespace(
        vendor=str(ctx_row.get("vendor") or "roxy"),
        lan_addr=str(ctx_row.get("lan_addr") or ""),
        access_key=ctx_row.get("access_key"),
    )
    client = FPBrowserClient()
    result = await _recover_leonardo_login_page(client=client, browser=browser, window_key=window_key)
    return result


@router.post("/api/admin/task-type-windows/{mapping_id}/leonardo-restart-login")
async def restart_leonardo_mapping_login(
    mapping_id: int,
    headless: bool = False,
    token: str = Depends(verify_admin_token),
):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")
    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
    if handler != "leonardo_workflow":
        raise HTTPException(status_code=400, detail="mapping is not Leonardo")
    return await _restart_and_login_leonardo_mapping(ctx_row=ctx_row, headless=headless)


async def _leonardo_cookie_probe_context(mapping_id: int) -> Dict[str, Any]:
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")
    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
    if handler != "leonardo_workflow":
        raise HTTPException(status_code=400, detail="mapping is not Leonardo")
    return ctx_row


@router.post("/api/admin/task-type-windows/{mapping_id}/leonardo-cookie-probe/capture")
async def capture_leonardo_cookie_probe(mapping_id: int, token: str = Depends(verify_admin_token)):
    ctx_row = await _leonardo_cookie_probe_context(mapping_id)
    try:
        from ..services.leonardo_cookie_probe import capture_and_probe_leonardo_cookie

        return await asyncio.wait_for(
            capture_and_probe_leonardo_cookie(mapping_id, ctx_row),
            timeout=120.0,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Leonardo cookie probe timed out") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/task-type-windows/{mapping_id}/leonardo-cookie-probe/retest")
async def retest_leonardo_cookie_probe(mapping_id: int, token: str = Depends(verify_admin_token)):
    await _leonardo_cookie_probe_context(mapping_id)
    try:
        from ..services.leonardo_cookie_probe import probe_saved_leonardo_cookie

        return await asyncio.wait_for(
            probe_saved_leonardo_cookie(mapping_id),
            timeout=90.0,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Leonardo saved-cookie probe timed out") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/task-type-windows/{mapping_id}/leonardo-cookie-probe/status")
async def leonardo_cookie_probe_status(mapping_id: int, token: str = Depends(verify_admin_token)):
    await _leonardo_cookie_probe_context(mapping_id)
    from ..services.leonardo_cookie_probe import get_leonardo_cookie_snapshot_status

    return await get_leonardo_cookie_snapshot_status(mapping_id)


@router.post("/api/admin/task-type-windows/{mapping_id}/leonardo-session-check")
async def check_leonardo_session_now(mapping_id: int, token: str = Depends(verify_admin_token)):
    """Run the production in-browser session check immediately and return only safe metadata."""
    await _leonardo_cookie_probe_context(mapping_id)
    from ..api.routes import task_service as _ts

    if _ts is None:
        raise HTTPException(status_code=503, detail="task service is not initialized")
    rows = await _ts._leonardo_keepalive_list_mapping_rows(include_disabled=True)
    row = next(
        (
            item
            for item in rows
            if int(item.get("mapping_id") or item.get("id") or 0) == int(mapping_id)
        ),
        None,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Leonardo mapping is unavailable")

    _ts._leonardo_keepalive_auth_failures.pop(int(mapping_id), None)
    _ts._leonardo_keepalive_next_due.pop(int(mapping_id), None)
    result = await asyncio.wait_for(_ts._leonardo_keepalive_probe_mapping(row), timeout=180.0)
    result = result if isinstance(result, dict) else {}
    cookie_state = result.get("cookie_state") if isinstance(result.get("cookie_state"), dict) else {}
    auth_session = result.get("auth_session") if isinstance(result.get("auth_session"), dict) else {}
    balance = result.get("balance") if isinstance(result.get("balance"), dict) else {}
    return {
        "success": bool(result.get("ok")),
        "mapping_id": int(mapping_id),
        "session_state": str(result.get("session_state") or "unknown"),
        "reason": str(result.get("reason") or ""),
        "page_url": str(result.get("page_url") or "")[:300],
        "cookie_state": {
            "cookie_count": int(cookie_state.get("cookie_count") or 0),
            "cf_access_token_present": bool(cookie_state.get("cf_access_token_present")),
            "cf_access_token_ttl_seconds": cookie_state.get("cf_access_token_ttl_seconds"),
            "better_auth_session_present": bool(cookie_state.get("better_auth_session_present")),
            "better_auth_session_count": int(cookie_state.get("better_auth_session_count") or 0),
            "better_auth_session_ttl_seconds": cookie_state.get("better_auth_session_ttl_seconds"),
            "better_auth_session_data_count": int(cookie_state.get("better_auth_session_data_count") or 0),
        },
        "auth_session": {
            "authenticated": auth_session.get("authenticated"),
            "authoritative": bool(auth_session.get("authoritative")),
            "http_status": int(auth_session.get("status") or 0),
            "cookie_cache_bypassed": bool(auth_session.get("cookie_cache_bypassed")),
            "session_expires_at": auth_session.get("session_expires_at"),
            "session_expires_in_seconds": auth_session.get("session_expires_in_seconds"),
            "session_updated_at": auth_session.get("session_updated_at"),
            "session_updated_age_seconds": auth_session.get("session_updated_age_seconds"),
            "session_created_at": auth_session.get("session_created_at"),
            "session_age_seconds": auth_session.get("session_age_seconds"),
        },
        "balance": {
            "remaining_quota": balance.get("remaining_quota"),
        },
    }


def _remote_account_row_id(row: Dict[str, Any]) -> int:
    if not isinstance(row, dict):
        return 0
    for key in ("id", "account_id", "accountId"):
        try:
            v = row.get(key)
            if v is not None and str(v).strip() != "":
                return int(v)
        except Exception:
            continue
    return 0


async def _remote_account_exists(
    client: FPBrowserClient,
    *,
    browser: Any,
    space: Any,
    account_id: int,
) -> Optional[bool]:
    """查询远端账号列表确认 account_id 是否存在。

    返回：
    - True：远端列表中存在；
    - False：远端列表可正常读取，但该 id 不存在；
    - None：列表读取失败，无法判断（此时不应把权限错误误判成本地脏数据）。
    """
    aid = int(account_id or 0)
    if aid <= 0:
        return False
    try:
        rows = await client.list_accounts(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
        )
    except Exception as e:
        logger.warning("verify remote account existence failed: account_id=%s err=%s", aid, e)
        return None
    for row in rows or []:
        if _remote_account_row_id(row) == aid:
            return True
    return False


async def _is_remote_account_already_missing(
    client: FPBrowserClient,
    *,
    browser: Any,
    space: Any,
    account_id: int,
    msg: str,
) -> bool:
    """判断 account/delete 的失败是否可视为“远端已不存在”。

    RoxyBrowser 在删除已不存在的账号 id 时可能返回“当前账号无该空间操作权限”。
    为避免把真正的 token/workspace 权限问题误删成本地成功，这类文案必须二次
    查询 account/list：只有列表可读且确实没有该 id，才按“远端已不存在”继续清理本地。
    """
    if _remote_msg_matches(msg, REMOTE_ACCOUNT_NOT_FOUND_HINTS):
        return True
    if not _remote_msg_matches(msg, REMOTE_ACCOUNT_MISSING_OR_NO_PERMISSION_HINTS):
        return False
    exists = await _remote_account_exists(client, browser=browser, space=space, account_id=account_id)
    return exists is False


async def _resolve_local_window_account(space_pk: int, target_win: Any) -> Optional[Any]:
    """从本地窗口绑定信息解析应写回指纹浏览器的账号。"""
    if not db or not target_win:
        return None

    account_id = int(getattr(target_win, "platform_account_id", 0) or 0)
    if account_id > 0:
        account = await db.get_platform_account(space_pk=space_pk, account_id=account_id)
        if account:
            return account

    username = str(getattr(target_win, "platform_account", "") or "").strip()
    if not username:
        return None

    account = await db.get_platform_account_by_username(space_pk=space_pk, username=username)
    if account:
        return account

    # 账号表会按 account_id 跨空间去重保存，历史数据可能不在当前 space_pk 下；
    # 再做一次全局账号库兜底匹配，避免本地窗口有账号名但查不到同空间账号。
    try:
        url = str(getattr(target_win, "platform_url", "") or "").strip()
        uname_norm = username.lower()
        for cand in await db.list_all_platform_accounts(include_deleted=False):
            cand_name = str(getattr(cand, "platform_username", "") or "").strip()
            if cand_name.lower() == uname_norm and _same_account_url(url, str(getattr(cand, "platform_url", "") or "")):
                return cand
    except Exception:
        return None
    return None


async def _mdf_window_account_and_proxy_from_local(
    *,
    space_pk: int,
    window_key: str,
    selected_account: Optional[Any] = None,
    clear_account: bool = False,
    require_local_account: bool = False,
) -> Dict[str, Any]:
    """把账号 + 本地 proxy_id 一起写入指纹浏览器窗口。

    - selected_account 传入时用于“切换账号”；
    - selected_account 为空且 clear_account=False 时，从本地窗口当前绑定解析账号，用于“同步当前账号”；
    - proxyInfo 始终从本地 windows.proxy_id 构造，避免只改账号时指纹浏览器侧代理被清空。
    """
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    wk = str(window_key or "").strip()
    if not wk:
        raise HTTPException(status_code=400, detail="window_key is required")

    target_win = await db.get_window_by_key(space_pk=space_pk, window_key=wk)
    if not target_win:
        raise HTTPException(status_code=404, detail="window not found")

    selected = selected_account
    if selected is None and not clear_account:
        selected = await _resolve_local_window_account(space_pk, target_win)
    if require_local_account and not selected:
        local_account_id = int(getattr(target_win, "platform_account_id", 0) or 0)
        local_account = str(getattr(target_win, "platform_account", "") or "").strip()
        if local_account_id > 0:
            raise HTTPException(status_code=404, detail=f"本地窗口绑定了账号#{local_account_id}，但本地账号库中未找到该账号")
        raise HTTPException(status_code=400, detail=f"本地窗口未绑定账号，无法同步（当前账号：{local_account or '-'}）")

    local_proxy_id = int(getattr(target_win, "proxy_id", 0) or 0) if target_win else 0

    if selected is not None:
        expected_kind = await _expected_account_platform_kind_for_space(space, browser)
        selected_kind = _account_platform_kind_from_url(str(getattr(selected, "platform_url", "") or ""))
        if expected_kind and selected_kind != expected_kind:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"account platform mismatch: this space expects {expected_kind}, "
                    f"but selected account is {selected_kind or 'unknown'} "
                    f"({str(getattr(selected, 'platform_url', '') or '-')})"
                ),
            )

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    try:
        mdf_payload: Dict[str, Any] = {
            "proxyInfo": _build_mdf_proxy_info(local_proxy_id),
            "windowPlatformList": [_account_to_window_platform_payload(selected)] if selected else [],
        }

        rsp = await client.browser_mdf(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
            window_key=wk,
            data=mdf_payload,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if _remote_response_code(rsp) != 0:
        raise HTTPException(status_code=400, detail=_remote_response_msg(rsp, "指纹浏览器修改窗口账号失败"))

    await db.update_window_platform_binding(
        space_pk=space_pk,
        window_key=wk,
        platform_account_id=(int(selected.account_id) if selected else None),
        platform_account=(selected.platform_username if selected else None),
        platform_url=(selected.platform_url if selected else None),
    )

    # 顺手用本地代理库回填 proxy_addr/proxy_country，保持 UI 代理信息与本地 proxy_id 一致。
    try:
        await db.update_window_proxy_id(space_pk=space_pk, window_key=wk, proxy_id=local_proxy_id)
    except Exception:
        pass
    updated = await db.get_window_by_key(space_pk=space_pk, window_key=wk)

    account_payload = _account_response_payload(selected)
    return {
        "success": True,
        "message": "已把本地账号和代理信息同步到指纹浏览器窗口" if require_local_account else "已提交修改窗口账号请求",
        "account_id": int(getattr(selected, "account_id", 0) or 0) if selected else 0,
        "platform_account": str(getattr(selected, "platform_username", "") or "").strip() if selected else "",
        "platform_url": str(getattr(selected, "platform_url", "") or "").strip() if selected else "",
        "account": account_payload,
        "proxy_id": int(local_proxy_id or 0),
        "proxy_addr": (str(getattr(updated, "proxy_addr", "") or "").strip() or None) if updated else None,
        "proxy_country": (str(getattr(updated, "proxy_country", "") or "").strip() or None) if updated else None,
        "roxy_response": rsp,
    }


@router.post("/api/admin/spaces/{space_pk}/windows/{window_key}/set-account")
async def set_window_account(
    space_pk: int,
    window_key: str,
    req: UpdateWindowAccountRequest,
    token: str = Depends(verify_admin_token),
):
    """将窗口账号改为本地账号列表中的某个 account_id（实际调用 /browser/mdf）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    account_id = int(req.account_id or 0)
    selected = None
    if account_id > 0:
        selected = await db.get_platform_account(space_pk=space_pk, account_id=account_id)
        if not selected:
            raise HTTPException(status_code=404, detail="account not found")

    result = await _mdf_window_account_and_proxy_from_local(
        space_pk=space_pk,
        window_key=window_key,
        selected_account=selected,
        clear_account=(account_id <= 0),
        require_local_account=False,
    )
    if selected is not None and _is_leonardo_account(selected):
        space = await db.get_space(space_pk)
        browser = await db.get_browser(space.browser_id) if space else None
        if space and browser:
            client = FPBrowserClient()
            result["clear_session_result"] = await _clear_leonardo_window_site_session(
                client=client,
                browser=browser,
                space=space,
                window_key=window_key,
            )
    return result


@router.post("/api/admin/spaces/{space_pk}/windows/{window_key}/sync-account")
async def sync_window_account_from_local(
    space_pk: int,
    window_key: str,
    token: str = Depends(verify_admin_token),
):
    """把本地 DB 中该窗口当前绑定的账号重新写回指纹浏览器窗口（同时带上本地代理配置）。"""
    return await _mdf_window_account_and_proxy_from_local(
        space_pk=space_pk,
        window_key=window_key,
        selected_account=None,
        clear_account=False,
        require_local_account=True,
    )


@router.post("/api/admin/spaces/{space_pk}/windows/{window_key}/set-proxy")
async def set_window_proxy(space_pk: int, window_key: str, req: UpdateWindowProxyRequest, token: str = Depends(verify_admin_token)):
    """将某个窗口的代理配置改为本地代理列表中的某个 proxy_id（实际调用 /browser/mdf）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    user = await _get_user_by_token(token)
    if not await _user_can_switch_proxy(user):
        raise HTTPException(status_code=403, detail="当前用户未开启切换代理权限")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    wk = str(window_key or "").strip()
    if not wk:
        raise HTTPException(status_code=400, detail="window_key is required")

    # 代理配置：
    # - proxy_id=0：不使用代理（按 Roxy 约定 moduleId=0 + noproxy）
    # - proxy_id>0：优先使用 choose + moduleId（由指纹浏览器从代理库绑定）
    proxy_id = int(req.proxy_id or 0)
    if proxy_id <= 0:
        proxy_info = {"moduleId": 0, "proxyMethod": "custom", "proxyCategory": "noproxy"}
    else:
        proxy_info = {"moduleId": proxy_id, "proxyMethod": "choose"}

    # 从本地 DB 读取窗口当前绑定的账号，直接构造 windowPlatformList，
    # 避免额外调用 get_browser_detail（省掉一次 Roxy API 往返）。
    target_win = await db.get_window_by_key(space_pk=space_pk, window_key=wk)
    local_account_id = int(getattr(target_win, "platform_account_id", 0) or 0) if target_win else 0
    wpl: list = []
    if local_account_id > 0:
        acct = await db.get_platform_account(space_pk=space_pk, account_id=local_account_id)
        if acct:
            wpl = [{
                "id": int(acct.account_id),
                "platformUrl": str(acct.platform_url or "").strip(),
                "platformUserName": str(acct.platform_username or "").strip(),
                "platformPassword": str(acct.platform_password or "").strip(),
                "platformEfa": str(acct.platform_efa or "").strip(),
                "platformRemarks": str(acct.platform_remarks or "").strip(),
            }]

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    try:
        mdf_payload: Dict[str, Any] = {"proxyInfo": proxy_info}
        if wpl:
            mdf_payload["windowPlatformList"] = wpl

        rsp = await client.browser_mdf(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
            window_key=wk,
            data=mdf_payload,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 同步更新本地 DB，确保 UI “当前代理/绑定数” 立即生效
    try:
        await db.update_window_proxy_id(space_pk=space_pk, window_key=wk, proxy_id=proxy_id)
    except Exception:
        pass

    return {"success": True, "message": "已提交修改窗口代理请求", "roxy_response": rsp}


@router.post("/api/admin/spaces/{space_pk}/windows/{window_key}/set-core-version")
async def set_window_core_version(
    space_pk: int,
    window_key: str,
    req: SetWindowCoreVersionRequest,
    token: str = Depends(verify_admin_token),
):
    """修改指纹浏览器窗口内核版本（Roxy：/browser/mdf 的 coreVersion）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    wk = str(window_key or "").strip()
    if not wk:
        raise HTTPException(status_code=400, detail="window_key is required")

    core_ver = str(req.core_version or "").strip()
    if not core_ver:
        raise HTTPException(status_code=400, detail="core_version is required")

    target_win = await db.get_window_by_key(space_pk=space_pk, window_key=wk)
    local_proxy_id = int(getattr(target_win, "proxy_id", 0) or 0) if target_win else 0
    if local_proxy_id > 0:
        proxy_info: Dict[str, Any] = {"moduleId": local_proxy_id, "proxyMethod": "choose"}
    else:
        proxy_info = {"moduleId": 0, "proxyMethod": "custom", "proxyCategory": "noproxy"}

    local_account_id = int(getattr(target_win, "platform_account_id", 0) or 0) if target_win else 0
    wpl: list = []
    if local_account_id > 0:
        acct = await db.get_platform_account(space_pk=space_pk, account_id=local_account_id)
        if acct:
            wpl = [{
                "id": int(acct.account_id),
                "platformUrl": str(acct.platform_url or "").strip(),
                "platformUserName": str(acct.platform_username or "").strip(),
                "platformPassword": str(acct.platform_password or "").strip(),
                "platformEfa": str(acct.platform_efa or "").strip(),
                "platformRemarks": str(acct.platform_remarks or "").strip(),
            }]

    mdf_payload: Dict[str, Any] = {"proxyInfo": proxy_info, "coreVersion": core_ver}
    if wpl:
        mdf_payload["windowPlatformList"] = wpl

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    try:
        rsp = await client.browser_mdf(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
            window_key=wk,
            data=mdf_payload,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        code = int((rsp or {}).get("code"))
    except (TypeError, ValueError):
        code = -1
    if code != 0:
        raise HTTPException(status_code=400, detail=str((rsp or {}).get("msg") or "指纹浏览器修改内核版本失败"))

    try:
        await db.update_window_raw_core_version(space_pk=space_pk, window_key=wk, core_version=core_ver)
    except Exception:
        pass

    return {"success": True, "message": "已修改窗口内核版本", "roxy_response": rsp}


@router.post("/api/admin/spaces/{space_pk}/windows/{window_key}/set-pure-mode")
async def set_window_pure_mode(
    space_pk: int,
    window_key: str,
    req: SetPureModeRequest,
    token: str = Depends(verify_admin_token),
):
    """切换纯净模式：设置窗口的 platformUrl 和 openWorkbench（实际调用 /browser/mdf）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    wk = str(window_key or "").strip()
    if not wk:
        raise HTTPException(status_code=400, detail="window_key is required")

    target_win = await db.get_window_by_key(space_pk=space_pk, window_key=wk)

    # 构造 proxyInfo
    local_proxy_id = int(getattr(target_win, "proxy_id", 0) or 0) if target_win else 0
    if local_proxy_id > 0:
        proxy_info: Dict[str, Any] = {"moduleId": local_proxy_id, "proxyMethod": "choose"}
    else:
        proxy_info = {"moduleId": 0, "proxyMethod": "custom", "proxyCategory": "noproxy"}

    # 构造 windowPlatformList
    local_account_id = int(getattr(target_win, "platform_account_id", 0) or 0) if target_win else 0
    wpl: list = []
    acct = None
    if local_account_id > 0:
        acct = await db.get_platform_account(space_pk=space_pk, account_id=local_account_id)
    elif target_win:
        pa = str(getattr(target_win, "platform_account", "") or "").strip()
        if pa:
            acct = await db.get_platform_account_by_username(space_pk=space_pk, username=pa)
    if acct and not req.pure_mode:
        wpl = [{
            "id": int(acct.account_id),
            "platformUrl": "https://accounts.google.com/",
            "platformUserName": str(acct.platform_username or "").strip(),
            "platformPassword": str(acct.platform_password or "").strip(),
            "platformEfa": str(acct.platform_efa or "").strip(),
            "platformRemarks": str(acct.platform_remarks or "").strip(),
        }]
    openWorkbench = 0 if req.pure_mode else 1
    mdf_payload: Dict[str, Any] = {
        "proxyInfo": proxy_info,
        "fingerInfo": {"openWorkbench": openWorkbench},
    }
    if wpl:
        mdf_payload["windowPlatformList"] = wpl

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    try:
        rsp = await client.browser_mdf(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
            window_key=wk,
            data=mdf_payload,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"success": True, "message": f"已{'开启' if req.pure_mode else '关闭'}纯净模式", "roxy_response": rsp}


@router.post("/api/admin/spaces/{space_pk}/windows/{window_key}/sync-proxy-addr")
async def sync_window_proxy_addr_local(space_pk: int, window_key: str, token: str = Depends(verify_admin_token)):
    """按窗口当前绑定的 proxy_id 回填本地 proxy_addr/proxy_country。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")

    wk = str(window_key or "").strip()
    if not wk:
        raise HTTPException(status_code=400, detail="window_key is required")

    windows = await db.list_windows(space_pk)
    target = next((w for w in (windows or []) if str(getattr(w, "window_key", "") or "").strip() == wk), None)
    if not target:
        raise HTTPException(status_code=404, detail="window not found")

    proxy_id = int(getattr(target, "proxy_id", 0) or 0)
    affected = await db.update_window_proxy_id(space_pk=int(space_pk), window_key=wk, proxy_id=proxy_id)
    if affected <= 0:
        raise HTTPException(status_code=404, detail="window not found or already deleted")

    updated_windows = await db.list_windows(space_pk)
    updated = next((w for w in (updated_windows or []) if str(getattr(w, "window_key", "") or "").strip() == wk), None)
    return {
        "success": True,
        "message": "窗口代理信息已同步到本地",
        "affected": int(affected or 0),
        "proxy_id": proxy_id,
        "proxy_addr": (str(getattr(updated, "proxy_addr", "") or "").strip() or None) if updated else None,
        "proxy_country": (str(getattr(updated, "proxy_country", "") or "").strip() or None) if updated else None,
    }


@router.post("/api/admin/spaces/{space_pk}/sync-windows")
async def sync_windows(space_pk: int, token: str = Depends(verify_admin_token)):
    """同步某个空间的窗口信息（从指纹浏览器拉取后写入本地 DB）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")

    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    try:
        windows = await client.list_windows(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
            project_ids=getattr(space, "project_ids", None),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db.upsert_windows(space_pk=space_pk, windows=windows)
    affected = result["affected"]
    skipped = result["skipped"]
    skipped_keys = result["skipped_keys"]
    msg = f"同步完成，写入/更新 {affected} 条窗口记录"
    if skipped > 0:
        msg += f"，跳过 {skipped} 个跨项目空间窗口"
    return {
        "success": True,
        "message": msg,
        "affected": affected,
        "skipped": skipped,
        "skipped_keys": skipped_keys,
    }


@router.post("/api/admin/spaces/{space_pk}/sync-window-status")
async def sync_window_status(space_pk: int, token: str = Depends(verify_admin_token)):
    """同步某个空间下本地窗口的“打开状态”（1=打开，0=未打开）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")

    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    local_windows = await db.list_windows(space_pk)
    window_keys = [str(w.window_key or "").strip() for w in local_windows if str(w.window_key or "").strip()]

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    try:
        opened = await client.list_open_window_connection_infos(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            window_keys=window_keys,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"查询浏览器窗口状态失败: {e}")

    open_keys: List[str] = []
    for row in opened:
        did = str((row or {}).get("dirId") or "").strip()
        if did:
            open_keys.append(did)

    affected = await db.sync_window_statuses(space_pk=space_pk, open_window_keys=open_keys)
    return {
        "success": True,
        "message": f"状态同步完成：{len(open_keys)} 个打开 / {len(window_keys)} 个窗口",
        "affected": affected,
        "open_count": len(open_keys),
        "total": len(window_keys),
    }


@router.post("/api/admin/spaces/{space_pk}/windows/{window_key}/delete")
async def delete_window_local(space_pk: int, window_key: str, req: DeleteWindowRequest = DeleteWindowRequest(), token: str = Depends(verify_admin_token)):
    """删除窗口。

    工程约束：
    - 如果勾选“同时删除指纹浏览器窗口”，必须先删远端窗口，成功后才删本地 DB。
    - 如果同时勾选“删除绑定账号”，远端窗口删除后再删远端账号，最后一次性清理本地 DB。
      这样避免“先清本地绑定/账号，导致远端失败后状态不一致”，也避免账号仍被远端窗口绑定时删除账号失败。
    """
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")

    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    wk = str(window_key or "").strip()
    if not wk:
        raise HTTPException(status_code=400, detail="window_key is required")

    local_window = await db.get_window_by_key(space_pk=space_pk, window_key=wk)

    delete_account = bool(req.delete_account)
    account_id = int(req.account_id or 0)
    local_account = None
    if delete_account:
        if account_id <= 0 and local_window is not None:
            try:
                account_id = int(getattr(local_window, "platform_account_id", 0) or 0)
            except Exception:
                account_id = 0
        if account_id <= 0 and local_window is not None:
            username = str(getattr(local_window, "platform_account", "") or "").strip()
            if username:
                local_account = await db.get_platform_account_by_username(space_pk=space_pk, username=username)
                if local_account:
                    account_id = int(getattr(local_account, "account_id", 0) or 0)
        if account_id <= 0:
            raise HTTPException(status_code=400, detail="已勾选删除绑定账号，但未找到有效 account_id")
        if local_account is None:
            local_account = await db.get_platform_account(space_pk=space_pk, account_id=account_id)

    window_rsp: Optional[Dict[str, Any]] = None
    account_rsp: Optional[Dict[str, Any]] = None
    remote_window_already_missing = False
    remote_account_already_missing = False

    syscfg = None
    client = None
    if req.delete_remote or delete_account:
        syscfg = await db.get_system_config()
        client = FPBrowserClient()

    if req.delete_remote:
        try:
            window_rsp = await client.delete_windows(
                vendor=browser.vendor,
                base_url=browser.lan_addr,
                access_key=browser.access_key,
                space_id=space.space_id,
                window_keys=[wk],
                is_soft_deleted=False,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"指纹浏览器删除窗口失败，本地未删除：{str(e) or '未知错误'}")

        window_code = _remote_response_code(window_rsp)
        window_msg = _remote_response_msg(window_rsp)
        remote_window_already_missing = window_code != 0 and _remote_msg_matches(window_msg, REMOTE_WINDOW_NOT_FOUND_HINTS)
        if window_code != 0 and not remote_window_already_missing:
            raise HTTPException(status_code=400, detail=f"指纹浏览器删除窗口失败，本地未删除：{window_msg or '未知错误'}")

    # 账号删除放在远端窗口删除之后：避免账号仍被远端窗口绑定时 account/delete 失败。
    if delete_account:
        assert client is not None
        account_code = -1
        account_msg = ""
        last_account_exc = ""
        for attempt in range(3):
            try:
                account_rsp = await client.delete_accounts(
                    vendor=browser.vendor,
                    base_url=browser.lan_addr,
                    access_key=browser.access_key,
                    space_id=space.space_id,
                    account_ids=[int(account_id)],
                )
                account_code = _remote_response_code(account_rsp)
                account_msg = _remote_response_msg(account_rsp)
                remote_account_already_missing = account_code != 0 and await _is_remote_account_already_missing(
                    client,
                    browser=browser,
                    space=space,
                    account_id=int(account_id),
                    msg=account_msg,
                )
                if account_code == 0 or remote_account_already_missing:
                    break
                last_account_exc = account_msg or "未知错误"
            except Exception as e:
                last_account_exc = str(e) or "未知错误"

            # Roxy 删除窗口后，账号绑定释放可能有极短延迟；这里短重试，避免用户必须手动点第二次。
            if attempt < 2:
                await asyncio.sleep(0.6 * (attempt + 1))

        if account_code != 0 and not remote_account_already_missing:
            raise HTTPException(status_code=400, detail=f"指纹浏览器删除账号失败，本地未删除：{last_account_exc or account_msg or '未知错误'}")

    # 到这里说明所有需要的远端动作已完成或远端对象本来就不存在，再开始本地 DB 清理。
    account_affected = 0
    account_binding_cleared = 0
    if delete_account:
        account_space_pk = int(getattr(local_account, "space_pk", 0) or space_pk)
        reason = str(req.account_delete_reason or "").strip()
        if reason:
            old_remark = str(getattr(local_account, "platform_remarks", "") or "")
            window_sort_num = getattr(local_window, "window_sort_num", None) if local_window is not None else None
            window_name = str(getattr(local_window, "window_name", "") or wk) if local_window is not None else wk
            merged_remark = _build_delete_account_remark(
                old_remark,
                reason,
                window_name=window_name,
                window_key=wk,
                window_sort_num=window_sort_num,
            )
            if merged_remark:
                await db.update_platform_account_remark(
                    space_pk=account_space_pk,
                    account_id=int(account_id),
                    remark=merged_remark,
                )
        account_affected = await db.delete_platform_account_by_account_id(account_id=int(account_id))
        account_binding_cleared = await db.clear_window_platform_binding_by_account_global(account_id=int(account_id))

    affected = await db.delete_window_by_key(space_pk=space_pk, window_key=wk)
    window_affected = int((affected or {}).get("window_affected") or 0)
    task_type_window_affected = int((affected or {}).get("task_type_window_affected") or 0)

    if not req.delete_remote:
        final_message = "窗口已删除（仅本地）"
    elif remote_window_already_missing:
        final_message = "远端窗口已不存在，已完成本地删除"
    else:
        final_message = "窗口已删除（远程 + 本地）"
    if delete_account:
        if remote_account_already_missing:
            final_message += "；远端账号已不存在，已完成本地账号删除"
        else:
            final_message += "；绑定账号已删除（远程 + 本地）"
    final_message += "，并级联标记 task_type_window 删除"

    if window_affected <= 0:
        final_message = "本地窗口已不存在或已删除" if not req.delete_remote else "远端删除流程已完成；本地窗口已不存在或已删除"
        if delete_account:
            final_message += "；本地账号清理已执行"

    return {
        "success": True,
        "message": final_message,
        "affected": window_affected,
        "task_type_window_affected": task_type_window_affected,
        "delete_account": delete_account,
        "account_id": account_id if delete_account else None,
        "account_affected": account_affected,
        "account_binding_cleared": account_binding_cleared,
        "remote_window_deleted": bool(req.delete_remote and not remote_window_already_missing),
        "remote_window_already_missing": remote_window_already_missing,
        "remote_account_deleted": bool(delete_account and not remote_account_already_missing),
        "remote_account_already_missing": remote_account_already_missing,
        "roxy_response": window_rsp,
        "account_roxy_response": account_rsp,
    }


@router.post("/api/admin/spaces/{space_pk}/windows/{window_key}/move")
async def move_window_local(space_pk: int, window_key: str, req: MoveWindowRequest, token: str = Depends(verify_admin_token)):
    """仅本地转移窗口到另一个空间（不调用指纹浏览器接口）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    source_space = await db.get_space(space_pk)
    if not source_space:
        raise HTTPException(status_code=404, detail="source space not found")

    target_space_pk = int(req.target_space_pk or 0)
    if target_space_pk <= 0:
        raise HTTPException(status_code=400, detail="target_space_pk is required")
    if target_space_pk == int(space_pk):
        raise HTTPException(status_code=400, detail="目标空间不能与源空间相同")

    target_space = await db.get_space(target_space_pk)
    if not target_space:
        raise HTTPException(status_code=404, detail="target space not found")

    wk = str(window_key or "").strip()
    if not wk:
        raise HTTPException(status_code=400, detail="window_key is required")

    try:
        affected = await db.move_window_to_space(
            source_space_pk=int(space_pk),
            target_space_pk=target_space_pk,
            window_key=wk,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="move window failed")

    if affected <= 0:
        raise HTTPException(status_code=404, detail="window not found or already deleted")
    return {
        "success": True,
        "message": f"窗口已转移到目标空间（space_pk={target_space_pk}）",
        "affected": affected,
        "source_space_pk": int(space_pk),
        "target_space_pk": target_space_pk,
    }


@router.post("/api/admin/spaces/{space_pk}/windows/{window_key}/remark")
async def update_window_remark_local(
    space_pk: int,
    window_key: str,
    req: UpdateWindowRemarkRequest,
    token: str = Depends(verify_admin_token),
):
    """仅本地更新窗口备注（不调用指纹浏览器接口）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")

    wk = str(window_key or "").strip()
    if not wk:
        raise HTTPException(status_code=400, detail="window_key is required")

    remark = str(req.window_remark or "").strip()
    affected = await db.update_window_remark(space_pk=int(space_pk), window_key=wk, remark=remark)
    if affected <= 0:
        raise HTTPException(status_code=404, detail="window not found or already deleted")
    return {"success": True, "message": "窗口备注已更新", "affected": affected, "window_remark": remark}


@router.get("/api/admin/browsers/{browser_id}/workspace-projects")
async def get_browser_workspace_projects(browser_id: int, token: str = Depends(verify_admin_token)):
    """读取指纹浏览器的“空间 + 项目列表”（只读展示，不落库）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    browser = await db.get_browser(browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    syscfg = await db.get_system_config()
    client = FPBrowserClient()

    try:
        rows = await client.list_workspace_projects(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"success": True, "browser_id": browser_id, "rows": rows}


@router.get("/api/admin/spaces/{space_pk}/proxy-detect-channels")
async def get_space_proxy_detect_channels(space_pk: int, token: str = Depends(verify_admin_token)):
    """读取指纹浏览器的代理检测渠道（只读展示，不落库）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    space = await db.get_space(space_pk)
    if not space:
        raise HTTPException(status_code=404, detail="space not found")
    browser = await db.get_browser(space.browser_id)
    if not browser:
        raise HTTPException(status_code=404, detail="browser not found")

    syscfg = await db.get_system_config()
    client = FPBrowserClient()

    try:
        rows = await client.list_proxy_detect_channels(
            vendor=browser.vendor,
            base_url=browser.lan_addr,
            access_key=browser.access_key,
            space_id=space.space_id,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"读取代理检测渠道失败：{e.__class__.__name__}: {e}")

    return {"success": True, "space_pk": space_pk, "browser_id": browser.id, "rows": rows}


@router.get("/api/admin/tree")
async def get_project_tree(project_id: Optional[int] = None, token: str = Depends(verify_admin_token)):
    """返回树形结构（项目 -> 浏览器 -> 空间 -> 窗口列表）。

    UI 侧可据此实现：项目切换、浏览器折叠/展开、空间加载窗口、以及“一键展开全部”。
    """
    # 仅要求已登录；不再强依赖 projects 页面权限。
    # 仍然会按项目权限（allowed_project_ids）过滤可见数据。
    user = await _get_user_by_token(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    tree = await db.get_project_tree(
        project_id=project_id,
        allowed_project_ids=None,
    )

    return {"success": True, "tree": tree}


@router.get("/api/admin/windows/all")
async def list_all_windows(project_id: Optional[int] = None, token: str = Depends(verify_admin_token)):
    """用于“选择窗口绑定任务类型”的弹窗数据源。"""
    user = await _ensure_page_access(token, "task_types")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    allowed_ids = await _get_allowed_project_ids(user)
    tree = await db.get_project_tree(
        project_id=project_id,
        allowed_project_ids=allowed_ids,
    )

    flat: List[Dict[str, Any]] = []
    for p in tree:
        for b in p.get("browsers", []):
            for s in b.get("spaces", []):
                for w in s.get("windows", []):
                    flat.append(
                        {
                            "project_id": p.get("id"),
                            "project_name": p.get("name"),
                            "browser_id": b.get("id"),
                            "browser_name": b.get("name"),
                            "space_pk": s.get("id"),
                            "space_id": s.get("space_id"),
                            "space_name": s.get("name"),
                            "window_pk": w.get("id"),
                            "window_sort_num": w.get("window_sort_num"),
                            "window_name": w.get("window_name"),
                            "window_status": w.get("window_status", 0),
                            "window_remark": w.get("window_remark"),
                            "platform_account": w.get("platform_account"),
                            "platform_url": w.get("platform_url"),
                            "proxy_addr": w.get("proxy_addr"),
                            "proxy_country": w.get("proxy_country"),
                            "proxy_expire_at": w.get("proxy_expire_at"),
                        }
                    )
    return {"success": True, "windows": flat}


# -------------------- card keys --------------------
@router.get("/api/admin/card-keys")
async def list_card_keys(token: str = Depends(verify_admin_token)):
    await _ensure_any_page_access(token, {"card_keys", "test"})
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {"success": True, "items": [x.model_dump() for x in await db.list_card_keys()]}


@router.post("/api/admin/card-keys/import")
async def import_card_keys(req: ImportCardKeysRequest, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    result = await db.batch_import_card_keys(req.content)
    return {"success": True, **result}


@router.put("/api/admin/card-keys/{card_key_id}")
async def update_card_key(card_key_id: int, req: UpdateCardKeyRequest, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    try:
        affected = await db.update_card_key(card_key_id=card_key_id, card_key=req.card_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if affected <= 0:
        raise HTTPException(status_code=404, detail="card key not found")
    return {"success": True}


@router.delete("/api/admin/card-keys/{card_key_id}")
async def delete_card_key(card_key_id: int, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    affected = await db.delete_card_key(card_key_id=card_key_id)
    if affected <= 0:
        raise HTTPException(status_code=404, detail="card key not found")
    return {"success": True}


@router.post("/api/admin/card-keys/batch-delete")
async def batch_delete_card_keys(req: BatchDeleteCardKeysRequest, token: str = Depends(verify_admin_token)):
    await _ensure_any_page_access(token, {"card_keys", "test"})
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    affected = await db.batch_delete_card_keys(req.ids)
    return {"success": True, "deleted": affected}


# -------------------- PayPal accounts/materials --------------------
@router.get("/api/admin/paypal-accounts")
async def list_paypal_accounts(
    q: str = "",
    include_deleted: bool = False,
    limit: int = 200,
    offset: int = 0,
    token: str = Depends(verify_admin_token),
):
    await _ensure_page_access(token, "paypal")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {
        "success": True,
        **await db.list_paypal_accounts(q=q, include_deleted=include_deleted, limit=limit, offset=offset),
    }


@router.post("/api/admin/paypal-accounts/import")
async def import_paypal_accounts(req: ImportPaypalAccountsRequest, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "paypal")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    result = await db.batch_import_paypal_accounts(req.content)
    return {"success": True, **result}


@router.post("/api/admin/paypal-accounts")
async def create_paypal_account(req: UpsertPaypalAccountRequest, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "paypal")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    try:
        account_id = await db.upsert_paypal_account(req.model_dump())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "id": account_id}


@router.put("/api/admin/paypal-accounts/{account_id}")
async def update_paypal_account(account_id: int, req: UpsertPaypalAccountRequest, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "paypal")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    try:
        updated_id = await db.upsert_paypal_account(req.model_dump(), account_id=account_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "id": updated_id}


@router.delete("/api/admin/paypal-accounts/{account_id}")
async def delete_paypal_account(
    account_id: int,
    hard_delete: bool = False,
    token: str = Depends(verify_admin_token),
):
    await _ensure_page_access(token, "paypal")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    affected = await db.delete_paypal_account(account_id, hard_delete=hard_delete)
    if affected <= 0:
        raise HTTPException(status_code=404, detail="PayPal 资料不存在或已删除")
    return {"success": True, "deleted": affected}


@router.post("/api/admin/paypal-accounts/batch-delete")
async def batch_delete_paypal_accounts(req: BatchDeletePaypalAccountsRequest, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "paypal")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    affected = await db.batch_delete_paypal_accounts(req.ids, hard_delete=req.hard_delete)
    return {"success": True, "deleted": affected}


@router.get("/api/admin/paypal-accounts/{account_id}/sms")
async def fetch_paypal_account_sms(account_id: int, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "paypal")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    account = await db.get_paypal_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="PayPal 资料不存在")
    url = str(account.sms_api_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="该资料没有配置接码地址")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="接码地址必须是 http/https")
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url)
        text = resp.text
        parsed: Any = None
        try:
            parsed = resp.json()
        except Exception:
            parsed = None
        return {
            "success": True,
            "status_code": resp.status_code,
            "text": text[:5000],
            "json": parsed,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"获取短信失败：{e}")


# -------------------- AI agent / browser agent --------------------
async def _effective_ai_agent_runtime_config(
    *,
    request_model: Optional[str] = None,
    request_api_key: Optional[str] = None,
) -> Dict[str, str]:
    from ..services.ai_browser_agent import (
        get_ai_agent_default_api_key,
        get_ai_agent_default_model,
        normalize_ai_agent_model,
    )

    row: Dict[str, Any] = {}
    if db is not None:
        try:
            row = await db.get_ai_agent_config()
        except Exception:
            row = {}

    model = normalize_ai_agent_model(
        request_model
        or str(row.get("default_model") or "").strip()
        or get_ai_agent_default_model()
    )
    api_key = (
        str(request_api_key or "").strip()
        or str(row.get("api_key") or "").strip()
        or get_ai_agent_default_api_key()
    )
    return {"model": model, "api_key": api_key}


@router.get("/api/admin/ai-agent/config")
async def get_ai_agent_config(token: str = Depends(verify_admin_token)):
    await _ensure_any_page_access(token, {"agent", "paypal", "test", "task_types"})
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    from ..services.ai_browser_agent import (
        AI_AGENT_MODELS,
        fixed_ai_agent_base_url,
        get_ai_agent_default_api_key,
        get_ai_agent_default_model,
        endpoint_path_for_model,
        model_api_type,
    )

    row = await db.get_ai_agent_config()
    db_default_model = str(row.get("default_model") or "").strip()
    default_model = db_default_model or get_ai_agent_default_model()
    db_key = str(row.get("api_key") or "").strip()
    env_or_file_key = get_ai_agent_default_api_key()
    models = [
        {
            "id": m,
            "endpoint_path": endpoint_path_for_model(m),
            "api_type": model_api_type(m),
        }
        for m in AI_AGENT_MODELS
    ]
    return {
        "success": True,
        "base_url": fixed_ai_agent_base_url(),
        "base_url_locked": True,
        "models": models,
        "default_model": default_model,
        # 不把服务端/数据库 key 明文下发到前端；请求里不传 api_key 时后端会自动使用数据库/环境变量配置。
        "has_default_api_key": bool(db_key or env_or_file_key),
        "has_db_api_key": bool(db_key),
        "db_updated_at": row.get("updated_at"),
    }


@router.post("/api/admin/ai-agent/config")
async def update_ai_agent_config(req: AIAgentConfigUpdateRequest, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "agent")
    await _ensure_admin_user(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    from ..services.ai_browser_agent import normalize_ai_agent_model

    await db.update_ai_agent_config(
        api_key=str(req.api_key or "").strip() if req.api_key is not None else None,
        default_model=normalize_ai_agent_model(req.default_model) if req.default_model is not None else None,
    )
    row = await db.get_ai_agent_config()
    return {
        "success": True,
        "default_model": normalize_ai_agent_model(row.get("default_model")),
        "has_db_api_key": bool(str(row.get("api_key") or "").strip()),
        "updated_at": row.get("updated_at"),
    }


@router.post("/api/admin/ai-agent/chat")
async def ai_agent_chat(req: AIAgentChatRequest, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "agent")
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")
    from ..services.ai_browser_agent import AIModelClient

    effective = await _effective_ai_agent_runtime_config(request_model=req.model, request_api_key=req.api_key)
    model = effective["model"]
    client = AIModelClient(model=model, api_key=effective["api_key"])
    try:
        answer = await client.chat_text(
            [m.model_dump() for m in req.messages],
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "success": True,
        "model": model,
        "base_url": client.base_url,
        "endpoint_path": client.endpoint_path,
        "api_type": client.api_type,
        "answer": answer,
    }


@router.post("/api/admin/ai-agent/windows/{mapping_id}/run")
async def ai_browser_agent_run_window(
    mapping_id: int,
    req: AIBrowserAgentRunRequest,
    token: str = Depends(verify_admin_token),
):
    """通用浏览器智能体接口。

    未来 Google 登录、页面分析等场景只需要传 prompt + 可选资料 data，就能复用这里的
    指纹浏览器打开、页面扫描、LLM 决策、Playwright 执行动作闭环。
    """

    user = await _ensure_any_page_access(token, {"agent", "paypal", "test", "task_types"})
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")
    allowed_ids = await _get_allowed_task_type_ids(user)
    if allowed_ids is not None and int(ctx_row.get("task_type_id") or 0) not in {int(x) for x in allowed_ids}:
        raise HTTPException(status_code=403, detail="无权操作该任务类型窗口")

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "").strip()
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "").strip()
    window_key = str(ctx_row.get("window_key") or "").strip()
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    effective_pure = _effective_browser_pure_mode(ctx_row, req.pure_mode)
    try:
        from ..services.ai_browser_agent import AIBrowserAgent, normalize_ai_agent_model
        from ..services.playwright_broswer_context import get_or_create_ctx, pick_working_page_from_context
        effective_ai = await _effective_ai_agent_runtime_config(request_model=req.model, request_api_key=req.api_key)

        ctx = get_or_create_ctx(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            space_id=space_id,
            window_key=window_key,
        )
        steps: list[str] = []
        async with ctx.driver_lock:
            await ctx.ensure_open(args=[], force_open=False, headless=req.headless, require_page=False, pure_mode=effective_pure)
            if ctx.context is None:
                raise RuntimeError("CDP 已连接但未获得 browser context")
            page = await pick_working_page_from_context(ctx.context)
            await page.bring_to_front()
            agent = AIBrowserAgent(
                page,
                model=normalize_ai_agent_model(effective_ai["model"]),
                api_key=effective_ai["api_key"],
                max_steps=req.max_steps,
                step_log=steps,
            )
            result = await agent.run(
                task=req.prompt,
                data=req.data or {},
                auto_submit=req.auto_submit,
                initial_url=req.target_url,
            )
            try:
                await ctx.disconnect_playwright_only()
            except Exception:
                pass
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"AI 浏览器智能体执行失败：{e}")

    return {
        "success": True,
        "mapping_id": mapping_id,
        "result": result,
        "steps": steps,
    }


async def _paypal_window_action_impl(
    *,
    mapping_id: int,
    action: str,
    req: PaypalWindowActionRequest,
    token: str,
):
    user = await _ensure_page_access(token, "paypal")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")
    allowed_ids = await _get_allowed_task_type_ids(user)
    if allowed_ids is not None and int(ctx_row.get("task_type_id") or 0) not in {int(x) for x in allowed_ids}:
        raise HTTPException(status_code=403, detail="无权操作该任务类型窗口")

    account = None
    if req.paypal_account_id:
        account = await db.get_paypal_account(req.paypal_account_id)
        if not account:
            raise HTTPException(status_code=404, detail="PayPal 资料不存在")

    effective_pure = _effective_browser_pure_mode(ctx_row, req.pure_mode)
    try:
        from ..services.paypal_account_executor import run_paypal_window_action

        effective_ai = await _effective_ai_agent_runtime_config(request_model=req.ai_model, request_api_key=req.ai_api_key)
        result = await run_paypal_window_action(
            ctx_row=ctx_row,
            action=action,
            target_url=req.target_url,
            register_url=req.register_url,
            account=account,
            headless=req.headless,
            pure_mode=effective_pure,
            auto_submit=req.auto_submit,
            ai_model=effective_ai["model"],
            ai_api_key=effective_ai["api_key"],
            ai_prompt=req.ai_prompt,
            max_steps=req.max_steps,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"PayPal 操作失败：{e}")
    return {"success": True, "mapping_id": mapping_id, "action": action, "result": result}


@router.post("/api/admin/paypal/windows/{mapping_id}/open")
async def paypal_open_window(mapping_id: int, req: PaypalWindowActionRequest, token: str = Depends(verify_admin_token)):
    return await _paypal_window_action_impl(mapping_id=mapping_id, action="open", req=req, token=token)


@router.post("/api/admin/paypal/windows/{mapping_id}/login")
async def paypal_login_window(mapping_id: int, req: PaypalWindowActionRequest, token: str = Depends(verify_admin_token)):
    return await _paypal_window_action_impl(mapping_id=mapping_id, action="login", req=req, token=token)


@router.post("/api/admin/paypal/windows/{mapping_id}/register")
async def paypal_register_window(mapping_id: int, req: PaypalWindowActionRequest, token: str = Depends(verify_admin_token)):
    return await _paypal_window_action_impl(mapping_id=mapping_id, action="register", req=req, token=token)


# -------------------- task types --------------------
async def _refresh_window_pool_after_task_type_write() -> None:
    """任务类型写入后立刻与 DB 对齐窗口池目标，避免最长等待一轮 reconcile 周期。"""
    from ..api.routes import task_service as _ts

    if _ts is None:
        return
    try:
        await _ts.refresh_window_pool_targets_now()
    except Exception:
        pass


@router.get("/api/admin/task-types")
async def list_task_types(include_all: bool = False, token: str = Depends(verify_admin_token)):
    user = await _ensure_any_page_access(token, {"task_types", "tasks", "test", "paypal", "users"})
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    allowed_ids = None if include_all else await _get_allowed_task_type_ids(user)
    return {"success": True, "task_types": [t.model_dump() for t in await db.list_task_types(allowed_task_type_ids=allowed_ids)]}


@router.get("/api/admin/fish-audio/voices")
async def list_admin_fish_audio_voices(
    source: str = Query("curated", pattern="^(curated|public)$"),
    q: str = Query("", max_length=100),
    language: str = Query("", max_length=32),
    gender: str = Query("", max_length=32),
    age_group: str = Query("", max_length=32),
    page: int = Query(1, ge=1, le=10),
    page_size: int = Query(100, ge=1, le=100),
    token: str = Depends(verify_admin_token),
):
    await _ensure_page_access(token, "test")
    result = await list_fish_audio_voices(
        source=source,
        query=q,
        language=language,
        gender=gender,
        age_group=age_group,
        page=page,
        page_size=page_size,
    )
    return {"success": True, **result}


@router.post("/api/admin/task-types")
async def create_task_type(req: CreateTaskTypeRequest, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    try:
        await _ensure_page_access(token, "task_types")
        user = await _ensure_admin_user(token)
        allowed_project_ids = await _get_allowed_project_ids(user)
        if req.project_id is not None:
            if allowed_project_ids is not None and int(req.project_id) not in {int(x) for x in allowed_project_ids}:
                raise HTTPException(status_code=403, detail="无权绑定到该项目")
            projects = await db.list_projects(allowed_project_ids=allowed_project_ids)
            if int(req.project_id) not in {int(p.id or 0) for p in projects}:
                raise HTTPException(status_code=400, detail="绑定项目不存在")

        # 校验 handler key（避免保存后运行时报错）
        from ..services.task_handler_registry import get_create_task_handler, get_refresh_quota_handler

        if req.create_task_handler:
            get_create_task_handler(req.create_task_handler)
        if req.refresh_quota_handler:
            get_refresh_quota_handler(req.refresh_quota_handler)

        tid = await db.create_task_type(
            req.name,
            req.code,
            req.project_id,
            req.concurrency,
            req.continuous_error_threshold,
            req.continuous_error_close_window_threshold,
            req.timeout_seconds,
            window_call_cooldown_seconds=req.window_call_cooldown_seconds,
            create_task_handler=req.create_task_handler,
            refresh_quota_handler=req.refresh_quota_handler,
            error_retry_count=req.error_retry_count,
            default_target_url=req.default_target_url,
            window_pool_enabled=req.window_pool_enabled,
            window_pool_reconcile_interval_sec=req.window_pool_reconcile_interval_sec,
            window_pool_cloudflare_interval_sec=req.window_pool_cloudflare_interval_sec,
        )
        await _refresh_window_pool_after_task_type_write()
        return {"success": True, "task_type_id": tid}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/api/admin/task-types/{task_type_id}")
async def update_task_type(task_type_id: int, req: UpdateTaskTypeRequest, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    try:
        await _ensure_page_access(token, "task_types")
        user = await _ensure_admin_user(token)
        allowed_project_ids = await _get_allowed_project_ids(user)
        if req.project_id is not None:
            if allowed_project_ids is not None and int(req.project_id) not in {int(x) for x in allowed_project_ids}:
                raise HTTPException(status_code=403, detail="无权绑定到该项目")
            projects = await db.list_projects(allowed_project_ids=allowed_project_ids)
            if int(req.project_id) not in {int(p.id or 0) for p in projects}:
                raise HTTPException(status_code=400, detail="绑定项目不存在")

        # 校验 handler key（避免保存后运行时报错）
        from ..services.task_handler_registry import get_create_task_handler, get_refresh_quota_handler

        if req.create_task_handler:
            get_create_task_handler(req.create_task_handler)
        if req.refresh_quota_handler:
            get_refresh_quota_handler(req.refresh_quota_handler)

        await db.update_task_type(
            task_type_id=task_type_id,
            name=req.name,
            code=req.code,
            project_id=req.project_id,
            concurrency=req.concurrency,
            continuous_error_threshold=req.continuous_error_threshold,
            continuous_error_close_window_threshold=req.continuous_error_close_window_threshold,
            timeout_seconds=req.timeout_seconds,
            window_call_cooldown_seconds=req.window_call_cooldown_seconds,
            create_task_handler=req.create_task_handler,
            refresh_quota_handler=req.refresh_quota_handler,
            enabled=req.enabled,
            error_retry_count=req.error_retry_count,
            default_target_url=req.default_target_url,
            window_pool_enabled=req.window_pool_enabled,
            window_pool_reconcile_interval_sec=req.window_pool_reconcile_interval_sec,
            window_pool_cloudflare_interval_sec=req.window_pool_cloudflare_interval_sec,
        )
        await _refresh_window_pool_after_task_type_write()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# -------------------- task type dynamic handlers --------------------
@router.get("/api/admin/task-type-handler-options")
async def list_task_type_handler_options(token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "task_types")
    from ..services.task_handler_registry import list_create_task_handler_options, list_refresh_quota_handler_options

    return {
        "success": True,
        "create_task_handlers": list_create_task_handler_options(),
        "refresh_quota_handlers": list_refresh_quota_handler_options(),
    }


@router.post("/api/admin/task-type-windows/{mapping_id}/refresh-remaining-quota")
async def refresh_mapping_remaining_quota(
    mapping_id: int,
    source: Optional[str] = None,  # auto/manual
    headless: bool = False,
    token: str = Depends(verify_admin_token),
):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")

    # 以 task_type 配置为准（避免 join 字段不全）
    task_code = str(ctx_row.get("task_code") or "").strip()
    if not task_code:
        raise HTTPException(status_code=400, detail="task_code missing")

    task_type = await db.get_task_type_by_code(task_code)
    if not task_type:
        raise HTTPException(status_code=404, detail="task_type not found")

    from ..services.task_handler_registry import (
        RefreshQuotaContext,
        get_refresh_quota_handler,
        refresh_quota__veo_flow_credits,
    )

    create_handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
    # veo_workflow：始终走 VEO credits handler（插件优先，失败回退两层代理），不依赖任务类型上选的 refresh_quota_handler
    if create_handler == "veo_workflow":
        fn = refresh_quota__veo_flow_credits
        handler_used = "veo_flow_credits"
        target_url = _admin_manual_open_target_url(ctx_row) or "https://labs.google/fx"
        ctx_row["default_target_url"] = target_url;
    elif create_handler == "dreamina_workflow":
        from ..services.task_handler_registry import refresh_quota__dreamina_credits

        fn = refresh_quota__dreamina_credits
        handler_used = "dreamina_credits"
    elif create_handler == "gpt_workflow":
        from ..services.task_handler_registry import refresh_quota__gpt_balance

        fn = refresh_quota__gpt_balance
        handler_used = "gpt_balance"
    elif create_handler == "leonardo_workflow":
        from ..services.task_handler_registry import refresh_quota__leonardo_tokens

        fn = refresh_quota__leonardo_tokens
        handler_used = "leonardo_tokens"
    else:
        try:
            fn = get_refresh_quota_handler(task_type.refresh_quota_handler)
        except KeyError as e:
            raise HTTPException(status_code=400, detail=str(e))
        handler_used = (task_type.refresh_quota_handler or "").strip() or "noop"

    is_auto_refresh = str(source or "").strip().lower() == "auto"
    ctx_row["_headless"] = headless
    try:
        new_remaining = await fn(RefreshQuotaContext(task_type=task_type, mapping_row=ctx_row, db=db))
        new_remaining = max(0, int(new_remaining))
    except Exception as e:
        no_penalty = bool(getattr(e, "no_penalty", False))
        if not is_auto_refresh and create_handler == "leonardo_workflow":
            recover_result: Dict[str, Any] = {}
            try:
                recover_result = await _restart_and_login_leonardo_mapping(ctx_row=ctx_row, headless=headless)
            except Exception as recover_exc:
                recover_result = {"success": False, "errors": [str(recover_exc)]}

            if bool(recover_result.get("manual_verification_required")):
                raise HTTPException(
                    status_code=400,
                    detail=f"刷新额度失败：{e}；已关闭并重启该 Leonardo 窗口，登录流程遇到 CF/人工验证，请在打开的窗口里手动完成验证后再刷新余额。",
                )
            if bool(recover_result.get("login_submitted")):
                raise HTTPException(
                    status_code=400,
                    detail=f"刷新额度失败：{e}；已关闭并重启该 Leonardo 窗口，并已提交账号密码登录。请等页面登录完成后再刷新余额。",
                )
            if bool(recover_result.get("already_logged_in")):
                raise HTTPException(
                    status_code=400,
                    detail=f"刷新额度失败：{e}；已关闭并重启该 Leonardo 窗口，窗口当前看起来仍是登录态，请稍后再刷新余额。",
                )
            if bool(recover_result.get("missing_credentials")):
                raise HTTPException(
                    status_code=400,
                    detail=f"刷新额度失败：{e}；已重启窗口并打开登录页，但该窗口绑定记录缺少 Leonardo 账号密码。",
                )
            recover_errors = "; ".join(str(x) for x in (recover_result.get("errors") or []) if str(x).strip())
            recover_suffix = f"；重启登录也失败：{recover_errors}" if recover_errors else "；已尝试重启登录但未能确认结果"
            raise HTTPException(status_code=400, detail=f"刷新额度失败：{e}{recover_suffix}")

        # 手工触发：不做风控/熔断副作用，仅返回错误即可
        if not is_auto_refresh:
            raise HTTPException(status_code=400, detail=f"刷新额度失败：{e}")

        # 定时触发：记录错误日志，并累计窗口连续错误；达到阈值才禁用
        try:
            from ..core.models import AutoRefreshErrorLog

            await db.add_auto_refresh_error_log(
                AutoRefreshErrorLog(
                    mapping_id=int(mapping_id),
                    task_type_id=int(ctx_row.get("task_type_id") or 0) or None,
                    task_code=str(ctx_row.get("task_code") or "").strip() or None,
                    window_pk=int(ctx_row.get("window_pk") or 0) or None,
                    window_name=str(ctx_row.get("window_name") or "").strip() or None,
                    platform_account=str(ctx_row.get("platform_account") or "").strip() or None,
                    error_message=str(e),
                )
            )
        except Exception:
            pass

        suffix = "（已记录）"
        if no_penalty:
            suffix = "（已记录；no_penalty：未计入连续错误）"
        else:
            thr = max(1, int(getattr(task_type, "continuous_error_threshold", 3) or 3))
            try:
                await db.mark_mapping_error(mapping_id=mapping_id, threshold=thr, cooldown_seconds=3600)
            except Exception:
                pass

            try:
                st = await db.get_mapping_runtime_state(mapping_id=mapping_id)
                ce = int((st or {}).get("consecutive_errors") or 0)
                if ce >= thr:
                    try:
                        await db.update_task_type_window(mapping_id=mapping_id, enabled=False)
                        suffix = f"（已记录；连续错误 {ce}/{thr} 达到阈值，已自动禁用该窗口）"
                    except Exception:
                        suffix = f"（已记录；连续错误 {ce}/{thr} 达到阈值）"
                else:
                    suffix = f"（已记录；已累计连续错误 {ce}/{thr}）"
            except Exception:
                suffix = f"（已记录；已累计连续错误，阈值 {thr}）"

        raise HTTPException(status_code=400, detail=f"刷新额度失败：{e}{suffix}")


    update_kwargs: Dict[str, Any] = {"remaining_quota": new_remaining}
    if not is_auto_refresh:
        if create_handler == "leonardo_workflow" and not str(ctx_row.get("platform_account") or "").strip():
            update_kwargs["enabled"] = False
        else:
            update_kwargs["enabled"] = True
    await db.update_task_type_window(mapping_id=mapping_id, **update_kwargs)
    # 一次成功：连续错误清零（手工/定时都可清零）
    try:
        await db.mark_mapping_success(mapping_id=mapping_id)
    except Exception:
        pass

    # 走指纹/CDP 的 handler 完成后断开本地 CDP（noop / reset_to_daily 无连接，此处不调用）
    if handler_used in ("veo_flow_credits", "sora_nf_check", "dreamina_credits"):
        _vendor = str(ctx_row.get("vendor") or "roxy")
        _base = str(ctx_row.get("lan_addr") or "")
        _ak = ctx_row.get("access_key")
        _sid = str(ctx_row.get("space_id") or "")
        _wk = str(ctx_row.get("window_key") or "")
        if _base and _sid and _wk:
            try:
                if handler_used == "veo_flow_credits":
                    from ..services.veo_workflow_executor import get_or_create_veo_session  # type: ignore

                    _veo = get_or_create_veo_session(
                        vendor=_vendor, base_url=_base, access_key=_ak, space_id=_sid, window_key=_wk
                    )
                    await _veo.disconnect_playwright_under_bring_lock()
                elif handler_used == "dreamina_credits":
                    from ..services.jimeng_task_executor import get_or_create_dreamina_session  # type: ignore

                    _dreamina = get_or_create_dreamina_session(
                        vendor=_vendor, base_url=_base, access_key=_ak, space_id=_sid, window_key=_wk
                    )
                    await _dreamina.disconnect_playwright_under_bring_lock()
                else:
                    from ..services.sora_task_executor import get_or_create_sora_session  # type: ignore

                    _sora = get_or_create_sora_session(
                        vendor=_vendor, base_url=_base, access_key=_ak, space_id=_sid, window_key=_wk
                    )
                    await _sora.disconnect_playwright_under_bring_lock()
            except Exception:
                pass

    ctx_after = await db.get_task_type_window_context(mapping_id)
    cooldown_until_out = (ctx_after or {}).get("cooldown_until")
    return {
        "success": True,
        "mapping_id": mapping_id,
        "remaining_quota": new_remaining,
        "handler": handler_used,
        "cooldown_until": cooldown_until_out,
    }


@router.post("/api/admin/task-type-windows/{mapping_id}/refresh-subscription-info")
async def refresh_mapping_subscription_info(mapping_id: int, headless: bool = False, token: str = Depends(verify_admin_token)):
    """通过指纹浏览器读取订阅信息并写回 mapping；完成后断开本地 CDP（不关指纹窗口）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")

    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    if handler == "grok_workflow":
        plan_title = "Grok Imagine（浏览器 Cookie 会话）"
        await db.update_task_type_window(mapping_id=mapping_id, sora_plan_title=plan_title)
        return {
            "success": True,
            "mapping_id": mapping_id,
            "plan_title": plan_title,
            "subscription_end": None,
        }

    if handler == "gpt_workflow":
        from ..services.gpt_task_executor import DEFAULT_GPT_TARGET, gpt_fetch_membership_in_window  # type: ignore

        access_token = str(ctx_row.get("sora_access_token") or "").strip()
        access_expires = str(ctx_row.get("sora_access_expires") or "").strip() or None
        try:
            info = await gpt_fetch_membership_in_window(
                browser_vendor=vendor,
                browser_base_url=base_url,
                browser_access_key=access_key,
                space_id=space_id,
                window_key=window_key,
                target_url=str(ctx_row.get("default_target_url") or "").strip() or DEFAULT_GPT_TARGET,
                access_token=access_token,
                access_expires=access_expires,
                headless=headless,
                pure_mode=bool(ctx_row.get("pure_mode")) if ctx_row.get("pure_mode") is not None else True,
                timeout_seconds=60.0,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"刷新会员信息失败：{e}")
        plan_title = str((info or {}).get("plan_title") or (info or {}).get("membership") or "").strip() or "ChatGPT（Web 账号）"
        new_access_token = str((info or {}).get("access_token") or "").strip()
        new_expires = str((info or {}).get("expires") or "").strip() or None
        update_kwargs: Dict[str, Any] = {"mapping_id": mapping_id, "sora_plan_title": plan_title}
        if new_access_token:
            update_kwargs["sora_access_token"] = new_access_token
            update_kwargs["sora_access_expires"] = new_expires
        await db.update_task_type_window(**update_kwargs)
        return {
            "success": True,
            "mapping_id": mapping_id,
            "plan_title": plan_title,
            "subscription_end": None,
            "source": (info or {}).get("source") or "extension.membership",
            "raw": (info or {}).get("raw"),
        }

    if handler == "veo_workflow":
        # VEO：使用已保存的长效 session-token，通过两层代理换短效 access_token 后请求 credits；
        # 不再打开/依赖指纹浏览器窗口。
        from ..services.veo_workflow_executor import (  # type: ignore
            fetch_short_access_token_by_proxy,
            veo_fetch_credits_by_proxy,
            veo_format_paygate_tier_label,
        )

        long_session_token = str(ctx_row.get("sora_access_token") or "").strip()
        if not long_session_token:
            raise HTTPException(status_code=400, detail="缺少 session-token，请先获取并保存")

        default_target_url = str(ctx_row.get("default_target_url") or "").strip()
        target_url = default_target_url or "https://labs.google/fx"
        picked = SimpleNamespace(
            window_pk=int(ctx_row.get("window_pk") or 0),
            mapping_id=int(mapping_id),
            space_id=space_id,
            window_key=window_key,
        )

        try:
            short_info = await fetch_short_access_token_by_proxy(
                session_token=long_session_token,
                target_url=target_url,
                db=db,
                picked=picked,
            )
            info = await veo_fetch_credits_by_proxy(
                sess=None,
                target_url=target_url,
                access_token=str((short_info or {}).get("access_token") or ""),
                db=db,
                picked=picked,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"刷新会员信息失败：{e}")

        tier = str((info or {}).get("user_paygate_tier") or "").strip() or None
        plan_title = veo_format_paygate_tier_label(tier)
        # 「刷新会员信息」仅更新套餐/档位展示，不写入 remaining_quota（余额请用「刷新额度」）
        await db.update_task_type_window(
            mapping_id=mapping_id,
            sora_plan_title=plan_title,
        )
        return {
            "success": True,
            "mapping_id": mapping_id,
            "plan_title": plan_title,
            "subscription_end": None,
            "user_paygate_tier": tier,
        }

    from ..services.sora_task_executor import get_or_create_sora_session  # type: ignore

    sora_ctx = get_or_create_sora_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
    sora_ctx.browser_headless = headless
    try:
        sora_ctx.set_access_token(ctx_row.get("sora_access_token"), ctx_row.get("sora_access_expires"))
    except Exception:
        pass

    try:
        info = await sora_ctx.api_subscription_info(target_url="https://sora.chatgpt.com/drafts")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"刷新会员信息失败：{e}")

    plan_title = str((info or {}).get("plan_title") or "").strip() or None
    subscription_end = str((info or {}).get("subscription_end") or "").strip() or None
    await db.update_task_type_window(
        mapping_id=mapping_id,
        sora_plan_title=plan_title,
        sora_subscription_end=subscription_end,
    )
    try:
        await sora_ctx.disconnect_playwright_under_bring_lock()
    except Exception:
        pass
    return {
        "success": True,
        "mapping_id": mapping_id,
        "plan_title": plan_title,
        "subscription_end": subscription_end,
    }


@router.post("/api/admin/task-type-windows/{mapping_id}/clear-drafts")
async def clear_mapping_drafts(mapping_id: int, headless: bool = False, token: str = Depends(verify_admin_token)):
    """通过指纹浏览器获取 drafts/v2 列表并逐个 DELETE 清除所有草稿。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    from ..services.sora_task_executor import get_or_create_sora_session  # type: ignore

    sora_ctx = get_or_create_sora_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
    sora_ctx.browser_headless = headless
    try:
        sora_ctx.set_access_token(ctx_row.get("sora_access_token"), ctx_row.get("sora_access_expires"))
    except Exception:
        pass

    try:
        result = await sora_ctx.api_clear_all_drafts(target_url="https://sora.chatgpt.com/drafts")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"清除草稿失败：{e}")

    try:
        await db.update_task_type_window(mapping_id=mapping_id, sora_drafts_count=0)
    except Exception:
        pass

    return {
        "success": True,
        "mapping_id": mapping_id,
        "total": result.get("total", 0),
        "deleted": result.get("deleted", []),
        "failed": result.get("failed", []),
    }


@router.get("/api/admin/auto-refresh-errors")
async def list_auto_refresh_errors(
    limit: int = 200,
    offset: int = 0,
    task_type_id: Optional[int] = None,
    mapping_id: Optional[int] = None,
    token: str = Depends(verify_admin_token),
):
    await _ensure_page_access(token, "task_types")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    items = await db.list_auto_refresh_error_logs(limit=limit, offset=offset, task_type_id=task_type_id, mapping_id=mapping_id)
    return {"success": True, "limit": max(1, min(500, int(limit or 200))), "offset": max(0, int(offset or 0)), "items": items}


@router.post("/api/admin/task-type-windows/{mapping_id}/refresh-invite-code")
async def refresh_mapping_invite_code(mapping_id: int, token: str = Depends(verify_admin_token)):
    """通过指纹浏览器读取 backend/project_y/invite/mine 并写回 mapping.sora_invite_code；完成后断开本地 CDP。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    from ..services.sora_task_executor import get_or_create_sora_session  # type: ignore

    sora_ctx = get_or_create_sora_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
    try:
        sora_ctx.set_access_token(ctx_row.get("sora_access_token"), ctx_row.get("sora_access_expires"))
    except Exception:
        pass
    try:
        info = await sora_ctx.api_invite_mine(target_url="https://sora.chatgpt.com/drafts")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"刷新邀请码失败：{e}")

    invite_code = (info or {}).get("invite_code")
    await db.update_task_type_window(mapping_id=mapping_id, sora_invite_code=str(invite_code or "").strip() or None)
    try:
        await sora_ctx.disconnect_playwright_under_bring_lock()
    except Exception:
        pass
    return {
        "success": True,
        "mapping_id": mapping_id,
        "invite_code": invite_code,
        "redeemed_count": info.get("redeemed_count"),
        "total_count": info.get("total_count"),
    }


@router.post("/api/admin/task-type-windows/{mapping_id}/convert-access-token")
async def convert_sora_session_token_to_access_token(
    mapping_id: int,
    req: UpsertSoraAccessTokenRequest,
    headless: bool = False,
    token: str = Depends(verify_admin_token),
):
    """写入/自动获取并写入 access_token + expires 到 mapping。

    规则：
    - 若请求携带 access_token（非空）：直接写入 DB（不发起自动获取）。
    - 若 access_token 为空：使用指纹浏览器对应窗口，在页面上下文内请求 `/api/auth/session` 获取并写入；成功后断开本地 CDP。
    """
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    input_token = str(req.access_token or "").strip() or None
    input_expires = str(req.expires or "").strip() or None

    if input_token:
        await db.update_task_type_window(mapping_id=mapping_id, sora_access_token=input_token, sora_access_expires=input_expires)
        return {"success": True, "mapping_id": mapping_id, "access_token": input_token, "expires": input_expires, "source": "manual"}

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")

    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    print(f"handler: {handler}")
    if handler == "grok_workflow":
        raise HTTPException(
            status_code=400,
            detail="grok_workflow 不支持从此接口自动获取令牌：请在上方「保存」中手工粘贴 Grok SSO（与 grok2api 池化 token 同源，可选），或依赖指纹窗口内已登录的 Cookie。",
        )

    if handler == "gpt_workflow":
        target_url = _admin_manual_open_target_url(ctx_row)
        try:
            from ..services.gpt_task_executor import gpt_fetch_access_token_in_window  # type: ignore
            from ..services.browser_extension_bridge import annotate_url_with_extension_config  # type: ignore

            info = await gpt_fetch_access_token_in_window(
                browser_vendor=vendor,
                browser_base_url=base_url,
                browser_access_key=access_key,
                space_id=space_id,
                window_key=window_key,
                target_url=annotate_url_with_extension_config(target_url, space_id=space_id, window_key=window_key),
                headless=headless,
                pure_mode=bool(ctx_row.get("pure_mode")) if ctx_row.get("pure_mode") is not None else True,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"自动获取 GPT access_token 失败：{e}")

        access_token = str((info or {}).get("access_token") or "").strip() or None
        expires = str((info or {}).get("expires") or "").strip() or None
        if not access_token:
            raise HTTPException(status_code=400, detail="自动获取失败：ChatGPT /api/auth/session 未返回 access_token")
        await db.update_task_type_window(mapping_id=mapping_id, sora_access_token=access_token, sora_access_expires=expires)
        return {
            "success": True,
            "mapping_id": mapping_id,
            "access_token": access_token,
            "expires": expires,
            "source": "chatgpt_window_cookie",
        }

    if handler == "dreamina_workflow":
        default_target_url = str(ctx_row.get("default_target_url") or "").strip()
        target_url = default_target_url or "https://dreamina.capcut.com/ai-tool/video/generate"

        try:
            from ..services.jimeng_task_executor import get_or_create_dreamina_session, dreamina_fetch_sessionid_in_window  # type: ignore

            dreamina_ctx = get_or_create_dreamina_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
            dreamina_ctx.browser_headless = headless
            info = await dreamina_fetch_sessionid_in_window(sess=dreamina_ctx, target_url=target_url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"自动获取失败：{e}")

        access_token = str((info or {}).get("access_token") or "").strip() or None
        expires = str((info or {}).get("expires") or "").strip() or None
        if not access_token:
            raise HTTPException(status_code=400, detail="自动获取失败：Cookies 中缺少 sessionid")

        await db.update_task_type_window(mapping_id=mapping_id, sora_access_token=access_token, sora_access_expires=expires)
        try:
            await dreamina_ctx.disconnect_playwright_under_bring_lock()
        except Exception:
            pass
        return {
            "success": True,
            "mapping_id": mapping_id,
            "access_token": access_token,
            "expires": expires,
            "cookie_name": "sessionid",
            "source": "window_cookie",
        }

    if handler == "veo_workflow":
        target_url = _admin_manual_open_target_url(ctx_row) or "https://labs.google/fx"

        try:
            from ..services.veo_workflow_executor import (  # type: ignore
                force_fetch_access_token_in_window,
                get_or_create_veo_session,
            )
            from ..services.browser_extension_bridge import annotate_url_with_extension_config  # type: ignore

            veo_ctx = get_or_create_veo_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
            veo_ctx.browser_headless = headless
            # “更新 AccessToken / 过期”是唯一主动向插件下发 space_id/window_key/bridge_url 的入口。
            # content script 收到 fpb_* 后会写入 chrome.storage.local，之后窗口关闭/重启仍沿用缓存。
            access_info = await force_fetch_access_token_in_window(
                sess=veo_ctx,
                target_url=target_url,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"自动获取失败：{e}")

        access_token = str((access_info or {}).get("session_token") or (access_info or {}).get("access_token") or "").strip() or None
        expires = str((access_info or {}).get("expires") or "").strip() or None

        await db.update_task_type_window(mapping_id=mapping_id, sora_access_token=access_token, sora_access_expires=expires)
        try:
            await veo_ctx.disconnect_playwright_under_bring_lock()
        except Exception:
            pass
        return {
            "success": True,
            "mapping_id": mapping_id,
            "access_token": access_token,
            "expires": expires,
            "session_token": str((access_info or {}).get("session_token") or "").strip() or None,
            "source": str((access_info or {}).get("source") or "extension").strip() or "extension",
        }
    else:
        try:
            from ..services.sora_task_executor import get_or_create_sora_session, sora_fetch_access_token_in_window  # type: ignore

            sora_ctx = get_or_create_sora_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
            sora_ctx.browser_headless = headless
            info = await sora_fetch_access_token_in_window(sess=sora_ctx, target_url="https://sora.chatgpt.com/drafts")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"自动获取失败：{e}")

        access_token = str((info or {}).get("access_token") or "").strip() or None
        expires = str((info or {}).get("expires") or "").strip() or None
        if not access_token:
            raise HTTPException(status_code=400, detail="自动获取失败：返回缺少 access_token")

        await db.update_task_type_window(mapping_id=mapping_id, sora_access_token=access_token, sora_access_expires=expires)
        try:
            sora_ctx.set_access_token(access_token, expires)
        except Exception:
            pass
        try:
            await sora_ctx.disconnect_playwright_under_bring_lock()
        except Exception:
            pass
        return {"success": True, "mapping_id": mapping_id, "access_token": access_token, "expires": expires, "source": "window"}


@router.post("/api/admin/task-type-windows/{mapping_id}/manual-open")
async def manual_open_mapping_window(
    mapping_id: int,
    headless: bool = False,
    pure_mode: Optional[bool] = Query(
        None, description="指纹 browser_open 纯净模式；省略时按绑定 pure_mode 列"
    ),
    browser_only: bool = Query(
        False, description="仅打开/唤起指纹浏览器窗口；不连接 CDP、不判断/打开目标页"
    ),
    token: str = Depends(verify_admin_token),
):
    """手动打开指纹浏览器窗口并禁止空闲自动关闭。

    browser_only=true 时只调用指纹浏览器 browser_open，不连接 CDP、不检查/导航目标页。
    """
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
    effective_pure = _effective_browser_pure_mode(ctx_row, pure_mode)
    target_url = "" if browser_only else _admin_manual_open_target_url(ctx_row)
    final_url = ""

    if handler == "veo_workflow":
        from ..services.veo_workflow_executor import get_or_create_veo_session  # type: ignore

        veo_ctx = get_or_create_veo_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        veo_ctx.browser_headless = headless
        veo_ctx.browser_pure_mode = effective_pure
        veo_ctx.idle_close_disabled = True
        try:
            await veo_ctx.close_and_drop()
        except Exception:
            pass
        try:
            veo_ctx._cancel_idle_close()
        except Exception:
            pass
        try:
            await veo_ctx.pw_ctx.open_fingerprint_window_only(
                args=[] if browser_only else veo_ctx.browser_open_args,
                force_open=veo_ctx.browser_force_open,
                headless=headless,
                pure_mode=effective_pure,
            )
            if not browser_only:
                final_url = await _ensure_manual_open_has_target_page(veo_ctx.pw_ctx, target_url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"打开窗口失败：{e}")
        finally:
            try:
                await veo_ctx.disconnect_playwright_under_bring_lock()
            except Exception:
                pass
    elif handler == "grok_workflow":
        from ..services.grok_workflow_executor import get_or_create_grok_session  # type: ignore

        grok_ctx = get_or_create_grok_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        grok_ctx.browser_headless = headless
        grok_ctx.browser_pure_mode = effective_pure
        grok_ctx.idle_close_disabled = True
        try:
            grok_ctx._cancel_idle_close()
        except Exception:
            pass
        try:
            await grok_ctx.pw_ctx.open_fingerprint_window_only(
                args=[],
                force_open=False,
                headless=headless,
                pure_mode=effective_pure,
            )
            if not browser_only:
                final_url = await _ensure_manual_open_has_target_page(grok_ctx.pw_ctx, target_url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"打开窗口失败：{e}")
        finally:
            try:
                await grok_ctx.disconnect_playwright_under_bring_lock()
            except Exception:
                pass
    elif handler == "dreamina_workflow":
        from ..services.jimeng_task_executor import get_or_create_dreamina_session  # type: ignore

        dreamina_ctx = get_or_create_dreamina_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        dreamina_ctx.browser_headless = headless
        dreamina_ctx.browser_pure_mode = effective_pure
        dreamina_ctx.idle_close_disabled = True
        try:
            dreamina_ctx._cancel_idle_close()
        except Exception:
            pass
        try:
            await dreamina_ctx.pw_ctx.open_fingerprint_window_only(
                args=[] if browser_only else dreamina_ctx.browser_open_args,
                force_open=dreamina_ctx.browser_force_open,
                headless=headless,
                pure_mode=effective_pure,
            )
            if not browser_only:
                final_url = await _ensure_manual_open_has_target_page(dreamina_ctx.pw_ctx, target_url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"open Dreamina window failed: {e}")
        finally:
            try:
                await dreamina_ctx.disconnect_playwright_under_bring_lock()
            except Exception:
                pass
    else:
        from ..services.sora_task_executor import get_or_create_sora_session  # type: ignore

        sora_ctx = get_or_create_sora_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        sora_ctx.browser_headless = headless
        sora_ctx.browser_pure_mode = effective_pure
        sora_ctx.idle_close_disabled = True
        try:
            sora_ctx._cancel_idle_close()
        except Exception:
            pass
        try:
            await sora_ctx.pw_ctx.open_fingerprint_window_only(
                args=[] if browser_only else sora_ctx.browser_open_args,
                force_open=sora_ctx.browser_force_open,
                headless=headless,
                pure_mode=effective_pure,
            )
            if not browser_only:
                final_url = await _ensure_manual_open_has_target_page(sora_ctx.pw_ctx, target_url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"打开窗口失败：{e}")
        finally:
            try:
                await sora_ctx.disconnect_playwright_under_bring_lock()
            except Exception:
                pass

    return {"success": True, "mapping_id": mapping_id, "idle_close_disabled": True, "target_url": target_url, "page_url": final_url}


@router.post("/api/admin/task-type-windows/{mapping_id}/manual-close")
async def manual_close_mapping_window(mapping_id: int, token: str = Depends(verify_admin_token)):
    """取消“保持打开”并触发一次 _schedule_idle_close（不会立刻关闭，避免影响正在运行的任务）。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
    if handler == "gpt_workflow":
        from ..services.veo_workflow_executor import get_or_create_veo_session  # type: ignore

        gpt_ctx = get_or_create_veo_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        gpt_ctx.idle_close_disabled = False
        try:
            gpt_ctx._schedule_idle_close()
        except Exception:
            pass
    elif handler == "grok_workflow":
        from ..services.grok_workflow_executor import get_or_create_grok_session  # type: ignore

        grok_ctx = get_or_create_grok_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        grok_ctx.idle_close_disabled = False
        try:
            grok_ctx._schedule_idle_close()
        except Exception:
            pass
    elif handler == "veo_workflow":
        from ..services.veo_workflow_executor import get_or_create_veo_session  # type: ignore

        veo_ctx = get_or_create_veo_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        veo_ctx.idle_close_disabled = False
        try:
            veo_ctx._schedule_idle_close()
        except Exception:
            pass
    elif handler == "dreamina_workflow":
        from ..services.jimeng_task_executor import get_or_create_dreamina_session  # type: ignore

        dreamina_ctx = get_or_create_dreamina_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        dreamina_ctx.idle_close_disabled = False
        try:
            dreamina_ctx._schedule_idle_close()
        except Exception:
            pass
    else:
        from ..services.sora_task_executor import get_or_create_sora_session  # type: ignore

        sora_ctx = get_or_create_sora_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        sora_ctx.idle_close_disabled = False
        try:
            sora_ctx._schedule_idle_close()
        except Exception:
            pass

    return {"success": True, "mapping_id": mapping_id, "idle_close_disabled": False}


@router.post("/api/admin/task-type-windows/{mapping_id}/clear-browser-cache")
async def clear_mapping_browser_cache(
    mapping_id: int,
    local_only: bool = Query(False, description="为 True 时仅清空本地缓存，不调用 clear_server_cache"),
    token: str = Depends(verify_admin_token),
):
    """调用 Roxy 清空窗口本地缓存；默认再清空窗口服务器缓存（dirId=window_key）。local_only=true 时仅本地。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    syscfg = await db.get_system_config()
    client = FPBrowserClient()
    keys = [window_key]
    local_rsp = None
    
    try:
        local_rsp = await client.browser_clear_local_cache(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            window_keys=keys,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=f"清空本地缓存失败：{e}")

    if int(local_rsp.get("code") if local_rsp.get("code") is not None else -1) != 0:
        raise HTTPException(
            status_code=400,
            detail=f"清空本地缓存失败：{local_rsp.get('msg') or local_rsp}",
        )
    
    if local_only:
        return {
            "success": True,
            "mapping_id": mapping_id,
            "local": local_rsp,
            "local_only": True,
        }

    try:
        server_rsp = await client.browser_clear_server_cache(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            space_id=space_id,
            window_keys=keys,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=f"清空服务器缓存失败（本地已清空）：{e}")

    if int(server_rsp.get("code") if server_rsp.get("code") is not None else -1) != 0:
        raise HTTPException(
            status_code=400,
            detail=f"清空服务器缓存失败（本地已清空）：{server_rsp.get('msg') or server_rsp}",
        )

    # 与「清缓存」语义一致：指纹侧清空后，同步清空本 mapping 保存的会话与额度/会员缓存字段
    await db.update_task_type_window(
        mapping_id=mapping_id,
        sora_access_token="",
        sora_access_expires="",
        remaining_quota=0,
        sora_remaining_count=0,
        sora_purchased_remaining_count=0,
        sora_rate_limit_reached=False,
        sora_access_resets_in_seconds=0,
        cooldown_until="",
        sora_plan_title="",
        sora_subscription_end="",
    )

    return {
        "success": True,
        "mapping_id": mapping_id,
        "local": local_rsp,
        "server": server_rsp,
        "local_only": False,
        "cleared_mapping_session_fields": True,
    }


async def _close_dreamina_mapping_window_now(ctx_row: Dict[str, Any], *, headless: bool = False, pure_mode: Optional[bool] = None) -> Dict[str, Any]:
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    from ..services.jimeng_task_executor import get_or_create_dreamina_session  # type: ignore

    effective_pure = _effective_browser_pure_mode(ctx_row, pure_mode)
    dreamina_ctx = get_or_create_dreamina_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
    dreamina_ctx.browser_headless = headless
    dreamina_ctx.browser_pure_mode = effective_pure
    dreamina_ctx.idle_close_disabled = False
    try:
        dreamina_ctx._cancel_idle_close()
    except Exception:
        pass
    await dreamina_ctx.close_and_drop()
    try:
        await dreamina_ctx.disconnect_playwright_under_bring_lock()
    except Exception:
        pass
    try:
        await db.update_window_status_by_space_and_key(space_id=space_id, window_key=window_key, window_status=0)
    except Exception:
        pass
    return {"success": True, "window_key": window_key, "closed": True}


@router.post("/api/admin/task-type-windows/batch-dreamina-logout")
async def batch_dreamina_logout_windows(req: BatchDreaminaLogoutRequest, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ids: List[int] = []
    seen: Set[int] = set()
    for raw in req.mapping_ids or []:
        mid = int(raw or 0)
        if mid > 0 and mid not in seen:
            seen.add(mid)
            ids.append(mid)
    if not ids:
        raise HTTPException(status_code=400, detail="mapping_ids is required")
    if len(ids) > 1000:
        raise HTTPException(status_code=400, detail="单次最多处理 1000 个窗口")

    def _err(e: Exception) -> str:
        if isinstance(e, HTTPException):
            return str(e.detail or "")
        return str(e or "")

    items: List[Dict[str, Any]] = []
    ok = 0
    partial = 0
    failed = 0
    for mapping_id in ids:
        item: Dict[str, Any] = {
            "mapping_id": mapping_id,
            "status": "pending",
            "steps": [],
            "errors": [],
        }
        ctx_row = await db.get_task_type_window_context(mapping_id)
        if not ctx_row:
            item["status"] = "failed"
            item["errors"].append("mapping not found")
            items.append(item)
            failed += 1
            continue
        handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
        if handler != "dreamina_workflow":
            item["status"] = "failed"
            item["errors"].append("not dreamina_workflow")
            items.append(item)
            failed += 1
            continue

        space_pk = int(ctx_row.get("space_pk") or 0)
        window_key = str(ctx_row.get("window_key") or "").strip()
        item["window_pk"] = int(ctx_row.get("window_pk") or 0)
        item["window_key"] = window_key
        item["window_name"] = str(ctx_row.get("window_name") or "").strip()

        try:
            item["close_result"] = await _close_dreamina_mapping_window_now(ctx_row)
            item["steps"].append("closed")
        except Exception as e:
            item["errors"].append(f"close failed: {_err(e)}")

        try:
            item["cache_result"] = await clear_mapping_browser_cache(
                mapping_id,
                local_only=bool(req.local_only),
                token=token,
            )
            item["steps"].append("cache_cleared")
        except Exception as e:
            item["errors"].append(f"clear cache failed: {_err(e)}")

        try:
            await db.update_task_type_window(
                mapping_id=mapping_id,
                sora_access_token="",
                sora_access_expires="",
                remaining_quota=0,
                sora_remaining_count=0,
                sora_purchased_remaining_count=0,
                sora_rate_limit_reached=False,
                sora_access_resets_in_seconds=0,
                cooldown_until="",
                sora_plan_title="",
                sora_subscription_end="",
            )
            item["steps"].append("local_session_cleared")
        except Exception as e:
            item["errors"].append(f"clear local session failed: {_err(e)}")

        try:
            if space_pk <= 0 or not window_key:
                raise HTTPException(status_code=400, detail="mapping missing space_pk/window_key")
            item["unbind_result"] = await _mdf_window_account_and_proxy_from_local(
                space_pk=space_pk,
                window_key=window_key,
                selected_account=None,
                clear_account=True,
                require_local_account=False,
            )
            item["steps"].append("account_unbound")
        except Exception as e:
            item["errors"].append(f"unbind account failed: {_err(e)}")

        if not item["errors"]:
            item["status"] = "ok"
            ok += 1
        elif item["steps"]:
            item["status"] = "partial"
            partial += 1
        else:
            item["status"] = "failed"
            failed += 1
        items.append(item)

    return {
        "success": ok > 0 or partial > 0,
        "message": f"Dreamina batch logout done: ok={ok}, partial={partial}, failed={failed}",
        "ok": ok,
        "partial": partial,
        "failed": failed,
        "items": items,
    }


@router.post("/api/admin/task-type-windows/{mapping_id}/open-account")
async def open_account_mapping_window(
    mapping_id: int,
    headless: bool = False,
    pure_mode: Optional[bool] = Query(
        None, description="指纹 browser_open 纯净模式；省略时按绑定 pure_mode 列"
    ),
    token: str = Depends(verify_admin_token),
):
    """开号/连接：Sora 走注册流程；Veo/Dreamina/Grok/GPT 打开目标页并断开 CDP。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")

    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
    window_pk = int(ctx_row.get("window_pk") or 0)
    if window_pk <= 0:
        raise HTTPException(status_code=400, detail="mapping 缺少 window_pk")

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    timeout_seconds = float(ctx_row.get("task_timeout_seconds") or 600)
    timeout_seconds = max(120.0, min(timeout_seconds, 3600.0))
    effective_pure = _effective_browser_pure_mode(ctx_row, pure_mode)

    async def _progress_cb(_pct: int, _meta: Optional[Dict[str, Any]] = None) -> None:
        return

    try:
        if handler == "sora_gen_video":
            from ..services.sora_plus_register_executor import sora_plus_register

            result = await sora_plus_register(
                {"headless": headless, "pure_mode": effective_pure},
                _progress_cb,
                db=db,
                window_pk=window_pk,
                browser_vendor=vendor,
                browser_base_url=base_url,
                browser_access_key=access_key,
                space_id=space_id,
                window_key=window_key,
                timeout_seconds=timeout_seconds,
            )
        elif handler == "veo_workflow":
            from ..services.veo_workflow_executor import veo_admin_unified_open_or_connect

            try:
                gl_ms = int(float(ctx_row.get("task_timeout_seconds") or 120) * 1000)
            except Exception:
                gl_ms = 120_000
            gl_ms = max(45_000, min(gl_ms, 240_000))
            result = await veo_admin_unified_open_or_connect(
                _progress_cb,
                db=db,
                window_pk=window_pk,
                browser_vendor=vendor,
                browser_base_url=base_url,
                browser_access_key=access_key,
                space_id=space_id,
                window_key=window_key,
                timeout_seconds=timeout_seconds,
                headless=headless,
                default_target_url=str(ctx_row.get("default_target_url") or "").strip(),
                google_login_timeout_ms=gl_ms,
                pure_mode=effective_pure,
            )
        elif handler == "dreamina_workflow":
            result = await _dreamina_direct_login_for_mapping(
                mapping_id,
                headless=headless,
                pure_mode=effective_pure,
            )
        elif handler == "grok_workflow":
            from ..services.grok_workflow_executor import grok_admin_open_connect_page  # type: ignore

            result = await grok_admin_open_connect_page(
                browser_vendor=vendor,
                browser_base_url=base_url,
                browser_access_key=access_key,
                space_id=space_id,
                window_key=window_key,
                headless=headless,
                default_target_url=str(ctx_row.get("default_target_url") or "").strip(),
                pure_mode=effective_pure,
                timeout_seconds=timeout_seconds,
            )
        elif handler == "gpt_workflow":
            from ..services.gpt_task_executor import DEFAULT_GPT_TARGET  # type: ignore
            from ..services.browser_extension_interaction import ensure_extension_connected_via_window  # type: ignore
            from ..services.veo_workflow_executor import get_or_create_veo_session  # type: ignore

            target_url = str(ctx_row.get("default_target_url") or "").strip() or DEFAULT_GPT_TARGET
            gpt_ctx = get_or_create_veo_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
            gpt_ctx.browser_headless = headless
            gpt_ctx.browser_pure_mode = effective_pure
            gpt_ctx.idle_close_disabled = True
            try:
                gpt_ctx._cancel_idle_close()
            except Exception:
                pass
            client = await ensure_extension_connected_via_window(
                sess=gpt_ctx,
                target_url=target_url,
                space_id=space_id,
                window_key=window_key,
                wait_seconds=30.0,
                headless=headless,
                pure_mode=effective_pure,
                auto_triger_connection=True,
            )
            if client is None:
                raise RuntimeError("GPT browser extension is not connected")
            try:
                await gpt_ctx.disconnect_playwright_under_bring_lock()
            except Exception:
                pass
            result = {"message": "已打开 ChatGPT 页面，已断开自动化连接（请在本窗口完成登录）", "target_url": target_url}
        else:
            raise HTTPException(
                status_code=400,
                detail="当前任务类型不支持开号/连接（仅 sora_gen_video / veo_workflow / dreamina_workflow / grok_workflow / gpt_workflow）",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"开号/连接失败：{e}")

    return {"success": True, "mapping_id": mapping_id, "result": result}


@router.post("/api/admin/task-type-windows/{mapping_id}/manual-start")
async def manual_start_mapping_window(
    mapping_id: int,
    headless: bool = False,
    pure_mode: Optional[bool] = Query(
        None, description="指纹 browser_open 纯净模式；省略时按绑定 pure_mode 列"
    ),
    token: str = Depends(verify_admin_token),
):
    """立刻关闭指纹浏览器窗口并重新打开，进入 Sora drafts / Veo/Grok/Dreamina/GPT 目标页。

    不修改绑定上的 enabled（启用）状态，仅做窗口重启与页面置前。
    置前完成后断开本地 CDP（不关指纹窗口），降低站点通过调试端口识别自动化、触发 Cloudflare 的概率。
    """
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
    default_target_url = str(ctx_row.get("default_target_url") or "").strip()
    effective_pure = _effective_browser_pure_mode(ctx_row, pure_mode)

    if handler == "veo_workflow":
        from ..services.veo_workflow_executor import get_or_create_veo_session  # type: ignore

        target_url = default_target_url or "https://veo.google.com"
        veo_ctx = get_or_create_veo_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        veo_ctx.browser_headless = headless
        veo_ctx.browser_pure_mode = effective_pure
        veo_ctx.idle_close_disabled = True
        try:
            veo_ctx._cancel_idle_close()
        except Exception:
            pass
        try:
            await veo_ctx.close_and_drop()
        except Exception:
            pass
        try:
            await veo_ctx.ensure_open(args=veo_ctx.browser_open_args, force_open=True, headless=headless)
            await veo_ctx._bring_target_page_to_front(refresh_target=False, drafts_url=target_url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"启动窗口失败：{e}")
        try:
            await veo_ctx.disconnect_playwright_under_bring_lock()
        except Exception:
            pass
    elif handler == "grok_workflow":
        from ..services.grok_workflow_executor import DEFAULT_GROK_TARGET, get_or_create_grok_session  # type: ignore

        target_url = default_target_url or DEFAULT_GROK_TARGET
        grok_ctx = get_or_create_grok_session(
            vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key
        )
        grok_ctx.browser_headless = headless
        grok_ctx.browser_pure_mode = effective_pure
        grok_ctx.idle_close_disabled = True
        try:
            grok_ctx._cancel_idle_close()
        except Exception:
            pass
        try:
            await grok_ctx.close_and_drop()
        except Exception:
            pass
        try:
            await grok_ctx.ensure_open(
                args=grok_ctx.browser_open_args,
                force_open=True,
                headless=headless,
                pure_mode=effective_pure,
            )
            await grok_ctx._bring_target_page_to_front(refresh_target=False, drafts_url=target_url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"启动窗口失败：{e}")
        try:
            await grok_ctx.disconnect_playwright_under_bring_lock()
        except Exception:
            pass
    elif handler == "dreamina_workflow":
        from ..services.jimeng_task_executor import DEFAULT_DREAMINA_TARGET, get_or_create_dreamina_session  # type: ignore

        target_url = default_target_url or DEFAULT_DREAMINA_TARGET
        dreamina_ctx = get_or_create_dreamina_session(
            vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key
        )
        dreamina_ctx.browser_headless = headless
        dreamina_ctx.browser_pure_mode = effective_pure
        dreamina_ctx.idle_close_disabled = True
        try:
            dreamina_ctx._cancel_idle_close()
        except Exception:
            pass
        try:
            await dreamina_ctx.close_and_drop()
        except Exception:
            pass
        try:
            await dreamina_ctx.ensure_open(
                args=dreamina_ctx.browser_open_args,
                force_open=True,
                headless=headless,
                pure_mode=effective_pure,
            )
            await dreamina_ctx._bring_target_page_to_front(refresh_target=False, drafts_url=target_url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"启动窗口失败：{e}")
        try:
            await dreamina_ctx.disconnect_playwright_under_bring_lock()
        except Exception:
            pass
    elif handler == "gpt_workflow":
        from ..services.gpt_task_executor import DEFAULT_GPT_TARGET  # type: ignore
        from ..services.browser_extension_interaction import ensure_extension_connected_via_window  # type: ignore
        from ..services.veo_workflow_executor import get_or_create_veo_session  # type: ignore

        target_url = default_target_url or DEFAULT_GPT_TARGET
        gpt_ctx = get_or_create_veo_session(
            vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key
        )
        gpt_ctx.browser_headless = headless
        gpt_ctx.browser_pure_mode = effective_pure
        gpt_ctx.idle_close_disabled = True
        try:
            gpt_ctx._cancel_idle_close()
        except Exception:
            pass
        try:
            await gpt_ctx.close_and_drop()
        except Exception:
            pass
        try:
            client = await ensure_extension_connected_via_window(
                sess=gpt_ctx,
                target_url=target_url,
                space_id=space_id,
                window_key=window_key,
                wait_seconds=30.0,
                force_open=True,
                headless=headless,
                pure_mode=effective_pure,
                auto_triger_connection=True,
            )
            if client is None:
                raise RuntimeError("GPT browser extension is not connected")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"启动窗口失败：{e}")
        try:
            await gpt_ctx.disconnect_playwright_under_bring_lock()
        except Exception:
            pass
    else:
        from ..services.sora_task_executor import get_or_create_sora_session  # type: ignore

        target_url = default_target_url or "https://sora.chatgpt.com/drafts"
        sora_ctx = get_or_create_sora_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        sora_ctx.browser_headless = headless
        sora_ctx.browser_pure_mode = effective_pure
        sora_ctx.idle_close_disabled = True
        try:
            sora_ctx._cancel_idle_close()
        except Exception:
            pass
        try:
            await sora_ctx.close_and_drop()
        except Exception:
            pass
        try:
            await sora_ctx.ensure_open(args=[], force_open=True, headless=headless)
            await sora_ctx._bring_sora_drafts_to_front(refresh_target=False, drafts_url=target_url)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"启动窗口失败：{e}")
        try:
            await sora_ctx.disconnect_playwright_under_bring_lock()
        except Exception:
            pass

    enabled_before = bool(int(ctx_row.get("enabled") or 0))
    return {"success": True, "mapping_id": mapping_id, "enabled": enabled_before, "idle_close_disabled": True}


@router.post("/api/admin/task-type-windows/{mapping_id}/veo-connect-bring")
async def veo_connect_bring_mapping_window(mapping_id: int, headless: bool = False, token: str = Depends(verify_admin_token)):
    """连接 CDP 并置前 Veo 目标页（不重启指纹窗口）。在 _bring_drafts_lock 下执行 ensure_open + _bring_target_page_to_front。

    若存在 Google 账号选择页会先点选 @gmail.com；随后在 accounts.google.com 上按窗口凭据自动填密码/邮箱/TOTP（与开号同源，EFA 可选）。
    完成后断开本地 CDP，与 manual-start 一致。
    """
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx_row = await db.get_task_type_window_context(mapping_id)
    if not ctx_row:
        raise HTTPException(status_code=404, detail="mapping not found")

    handler = str(ctx_row.get("create_task_handler") or "").strip().lower()
    if handler != "veo_workflow":
        raise HTTPException(status_code=400, detail="仅 veo_workflow 任务类型支持该操作")

    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "")
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "")
    window_key = str(ctx_row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="mapping missing vendor/lan_addr/space_id/window_key")

    from ..services.veo_workflow_executor import get_or_create_veo_session  # type: ignore

    target_url = str(ctx_row.get("default_target_url") or "").strip() or "https://veo.google.com"
    veo_ctx = get_or_create_veo_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
    veo_ctx.browser_headless = headless
    veo_ctx.idle_close_disabled = True
    try:
        veo_ctx._cancel_idle_close()
    except Exception:
        pass
    try:
        async with veo_ctx._bring_drafts_lock:
            await veo_ctx.ensure_open(
                args=veo_ctx.browser_open_args,
                force_open=veo_ctx.browser_force_open,
                headless=headless,
                acquire_bring_lock=False,
            )
            try:
                gl_ms = int(float(ctx_row.get("task_timeout_seconds") or 120) * 1000)
            except Exception:
                gl_ms = 120_000
            gl_ms = max(45_000, min(gl_ms, 240_000))
            wpk = int(ctx_row.get("window_pk") or 0)
            await veo_ctx._bring_target_page_to_front(
                refresh_target=True,
                drafts_url=target_url,
                acquire_bring_lock=False,
                google_login_db=db,
                google_login_window_pk=wpk if wpk > 0 else None,
                google_login_timeout_ms=gl_ms,
            )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"连接并置前失败：{e}")
    try:
        await veo_ctx.disconnect_playwright_under_bring_lock()
    except Exception:
        pass

    enabled_before = bool(int(ctx_row.get("enabled") or 0))
    return {"success": True, "mapping_id": mapping_id, "enabled": enabled_before, "idle_close_disabled": True}


@router.delete("/api/admin/task-types/{task_type_id}")
async def delete_task_type(task_type_id: int, token: str = Depends(verify_admin_token)):
    await _ensure_admin_user(token)
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    await db.delete_task_type(task_type_id)
    return {"success": True}


@router.get("/api/admin/task-types/{task_type_id}/windows")
async def list_task_type_windows(task_type_id: int, token: str = Depends(verify_admin_token)):
    user = await _ensure_any_page_access(token, {"task_types", "test", "paypal"})
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    allowed_ids = await _get_allowed_task_type_ids(user)
    if allowed_ids is not None and int(task_type_id) not in {int(x) for x in allowed_ids}:
        raise HTTPException(status_code=403, detail="无权查看该任务类型")
    return {"success": True, "items": await db.list_task_type_windows(task_type_id)}


@router.post("/api/admin/task-types/{task_type_id}/windows")
async def add_task_type_windows(task_type_id: int, req: AddTaskTypeWindowsRequest, token: str = Depends(verify_admin_token)):
    user = await _ensure_page_access(token, "task_types")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    allowed_task_type_ids = await _get_allowed_task_type_ids(user)
    if allowed_task_type_ids is not None and int(task_type_id) not in {int(x) for x in allowed_task_type_ids}:
        raise HTTPException(status_code=403, detail="无权操作该任务类型")

    allowed_project_ids = await _get_allowed_project_ids(user)
    if allowed_project_ids is not None:
        pairs = await db.list_window_project_pairs([int(x) for x in (req.window_pks or [])])
        missing = [int(x) for x in (req.window_pks or []) if int(x) not in pairs]
        if missing:
            raise HTTPException(status_code=400, detail=f"窗口不存在或已删除: {missing[:5]}")
        allowed_set = {int(x) for x in allowed_project_ids}
        denied = [wid for wid, pid in pairs.items() if int(pid) not in allowed_set]
        if denied:
            raise HTTPException(status_code=403, detail=f"无权绑定这些窗口: {denied[:5]}")
    affected = await db.add_task_type_windows(
        task_type_id=task_type_id,
        window_pks=req.window_pks,
        daily_quota=req.daily_quota,
        remaining_quota=req.remaining_quota,
        enabled=req.enabled,
    )
    return {"success": True, "affected": affected}


@router.patch("/api/admin/task-type-windows/{mapping_id}")
async def update_task_type_window(mapping_id: int, req: UpdateTaskTypeWindowRequest, token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    await _ensure_page_access(token, "task_types")
    if req.task_type_id is not None:
        ctx = await db.get_task_type_window_context(mapping_id)
        if not ctx:
            raise HTTPException(status_code=404, detail="绑定不存在或已删除")

        src_type_id = int(ctx.get("task_type_id") or 0)
        target_type_id = int(req.task_type_id)
        if target_type_id != src_type_id:
            target = await db.get_task_type(target_type_id)
            if not target:
                raise HTTPException(status_code=400, detail="目标任务类型不存在")

            same_type_rows = await db.list_task_type_windows(target_type_id)
            target_has_same_window = any(
                int(row.get("window_pk") or 0) == int(ctx.get("window_pk") or 0)
                and int(row.get("id") or 0) != int(mapping_id)
                for row in (same_type_rows or [])
            )
            if target_has_same_window:
                raise HTTPException(status_code=400, detail="目标任务类型已绑定该窗口")
    try:
        await db.update_task_type_window(
            mapping_id=mapping_id,
            enabled=req.enabled,
            headless=req.headless,
            pure_mode=req.pure_mode,
            deleted=req.deleted,
            task_type_id=req.task_type_id,
            daily_quota=req.daily_quota,
            remaining_quota=req.remaining_quota,
            cooldown_until=req.cooldown_until,
            error_cooldown_until=req.error_cooldown_until,
            total_errors=req.total_errors,
            consecutive_errors=req.consecutive_errors,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}


def _admin_veo_pool_base_name(raw: Optional[str]) -> str:
    s = (raw or "").strip()
    if s:
        parts = s.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].startswith("P") and parts[1][1:].isdigit():
            return parts[0]
        return s
    return datetime.now().strftime("%b %d - %H:%M")


def _admin_veo_pooled_project_title(pool_index: int, base_name: Optional[str]) -> str:
    return f"{_admin_veo_pool_base_name(base_name)} P{pool_index}"


@router.get("/api/admin/task-type-windows/{mapping_id}/veo-flow-projects")
async def admin_list_veo_flow_projects(mapping_id: int, token: str = Depends(verify_admin_token)):
    user = await _ensure_any_page_access(token, {"task_types", "test"})
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    ctx = await db.get_task_type_window_context(mapping_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="绑定不存在或已删除")
    allowed = await _get_allowed_task_type_ids(user)
    ttid = int(ctx.get("task_type_id") or 0)
    if allowed is not None and ttid not in {int(x) for x in allowed}:
        raise HTTPException(status_code=403, detail="无权查看该绑定")
    if str(ctx.get("create_task_handler") or "").strip().lower() != "veo_workflow":
        raise HTTPException(status_code=400, detail="仅 veo_workflow 任务类型支持 Veo 项目管理")
    items = await db.list_veo_flow_projects(mapping_id)
    return {"success": True, "items": items}


@router.post("/api/admin/task-type-windows/{mapping_id}/veo-flow-projects")
async def admin_create_veo_flow_project(
    mapping_id: int,
    headless: bool = False,
    base_name: Optional[str] = Query(None, max_length=240),
    token: str = Depends(verify_admin_token),
):
    """在指纹窗口内创建 Flow 项目并入库；成功后断开本地 CDP（不关指纹窗口）。"""
    await _ensure_page_access(token, "task_types")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx = await db.get_task_type_window_context(mapping_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="绑定不存在或已删除")

    user = await _get_user_by_token(token)
    allowed_task_type_ids = await _get_allowed_task_type_ids(user)
    ttid = int(ctx.get("task_type_id") or 0)
    if allowed_task_type_ids is not None and ttid not in {int(x) for x in allowed_task_type_ids}:
        raise HTTPException(status_code=403, detail="无权操作该绑定")

    if str(ctx.get("create_task_handler") or "").strip().lower() != "veo_workflow":
        raise HTTPException(status_code=400, detail="仅 veo_workflow 任务类型可通过此处创建 Flow 项目")

    vendor = str(ctx.get("vendor") or "roxy")
    base_url = str(ctx.get("lan_addr") or "")
    access_key = ctx.get("access_key")
    space_id = str(ctx.get("space_id") or "")
    window_key = str(ctx.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="绑定缺少浏览器/空间/窗口信息")

    n_existing = await db.count_veo_flow_projects(mapping_id)
    pool_index = n_existing + 1
    project_title = _admin_veo_pooled_project_title(pool_index, base_name)

    default_target_url = str(ctx.get("default_target_url") or "").strip()
    target_url = default_target_url or "https://labs.google/fx"

    from ..services.veo_workflow_executor import get_or_create_veo_session, veo_create_flow_project_in_window  # type: ignore

    sess = get_or_create_veo_session(
        vendor=vendor,
        base_url=base_url,
        access_key=access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = bool(headless)

    try:
        flow_project_id = await veo_create_flow_project_in_window(
            sess=sess,
            target_url=target_url,
            title=project_title,
            tool_name="PINHOLE",
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"创建 Flow 项目失败：{e}")

    row_id = await db.add_veo_flow_project(
        task_type_window_id=mapping_id,
        project_id=flow_project_id,
        project_name=project_title,
        tool_name="PINHOLE",
    )

    try:
        await sess.disconnect_playwright_under_bring_lock()
    except Exception:
        pass

    return {
        "success": True,
        "id": row_id,
        "project_id": flow_project_id,
        "project_name": project_title,
        "veo_flow_project_count": n_existing + 1,
    }


@router.delete("/api/admin/task-type-windows/{mapping_id}/veo-flow-projects/{flow_project_id}")
async def admin_delete_veo_flow_project(
    mapping_id: int,
    flow_project_id: str,
    headless: bool = False,
    token: str = Depends(verify_admin_token),
):
    """在指纹窗口内删除 Flow 项目，并将本地项目池记录标记为 deleted。"""
    await _ensure_page_access(token, "task_types")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    ctx = await db.get_task_type_window_context(mapping_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="绑定不存在或已删除")

    user = await _get_user_by_token(token)
    allowed_task_type_ids = await _get_allowed_task_type_ids(user)
    ttid = int(ctx.get("task_type_id") or 0)
    if allowed_task_type_ids is not None and ttid not in {int(x) for x in allowed_task_type_ids}:
        raise HTTPException(status_code=403, detail="无权操作该绑定")

    if str(ctx.get("create_task_handler") or "").strip().lower() != "veo_workflow":
        raise HTTPException(status_code=400, detail="仅 veo_workflow 任务类型可通过此处删除 Flow 项目")

    project_id = str(flow_project_id or "").strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id 不能为空")

    row = await db.get_veo_flow_project(mapping_id, project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Flow 项目不存在或已删除")

    vendor = str(ctx.get("vendor") or "roxy")
    base_url = str(ctx.get("lan_addr") or "")
    access_key = ctx.get("access_key")
    space_id = str(ctx.get("space_id") or "")
    window_key = str(ctx.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise HTTPException(status_code=400, detail="绑定缺少浏览器/空间/窗口信息")

    default_target_url = str(ctx.get("default_target_url") or "").strip()
    target_url = default_target_url or "https://labs.google/fx"

    from ..services.veo_workflow_executor import get_or_create_veo_session, veo_delete_flow_project_in_window  # type: ignore

    sess = get_or_create_veo_session(
        vendor=vendor,
        base_url=base_url,
        access_key=access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = bool(headless)

    remote_result: Dict[str, Any]
    try:
        try:
            remote_result = await veo_delete_flow_project_in_window(
                sess=sess,
                target_url=target_url,
                project_id=project_id,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"删除 Flow 项目失败：{e}")
    finally:
        try:
            await sess.disconnect_playwright_under_bring_lock()
        except Exception:
            pass

    affected = await db.mark_veo_flow_project_deleted(mapping_id, project_id)
    remaining = await db.count_veo_flow_projects(mapping_id)

    return {
        "success": True,
        "project_id": project_id,
        "project_name": row.get("project_name") or "",
        "deleted": affected > 0,
        "veo_flow_project_count": remaining,
        "remote": remote_result,
    }


# -------------------- tasks --------------------
@router.get("/api/admin/tasks")
async def list_tasks(
    limit: int = 50,
    offset: int = 0,
    task_type_code: Optional[str] = None,
    status: Optional[str] = None,
    window_pk: Optional[int] = None,
    window_ip: Optional[str] = None,
    q: Optional[str] = None,
    prompt_tag: Optional[str] = None,
    token: str = Depends(verify_admin_token),
):
    await _ensure_page_access(token, "tasks")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    lim = max(1, min(200, int(limit or 50)))
    off = max(0, int(offset or 0))
    total = await db.count_tasks(task_type_code=task_type_code, status=status, window_pk=window_pk, window_ip=window_ip, q=q, prompt_tag=prompt_tag)
    items = await db.list_tasks(
        limit=lim,
        offset=off,
        task_type_code=task_type_code,
        status=status,
        window_pk=window_pk,
        window_ip=window_ip,
        q=q,
        prompt_tag=prompt_tag,
    )
    return {"success": True, "total": total, "limit": lim, "offset": off, "items": items}


@router.get("/api/admin/tasks/timeline")
async def list_task_success_fail_timeline(
    group_by: str = "account",  # account | ip
    bucket: str = "day",  # month | week | day | hour | minute
    limit: int = 100,
    offset: int = 0,
    task_type_code: Optional[str] = None,
    q: Optional[str] = None,
    start_at: Optional[str] = None,
    end_at: Optional[str] = None,
    token: str = Depends(verify_admin_token),
):
    await _ensure_page_access(token, "tasks")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    data = await db.list_task_success_fail_timeline(
        group_by=group_by,
        bucket=bucket,
        limit=limit,
        offset=offset,
        task_type_code=task_type_code,
        q=q,
        start_at=start_at,
        end_at=end_at,
    )
    return {"success": True, **data}


@router.get("/api/admin/tasks/timeline-items")
async def list_task_timeline_items(
    group_by: str = "account",  # account | ip
    bucket: str = "day",  # month | week | day | hour | minute
    group_key: str = "",
    bucket_start: str = "",
    limit: int = 200,
    offset: int = 0,
    task_type_code: Optional[str] = None,
    token: str = Depends(verify_admin_token),
):
    await _ensure_page_access(token, "tasks")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    if not str(bucket_start or "").strip():
        raise HTTPException(status_code=400, detail="bucket_start is required")

    data = await db.list_task_timeline_items(
        group_by=group_by,
        bucket=bucket,
        group_key=group_key,
        bucket_start=bucket_start,
        limit=limit,
        offset=offset,
        task_type_code=task_type_code,
    )
    return {"success": True, **data}


@router.get("/api/admin/tasks/{task_id}")
async def get_admin_task_detail(
    task_id: str,
    token: str = Depends(verify_admin_token),
):
    await _ensure_page_access(token, "tasks")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    tid = str(task_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="task_id is required")

    async with db._read_conn() as conn:
        conn.row_factory = __import__("aiosqlite").Row
        cur = await conn.execute(
            """
            SELECT
              t.task_id,
              t.task_type_code,
              t.generation_id,
              t.status,
              t.progress,
              t.prompt,
              t.window_pk,
              t.window_ip,
              w.platform_account AS window_account,
              w.window_sort_num AS window_sort_num,
              s.name AS space_name,
              t.error_message,
              t.content_violation,
              t.result_json,
              t.created_at,
              t.updated_at
            FROM tasks t
            LEFT JOIN windows w ON w.id = t.window_pk
            LEFT JOIN spaces s ON s.id = w.space_pk
            WHERE t.task_id = ?
            LIMIT 1
            """,
            (tid,),
        )
        row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    return {"success": True, "item": dict(row)}


# -------------------- logs --------------------
@router.get("/api/admin/image-resources")
async def list_image_resources(
    limit: int = 50,
    offset: int = 0,
    token: str = Depends(verify_admin_token),
):
    await _ensure_page_access(token, "image_resources")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    lim = max(1, min(200, int(limit or 50)))
    off = max(0, int(offset or 0))
    async with db._read_conn() as conn:
        conn.row_factory = __import__("aiosqlite").Row
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dreamina_subject_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT,
                subject_id TEXT,
                data_id TEXT,
                name TEXT NOT NULL,
                image_uri TEXT NOT NULL,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
            """
        )
        cur = await conn.execute("SELECT COUNT(*) FROM dreamina_subject_images")
        total = int((await cur.fetchone())[0] or 0)
        cur = await conn.execute(
            """
            SELECT id, uid, subject_id, data_id, name, image_uri, width, height, created_at, updated_at
            FROM dreamina_subject_images
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (lim, off),
        )
        rows = await cur.fetchall()
    return {"success": True, "total": total, "limit": lim, "offset": off, "items": [dict(r) for r in rows]}


@router.get("/api/admin/logs")
async def get_logs(limit: int = 200, token: str = Depends(verify_admin_token)):
    await _ensure_page_access(token, "logs")
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    return {"success": True, "logs": await db.get_request_logs(limit=limit)}


@router.delete("/api/admin/logs")
async def clear_logs(token: str = Depends(verify_admin_token)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    rebuilt = await db.clear_request_logs()
    return {"success": True, "rebuilt": rebuilt}
