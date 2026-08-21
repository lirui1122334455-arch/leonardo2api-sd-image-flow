"""任务类型动态函数注册表（创建任务 / 刷新窗口额度）。

设计目标：
- 前端可为每个 task_type 选择“创建任务函数(create_task_handler)”与“刷新剩余额度函数(refresh_quota_handler)”
- 后端仅保存函数 key（字符串），运行时通过注册表映射到真实 callable
- 你后续要新增实现：只需在本文件新增函数并注册到字典即可（无需改 DB 结构）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..core.models import TaskType


# -------------------- contexts --------------------
@dataclass(frozen=True)
class CreateTaskContext:
    task_type: TaskType
    # 任意参数（由具体 handler 自行解析）
    payload: Dict[str, Any]

    # 延迟导入/传入：避免循环依赖（TaskService 也会 import 其它 services）
    db: Any
    task_service: Any

    # 可选：指定执行窗口（仅用于调试/测试；不指定则走默认调度）
    mapping_id: Optional[int] = None
    window_pk: Optional[int] = None


@dataclass(frozen=True)
class RefreshQuotaContext:
    """刷新窗口剩余额度上下文。

    mapping_row 为 DB join 后的 dict，至少包含：
    - id (mapping_id)
    - task_code (task_type.code)
    - remaining_quota / daily_quota
    - window_pk / window_key
    - vendor / lan_addr / access_key / space_id
    """

    task_type: TaskType
    mapping_row: Dict[str, Any]
    db: Any


CreateTaskHandler = Callable[[CreateTaskContext], Awaitable[str]]
RefreshQuotaHandler = Callable[[RefreshQuotaContext], Awaitable[int]]


def _label(key: str, text: str) -> Dict[str, str]:
    return {"key": key, "label": text}


# -------------------- built-in create task handlers --------------------
async def create_task__default_submit(ctx: CreateTaskContext) -> str:
    """默认：走 TaskService.submit_task（原有行为）。"""

    return await ctx.task_service.submit_task(
        ctx.task_type.code,
        ctx.payload or {},
        mapping_id=ctx.mapping_id,
        window_pk=ctx.window_pk,
    )


async def create_task__force_gen_image(ctx: CreateTaskContext) -> str:
    """示例：无论选择什么任务类型，都强制按 gen_image 创建（便于演示/调试）。"""

    return await ctx.task_service.submit_task(
        "gen_image",
        ctx.payload or {},
        mapping_id=ctx.mapping_id,
        window_pk=ctx.window_pk,
    )


async def create_task__sora_gen_video(ctx: CreateTaskContext) -> str:
    """Sora 生视频：同样走 submit_task，执行阶段在 _run_task 中分发到 sora_task_executor.py。"""

    return await ctx.task_service.submit_task(
        ctx.task_type.code,
        ctx.payload or {},
        mapping_id=ctx.mapping_id,
        window_pk=ctx.window_pk,
    )


CREATE_TASK_HANDLERS: Dict[str, Tuple[str, CreateTaskHandler]] = {
    "leonardo_workflow": ("Leonardo Seedance 2.0 video generation", create_task__sora_gen_video),
    "default_submit": ("默认：submit_task 调度", create_task__default_submit),
    "force_gen_image": ("示例：强制 gen_image", create_task__force_gen_image),
    "sora_gen_video": ("Sora：生成视频（prompt + first_image_url + duration）", create_task__sora_gen_video),
    "sora_wm_remove": ("Sora：视频去水印（输入 soraUrl 返回 videoUrl）", create_task__sora_gen_video),
    "sora_plus_register": ("Sora注册Plus）", create_task__sora_gen_video),
    "veo_workflow": ("veo生成视频", create_task__sora_gen_video),
    "gpt_workflow": ("GPT/ChatGPT 图片/视频生成（浏览器插件；长 access_token）", create_task__sora_gen_video),
    "grok_workflow": ("Grok Imagine 视频（指纹浏览器：文生/多图参考；可选 mapping SSO 或 payload grok_access_token）", create_task__sora_gen_video),
    "dreamina_workflow": ("Dreamina Seedance 视频生成（指纹浏览器：文生/图生；Cookie 鉴权）", create_task__sora_gen_video),
    "fish_audio_workflow": ("Fish Audio TTS（指纹浏览器登录会话）", create_task__sora_gen_video),
    "elevenlabs_workflow": ("ElevenLabs 音效/TTS（指纹浏览器登录会话）", create_task__sora_gen_video),
    "zarklab_workflow": ("Zark Lab 视频生成（指纹浏览器登录会话）", create_task__sora_gen_video),
}


# -------------------- built-in quota refresh handlers --------------------
async def refresh_quota__noop(ctx: RefreshQuotaContext) -> int:
    """默认：不查询外部平台，仅返回当前 remaining_quota。"""

    v = ctx.mapping_row.get("remaining_quota")
    try:
        return int(v or 0)
    except Exception:
        return 0


async def refresh_quota__reset_to_daily(ctx: RefreshQuotaContext) -> int:
    """示例：将 remaining_quota 重置为 daily_quota（适合“每日刷新”场景）。"""

    v = ctx.mapping_row.get("daily_quota")
    try:
        return int(v or 0)
    except Exception:
        return 0


async def refresh_quota__sora_nf_check(ctx: RefreshQuotaContext) -> int:
    """Sora：通过指纹浏览器读取 /backend/nf/check，并把余额字段写回 mapping。"""
    row = ctx.mapping_row or {}
    vendor = str(row.get("vendor") or "roxy")
    base_url = str(row.get("lan_addr") or "")
    access_key = row.get("access_key")
    space_id = str(row.get("space_id") or "")
    window_key = str(row.get("window_key") or "")
    if not base_url or not space_id or not window_key:
        raise RuntimeError("mapping 缺少 vendor/lan_addr/space_id/window_key，无法刷新 Sora 余额")

    # 复用 SoraSession（避免重复开浏览器）
    from .sora_task_executor import get_or_create_sora_session  # type: ignore

    sora_ctx = get_or_create_sora_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
    if "_headless" in row:
        sora_ctx.browser_headless = bool(row["_headless"])
    try:
        sora_ctx.set_access_token(row.get("sora_access_token"), row.get("sora_access_expires"))
    except Exception:
        pass
    target_url = "https://sora.chatgpt.com/drafts"
    info = await sora_ctx.api_nf_check(target_url=target_url)

    remaining = int(info.get("remaining_count") or 0)
    purchased_remaining = int(info.get("purchased_remaining_count") or 0)
    rate_limit_reached = bool(info.get("rate_limit_reached", False))
    resets = int(info.get("access_resets_in_seconds") or 0)
    cooldown_until = info.get("cooldown_until")

    # 写回 DB：remaining_quota 也同步为 remaining_count，保持调度逻辑一致
    kwargs: Dict[str, Any] = {
        "mapping_id": int(row.get("id") or 0),
        "remaining_quota": remaining,
        "sora_remaining_count": remaining,
        "sora_purchased_remaining_count": purchased_remaining,
        "sora_rate_limit_reached": rate_limit_reached,
        "sora_access_resets_in_seconds": resets,
    }
    # api_nf_check 会返回 cooldown_until（当前时间+resets 秒），用于表示“额度重置时间点”
    if cooldown_until:
        kwargs["cooldown_until"] = str(cooldown_until)
    await ctx.db.update_task_type_window(**kwargs)
    return remaining


async def refresh_quota__veo_flow_credits(ctx: RefreshQuotaContext) -> int:
    """VEO / Google Labs：优先通过浏览器插件刷新余额，失败后回退两层代理。"""
    row = ctx.mapping_row or {}
    space_id = str(row.get("space_id") or "")
    window_key = str(row.get("window_key") or "")
    if not space_id or not window_key:
        raise RuntimeError("mapping 缺少 space_id/window_key，无法读取窗口代理")

    default_target_url = str(row.get("default_target_url") or "").strip()
    target_url = default_target_url or "https://labs.google/fx"

    from types import SimpleNamespace

    from .veo_workflow_executor import (  # type: ignore
        fetch_short_access_token_by_proxy,
        refresh_veo_balance_via_extension,
        veo_fetch_credits_by_proxy,
    )

    def _row_bool(key: str, default: bool = False) -> bool:
        v = row.get(key)
        if v is None:
            return bool(default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(v)

    picked = SimpleNamespace(
        window_pk=int(row.get("window_pk") or 0),
        mapping_id=int(row.get("id") or row.get("mapping_id") or 0),
        task_code=str(row.get("task_code") or getattr(ctx.task_type, "code", "") or ""),
        default_target_url=target_url,
        browser_vendor=str(row.get("vendor") or "roxy"),
        browser_base_url=str(row.get("lan_addr") or ""),
        browser_access_key=row.get("access_key"),
        space_id=space_id,
        window_key=window_key,
        headless=_row_bool("_headless", _row_bool("headless", False)),
        pure_mode=_row_bool("pure_mode", True),
    )

    # 快路径：浏览器插件直接读取 short token + credits，避免本地两层代理反复换 token/查余额。
    # refresh_veo_balance_via_extension 内部会捕获异常并返回 None；只要没有 credits 就走旧逻辑兜底。
    ext_info = await refresh_veo_balance_via_extension(
        db=ctx.db,
        picked=picked,
        refresh_timeout_seconds=30.0,
        auto_triger_connection = False,
        force_refresh_token=True,
    )
    if isinstance(ext_info, dict) and ext_info.get("credits") is not None:
        return int(ext_info.get("credits") or 0)

    # 兜底：沿用旧的两层代理流程（长 session-token -> short access_token -> credits）。
    at = str(row.get("sora_access_token") or "").strip()
    if not at:
        raise RuntimeError("缺少 access_token（插件未读取到余额，且未保存 access_token，无法回退两层代理）")

    short_info = await fetch_short_access_token_by_proxy(
        session_token=at,
        target_url=target_url,
        db=ctx.db,
        picked=picked,
    )
    info = await veo_fetch_credits_by_proxy(
        sess=None,
        target_url=target_url,
        access_token=str((short_info or {}).get("access_token") or ""),
        db=ctx.db,
        picked=picked,
    )
    credits = int(info.get("credits") or 0)

    kwargs: Dict[str, Any] = {
        "mapping_id": int(row.get("id") or 0),
        "remaining_quota": credits,
        "sora_remaining_count": credits,
    }
    cu = info.get("cooldown_until")
    if cu:
        kwargs["cooldown_until"] = str(cu)
    await ctx.db.update_task_type_window(**kwargs)
    return credits


async def refresh_quota__dreamina_credits(ctx: RefreshQuotaContext) -> int:
    """Dreamina：本地请求 commerce API 读取余额（total_credit）。"""
    row = ctx.mapping_row or {}
    space_id = str(row.get("space_id") or "")
    window_key = str(row.get("window_key") or "")
    if not space_id or not window_key:
        raise RuntimeError("mapping 缺少 space_id/window_key，无法读取窗口绑定 IP 国家")

    default_target_url = str(row.get("default_target_url") or "").strip()
    target_url = default_target_url or "https://dreamina.capcut.com/ai-tool/video/generate"

    from types import SimpleNamespace

    from .jimeng_task_executor import dreamina_fetch_credits_in_window  # type: ignore

    access_token = str(row.get("sora_access_token") or "").strip()
    if not access_token:
        raise RuntimeError("缺少 Dreamina sessionid，请先点击 access_token 列的“更新”读取并保存")

    window_pk = int(row.get("window_pk") or 0)
    country_code = ""
    try:
        if window_pk > 0:
            country_code = await ctx.db.get_window_bound_ip_last_country(window_pk=window_pk)
        if not country_code:
            country_code = await ctx.db.get_window_bound_ip_last_country(space_id=space_id, window_key=window_key)
    except Exception:
        country_code = ""

    picked = SimpleNamespace(
        window_pk=window_pk,
        mapping_id=int(row.get("id") or row.get("mapping_id") or 0),
        space_id=space_id,
        window_key=window_key,
    )
    info = await dreamina_fetch_credits_in_window(
        target_url=target_url,
        access_token=access_token,
        db=ctx.db,
        picked=picked,
        country_code=country_code,
    )
    total_credit = int(info.get("total_credit") or 0)

    kwargs: Dict[str, Any] = {
        "mapping_id": int(row.get("id") or 0),
        "remaining_quota": total_credit,
        "sora_remaining_count": total_credit,
    }
    cu = info.get("cooldown_until")
    if cu:
        kwargs["cooldown_until"] = str(cu)
    await ctx.db.update_task_type_window(**kwargs)
    return total_credit


async def refresh_quota__gpt_balance(ctx: RefreshQuotaContext) -> int:
    """GPT/ChatGPT：两层代理刷新余额框架（长 access_token）。"""
    from .gpt_task_executor import refresh_gpt_balance  # type: ignore

    return await refresh_gpt_balance(ctx)


async def refresh_quota__leonardo_tokens(ctx: RefreshQuotaContext) -> int:
    """Leonardo: read Fast Tokens from the logged-in app GraphQL session."""
    row = ctx.mapping_row or {}
    space_id = str(row.get("space_id") or "")
    window_key = str(row.get("window_key") or "")
    base_url = str(row.get("lan_addr") or "").strip()
    if not space_id or not window_key or not base_url:
        raise RuntimeError("mapping missing lan_addr/space_id/window_key; cannot read Leonardo tokens")

    from .leonardo_task_executor import (  # type: ignore
        DEFAULT_LEONARDO_TARGET,
        LEONARDO_DEFAULT_AUTH_CACHE_SECONDS,
        leonardo_fetch_token_balance,
    )

    def _row_bool(key: str, default: bool = False) -> bool:
        v = row.get(key)
        if v is None:
            return bool(default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(v)

    info = await leonardo_fetch_token_balance(
        browser_vendor=str(row.get("vendor") or "roxy"),
        browser_base_url=base_url,
        browser_access_key=row.get("access_key"),
        space_id=space_id,
        window_key=window_key,
        target_url=str(row.get("default_target_url") or "").strip() or DEFAULT_LEONARDO_TARGET,
        headless=_row_bool("_headless", _row_bool("headless", False)),
        pure_mode=_row_bool("pure_mode", True),
        auth_cache_seconds=LEONARDO_DEFAULT_AUTH_CACHE_SECONDS,
        auth_capture_timeout_seconds=25.0,
    )
    remaining = int(info.get("remaining_quota") or 0)
    update_kwargs: Dict[str, Any] = {
        "mapping_id": int(row.get("id") or row.get("mapping_id") or 0),
        "remaining_quota": remaining,
        "sora_remaining_count": remaining,
        "sora_purchased_remaining_count": int(info.get("paid_tokens") or 0),
        "sora_rate_limit_reached": False,
        "sora_access_resets_in_seconds": 0,
    }
    plan = str(info.get("plan") or "").strip()
    if plan:
        update_kwargs["sora_plan_title"] = plan
    token_renewal_date = str(info.get("token_renewal_date") or "").strip()
    if token_renewal_date:
        update_kwargs["sora_subscription_end"] = token_renewal_date
    await ctx.db.update_task_type_window(**update_kwargs)
    return remaining


async def refresh_quota__elevenlabs_credits(ctx: RefreshQuotaContext) -> int:
    """ElevenLabs: read the current web subscription credit balance."""
    row = ctx.mapping_row or {}
    space_id = str(row.get("space_id") or "")
    window_key = str(row.get("window_key") or "")
    base_url = str(row.get("lan_addr") or "").strip()
    if not space_id or not window_key or not base_url:
        raise RuntimeError("mapping missing lan_addr/space_id/window_key; cannot read ElevenLabs credits")

    from .elevenlabs_task_executor import DEFAULT_ELEVENLABS_TARGET, elevenlabs_fetch_subscription

    def _row_bool(key: str, default: bool = False) -> bool:
        value = row.get(key)
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    info = await elevenlabs_fetch_subscription(
        browser_vendor=str(row.get("vendor") or "roxy"),
        browser_base_url=base_url,
        browser_access_key=row.get("access_key"),
        space_id=space_id,
        window_key=window_key,
        target_url=str(row.get("default_target_url") or "").strip() or DEFAULT_ELEVENLABS_TARGET,
        headless=_row_bool("_headless", _row_bool("headless", False)),
        pure_mode=_row_bool("pure_mode", True),
        timeout_seconds=30.0,
    )
    remaining = int(info.get("remaining_quota") or 0)
    update_kwargs: Dict[str, Any] = {
        "mapping_id": int(row.get("id") or row.get("mapping_id") or 0),
        "remaining_quota": remaining,
        "sora_remaining_count": remaining,
    }
    tier = str(info.get("tier") or "").strip()
    if tier:
        update_kwargs["sora_plan_title"] = tier
    await ctx.db.update_task_type_window(**update_kwargs)
    return remaining


async def refresh_quota__zarklab_credits(ctx: RefreshQuotaContext) -> int:
    """Zark Lab: quote a default video without generating and read available credits."""
    row = ctx.mapping_row or {}
    space_id = str(row.get("space_id") or "")
    window_key = str(row.get("window_key") or "")
    base_url = str(row.get("lan_addr") or "").strip()
    if not space_id or not window_key or not base_url:
        raise RuntimeError("mapping missing lan_addr/space_id/window_key; cannot read Zark Lab credits")

    from .zarklab_task_executor import DEFAULT_ZARKLAB_TARGET, zarklab_fetch_credits

    def _row_bool(key: str, default: bool = False) -> bool:
        value = row.get(key)
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    info = await zarklab_fetch_credits(
        browser_vendor=str(row.get("vendor") or "roxy"),
        browser_base_url=base_url,
        browser_access_key=row.get("access_key"),
        space_id=space_id,
        window_key=window_key,
        target_url=str(row.get("default_target_url") or "").strip() or DEFAULT_ZARKLAB_TARGET,
        headless=_row_bool("_headless", _row_bool("headless", False)),
        pure_mode=_row_bool("pure_mode", True),
    )
    remaining = int(info.get("remaining_quota") or 0)
    await ctx.db.update_task_type_window(
        mapping_id=int(row.get("id") or row.get("mapping_id") or 0),
        remaining_quota=remaining,
        sora_remaining_count=remaining,
    )
    return remaining


REFRESH_QUOTA_HANDLERS: Dict[str, Tuple[str, RefreshQuotaHandler]] = {
    "noop": ("默认：不刷新（保持当前值）", refresh_quota__noop),
    "reset_to_daily": ("示例：重置为 daily_quota", refresh_quota__reset_to_daily),
    "sora_nf_check": ("Sora：读取余额 backend/nf/check", refresh_quota__sora_nf_check),
    "veo_flow_credits": ("VEO/Labs：优先插件读取 credits，失败回退两层代理", refresh_quota__veo_flow_credits),
    "dreamina_credits": ("Dreamina：指纹窗口内读取余额（commerce API，total_credit）", refresh_quota__dreamina_credits),
    "gpt_balance": ("GPT/ChatGPT：两层代理刷新余额/账号信息（需长 AT）", refresh_quota__gpt_balance),
    "leonardo_tokens": ("Leonardo：读取 Fast Tokens（GraphQL）", refresh_quota__leonardo_tokens),
    "elevenlabs_credits": ("ElevenLabs：读取网页订阅剩余额度", refresh_quota__elevenlabs_credits),
    "zarklab_credits": ("Zark Lab：不生成内容，读取可用 credits", refresh_quota__zarklab_credits),
}


# -------------------- registry helpers --------------------
def list_create_task_handler_options() -> List[Dict[str, str]]:
    return [_label(k, v[0]) for k, v in CREATE_TASK_HANDLERS.items()]


def list_refresh_quota_handler_options() -> List[Dict[str, str]]:
    return [_label(k, v[0]) for k, v in REFRESH_QUOTA_HANDLERS.items()]


def get_create_task_handler(key: Optional[str]) -> CreateTaskHandler:
    k = (key or "").strip() or "default_submit"
    if k not in CREATE_TASK_HANDLERS:
        raise KeyError(f"未知 create_task_handler: {k}")
    return CREATE_TASK_HANDLERS[k][1]


def get_refresh_quota_handler(key: Optional[str]) -> RefreshQuotaHandler:
    k = (key or "").strip() or "noop"
    if k not in REFRESH_QUOTA_HANDLERS:
        raise KeyError(f"未知 refresh_quota_handler: {k}")
    return REFRESH_QUOTA_HANDLERS[k][1]

