"""Grok Imagine 视频工作流（指纹浏览器 + 页面内 fetch）。

与 flow2api / grok2api 的「直连 REST」不同：本模块在**已登录 grok.com 的指纹窗口**内用 `fetch(..., credentials: 'include')`
调用官方 `app-chat`、`media/post/create`、`upload-file` 等接口，复用浏览器 Cookie。

与 grok2api ``VideoService`` / ``AppChatReverse`` / ``MediaPostReverse`` 对齐的要点：
单轮视频、``MEDIA_POST_TYPE_VIDEO`` + ``prompt``、``modelMap.videoGenModelConfig``（aspectRatio / parentPostId / resolutionName / videoLength，
多图时 ``imageReferences`` + ``isReferenceToVideo``）、``fileAttachments`` 为上传得到的 ``fileMetadataId``、``toolOverrides.videoGen``、
``message`` 末尾附带 ``--mode=…``。grok2api 侧使用的 token 即 SSO，对应 Cookie ``sso`` / ``sso-rw``；指纹 ``fetch`` 不能靠请求头注入 Cookie，
故可选 SSO 通过 Playwright ``add_cookies`` 写入（mapping 的 ``sora_access_token`` 字段或 payload 的 ``grok_access_token`` / ``access_token``）。

支持：
- 文生视频（无参考图）
- 多参考图生成视频（与 grok2api 一致：`imageReferences` + `fileAttachments`，最多 7 张）

默认生成 **720p**、**10 秒**、**16:9**（横屏）；比例/时长字段解析与 ``veo_workflow_executor`` 一致（``size_ratio``/``orientation``/``n_frames``/``时长`` 等）。
返回 ``video_url`` 优先为 ``imagine-public.x.ai`` 可分享直链，另附 ``video_asset_url``（``assets.grok.com`` 绝对路径，便于带 Cookie 下载）。

入口：`grok_workflow`（由 `task_service._run_task` 调用）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import string
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..core.paths import MONITOR_LOG_FILE
from .playwright_broswer_context import (
    acquire_browser_open_slot,
    append_log,
    get_or_create_ctx as get_or_create_playwright_ctx,
    page_fetch_json,
    page_fetch_tx,
    safe_trim,
)
from .veo_workflow_executor import (
    _build_debug_progress_panel_script,
    _short_err_msg,
    _veo_resolve_orientation_str,
)
from .sora_task_executor import _download_bytes_local_async
from .task_executor_types import NonPenalizedTaskError, ProgressCB

CHAT_API = "https://grok.com/rest/app-chat/conversations/new"
MEDIA_POST_API = "https://grok.com/rest/media/post/create"
UPLOAD_API = "https://grok.com/rest/app-chat/upload-file"

# 流式响应里 videoUrl 常为相对路径；与 grok2api 一致拼 assets + imagine-public 分享链
_GROK_ASSETS_ORIGIN = "https://assets.grok.com"
_GROK_SHARE_VIDEO_TMPL = "https://imagine-public.x.ai/imagine-public/share-videos/{video_id}.mp4?cache=1"
_GROK_GEN_POST_ID_RE = re.compile(r"/generated/([0-9a-fA-F-]{32,36})/", re.IGNORECASE)
_GROK_GEN_VIDEO_TAIL_RE = re.compile(
    r"/([0-9a-fA-F-]{32,36})/generated_video", re.IGNORECASE
)

_GROK_SESSIONS: Dict[str, "GrokSession"] = {}

_REFERENCE_PLACEHOLDER_RE = re.compile(r"@(?:(?:图|image|img)\s*(\d+))", re.IGNORECASE)

_SIZE_TO_ASPECT = {
    "1280x720": "16:9",
    "720x1280": "9:16",
    "1792x1024": "3:2",
    "1024x1792": "2:3",
    "1024x1024": "1:1",
}


def _grok_key(vendor: str, base_url: str, space_id: str, window_key: str) -> str:
    return f"grok|{vendor}|{base_url}|{space_id}|{window_key}"


# 与 grok2api ``StatsigGenerator`` 一致；错误格式的 ``x-statsig-id`` 易触发 anti-bot 403。
_GROK_STATSIG_STATIC_B64 = (
    "ZTpUeXBlRXJyb3I6IENhbm5vdCByZWFkIHByb3BlcnRpZXMgb2YgdW5kZWZpbmVkIChyZWFkaW5nICdjaGlsZE5vZGVzJyk="
)
_GROK_SENTRY_BAGGAGE = (
    "sentry-environment=production,sentry-release=d6add6fb0460641fd482d767a335ef72b9b6abb8,"
    "sentry-public_key=b311e0f2690c81f25e2c4cf6d4f7ce1c"
)


def _grok_statsig_id(*, dynamic: bool = False) -> str:
    """生成 ``x-statsig-id``（须为 Base64 形态，勿用 UUID hex）。"""
    if not dynamic:
        return _GROK_STATSIG_STATIC_B64
    lo = string.ascii_lowercase + string.digits
    alnum = string.ascii_lowercase + string.digits
    if random.choice([True, False]):
        rand = "".join(random.choices(alnum, k=5))
        msg = f"e:TypeError: Cannot read properties of null (reading 'children['{rand}']')"
    else:
        rand = "".join(random.choices(lo, k=10))
        msg = f"e:TypeError: Cannot read properties of undefined (reading '{rand}')"
    return base64.b64encode(msg.encode("utf-8")).decode("ascii")


def _grok_normalize_sso_token(raw: Optional[str]) -> str:
    """与 grok2api ``build_sso_cookie`` 一致：去掉 ``sso=`` 前缀与空白。"""
    s = _one_str(raw)
    if s.lower().startswith("sso="):
        s = s[4:].strip()
    return s


def _grok_browser_json_headers(*, dynamic_statsig: bool = False) -> Dict[str, str]:
    """浏览器内 fetch 可用的 JSON 头（对齐 grok2api ``build_headers``：Accept/Baggage/Statsig 等）。"""
    # grok2api JSON POST 使用 Accept */*，非 application/json
    return {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Baggage": _GROK_SENTRY_BAGGAGE,
        "Content-Type": "application/json",
        "Priority": "u=1, i",
        "x-statsig-id": _grok_statsig_id(dynamic=dynamic_statsig),
        "x-xai-request-id": str(uuid.uuid4()),
    }


async def _grok_merge_optional_sso_cookies(page: Any, raw_token: Optional[str], log_file: Path) -> None:
    """若提供 SSO，写入当前 BrowserContext（``fetch`` 仍走 ``credentials:'include'``，与窗口 Cookie 合并）。"""
    tok = _grok_normalize_sso_token(raw_token)
    if not tok:
        return
    ctx = getattr(page, "context", None)
    if ctx is None:
        append_log(log_file, "[grok] skip SSO cookie merge: page.context is None")
        return
    try:
        add = getattr(ctx, "add_cookies", None)
        if not callable(add):
            append_log(log_file, "[grok] skip SSO cookie merge: context.add_cookies missing")
            return
        cookies = [
            {
                "name": "sso",
                "value": tok,
                "domain": ".grok.com",
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            },
            {
                "name": "sso-rw",
                "value": tok,
                "domain": ".grok.com",
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            },
        ]
        await add(cookies)
        append_log(log_file, "[grok] merged optional SSO cookies (sso, sso-rw) for .grok.com")
    except Exception as e:
        append_log(log_file, f"[grok] optional SSO cookie merge failed (non-fatal): {e}")


def _drop_grok_session(cache_key: str) -> None:
    k = (cache_key or "").strip()
    if k:
        _GROK_SESSIONS.pop(k, None)


def _one_str(v: Any) -> str:
    return str(v or "").strip()


def _append_url(bucket: List[str], u: str) -> None:
    s = _one_str(u)
    if s and s not in bucket:
        bucket.append(s)


def _grok_collect_reference_image_urls(payload: Dict[str, Any]) -> List[str]:
    """收集参考图 URL（http(s) 或 data:），顺序与 flow2api / 管理台习惯字段对齐。"""
    p = payload or {}
    out: List[str] = []

    def _from_list_key(k: str) -> None:
        raw = p.get(k)
        if isinstance(raw, list):
            for it in raw:
                if isinstance(it, str):
                    _append_url(out, it)
                elif isinstance(it, dict):
                    _append_url(out, _one_str(it.get("url") or it.get("image_url")))

    for key in (
        "ingredients_images",
        "Ingredients_images",
        "reference_images",
        "image_reference",
        "image_urls",
        "images",
    ):
        _from_list_key(key)

    _append_url(out, p.get("first_image_url"))
    _append_url(out, p.get("image_url"))

    return out


def grok_ref_url_count(payload: Optional[Dict[str, Any]]) -> int:
    return len(_grok_collect_reference_image_urls(payload or {}))


def _grok_share_video_id_from_url(url: str) -> str:
    """与 grok2api ``_extract_video_id`` 同源：从路径取出帖/视频 UUID。"""
    s = _one_str(url)
    m = _GROK_GEN_POST_ID_RE.search(s)
    if m:
        return m.group(1)
    m = _GROK_GEN_VIDEO_TAIL_RE.search(s)
    if m:
        return m.group(1)
    return ""


def _grok_normalize_video_urls(raw: str) -> Dict[str, str]:
    """返回可下载的 assets 绝对 URL + 可分享的 imagine-public 链（与 grok2api 公开资源 URL 形态一致）。"""
    u = _one_str(raw)
    if not u:
        return {"video_url": "", "video_asset_url": "", "video_share_url": ""}
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("http://") or u.startswith("https://"):
        asset = u
    else:
        asset = f"{_GROK_ASSETS_ORIGIN.rstrip('/')}/{u.lstrip('/')}"
    vid = _grok_share_video_id_from_url(asset)
    share = _GROK_SHARE_VIDEO_TMPL.format(video_id=vid) if vid else ""
    # 主字段：优先公开分享（免登录直链），否则回退 assets
    main = share or asset
    return {"video_url": main, "video_asset_url": asset, "video_share_url": share}


def _grok_resolve_aspect_ratio(payload: Dict[str, Any]) -> str:
    """比例：先走 Veo 同款朝向/尺寸字段解析，再回落 Grok 比例串；默认 16:9（横屏）。"""
    p = payload or {}
    o = _veo_resolve_orientation_str(p)
    if o == "portrait":
        return "9:16"
    if o == "landscape":
        return "16:9"
    raw = _one_str(
        p.get("aspect_ratio")
        or p.get("size")
        or p.get("size_ratio")
        or p.get("ratio")
        or p.get("尺寸")
    )
    if raw in _SIZE_TO_ASPECT:
        return _SIZE_TO_ASPECT[raw]
    if raw in ("16:9", "9:16", "3:2", "2:3", "1:1"):
        return raw
    return "16:9"


def _grok_resolve_video_length_seconds(payload: Dict[str, Any]) -> int:
    """时长（秒）：读取与 Veo/Sora 一致的 ``duration``/``时长``/``n_frames`` 等，默认 10s， clamp 6–30。"""
    p = payload or {}
    for k in ("video_length", "seconds", "duration", "时长"):
        try:
            v = int(float(p.get(k) or 0))
            if v > 0:
                return max(6, min(30, v))
        except (TypeError, ValueError):
            pass
    for k in ("n_frames", "duration_frames"):
        try:
            nf = int(float(p.get(k) or 0))
        except (TypeError, ValueError):
            nf = 0
        if nf == 300:
            return 10
        if nf == 450:
            return 15
        if nf >= 30:
            secs = max(6, min(30, nf // 30))
            return secs
    return 10


def _grok_resolve_resolution(payload: Dict[str, Any]) -> str:
    """默认 720p；显式 sd/480p 可降为 480p。"""
    r = _one_str(
        payload.get("resolution_name") or payload.get("resolution") or payload.get("quality")
    ).lower()
    if r in ("480p", "sd", "low", "标清"):
        return "480p"
    if r in ("high", "720p", "hd", "高清"):
        return "720p"
    return "720p"


def _normalize_preset(payload: Dict[str, Any]) -> str:
    pr = _one_str(payload.get("preset")).lower() or "custom"
    if pr in ("fun", "normal", "spicy", "custom"):
        return pr
    return "custom"


def _build_mode_suffix(preset: str) -> str:
    mode_map = {
        "fun": "--mode=extremely-crazy",
        "normal": "--mode=normal",
        "spicy": "--mode=extremely-spicy-or-crazy",
        "custom": "--mode=custom",
    }
    return mode_map.get(preset, "--mode=custom")


def _build_app_chat_payload(
    *,
    message: str,
    model: str,
    model_config_override: Dict[str, Any],
    file_attachments: List[str],
) -> Dict[str, Any]:
    return {
        "deviceEnvInfo": {
            "darkModeEnabled": False,
            "devicePixelRatio": 2,
            "screenHeight": 1329,
            "screenWidth": 2056,
            "viewportHeight": 1083,
            "viewportWidth": 2056,
        },
        "disableMemory": True,
        "disableSearch": False,
        "disableSelfHarmShortCircuit": False,
        "disableTextFollowUps": False,
        "enableImageGeneration": True,
        "enableImageStreaming": True,
        "enableSideBySide": True,
        "fileAttachments": list(file_attachments or []),
        "forceConcise": False,
        "forceSideBySide": False,
        "imageAttachments": [],
        "imageGenerationCount": 2,
        "isAsyncChat": False,
        "isReasoning": False,
        "message": message,
        "modelMode": None,
        "modelName": model,
        "responseMetadata": {
            "requestModelDetails": {"modelId": model},
            "modelConfigOverride": model_config_override,
        },
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "sendFinalMetadata": True,
        "temporary": True,
        "toolOverrides": {"videoGen": True},
    }


def _parse_video_url_from_stream_body(body: str) -> str:
    last = ""
    for raw in (body or "").splitlines():
        line = raw.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        root = payload.get("result") if isinstance(payload, dict) else None
        resp = root.get("response") if isinstance(root, dict) else None
        if not isinstance(resp, dict):
            continue
        vr = resp.get("streamingVideoGenerationResponse")
        if isinstance(vr, dict):
            u = _one_str(vr.get("videoUrl"))
            if u:
                last = u
    return last


def _replace_reference_placeholders(prompt: str, asset_ids: List[str]) -> str:
    def _sub(m: re.Match[str]) -> str:
        idx = int(m.group(1)) - 1
        if idx < 0 or idx >= len(asset_ids):
            raise NonPenalizedTaskError(
                f"提示词中的占位符 {m.group(0)} 没有对应的上传图片（共 {len(asset_ids)} 张）",
                status_code=400,
            )
        return f"@{asset_ids[idx]}"

    return _REFERENCE_PLACEHOLDER_RE.sub(_sub, prompt)


class GrokSession:
    """按 window 缓存：复用指纹浏览器与 Playwright CDP。

    与 ``VeoSession`` 对齐的「三件套」：
    - ``ensure_open``（可选 ``acquire_bring_lock`` 与 ``_bring_drafts_lock`` 配合）
    - ``_bring_target_page_to_front``（内部按 ``acquire_bring_lock`` 使用 ``_bring_drafts_lock``）
    - ``_bring_drafts_lock``：与 ``disconnect_playwright_under_bring_lock``、置前逻辑互斥
    """

    def __init__(self, cache_key: str, pw_ctx: Any) -> None:
        self.cache_key = cache_key
        self.pw_ctx = pw_ctx

        self.last_used_at: float = time.time()
        self.create_lock = asyncio.Lock()
        self._bring_drafts_lock = asyncio.Lock()

        self.idle_close_task: Optional[asyncio.Task] = None
        self.idle_close_disabled: bool = False

        self.monitor_log_path: Optional[str] = None
        self.idle_close_seconds: float = 30.0

        self.browser_open_args: list[str] = []
        self.browser_force_open: bool = False
        self.browser_headless: bool = False
        self.browser_pure_mode: bool = True

        self.debug_panel_seq: int = 0
        self.debug_panel_entries: list[Dict[str, str]] = []

    @property
    def _log_file(self) -> Path:
        if self.monitor_log_path:
            return Path(self.monitor_log_path)
        return MONITOR_LOG_FILE

    async def ensure_open(
        self,
        *,
        args: Optional[list[str]] = None,
        force_open: bool = False,
        headless: bool = False,
        acquire_bring_lock: bool = False,
        pure_mode: Optional[bool] = None,
    ) -> None:
        """确保指纹浏览器窗口已打开、CDP 已连接（与 ``VeoSession.ensure_open`` 一致）。"""
        self.last_used_at = time.time()
        pm = self.browser_pure_mode if pure_mode is None else bool(pure_mode)

        async def _inner() -> None:
            await self.pw_ctx.ensure_open(
                args=args, force_open=force_open, headless=headless, require_page=False, pure_mode=pm
            )

        if acquire_bring_lock:
            async with self._bring_drafts_lock:
                await _inner()
        else:
            await _inner()

    async def disconnect_playwright_under_bring_lock(self) -> None:
        async with self._bring_drafts_lock:
            async with self.pw_ctx.driver_lock:
                await self.pw_ctx.disconnect_playwright_only()

    @staticmethod
    def _grok_is_page_closed(p: Any) -> bool:
        try:
            return bool(getattr(p, "is_closed", lambda: False)())
        except Exception:
            return True

    async def _grok_snapshot_contexts_pages(self) -> tuple[list[Any], list[tuple[Any, Any, str]]]:
        ctx0 = getattr(self.pw_ctx, "context", None)
        br0 = getattr(self.pw_ctx, "browser", None)
        try:
            ctxs0 = list(getattr(br0, "contexts", []) or [])
        except Exception:
            ctxs0 = []
        if ctx0 is not None and ctx0 not in ctxs0:
            ctxs0.insert(0, ctx0)
        open_pages0: list[tuple[Any, Any, str]] = []
        for c0 in (ctxs0 or []):
            try:
                pages0 = list(getattr(c0, "pages", []) or [])
            except Exception:
                pages0 = []
            for p0 in pages0:
                if self._grok_is_page_closed(p0):
                    continue
                try:
                    u0 = str(getattr(p0, "url", "") or "").strip()
                except Exception:
                    u0 = ""
                open_pages0.append((c0, p0, u0))
        return ctxs0, open_pages0

    async def _is_cloudflare_page(self, page: Any, *, deep: bool = False) -> bool:
        if page is None:
            return False
        try:
            u = str(getattr(page, "url", "") or "").strip()
        except Exception:
            u = ""
        ul = u.lower()
        if "/cdn-cgi/" in ul:
            return True
        try:
            title = await page.title()
        except Exception:
            title = ""
        tl = (title or "").strip().lower()
        if "just a moment" in tl or "attention required" in tl:
            return True
        if not deep:
            return False
        try:
            html = await page.content()
        except Exception:
            html = ""
        hl = (html or "").lower()
        if "cloudflare" in hl and ("just a moment" in hl or "/cdn-cgi/" in hl or "cf-ray" in hl):
            return True
        if ("turnstile" in hl or "cf-challenge" in hl) and ("/cdn-cgi/" in hl or "cloudflare" in hl):
            return True
        return False

    async def _push_debug_progress(self, page: Any, text: str, *, level: str = "info") -> None:
        if page is None:
            return
        try:
            msg = str(text or "").strip()
        except Exception:
            msg = ""
        if not msg:
            return
        self.debug_panel_seq += 1
        now_str = time.strftime("%H:%M:%S")
        self.debug_panel_entries.append(
            {
                "idx": str(self.debug_panel_seq),
                "ts": now_str,
                "level": str(level or "info"),
                "text": msg,
            }
        )
        if len(self.debug_panel_entries) > 80:
            self.debug_panel_entries = self.debug_panel_entries[-80:]
        payload = {
            "title": "Grok 调试进度",
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "entries": list(self.debug_panel_entries),
        }
        script = _build_debug_progress_panel_script()
        try:
            await page.evaluate(script, payload)
        except Exception:
            pass

    async def _try_click_cloudflare_checkbox(self, page: Any) -> bool:
        log_file = self._log_file

        def _log(msg: str) -> None:
            try:
                append_log(log_file, f"[grok_cf_checkbox] {msg}")
            except Exception:
                pass

        try:
            cf_frame = None
            try:
                for i, f in enumerate(page.frames):
                    fu = str(getattr(f, "url", "") or "")
                    if "challenges.cloudflare.com" in fu or "/cdn-cgi/" in fu:
                        cf_frame = f
                        _log(f"找到 cf_frame: frame[{i}]")
                        break
            except Exception as e:
                _log(f"遍历 page.frames 异常: {e}")

            if cf_frame is None:
                await self._push_debug_progress(page, "未发现 Cloudflare checkbox iframe", level="warn")
                return False

            try:
                loc = cf_frame.locator("input[type='checkbox']")
                cnt = await loc.count()
                _log(f"策略1 locator count={cnt}")
                if cnt > 0:
                    await self._push_debug_progress(page, "发现了 checkbox（locator）", level="info")
                    await loc.first.click(force=True, timeout=1500)
                    _log("策略1 locator click 成功")
                    await self._push_debug_progress(page, "点击 checkbox 成功（locator）", level="ok")
                    return True
            except Exception as e:
                _log(f"策略1 locator 失败: {e}")
                await self._push_debug_progress(page, f"点击 checkbox 失败（locator）：{_short_err_msg(e)}", level="warn")

            try:
                iframe_handle = await cf_frame.frame_element()
                box = await iframe_handle.bounding_box()
                if not (box and box.get("width", 0) > 0 and box.get("height", 0) > 0):
                    _log(f"策略2 bounding_box 无效: {box}")
                    return False

                target_x = box["x"] + 26.0
                target_y = box["y"] + box["height"] / 2.0
                _log(f"策略2 目标坐标: ({target_x:.1f}, {target_y:.1f})")
                await self._push_debug_progress(page, "发现了 checkbox（坐标法）", level="info")

                start_x = target_x + random.uniform(-80, 120)
                start_y = target_y + random.uniform(-50, 50)
                await page.mouse.move(start_x, start_y)
                await asyncio.sleep(random.uniform(0.08, 0.20))
                await page.mouse.move(target_x, target_y, steps=random.randint(6, 12))
                await asyncio.sleep(random.uniform(0.04, 0.12))
                await page.mouse.click(target_x, target_y)
                _log("策略2 坐标点击完成")
                await self._push_debug_progress(page, "点击 checkbox 成功（坐标法）", level="ok")
                return True
            except Exception as e:
                _log(f"策略2 坐标法异常: {e}")
                await self._push_debug_progress(page, f"点击 checkbox 失败（坐标法）：{_short_err_msg(e)}", level="warn")

        except Exception as e:
            _log(f"顶层异常: {e}")

        _log("所有策略均未成功点击 checkbox")
        await self._push_debug_progress(page, "checkbox 点击失败：所有策略已尝试", level="error")
        return False

    async def _wait_cloudflare_auto_pass(
        self,
        page: Any,
        *,
        max_wait_seconds: float = 10.0,
        max_success_clicks: int = 2,
    ) -> bool:
        try:
            deadline = time.time() + max(0.0, float(max_wait_seconds))
        except Exception:
            deadline = time.time() + 10.0
        await asyncio.sleep(5.0)
        try:
            max_click_success = max(0, int(max_success_clicks))
        except Exception:
            max_click_success = 2
        poll_after_click = 6.0
        poll_idle = 1.0
        await self._push_debug_progress(page, "检测到 Cloudflare，开始等待自动放行并尝试点击 checkbox", level="warn")
        reported_click_fail = False
        consecutive_not_cf = 0
        clicked_success_count = 0
        while time.time() < deadline:
            try:
                is_closed = bool(getattr(page, "is_closed", lambda: False)())
            except Exception:
                is_closed = False
            if is_closed:
                return True

            try:
                still_cf = await self._is_cloudflare_page(page, deep=True)
            except Exception:
                still_cf = True
            if not still_cf:
                consecutive_not_cf += 1
                if consecutive_not_cf >= 2:
                    await self._push_debug_progress(page, "Cloudflare 已放行", level="ok")
                    return False
                await self._push_debug_progress(page, "Cloudflare 疑似已放行，进行二次确认", level="info")
                remain = deadline - time.time()
                if remain <= 0:
                    break
                try:
                    await asyncio.sleep(min(poll_idle, max(0.1, remain)))
                except Exception:
                    break
                continue
            consecutive_not_cf = 0

            clicked = False
            try:
                clicked = await self._try_click_cloudflare_checkbox(page)
            except Exception:
                pass
            if clicked:
                clicked_success_count += 1
                await self._push_debug_progress(
                    page,
                    f"checkbox 已点击（第 {clicked_success_count} 次），等待 Cloudflare 验证结果",
                    level="info",
                )
                if max_click_success > 0 and clicked_success_count >= max_click_success:
                    await self._push_debug_progress(
                        page,
                        f"checkbox 成功点击已达上限（{max_click_success} 次），提前结束等待",
                        level="warn",
                    )
                    try:
                        await asyncio.sleep(1.0)
                    except Exception:
                        pass
                    try:
                        still_cf_after_limit = await self._is_cloudflare_page(page, deep=True)
                    except Exception:
                        still_cf_after_limit = True
                    if not still_cf_after_limit:
                        await self._push_debug_progress(page, "Cloudflare 已放行", level="ok")
                        return False
                    return True
            elif not reported_click_fail:
                await self._push_debug_progress(page, "尚未成功点击 checkbox，继续重试", level="warn")
                reported_click_fail = True

            remain = deadline - time.time()
            if remain <= 0:
                break
            sleep_sec = poll_after_click if clicked else poll_idle
            try:
                await asyncio.sleep(min(sleep_sec, max(0.1, remain)))
            except Exception:
                break
        return True

    async def _restart_window_and_restore_single_drafts(self, *, drafts_url: str, target_host: str) -> Any:
        log_file = self._log_file
        try:
            append_log(log_file, "[grok][drafts] cloudflare interstitial, restarting fp window once")
        except Exception:
            pass

        try:
            await self.pw_ctx.close()
        except Exception:
            pass
        try:
            await asyncio.sleep(0.5)
        except Exception:
            pass

        async with acquire_browser_open_slot(self.pw_ctx.base_url):
            try:
                rsp = await self.pw_ctx.fp_client.browser_open(
                    vendor=self.pw_ctx.vendor,
                    base_url=self.pw_ctx.base_url,
                    access_key=self.pw_ctx.access_key,
                    space_id=self.pw_ctx.space_id,
                    window_key=self.pw_ctx.window_key,
                    args=self.browser_open_args,
                    force_open=self.browser_force_open,
                    headless=self.browser_headless,
                    pure_mode=self.browser_pure_mode,
                )
                try:
                    code = int((rsp or {}).get("code", -1))
                except Exception:
                    code = -1
                try:
                    append_log(log_file, f"[grok][drafts] browser_open result code={code}")
                except Exception:
                    pass
            except Exception as e:
                try:
                    append_log(log_file, f"[grok][drafts] browser_open failed: {e}")
                except Exception:
                    pass

            try:
                self.pw_ctx.browser = None
                self.pw_ctx.context = None
                self.pw_ctx.page = None
                self.pw_ctx.cdp_endpoint = None
            except Exception:
                pass

            try:
                await asyncio.sleep(20.0)
            except Exception:
                pass

        try:
            await self.pw_ctx.ensure_open(
                args=self.browser_open_args,
                force_open=False,
                headless=self.browser_headless,
                require_page=False,
                pure_mode=self.browser_pure_mode,
            )
        except Exception as e:
            try:
                append_log(log_file, f"[grok][drafts] CDP reconnect after restart failed: {e}")
            except Exception:
                pass
        return None

    async def _maybe_click_get_started_button_if_prompted(self, page: Any) -> tuple:
        has_get_started = False
        if page is None:
            return False, has_get_started

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=4000)
        except Exception:
            pass

        scopes: list[Any] = [page]
        try:
            for fr in list(getattr(page, "frames", []) or []):
                if fr is not page and fr not in scopes:
                    scopes.append(fr)
        except Exception:
            pass

        get_started_re = re.compile(r"get\s*started", re.IGNORECASE)

        try:
            for sc in scopes:
                try:
                    if hasattr(sc, "get_by_role"):
                        btn_cnt = await sc.get_by_role("button", name=get_started_re).count()
                        link_cnt = await sc.get_by_role("link", name=get_started_re).count()
                        if (btn_cnt + link_cnt) > 0:
                            has_get_started = True
                            break
                    loc_probe = sc.locator('button, a, [role="button"], [role="link"]').filter(has_text=get_started_re)
                    if (await loc_probe.count()) > 0:
                        has_get_started = True
                        break
                except Exception:
                    continue
        except Exception:
            has_get_started = False

        if not has_get_started:
            await self._push_debug_progress(page, "未发现 Get started 按钮/链接", level="info")
            return False, has_get_started
        await self._push_debug_progress(page, "发现 Get started 按钮/链接，准备点击", level="info")

        for sc in scopes:
            try:
                scope_name = "page" if sc is page else "frame"
                if hasattr(sc, "get_by_role"):
                    try:
                        btn = sc.get_by_role("button", name=get_started_re)
                        await btn.first.click(timeout=3000)
                        await self._push_debug_progress(page, f"点击 Get started 成功（button/{scope_name}）", level="ok")
                        return True, has_get_started
                    except Exception as e:
                        await self._push_debug_progress(page, f"点击 Get started 失败（button/{scope_name}）：{_short_err_msg(e)}", level="warn")
                    try:
                        link = sc.get_by_role("link", name=get_started_re)
                        await link.first.click(timeout=3000)
                        await self._push_debug_progress(page, f"点击 Get started 成功（link/{scope_name}）", level="ok")
                        return True, has_get_started
                    except Exception as e:
                        await self._push_debug_progress(page, f"点击 Get started 失败（link/{scope_name}）：{_short_err_msg(e)}", level="warn")

                try:
                    loc2 = sc.locator('button, a, [role="button"], [role="link"]').filter(has_text=get_started_re)
                    await loc2.first.click(timeout=3000)
                    await self._push_debug_progress(page, f"点击 Get started 成功（text fallback/{scope_name}）", level="ok")
                    return True, has_get_started
                except Exception as e:
                    await self._push_debug_progress(page, f"点击 Get started 失败（text fallback/{scope_name}）：{_short_err_msg(e)}", level="warn")
            except Exception:
                continue

        await self._push_debug_progress(page, "点击 Get started 失败（全部策略）", level="error")
        return False, has_get_started

    async def _nonpenalized_raise_if_not_on_grok_site(self, drafts_page: Any) -> None:
        """置前结束后若仍不在 Grok 主站，提示登录（不做 Google 账号页检测）。"""
        if drafts_page is None or self._grok_is_page_closed(drafts_page):
            return
        try:
            u = str(getattr(drafts_page, "url", "") or "").strip().lower()
        except Exception:
            u = ""
        if "grok.com" in u or "x.ai" in u:
            return
        raise NonPenalizedTaskError(
            "当前页面不在 Grok 域名（grok.com / x.ai）下，请先在指纹窗口登录 Grok",
            status_code=401,
        )

    async def _bring_target_page_to_front(
        self,
        refresh_target: bool = True,
        *,
        drafts_url: str,
        acquire_bring_lock: bool = True,
    ) -> None:
        """将 Grok 目标页置前；逻辑参考 ``VeoSession._bring_target_page_to_front``（无 Google 自动登录分支）。"""
        try:
            target_host = urlparse(drafts_url).netloc.strip().lower()
        except Exception:
            target_host = ""

        async def _inner() -> None:
            def _is_page_closed(p: Any) -> bool:
                try:
                    return bool(getattr(p, "is_closed", lambda: False)())
                except Exception:
                    return False

            def _safe_page_url(p: Any) -> str:
                try:
                    return str(getattr(p, "url", "") or "").strip()
                except Exception:
                    return ""

            def _safe_page_host(u: str) -> str:
                try:
                    return (urlparse(u).netloc or "").strip().lower()
                except Exception:
                    return ""

            def _url_matches_target(u: str) -> bool:
                if u.startswith(drafts_url):
                    return True
                if target_host and _safe_page_host(u) == target_host:
                    return True
                return False

            async def _keep_only_one_drafts_page(keep_page: Any) -> Any:
                ctxs1, open_pages1 = await self._grok_snapshot_contexts_pages()
                keep_ctx = None
                for c1, p1, _u1 in open_pages1:
                    if p1 is keep_page:
                        keep_ctx = c1
                        break
                if keep_ctx is None:
                    try:
                        maybe_ctx = getattr(keep_page, "context", None)
                        keep_ctx = maybe_ctx() if callable(maybe_ctx) else maybe_ctx
                    except Exception:
                        keep_ctx = None

                for _c1, p1, _u1 in open_pages1:
                    if p1 is keep_page:
                        continue
                    try:
                        await p1.close()
                    except Exception:
                        pass

                if keep_ctx is not None:
                    for c1 in ctxs1:
                        if c1 is keep_ctx:
                            continue
                        try:
                            await c1.close()
                        except Exception:
                            pass

                try:
                    if keep_ctx is not None:
                        self.pw_ctx.context = keep_ctx
                except Exception:
                    pass
                return keep_page

            ctxs, open_pages = await self._grok_snapshot_contexts_pages()
            if not ctxs:
                return

            drafts_page = None
            cur_page = getattr(self.pw_ctx, "page", None)
            if cur_page is not None and not _is_page_closed(cur_page):
                cur_u0 = _safe_page_url(cur_page)
                if _url_matches_target(cur_u0):
                    drafts_page = cur_page

            if drafts_page is None:
                for _c, p, u in open_pages:
                    if _url_matches_target(u):
                        drafts_page = p
                        break

            if drafts_page is None:
                ctx_pref = getattr(self.pw_ctx, "context", None) or (ctxs[0] if ctxs else None)
                if ctx_pref is None:
                    return
                try:
                    drafts_page = await ctx_pref.new_page()
                except Exception:
                    return
                try:
                    await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
                except Exception:
                    pass

            await self._push_debug_progress(drafts_page, "已选定 Grok 目标页面，准备清理其它页面", level="info")

            drafts_page = await _keep_only_one_drafts_page(drafts_page)

            try:
                self.pw_ctx.page = drafts_page
            except Exception:
                pass
            try:
                await drafts_page.bring_to_front()
            except Exception:
                pass

            if refresh_target:
                try:
                    await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
                    await self._push_debug_progress(drafts_page, "Grok 目标页面刷新完成", level="ok")
                except Exception:
                    await self._push_debug_progress(drafts_page, "Grok 目标页面刷新失败（将继续流程）", level="warn")

                try:
                    await drafts_page.evaluate("() => { try { window.focus(); } catch(e) {} }")
                except Exception:
                    pass

                await asyncio.sleep(3.0)

            try:
                try:
                    page_html = await drafts_page.content()
                    if "Something went wrong. Please try again in a few minutes." in (page_html or ""):
                        await self._push_debug_progress(
                            drafts_page,
                            "检测到 Something went wrong 提示，先刷新目标页面",
                            level="warn",
                        )
                        try:
                            await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
                        except Exception:
                            pass
                        try:
                            await asyncio.sleep(2.0)
                        except Exception:
                            pass
                except Exception:
                    pass

                clicked, has_get_started = await self._maybe_click_get_started_button_if_prompted(drafts_page)
                if clicked:
                    try:
                        await asyncio.sleep(3.0)
                    except Exception:
                        pass

                    try:
                        await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
                    except Exception:
                        pass

                if not clicked and has_get_started:
                    await self._push_debug_progress(drafts_page, "重新再试一次点击 Get started", level="ok")
                    try:
                        await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
                    except Exception:
                        pass

                    clicked, has_get_started = await self._maybe_click_get_started_button_if_prompted(drafts_page)
                    if clicked:
                        await self._push_debug_progress(drafts_page, "重新再试一次点击 Get started 成功", level="ok")
                    else:
                        await self._push_debug_progress(drafts_page, "重新再试一次点击 Get started 失败", level="error")
            except Exception:
                pass

            try:
                maybe_cf = await self._is_cloudflare_page(drafts_page, deep=False)
                if maybe_cf:
                    try:
                        await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
                    except Exception:
                        pass
                    await asyncio.sleep(3.0)

                    await self._push_debug_progress(drafts_page, "页面疑似 Cloudflare，进入自愈流程", level="warn")
                    still_cf_after_wait = await self._wait_cloudflare_auto_pass(
                        drafts_page,
                        max_wait_seconds=45.0,
                        max_success_clicks=3,
                    )
                    if still_cf_after_wait and await self._is_cloudflare_page(drafts_page, deep=True):
                        await self._push_debug_progress(drafts_page, "Cloudflare 持续存在，准备重启窗口", level="warn")
                        await self._restart_window_and_restore_single_drafts(
                            drafts_url=drafts_url, target_host=target_host
                        )
                        try:
                            ctx_new = getattr(self.pw_ctx, "context", None)
                            br_new = getattr(self.pw_ctx, "browser", None)
                            ctxs_new: list[Any] = []
                            try:
                                ctxs_new = list(getattr(br_new, "contexts", []) or [])
                            except Exception:
                                pass
                            if ctx_new is not None and ctx_new not in ctxs_new:
                                ctxs_new.insert(0, ctx_new)

                            all_pages_new: list[tuple[Any, str]] = []
                            for c_n in ctxs_new:
                                try:
                                    ps = list(getattr(c_n, "pages", []) or [])
                                except Exception:
                                    ps = []
                                for p_n in ps:
                                    try:
                                        closed = bool(getattr(p_n, "is_closed", lambda: False)())
                                    except Exception:
                                        closed = False
                                    if closed:
                                        continue
                                    try:
                                        u_n = str(getattr(p_n, "url", "") or "").strip()
                                    except Exception:
                                        u_n = ""
                                    all_pages_new.append((p_n, u_n))

                            target_page_new: Any = None
                            for p_n, u_n in all_pages_new:
                                if _url_matches_target(u_n):
                                    target_page_new = p_n
                                    break
                            if target_page_new is None:
                                for p_n, u_n in all_pages_new:
                                    try:
                                        h_n = (urlparse(u_n).netloc or "").strip().lower()
                                    except Exception:
                                        h_n = ""
                                    if h_n == target_host:
                                        target_page_new = p_n
                                        break

                            if target_page_new is not None:
                                for p_n, _u_n in all_pages_new:
                                    if p_n is target_page_new:
                                        continue
                                    try:
                                        await p_n.close()
                                    except Exception:
                                        pass
                                try:
                                    self.pw_ctx.page = target_page_new
                                    drafts_page = target_page_new
                                except Exception:
                                    pass
                                try:
                                    await target_page_new.bring_to_front()
                                except Exception:
                                    pass
                                await self._push_debug_progress(
                                    drafts_page, "重启后已恢复 Grok 目标页面并置前", level="ok"
                                )
                        except Exception:
                            pass
            except Exception:
                pass

            await self._nonpenalized_raise_if_not_on_grok_site(drafts_page)

        if acquire_bring_lock:
            async with self._bring_drafts_lock:
                await _inner()
        else:
            await _inner()

    def _cancel_idle_close(self) -> None:
        t = self.idle_close_task
        self.idle_close_task = None
        if t and not t.done():
            try:
                cur = asyncio.current_task()
            except Exception:
                cur = None
            if cur is not None and t is cur:
                return
            t.cancel()

    def _schedule_idle_close(self) -> None:
        if bool(self.idle_close_disabled):
            self._cancel_idle_close()
            return
        self._cancel_idle_close()

        async def _job() -> None:
            try:
                secs = max(0.0, float(self.idle_close_seconds))
                if secs <= 0:
                    return
                await asyncio.sleep(secs)
                if bool(self.idle_close_disabled):
                    return
                if self.create_lock.locked():
                    return
                await self.close_and_drop()
            except asyncio.CancelledError:
                return
            except Exception:
                return

        self.idle_close_task = asyncio.create_task(_job())

    async def close_and_drop(self) -> None:
        await self.close()
        _drop_grok_session(self.cache_key)

    async def close(self) -> None:
        self._cancel_idle_close()
        await self.pw_ctx.close_and_drop()


def get_or_create_grok_session(
    *,
    vendor: str,
    base_url: str,
    access_key: Optional[str],
    space_id: str,
    window_key: str,
) -> GrokSession:
    k = _grok_key(vendor, base_url, space_id, window_key)
    sess = _GROK_SESSIONS.get(k)
    if sess is None:
        pw_ctx = get_or_create_playwright_ctx(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            space_id=space_id,
            window_key=window_key,
        )
        sess = GrokSession(cache_key=k, pw_ctx=pw_ctx)
        _GROK_SESSIONS[k] = sess
    else:
        sess.pw_ctx.access_key = access_key
    return sess


async def _grok_page_fetch_json(
    page: Any,
    *,
    url: str,
    method: str,
    json_data: Optional[Dict[str, Any]],
    log_file: Path,
    dynamic_statsig: bool = False,
) -> Dict[str, Any]:
    headers = _grok_browser_json_headers(dynamic_statsig=dynamic_statsig)
    return await page_fetch_json(page, url=url, method=method, headers=headers, json_data=json_data, log_file=log_file)


async def _grok_create_media_post(
    page: Any, *, prompt: str, log_file: Path, dynamic_statsig: bool = False
) -> str:
    tx = await _grok_page_fetch_json(
        page,
        url=MEDIA_POST_API,
        method="POST",
        json_data={
            "mediaType": "MEDIA_POST_TYPE_VIDEO",
            "prompt": _one_str(prompt) or " ",
        },
        log_file=log_file,
        dynamic_statsig=dynamic_statsig,
    )
    obj = tx.get("_json")
    if not isinstance(obj, dict):
        raise NonPenalizedTaskError("创建 Grok media post 失败：响应非 JSON", status_code=502)
    post = obj.get("post")
    pid = _one_str((post or {}).get("id")) if isinstance(post, dict) else ""
    if not pid:
        raise NonPenalizedTaskError(
            f"创建 Grok media post 失败：无 post.id body={safe_trim(json.dumps(obj, ensure_ascii=False), 400)}",
            status_code=502,
        )
    return pid


async def _grok_upload_one_image(
    page: Any,
    *,
    image_url: str,
    index: int,
    log_file: Path,
    dynamic_statsig: bool = False,
) -> Tuple[str, str]:
    """下载并上传到 Grok assets；返回 (fileMetadataId, assets_https_url)。"""
    u = _one_str(image_url)
    if u.startswith("data:"):
        # data URI
        try:
            header, b64 = u.split(",", 1)
        except ValueError as e:
            raise NonPenalizedTaskError("无效的 data URI 参考图", status_code=400) from e
        mime = "image/png"
        if ";" in header[5:]:
            mime = header[5:].split(";", 1)[0].strip() or mime
        ext = "png"
        if "/" in mime:
            ext = mime.split("/")[-1].split("+")[0] or ext
        fname = f"ref_{index + 1}.{ext}"
        content_b64 = re.sub(r"\s+", "", b64)
    else:
        raw, _hdrs = await _download_bytes_local_async(u, timeout_seconds=120.0)
        if not raw:
            raise NonPenalizedTaskError(f"参考图下载为空：{safe_trim(u, 120)}", status_code=400)
        content_b64 = base64.b64encode(raw).decode("ascii")
        fname = f"ref_{index + 1}.jpg"
        mime = "image/jpeg"

    tx = await _grok_page_fetch_json(
        page,
        url=UPLOAD_API,
        method="POST",
        json_data={"fileName": fname, "fileMimeType": mime, "content": content_b64},
        log_file=log_file,
        dynamic_statsig=dynamic_statsig,
    )
    obj = tx.get("_json")
    if not isinstance(obj, dict):
        raise NonPenalizedTaskError("上传参考图失败：响应非 JSON", status_code=502)
    fid = _one_str(obj.get("fileMetadataId"))
    furi = _one_str(obj.get("fileUri"))
    if not fid or not furi:
        raise NonPenalizedTaskError(
            f"上传参考图失败：缺少 fileMetadataId/fileUri {safe_trim(json.dumps(obj, ensure_ascii=False), 400)}",
            status_code=502,
        )
    return fid, f"https://assets.grok.com/{furi}"


async def _grok_post_app_chat_stream(
    page: Any, *, body: Dict[str, Any], log_file: Path, dynamic_statsig: bool = False
) -> str:
    """整段读取流式响应 body，解析最终 videoUrl。"""
    if page is None:
        raise RuntimeError("page 为 None")
    tx = await page_fetch_tx(
        page,
        url=CHAT_API,
        method="POST",
        headers=_grok_browser_json_headers(dynamic_statsig=dynamic_statsig),
        json_data=body,
        log_file=log_file,
    )
    status = tx.get("status")
    text = str(tx.get("response_body") or "")
    if status != 200:
        raise NonPenalizedTaskError(
            f"Grok app-chat 失败 status={status} body={safe_trim(text, 800)}",
            status_code=502,
        )
    url = _parse_video_url_from_stream_body(text)
    if not url:
        raise NonPenalizedTaskError(
            f"未从响应中解析到视频 URL（请确认窗口已登录且账号支持 Imagine 视频）body={safe_trim(text, 600)}",
            status_code=502,
        )
    return url


DEFAULT_GROK_TARGET = "https://grok.com/imagine"


async def grok_workflow(
    payload: Dict[str, Any],
    progress_cb: ProgressCB,
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    timeout_seconds: float,
    default_target_url: Optional[str] = None,
    headless: bool = False,
    access_token: Optional[str] = None,
    access_expires: Optional[str] = None,
    db: Any = None,
    task_type_window_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Grok Imagine 视频：指纹窗口内 fetch（默认 Cookie 鉴权；可选 SSO 见 ``access_token`` / payload）。"""
    del db, task_type_window_id, access_expires  # 预留与 Veo 一致的签名；expires 仅占位

    p = dict(payload or {})
    prompt = _one_str(p.get("prompt"))
    if not prompt:
        raise NonPenalizedTaskError("payload.prompt 不能为空", status_code=400)

    ref_urls = _grok_collect_reference_image_urls(p)
    if len(ref_urls) > 7:
        raise NonPenalizedTaskError("参考图最多 7 张", status_code=400)

    aspect_ratio = _grok_resolve_aspect_ratio(p)
    video_length = _grok_resolve_video_length_seconds(p)
    resolution = _grok_resolve_resolution(p)
    preset = _normalize_preset(p)
    dynamic_statsig = bool(p.get("grok_dynamic_statsig"))

    monitor_log_path = _one_str(p.get("monitor_log_path")) or None
    target_page = _one_str(default_target_url) or _one_str(p.get("grok_url") or p.get("target_url")) or DEFAULT_GROK_TARGET

    sess = get_or_create_grok_session(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = headless
    sess.monitor_log_path = monitor_log_path
    sess.idle_close_seconds = float(p.get("ctx_idle_close_seconds") or 30.0)

    log_file = sess._log_file
    started = time.time()

    await progress_cb(
        2,
        {
            "stage": "init",
            "workflow_kind": "video",
            "video_mode": "r2v" if len(ref_urls) > 1 else ("i2v" if len(ref_urls) == 1 else "t2v"),
            "prompt": safe_trim(prompt, 200),
            "reference_count": len(ref_urls),
        },
    )

    async with sess.create_lock:
        try:
            sess.idle_close_disabled = True
            sess._cancel_idle_close()
            await sess.ensure_open(
                args=sess.browser_open_args,
                force_open=sess.browser_force_open,
                headless=headless,
            )
            append_log(log_file, f"[grok] ensure_open ok target={safe_trim(target_page, 120)!r}")

            await sess._bring_target_page_to_front(refresh_target=True, drafts_url=target_page)
            await progress_cb(8, {"stage": "navigate", "url": target_page})
            append_log(log_file, "[grok] _bring_target_page_to_front completed")

            page = sess.pw_ctx.page
            if page is None:
                raise NonPenalizedTaskError("Grok 目标页面未初始化（pw_ctx.page 为空）", status_code=502)

            try:
                await asyncio.sleep(float(p.get("grok_post_nav_sleep_seconds") or 1.5))
            except Exception:
                pass

            sso_raw = (
                _one_str(p.get("grok_access_token") or p.get("access_token") or "").strip()
                or _one_str(access_token or "").strip()
            )
            if sso_raw:
                await _grok_merge_optional_sso_cookies(page, sso_raw, log_file)
                await progress_cb(9, {"stage": "sso_cookies", "applied": True})
            else:
                await progress_cb(9, {"stage": "sso_cookies", "applied": False})

            prompt_text = prompt
            asset_ids: List[str] = []
            image_https: List[str] = []

            for i, u in enumerate(ref_urls):
                await progress_cb(
                    10 + i * 3,
                    {"stage": "upload_reference", "index": i + 1, "total": len(ref_urls)},
                )
                fid, https_u = await _grok_upload_one_image(
                    page,
                    image_url=u,
                    index=i,
                    log_file=log_file,
                    dynamic_statsig=dynamic_statsig,
                )
                asset_ids.append(fid)
                image_https.append(https_u)
                append_log(log_file, f"[grok] uploaded ref {i + 1} id={safe_trim(fid, 40)!r}")

            if asset_ids:
                prompt_text = _replace_reference_placeholders(prompt, asset_ids)
            elif _REFERENCE_PLACEHOLDER_RE.search(prompt_text):
                raise NonPenalizedTaskError(
                    "提示词含 @图N 占位符但未提供参考图", status_code=400
                )

            post_id = await _grok_create_media_post(
                page, prompt=prompt_text, log_file=log_file, dynamic_statsig=dynamic_statsig
            )
            append_log(log_file, f"[grok] media post id={safe_trim(post_id, 48)!r}")

            vcfg: Dict[str, Any] = {
                "aspectRatio": aspect_ratio,
                "parentPostId": post_id,
                "resolutionName": resolution,
                "videoLength": video_length,
            }
            if image_https:
                vcfg["imageReferences"] = image_https
                vcfg["isReferenceToVideo"] = True

            model_override = {"modelMap": {"videoGenModelConfig": vcfg}}
            message = f"{prompt_text} {_build_mode_suffix(preset)}".strip()
            chat_body = _build_app_chat_payload(
                message=message,
                model="grok-3",
                model_config_override=model_override,
                file_attachments=list(asset_ids),
            )

            await progress_cb(55, {"stage": "app_chat", "message_len": len(message)})
            video_raw = await _grok_post_app_chat_stream(
                page, body=chat_body, log_file=log_file, dynamic_statsig=dynamic_statsig
            )
            vurls = _grok_normalize_video_urls(video_raw)
            video_url = vurls["video_url"]
            video_asset_url = vurls["video_asset_url"]
            video_share_url = vurls["video_share_url"]
            append_log(
                log_file,
                f"[grok] video urls main={safe_trim(video_url, 120)!r} asset={safe_trim(video_asset_url, 120)!r}",
            )

            elapsed_ms = int(max(0.0, (time.time() - started) * 1000.0))
            await progress_cb(
                100,
                {
                    "stage": "done",
                    "video_url": video_url,
                    "video_asset_url": video_asset_url,
                    "video_share_url": video_share_url,
                    "elapsed_ms": elapsed_ms,
                },
            )

            out: Dict[str, Any] = {
                "type": "grok_workflow_video",
                "message": "Grok 多参考图视频生成完成" if len(ref_urls) > 1 else ("Grok 图生视频完成" if ref_urls else "Grok 文生视频完成"),
                "video_url": video_url,
                "workflow_kind": "video",
                "video_mode": "r2v" if len(ref_urls) > 1 else ("i2v" if ref_urls else "t2v"),
                "reference_count": len(ref_urls),
                "aspect_ratio": aspect_ratio,
                "video_length": video_length,
                "resolution": resolution,
                "preset": preset,
                "elapsed_ms": elapsed_ms,
            }
            if video_asset_url:
                out["video_asset_url"] = video_asset_url
            if video_share_url:
                out["video_share_url"] = video_share_url
            return out
        finally:
            try:
                await sess.disconnect_playwright_under_bring_lock()
            except Exception:
                pass


async def grok_admin_open_connect_page(
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    headless: bool = False,
    default_target_url: Optional[str] = None,
    pure_mode: bool = True,
    timeout_seconds: float = 120.0,
) -> Dict[str, Any]:
    """管理台：打开 Grok 页面并断开 CDP，供用户手动登录。"""
    del timeout_seconds  # 导航超时由 Playwright 默认与 bring 内部处理
    sess = get_or_create_grok_session(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = headless
    sess.browser_pure_mode = pure_mode
    sess.idle_close_disabled = True
    sess._cancel_idle_close()
    url = _one_str(default_target_url) or DEFAULT_GROK_TARGET
    await sess.ensure_open(
        args=sess.browser_open_args,
        force_open=sess.browser_force_open,
        headless=headless,
        pure_mode=pure_mode,
    )
    await sess._bring_target_page_to_front(refresh_target=False, drafts_url=url)
    try:
        await sess.disconnect_playwright_under_bring_lock()
    except Exception:
        pass
    return {
        "message": "已打开 Grok 页面并断开 CDP；请在本窗口登录 xAI 账号。视频任务默认使用窗口 Cookie；可选在任务类型绑定中保存 Grok SSO（与 grok2api 令牌同源，写入 mapping 的 access_token 列），执行前会注入 sso/sso-rw Cookie。",
        "url": url,
    }
