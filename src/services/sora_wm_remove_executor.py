"""Sora 视频去水印执行器（removesorawatermark.pro）。

实现约束（按你的需求）：
- 默认先打开页面 `https://www.removesorawatermark.pro/zh`
- 然后在“指纹浏览器页面上下文”里 POST `https://www.removesorawatermark.pro/api/jobs/post-url`
  body: {"soraUrl": "<sora share url>"}
- 读取返回 JSON 中的 videoUrl 并返回
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.paths import MONITOR_LOG_FILE
from .playwright_broswer_context import get_or_create_ctx, page_fetch_json, pick_working_page_from_context, safe_trim
from .task_executor_types import NonPenalizedTaskError, ProgressCB


def _pick_sora_share_url(payload: Dict[str, Any]) -> str:
    candidates = [
        payload.get("soraUrl"),
        payload.get("sora_url"),
        payload.get("soraUrl".lower()),
        payload.get("sora_share_url"),
        payload.get("share_url"),
        payload.get("url"),
        payload.get("link"),
    ]
    for v in candidates:
        s = str(v or "").strip()
        if s:
            return s
    return ""


async def sora_wm_remove(
    payload: Dict[str, Any],
    progress_cb: ProgressCB,
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    """Sora 视频去水印：复用同一指纹浏览器窗口，在页面上下文里调用 removesorawatermark.pro 的 API。"""

    payload = payload or {}
    headless = bool(payload.get("headless", False))
    sora_url = _pick_sora_share_url(payload)
    if not sora_url:
        raise ValueError("payload 缺少 soraUrl（或 sora_url/share_url/url/link）")

    entry_url = str(payload.get("wm_remove_entry_url") or "https://www.removesorawatermark.pro/zh").strip()
    api_url = str(payload.get("wm_remove_api_url") or "https://www.removesorawatermark.pro/api/jobs/post-url").strip()

    log_file = None
    log_path = str(payload.get("wm_remove_monitor_log_path") or payload.get("monitor_log_path") or "").strip()
    if log_path:
        log_file = Path(log_path)
    else:
        log_file = (MONITOR_LOG_FILE)

    ctx = get_or_create_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    await ctx.ensure_open(args=[], force_open=False, headless=headless)

    await progress_cb(1, {"stage": "open", "url": entry_url})
    started_at = time.time()

    async with ctx.driver_lock:
        if ctx.page is None:
            if ctx.context is None:
                raise RuntimeError("Browser context 未初始化（ensure_open 失败或被回收）")
            ctx.page = await pick_working_page_from_context(ctx.context)

        # 先打开入口页（建立该站点上下文/ cookie / 本地存储等）
        try:
            await ctx.page.goto(entry_url, wait_until="domcontentloaded", timeout=int(max(10_000, min(60_000, float(timeout_seconds) * 1000))))
        except Exception as e:
            raise NonPenalizedTaskError(f"打开去水印页面失败：{e}", status_code=400) from e

        await progress_cb(10, {"stage": "post_job", "api": api_url})
        tx = await page_fetch_json(
            ctx.page,
            url=api_url,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json_data={"soraUrl": sora_url},
            log_file=log_file,
        )

    data = tx.get("_json")
    if not isinstance(data, dict):
        raise NonPenalizedTaskError(
            f"去水印接口返回非 JSON：status={tx.get('status')} body={safe_trim(str(tx.get('response_body') or ''), 600)!r}",
            status_code=int(tx.get("status") or 500) if tx.get("status") is not None else None,
        )

    success = bool(data.get("success", False))
    job_id = str(data.get("jobId") or "").strip() or None
    video_url = str(data.get("videoUrl") or "").strip() or None
    message = str(data.get("message") or "").strip() or None

    if not success:
        err_msg = str(data.get("message") or data.get("error") or "去水印任务创建失败").strip()
        raise NonPenalizedTaskError(err_msg, status_code=int(tx.get("status") or 400) if tx.get("status") is not None else 400)

    if not video_url:
        # 你当前需求示例是“直接返回 videoUrl”，这里做保守兜底提示
        hint = f"去水印任务已提交但未返回 videoUrl：jobId={job_id!r} message={message!r}"
        raise NonPenalizedTaskError(hint, status_code=202)

    elapsed_ms = int(max(0.0, (time.time() - started_at) * 1000.0))
    await progress_cb(100, {"stage": "done", "job_id": job_id, "elapsed_ms": elapsed_ms})
    return {
        "type": "sora_wm_remove",
        "message": "去水印完成",
        "job_id": job_id,
        "video_url": video_url,
        "raw": data,
    }
