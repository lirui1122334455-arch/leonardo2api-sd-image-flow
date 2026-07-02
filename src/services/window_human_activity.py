"""窗口拟人化操作工具。

把窗口池里原本散落在 ``task_service.py`` 的”模拟真人浏览/滑动/点击”等逻辑集中在这里，
供窗口池巡检、VEO 工作流入口、Dreamina 工作流入口复用。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Dict, Optional

from ..core.logger import logger
from .browser_automation_base import FingerprintBrowserAutomationBase
from .playwright_broswer_context import (
    get_or_create_ctx as get_or_create_playwright_ctx,
    pick_working_page_from_context,
)


def random_human_activity_delay(reconcile_interval: float, cf_interval: float) -> float:
    """下一轮窗口池拟人操作延迟：在 reconcile_interval 与 cf_interval 之间随机。"""
    try:
        r = float(reconcile_interval)
    except Exception:
        r = 600.0
    try:
        c = float(cf_interval)
    except Exception:
        c = 1800.0
    lo = max(1.0, min(r, c))
    hi = max(lo, max(r, c))
    if hi <= lo:
        return lo
    return random.uniform(lo, hi)


async def disconnect_pw_context_best_effort(pw_ctx: Any) -> None:
    try:
        async with pw_ctx.driver_lock:
            await pw_ctx.disconnect_playwright_only()
    except Exception:
        pass


def page_is_closed(page: Any) -> bool:
    if page is None:
        return True
    try:
        return bool(getattr(page, "is_closed", lambda: False)())
    except Exception:
        return True


async def perform_human_activity_on_pw_context(
    pw_ctx: Any,
    *,
    mapping_id: Optional[int] = None,
    target_url: Optional[str] = None,
    max_refreshes: int = 1,
    mouse_moves: Optional[int] = None,
    scrolls: Optional[int] = None,
    input_attempts: Optional[int] = None,
) -> Dict[str, Any]:
    """在已 connect_over_cdp 的 PlaywrightBrowserContext 上执行一轮拟人操作并断开 CDP。"""
    async with pw_ctx.driver_lock:
        try:
            ctx = getattr(pw_ctx, "context", None)
            if ctx is None:
                return {}
            page = getattr(pw_ctx, "page", None)
            if page_is_closed(page):
                page = None
            if page is None:
                page = await pick_working_page_from_context(ctx)
                try:
                    pw_ctx.page = page
                except Exception:
                    pass
            if page is None:
                return {}
            try:
                await page.bring_to_front()
            except Exception:
                pass
            tu = str(target_url or "").strip()
            if tu:
                try:
                    cur_url = str(getattr(page, "url", "") or "").strip().lower()
                except Exception:
                    cur_url = ""
                if not cur_url or cur_url == "about:blank":
                    try:
                        await page.goto(tu, wait_until="domcontentloaded", timeout=60_000)
                    except Exception:
                        pass
            automation = FingerprintBrowserAutomationBase(page, default_timeout=3500)
            result = await automation.perform_human_like_activity(
                max_refreshes=max_refreshes,
                mouse_moves=mouse_moves,
                scrolls=scrolls,
                input_attempts=input_attempts,
            )
            logger.info("window human activity mapping=%s result=%s", mapping_id, result)
            return result
        finally:
            try:
                await pw_ctx.disconnect_playwright_only()
            except Exception:
                pass


async def perform_deep_human_activity(
    page: Any,
    *,
    min_seconds: float = 15.0,
    max_seconds: float = 30.0,
) -> Dict[str, Any]:
    """在指定 page 上执行深度拟人行为，持续 min_seconds ~ max_seconds 秒。

    适用于需要给 reCAPTCHA / 风控系统积累足够行为信号的场景。
    不会 reload 页面、不会断开 CDP，不会点击按钮。
    动作包括：鼠标移动、滚动、在输入框中模拟输入、停顿。

    Parameters
    ----------
    page : playwright Page 对象
    min_seconds : 拟人行为最短持续时间（秒），默认 15
    max_seconds : 拟人行为最长持续时间（秒），默认 30

    Returns
    -------
    dict : 统计信息 {"mouse_moves": int, "scrolls": int, "inputs": int, "elapsed": float}
    """
    min_s = max(1.0, float(min_seconds))
    max_s = max(min_s, float(max_seconds))
    target_duration = random.uniform(min_s, max_s)

    result: Dict[str, Any] = {"mouse_moves": 0, "scrolls": 0, "inputs": 0, "elapsed": 0.0}

    if page is None:
        return result
    try:
        if page.is_closed():
            return result
    except Exception:
        return result

    start = time.monotonic()
    automation = FingerprintBrowserAutomationBase(page, default_timeout=5000)

    try:
        # 阶段1：初始停留（模拟用户刚看到页面）
        await asyncio.sleep(random.uniform(1.5, min(4.0, target_duration * 0.15)))

        while (time.monotonic() - start) < target_duration:
            remaining = target_duration - (time.monotonic() - start)
            if remaining <= 0:
                break

            # 随机选择动作
            action = random.choices(
                ["mouse", "scroll", "input", "pause"],
                weights=[0.4, 0.25, 0.15, 0.2],
                k=1,
            )[0]

            if action == "mouse":
                try:
                    await automation.human_mouse_move(moves=1)
                    result["mouse_moves"] += 1
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.3, min(1.8, remaining * 0.3)))

            elif action == "scroll":
                try:
                    await automation.human_scroll_page(scrolls=1)
                    result["scrolls"] += 1
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.5, min(2.5, remaining * 0.4)))

            elif action == "input":
                try:
                    if await automation.human_type_random_input():
                        result["inputs"] += 1
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.5, min(2.0, remaining * 0.3)))

            else:  # pause — 模拟用户阅读/思考
                await asyncio.sleep(random.uniform(1.0, min(4.0, remaining * 0.5)))

        # 最终：鼠标移向页面中心区域（模拟准备操作）
        try:
            viewport = page.viewport_size
            if viewport:
                vw = viewport.get("width", 1200)
                vh = viewport.get("height", 800)
                await page.mouse.move(
                    random.randint(int(vw * 0.3), int(vw * 0.7)),
                    random.randint(int(vh * 0.3), int(vh * 0.6)),
                    steps=random.randint(8, 18),
                )
                result["mouse_moves"] += 1
        except Exception:
            pass

    except Exception as e:
        logger.debug("perform_deep_human_activity error: %s", e)

    result["elapsed"] = round(time.monotonic() - start, 2)
    logger.info("deep human activity done: %s", result)
    return result


async def perform_human_activity_for_window_mapping(
    db: Any,
    mapping_id: int,
    *,
    max_refreshes: int = 1,
    mouse_moves: Optional[int] = None,
    scrolls: Optional[int] = None,
    input_attempts: Optional[int] = None,
) -> None:
    """按 task_type_window mapping_id 连接对应窗口并执行拟人操作。"""
    from .grok_workflow_executor import DEFAULT_GROK_TARGET, get_or_create_grok_session
    from .jimeng_task_executor import DEFAULT_DREAMINA_TARGET, get_or_create_dreamina_session
    from .sora_task_executor import get_or_create_sora_session
    from .task_service import _effective_browser_pure_mode_from_context
    from .veo_workflow_executor import get_or_create_veo_session

    ctx = await db.get_task_type_window_context(mapping_id)
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
    headless = bool(ctx.get("headless"))
    pure_mode = _effective_browser_pure_mode_from_context(ctx)
    target_url = (str(ctx.get("default_target_url") or "").strip() or None)

    if handler == "veo_workflow":
        picked_pid = await db.get_random_veo_flow_project_id(mapping_id)
        tu = target_url or "https://labs.google/fx"
        if picked_pid is not None:
            tu = f"https://labs.google/fx/tools/flow/project/{picked_pid}"
        sess = get_or_create_veo_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        sess.browser_headless = headless
        sess.browser_pure_mode = pure_mode
        sess.idle_close_disabled = True
        sess._cancel_idle_close()
        async with sess._bring_drafts_lock:
            await sess.ensure_open(args=sess.browser_open_args, force_open=False, headless=headless, acquire_bring_lock=False, pure_mode=pure_mode)
            await sess._bring_target_page_to_front(refresh_target=False, drafts_url=tu, acquire_bring_lock=False)
            await perform_human_activity_on_pw_context(
                sess.pw_ctx,
                mapping_id=mapping_id,
                target_url=tu,
                max_refreshes=max_refreshes,
                mouse_moves=mouse_moves,
                scrolls=scrolls,
                input_attempts=input_attempts,
            )
        return

    if handler == "grok_workflow":
        tu = target_url or DEFAULT_GROK_TARGET
        sess = get_or_create_grok_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        bring = sess._bring_target_page_to_front
    elif handler == "dreamina_workflow":
        return;
        tu = target_url or DEFAULT_DREAMINA_TARGET
        sess = get_or_create_dreamina_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        bring = sess._bring_target_page_to_front
    elif handler in ("sora_gen_video", "sora_wm_remove", "sora_plus_register"):
        tu = target_url or "https://sora.chatgpt.com/drafts"
        sess = get_or_create_sora_session(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        tok = str(ctx.get("sora_access_token") or "").strip()
        if tok:
            sess.set_access_token(tok, str(ctx.get("sora_access_expires") or "").strip() or None)
        bring = sess._bring_sora_drafts_to_front
    else:
        pw = get_or_create_playwright_ctx(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        await pw.ensure_open(args=[], force_open=False, headless=headless, require_page=False, pure_mode=pure_mode)
        await perform_human_activity_on_pw_context(
            pw,
            mapping_id=mapping_id,
            target_url=target_url,
            max_refreshes=max_refreshes,
            mouse_moves=mouse_moves,
            scrolls=scrolls,
            input_attempts=input_attempts,
        )
        return

    sess.browser_headless = headless
    sess.browser_pure_mode = pure_mode
    sess.idle_close_disabled = True
    sess._cancel_idle_close()
    async with sess._bring_drafts_lock:
        await sess.ensure_open(args=sess.browser_open_args, force_open=sess.browser_force_open, headless=headless, acquire_bring_lock=False, pure_mode=pure_mode)
        await bring(refresh_target=False, drafts_url=tu, acquire_bring_lock=False)
        await perform_human_activity_on_pw_context(
            sess.pw_ctx,
            mapping_id=mapping_id,
            target_url=tu,
            max_refreshes=max_refreshes,
            mouse_moves=mouse_moves,
            scrolls=scrolls,
            input_attempts=input_attempts,
        )
