"""VEO 视频生成工作流执行器。

职责边界：
- `playwright_broswer_context.py`：通用"指纹浏览器自动化层"（开窗/连CDP/挑页/page内fetch）
- `veo_workflow_executor.py`：VEO 站点侧逻辑（打开页面、提交生成任务、轮询进度、获取结果）

入口：
- `veo_workflow`（由 `task_service.py` 发起调用）
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import io
import json
import random
from math import gcd
import re
import socket
import ssl
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import httpx
from ..core.config import config as app_config
from ..core.database import Database
from ..core.logger import logger
from ..core.paths import APP_ROOT, MONITOR_LOG_FILE
from .playwright_broswer_context import (
    PlaywrightBrowserContext,
    acquire_browser_open_slot,
    append_log,
    get_or_create_ctx as get_or_create_playwright_ctx,
    page_fetch_json,
    safe_trim,
)
from .sora_task_executor import (
    _pick_n_frames,
)
from .oss_uploader import build_veo_upsample_object_key, oss_config_from_setting_section, upload_bytes_to_oss
from .task_executor_types import NonPenalizedTaskError, ProgressCB
from .browser_extension_bridge import should_use_extension_executor
from .browser_extension_interaction import (
    ensure_extension_connected_via_window,
    submit_extension_task,
    wait_extension_client,
)


async def _noop_progress_cb(progress: int, data: Dict[str, Any]) -> None:
    return None


def _default_browser_extension_args() -> list[str]:
    ext_dir = APP_ROOT / "browser_extension"
    if not (ext_dir / "manifest.json").exists():
        return []
    return [f"--load-extension={ext_dir.resolve()}"]


async def refresh_veo_balance_via_extension(
    *,
    db: Database,
    picked: Any,
    refresh_timeout_seconds: float,
    signal_window_pool_replenish: Optional[Callable[[], None]] = None,
    auto_triger_connection: Optional[bool] = True,
    force_refresh_token: Optional[bool] = False,
) -> Optional[Dict[str, Any]]:
    """Refresh VEO credits through the browser extension token/balance interfaces."""
    try:
        veo_target = str(picked.default_target_url or "").strip() or "https://labs.google/fx"
        veo_sess = get_or_create_veo_session(
            vendor=picked.browser_vendor,
            base_url=picked.browser_base_url,
            access_key=picked.browser_access_key,
            space_id=picked.space_id,
            window_key=picked.window_key,
        )
        veo_sess.browser_headless = bool(getattr(picked, "headless", False))
        veo_sess.browser_pure_mode = bool(getattr(picked, "pure_mode", True))

        access_token = ""
        access_expires = ""

        try:
            mid = int(picked.mapping_id)
            if mid > 0:
                row = await db.get_task_type_window_context(mid)
                if row:
                    access_token = str(row.get("sora_access_token") or "").strip() or None
                    access_expires = str(row.get("sora_access_expires") or "").strip() or None
        except Exception as e:
            pass

        if force_refresh_token or not _veo_cached_access_still_valid(access_token, access_expires, margin_seconds=10):
            token_info = await veo_fetch_access_tokens_via_extension(
                sess=veo_sess,
                target_url=veo_target,
                space_id=picked.space_id,
                window_key=picked.window_key,
                connect_wait_seconds=8.0,
                token_timeout_seconds=min(45.0, max(10.0, float(refresh_timeout_seconds or 45.0))),
                log_file=MONITOR_LOG_FILE,
                auto_triger_connection = auto_triger_connection,
                access_token=access_token,
                access_expires=access_expires,
                short_access_token=access_token,
                short_expires=access_expires,
            )
            
            access_token = str((token_info or {}).get("short_access_token") or "").strip()
            access_expires = str((token_info or {}).get("short_expires") or "").strip() or None
        if not access_token:
            return None
        print(f"veo_target:{veo_target}");
        result = await asyncio.wait_for(
            submit_extension_task(
                space_id=picked.space_id,
                window_key=picked.window_key,
                provider="veo",
                payload={
                    "action": "refresh_balance",
                    "workflow_kind": "balance_refresh",
                    "project_page": veo_target,
                    "access_token": access_token,
                    "access_expires": access_expires,
                    "fetch_cooldown": False,
                },
                progress_cb=_noop_progress_cb,
                timeout_seconds=max(1.0, float(refresh_timeout_seconds or 45.0)),
            ),
            timeout=max(1.0, float(refresh_timeout_seconds or 45.0)) + 5.0,
        )
        if result is not None and result.get("credits") is not None:
            _kw: Dict[str, Any] = {
                "mapping_id": picked.mapping_id,
                "remaining_quota": int(result.get("credits") or 0),
                "sora_remaining_count": int(result.get("credits") or 0),
                "sora_access_token": access_token,
                "sora_access_expires": access_expires,
            }
            _kw["cooldown_until"] = str(result.get("cooldown_until") or _veo_local_next_0105_cooldown_str())
            await db.update_task_type_window(**_kw)
            try:
                cur_q = int(result.get("credits") or 0)
                if cur_q < 30 and signal_window_pool_replenish is not None:
                    hi = await db.task_type_has_mapping_remaining_quota_above(picked.task_code, 30)
                    if hi:
                        signal_window_pool_replenish()
            except Exception:
                pass
            return result
        return None
    except Exception as e:
        logger.warning("refresh_veo_balance_via_extension skipped: mapping=%s err=%s", getattr(picked, "mapping_id", None), e)
        return None


async def _veo_extension_upload_upsample_data_url_to_oss(
    result: Dict[str, Any],
    *,
    project_id: str,
    log_file: Path,
) -> Dict[str, Any]:
    """插件模式：把插件返回的 2K data:image/jpeg;base64 上传 OSS，并将结果 URL 回填。

    非插件路径的 2K 放大是在 Python 内直接拿到 base64 后上传 OSS；插件路径的 base64
    由浏览器插件返回，这里对齐非插件行为，避免最终 API 返回大段 base64。
    """
    if not isinstance(result, dict):
        return result
    if str(result.get("type") or "") != "veo_workflow_image":
        return result
    if not result.get("upsample_ok"):
        return result
    share = str(result.get("share_url") or result.get("image_url") or "").strip()
    prefix = "data:image/"
    if not share.startswith(prefix) or ";base64," not in share:
        return result

    oss_cfg = oss_config_from_setting_section((app_config.get_raw_config() or {}).get("oss"))
    if not oss_cfg.enabled:
        append_log(log_file, "[veo][extension][image] 2K data URL kept because OSS disabled")
        return result

    try:
        b64 = share.split(";base64,", 1)[1].strip()
        raw = base64.b64decode(b64, validate=False)
        if not raw:
            raise ValueError("base64 解码后为空")
        media_name = str(result.get("generated_media_id") or "").strip() or None
        object_key = build_veo_upsample_object_key(project_id=str(project_id), media_name=media_name)
        url = await asyncio.to_thread(
            upload_bytes_to_oss,
            cfg=oss_cfg,
            data=raw,
            object_key=object_key,
            content_type="image/jpeg",
        )
        out = dict(result)
        out["share_url"] = url
        out["image_url"] = url
        out["upsample_url"] = url
        out["upsample_oss_object_key"] = object_key
        append_log(log_file, f"[veo][extension][image] uploaded 2K data URL to OSS object_key={object_key!r}")
        return out
    except Exception as e:
        out = dict(result)
        out["upsample_error"] = str(out.get("upsample_error") or f"OSS上传失败：{_short_err_msg(e, max_len=200)}")
        append_log(log_file, f"[veo][extension][image] upload 2K data URL to OSS failed, keep data URL: {e}")
        return out

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _short_err_msg(err: Any, *, max_len: int = 120) -> str:
    try:
        s = str(err or "").strip()
    except Exception:
        s = ""
    if not s:
        return ""
    if len(s) <= max_len:
        return s
    return s[: max(10, max_len - 3)] + "..."

def _veo_is_unsafe_generation_message(err: Any) -> bool:
    try:
        s = str(err or "").strip()
    except Exception:
        s = ""
    return "PUBLIC_ERROR_UNSAFE_GENERATION" in s


def _veo_is_auth_credentials_error(err: Any) -> bool:
    """VEO 上游/插件返回的 401 凭证失效错误。

    插件模式下图片/视频提交都可能因为 short access_token 过期或长效
    session-token 轮换而返回 UNAUTHENTICATED。命中后需要强制开窗刷新长短 token。
    """
    try:
        s = str(err or "").strip()
    except Exception:
        s = ""
    if not s:
        return False
    low = s.lower()
    return (
        "unauthenticated" in low
        or "invalid authentication credentials" in low
        or "expected oauth 2 access token" in low
        or "http 401" in low
        or " 401 " in f" {low} "
    )


def _veo_parse_access_expires(raw: Any) -> Optional[datetime]:
    """解析 Labs / NextAuth 返回的 expires（ISO-8601、带 Z、或 SQLite 本地时间串）。"""
    s = str(raw or "").strip()
    if not s:
        return None
    if s.endswith("Z") and len(s) > 1:
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1))
        return dt
    except ValueError:
        pass
    try:
        if len(s) >= 19:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _veo_cached_access_still_valid(
    token: Optional[str],
    expires_raw: Optional[str],
    *,
    margin_seconds: float,
) -> bool:
    """若 mapping 上已有 access_token 且 expires 未到（预留 margin），则无需再开窗拉取。"""
    at = str(token or "").strip()
    if not at:
        return False
    exp_s = str(expires_raw or "").strip()
    if not exp_s:
        # auth/session 未给 expires、或仅存 session_token 时无法判断过期点，沿用缓存避免每次开窗
        return True
    dt = _veo_parse_access_expires(exp_s)
    if dt is None:
        return False
    margin = timedelta(seconds=max(0.0, float(margin_seconds)))
    if dt.tzinfo is None:
        return datetime.now() + margin < dt
    return datetime.now(timezone.utc) + margin < dt.astimezone(timezone.utc)


def _build_debug_progress_panel_script() -> str:
    """返回调试进度面板注入脚本（单实例，重复调用只更新内容）。"""
    return r"""
(payload) => {
  try {
    const PANEL_ID = "__veo_debug_progress_panel__";
    const STYLE_ID = "__veo_debug_progress_panel_style__";
    const safe = (v) => (v === null || v === undefined) ? "" : String(v);
    const data = {
      title: safe(payload && payload.title),
      updatedAt: safe(payload && payload.updatedAt),
      entries: Array.isArray(payload && payload.entries) ? payload.entries.map((x) => ({
        idx: safe(x && x.idx),
        ts: safe(x && x.ts),
        level: safe(x && x.level),
        text: safe(x && x.text),
      })) : [],
    };

    if (!document.getElementById(STYLE_ID)) {
      const style = document.createElement("style");
      style.id = STYLE_ID;
      style.textContent = `
#${PANEL_ID}{
position:fixed;top:16px;right:16px;z-index:2147483647;width:420px;
background:rgba(15,23,42,.95);color:#e5e7eb;border:1px solid rgba(148,163,184,.35);
border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.35);font-size:12px;font-family:Arial,sans-serif;
}
#${PANEL_ID}.min{width:220px}
#${PANEL_ID} .hdr{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border-bottom:1px solid rgba(148,163,184,.25)}
#${PANEL_ID} .ttl{font-weight:700;color:#f8fafc}
#${PANEL_ID} .btn{background:#334155;color:#f8fafc;border:0;border-radius:8px;padding:4px 8px;cursor:pointer}
#${PANEL_ID} .bd{padding:10px 12px;max-height:55vh;overflow:auto}
#${PANEL_ID} .it{padding:7px 8px;margin-bottom:6px;border-radius:8px;background:rgba(30,41,59,.75)}
#${PANEL_ID} .it.ok{border-left:3px solid #22c55e}
#${PANEL_ID} .it.warn{border-left:3px solid #f59e0b}
#${PANEL_ID} .it.err{border-left:3px solid #ef4444}
#${PANEL_ID} .meta{font-size:11px;color:#94a3b8;margin-bottom:3px}
#${PANEL_ID} .txt{white-space:pre-wrap;word-break:break-word;line-height:1.4}
#${PANEL_ID} .fts{padding:8px 12px;border-top:1px solid rgba(148,163,184,.2);color:#94a3b8}
      `.trim();
      document.documentElement.appendChild(style);
    }

    let panel = document.getElementById(PANEL_ID);
    if (!panel) {
      panel = document.createElement("div");
      panel.id = PANEL_ID;
      panel.innerHTML = `
        <div class="hdr">
          <span class="ttl"></span>
          <button class="btn tg" type="button">收起</button>
        </div>
        <div class="bd"></div>
        <div class="fts"></div>
      `;
      document.documentElement.appendChild(panel);
    }

    const ttl = panel.querySelector(".ttl");
    const bd = panel.querySelector(".bd");
    const fts = panel.querySelector(".fts");
    const tg = panel.querySelector(".tg");
    if (!ttl || !bd || !fts || !tg) return;

    ttl.textContent = data.title || "VEO 调试进度";
    bd.innerHTML = "";
    for (const e of data.entries) {
      const item = document.createElement("div");
      const lv = (e.level || "info").toLowerCase();
      let cls = "it";
      if (lv === "ok" || lv === "success") cls += " ok";
      else if (lv === "warn" || lv === "warning") cls += " warn";
      else if (lv === "err" || lv === "error" || lv === "fail") cls += " err";
      item.className = cls;

      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `#${safe(e.idx)}  ${safe(e.ts)}  [${lv || "info"}]`;

      const txt = document.createElement("div");
      txt.className = "txt";
      txt.textContent = safe(e.text);

      item.appendChild(meta);
      item.appendChild(txt);
      bd.appendChild(item);
    }

    fts.textContent = data.updatedAt ? `更新时间: ${data.updatedAt}` : "";
    try {
      bd.scrollTop = bd.scrollHeight;
    } catch (_e1) {}

    tg.onclick = () => {
      const min = panel.classList.toggle("min");
      bd.style.display = min ? "none" : "block";
      fts.style.display = min ? "none" : "block";
      tg.textContent = min ? "展开" : "收起";
    };
  } catch (_e) {
    // 忽略注入失败，避免影响主流程。
  }
}
"""


# ---------------------------------------------------------------------------
# VEO Session：按 window 维度缓存
# ---------------------------------------------------------------------------
_VEO_SESSIONS: Dict[str, "VeoSession"] = {}


def _veo_key(vendor: str, base_url: str, space_id: str, window_key: str) -> str:
    return f"veo|{vendor}|{base_url}|{space_id}|{window_key}"


def _drop_veo_session(cache_key: str) -> None:
    k = (cache_key or "").strip()
    if not k:
        return
    _VEO_SESSIONS.pop(k, None)


class VeoSession:
    """按 window 维度缓存的 VEO 会话。

    复用同一个指纹浏览器窗口与 Playwright CDP 连接。
    """

    def __init__(self, cache_key: str, pw_ctx: PlaywrightBrowserContext) -> None:
        self.cache_key = cache_key
        self.pw_ctx = pw_ctx

        self.last_used_at: float = time.time()
        self.create_lock = asyncio.Lock()
        self._bring_drafts_lock = asyncio.Lock()

        self.idle_close_task: Optional[asyncio.Task] = None
        self.idle_close_disabled: bool = False

        self.monitor_log_path: Optional[str] = None
        self.idle_close_seconds: float = 30.0

        self.browser_open_args: list[str] = _default_browser_extension_args()
        self.browser_force_open: bool = False
        self.browser_headless: bool = False
        self.browser_pure_mode: bool = True

        self.debug_panel_seq: int = 0
        self.debug_panel_entries: list[Dict[str, str]] = []

        self.veo_short_access_token: Optional[str] = None
        self.veo_short_access_expires: Optional[str] = None
        self.veo_short_session_token: Optional[str] = None
        self.veo_short_email: Optional[str] = None
        self.veo_short_token_lock = asyncio.Lock()

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
        """确保指纹浏览器窗口已打开、CDP 已连接。"""
        self.last_used_at = time.time()
        pm = self.browser_pure_mode if pure_mode is None else bool(pure_mode)
        # 串行化窗口 open/close：避免并发 ensure_open 与 Cloudflare 自愈重启产生竞态。
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
        """先占 _bring_drafts_lock，再占 pw_ctx.driver_lock，再断开 CDP（与 bring 及仅持 driver_lock 的页面逻辑互斥）。"""
        async with self._bring_drafts_lock:
            async with self.pw_ctx.driver_lock:
                await self.pw_ctx.disconnect_playwright_only()

    async def navigate_to(self, url: str, *, timeout_ms: int = 60_000) -> None:
        """导航到指定 URL。"""
        if self.pw_ctx.page is None:
            raise RuntimeError("page 未初始化，请先调用 ensure_open")
        try:
            await self.pw_ctx.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e:
            raise NonPenalizedTaskError(f"打开 VEO 页面失败：{e}", status_code=400) from e

    async def page_fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """在浏览器页面上下文中发起 fetch 请求（走指纹浏览器网络栈）。"""
        if self.pw_ctx.page is None:
            raise RuntimeError("page 未初始化，请先调用 ensure_open")
        return await page_fetch_json(
            self.pw_ctx.page,
            url=url,
            method=method,
            headers=headers or {"Accept": "application/json", "Content-Type": "application/json"},
            json_data=json_data,
            log_file=self._log_file,
        )

    # ------------------------------------------------------------------
    # 调试面板
    # ------------------------------------------------------------------
    async def _push_debug_progress(self, page: Any, text: str, *, level: str = "info") -> None:
        """向页面插件弹窗写入调试步骤；同一页面始终复用单个面板。"""
        return;
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
            "title": "VEO 调试进度",
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "entries": list(self.debug_panel_entries),
        }
        script = _build_debug_progress_panel_script()
        try:
            await page.evaluate(script, payload)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Cloudflare 检测 & 自愈
    # ------------------------------------------------------------------
    async def _is_cloudflare_page(self, page, *, deep: bool = False) -> bool:
        """判断当前页面是否为 Cloudflare 拦截/挑战页。"""
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

    async def raise_if_cloudflare_page_nonpenalized(
        self,
        page,
        *,
        stage: str,
        target_url: str,
        window_pool_google_relogin_db: Optional[Database] = None,
        window_pool_google_relogin_window_pk: Optional[int] = None,
        window_pool_google_relogin_timeout_ms: Optional[int] = None,
    ) -> None:
        """与 Sora `_raise_if_cloudflare_page_nonpenalized` 同类：bring 目标页 + 等待/重启，仍判定 CF 则抛 NonPenalizedTaskError（用于窗口池巡检）。

        window_pool_google_*：窗口池巡检时若检测到 Google 登录/选账号页，先按窗口凭据自动登录再 bring。
        """
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
            if (
                window_pool_google_relogin_db is not None
                and window_pool_google_relogin_window_pk is not None
                and int(window_pool_google_relogin_window_pk) > 0
            ):
                try:
                    to_ms = int(window_pool_google_relogin_timeout_ms or 120_000)
                except Exception:
                    to_ms = 120_000
                to_ms = max(45_000, min(to_ms, 240_000))
                await self._perform_google_relogin_if_stuck_on_accounts(
                    db=window_pool_google_relogin_db,
                    window_pk=int(window_pool_google_relogin_window_pk),
                    timeout_ms=to_ms,
                )
            await self._bring_target_page_to_front(
                refresh_target=False, drafts_url=target_url, acquire_bring_lock=False
            )
            try:
                th = (urlparse(target_url).netloc or "").strip().lower()
            except Exception:
                th = ""
            cur = self.pw_ctx.page
            for _ in range(2):
                if cur is None:
                    return
                if not await self._is_cloudflare_page(cur, deep=False):
                    return
                await self._bring_target_page_to_front(
                    refresh_target=False, drafts_url=target_url, acquire_bring_lock=False
                )
                cur = self.pw_ctx.page
                if cur is None:
                    return
            if await self._is_cloudflare_page(cur, deep=False):
                raise NonPenalizedTaskError(
                    f"当前页面为 Cloudflare 验证/拦截页，无法继续：{stage}",
                    status_code=503,
                )

    async def _try_click_cloudflare_checkbox(self, page) -> bool:
        """尝试点击 Cloudflare Turnstile challenge 的 checkbox。

        策略：
        1. frame.locator()：CDP 原生可穿透 shadow-root，尝试直接点击
        2. 坐标法：frame_element().bounding_box() 拿到 iframe 屏幕位置，
           模拟拟人化鼠标移动后 mouse.click() 点击 checkbox 坐标
        """
        log_file = (
            Path(self.monitor_log_path)
            if self.monitor_log_path
            else (MONITOR_LOG_FILE)
        )

        def _log(msg: str) -> None:
            try:
                append_log(log_file, f"[cf_checkbox] {msg}")
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

            # 策略1：frame.locator()（CDP 可穿 closed shadow-root）
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

            # 策略2：坐标法 + 拟人化鼠标移动
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
        page,
        *,
        max_wait_seconds: float = 10.0,
        max_success_clicks: int = 2,
    ) -> bool:
        """等待 Cloudflare 可能自动放行，同时尝试点击 Turnstile checkbox。

        返回：
        - True: 超时后仍像 Cloudflare（可考虑重启）
        - False: 已不再像 Cloudflare（无需重启）
        """
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
        """关闭并重开指纹浏览器窗口（仅打开窗口，不连接 CDP/不查找页面）。"""
        log_file = (
            Path(self.monitor_log_path)
            if self.monitor_log_path
            else (MONITOR_LOG_FILE)
        )
        try:
            append_log(log_file, "[veo][drafts] detected cloudflare interstitial, restarting fp window once")
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

        try:
            append_log(log_file, "[veo][drafts] reopen window only: skip cdp connect/page probing")
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
                    append_log(log_file, f"[veo][drafts] browser_open result code={code}")
                except Exception:
                    pass
            except Exception as e:
                try:
                    append_log(log_file, f"[veo][drafts] browser_open failed: {e}")
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
                append_log(log_file, f"[veo][drafts] CDP reconnect after restart failed: {e}")
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------
    # Login 按钮检测 & 点击
    # ------------------------------------------------------------------
    async def _maybe_click_login_button_if_prompted(self, page) -> tuple:
        has_login_button = False
        """尝试点击页面上的 Log in 按钮/链接（不依赖固定提示文案）。"""
        if page is None:
            return False, has_login_button

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

        login_name_re = re.compile(r"log\s*in", re.IGNORECASE)

        try:
            for sc in scopes:
                try:
                    if hasattr(sc, "get_by_role"):
                        btn_cnt = await sc.get_by_role("button", name=login_name_re).count()
                        link_cnt = await sc.get_by_role("link", name=login_name_re).count()
                        if (btn_cnt + link_cnt) > 0:
                            has_login_button = True
                            break
                    loc_probe = sc.locator('button, a, [role="button"], [role="link"]').filter(has_text=login_name_re)
                    if (await loc_probe.count()) > 0:
                        has_login_button = True
                        break
                except Exception:
                    continue
        except Exception:
            has_login_button = False

        if not has_login_button:
            await self._push_debug_progress(page, "未发现 Log in 按钮/链接", level="info")
            return False, has_login_button
        await self._push_debug_progress(page, "发现 Log in 按钮/链接，准备点击", level="info")

        for sc in scopes:
            try:
                scope_name = "page" if sc is page else "frame"
                if hasattr(sc, "get_by_role"):
                    try:
                        btn = sc.get_by_role("button", name=login_name_re)
                        await btn.first.click(timeout=3000)
                        await self._push_debug_progress(page, f"点击 Log in 成功（button/{scope_name}）", level="ok")
                        return True, has_login_button
                    except Exception as e:
                        await self._push_debug_progress(page, f"点击 Log in 失败（button/{scope_name}）：{_short_err_msg(e)}", level="warn")
                    try:
                        link = sc.get_by_role("link", name=login_name_re)
                        await link.first.click(timeout=3000)
                        await self._push_debug_progress(page, f"点击 Log in 成功（link/{scope_name}）", level="ok")
                        return True, has_login_button
                    except Exception as e:
                        await self._push_debug_progress(page, f"点击 Log in 失败（link/{scope_name}）：{_short_err_msg(e)}", level="warn")

                try:
                    loc2 = sc.locator('button, a, [role="button"], [role="link"]').filter(has_text=login_name_re)
                    await loc2.first.click(timeout=3000)
                    await self._push_debug_progress(page, f"点击 Log in 成功（text fallback/{scope_name}）", level="ok")
                    return True, has_login_button
                except Exception as e:
                    await self._push_debug_progress(page, f"点击 Log in 失败（text fallback/{scope_name}）：{_short_err_msg(e)}", level="warn")
            except Exception:
                continue

        await self._push_debug_progress(page, "点击 Log in 失败（全部策略）", level="error")
        return False, has_login_button

    async def _maybe_click_get_started_button_if_prompted(self, page) -> tuple:
        has_get_started = False
        """尝试点击页面上的 Get started 按钮/链接。"""
        if page is None:
            return False, has_get_started

        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

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

        button_patterns = [
            ("Get started", re.compile(r"get\s*started", re.IGNORECASE)),
            ("使ってみる", re.compile(r"使ってみる")),
        ]

        matched_label: str = ""
        matched_re: Any = None

        try:
            for sc in scopes:
                try:
                    for label, pat in button_patterns:
                        if hasattr(sc, "get_by_role"):
                            btn_cnt = await sc.get_by_role("button", name=pat).count()
                            link_cnt = await sc.get_by_role("link", name=pat).count()
                            if (btn_cnt + link_cnt) > 0:
                                has_get_started = True
                                matched_label, matched_re = label, pat
                                break
                        loc_probe = sc.locator('button, a, [role="button"], [role="link"]').filter(has_text=pat)
                        if (await loc_probe.count()) > 0:
                            has_get_started = True
                            matched_label, matched_re = label, pat
                            break
                    if has_get_started:
                        break
                except Exception:
                    continue
        except Exception:
            has_get_started = False

        if not has_get_started:
            await self._push_debug_progress(page, "未发现 Get started / 使ってみる 按钮/链接", level="info")
            return False, has_get_started
        await self._push_debug_progress(page, f"发现 {matched_label} 按钮/链接，准备点击", level="info")

        for sc in scopes:
            try:
                scope_name = "page" if sc is page else "frame"
                if hasattr(sc, "get_by_role"):
                    try:
                        btn = sc.get_by_role("button", name=matched_re)
                        await btn.first.click(timeout=3000)
                        await self._push_debug_progress(page, f"点击 {matched_label} 成功（button/{scope_name}）", level="ok")
                        return True, has_get_started
                    except Exception as e:
                        await self._push_debug_progress(page, f"点击 {matched_label} 失败（button/{scope_name}）：{_short_err_msg(e)}", level="warn")
                    try:
                        link = sc.get_by_role("link", name=matched_re)
                        await link.first.click(timeout=3000)
                        await self._push_debug_progress(page, f"点击 {matched_label} 成功（link/{scope_name}）", level="ok")
                        return True, has_get_started
                    except Exception as e:
                        await self._push_debug_progress(page, f"点击 {matched_label} 失败（link/{scope_name}）：{_short_err_msg(e)}", level="warn")

                try:
                    loc2 = sc.locator('button, a, [role="button"], [role="link"]').filter(has_text=matched_re)
                    await loc2.first.click(timeout=3000)
                    await self._push_debug_progress(page, f"点击 {matched_label} 成功（text fallback/{scope_name}）", level="ok")
                    return True, has_get_started
                except Exception as e:
                    await self._push_debug_progress(page, f"点击 {matched_label} 失败（text fallback/{scope_name}）：{_short_err_msg(e)}", level="warn")
            except Exception:
                continue

        await self._push_debug_progress(page, f"点击 {matched_label} 失败（全部策略）", level="error")
        return False, has_get_started

    async def _click_google_gmail_account_row(self, p: Any) -> None:
        """Google「Choose an account」页：真实可点击区域多为外层 [role=link]，邮箱在 div[data-email]（见 Google 新版 DOM）。"""
        gmail_re = re.compile(r"@gmail\.com", re.I)
        timeout_ms = 15_000

        async def _try_click(locator: Any, *, force: bool = False) -> bool:
            try:
                n = await locator.count()
            except Exception:
                n = 0
            if n <= 0:
                return False
            el = locator.first
            try:
                await el.scroll_into_view_if_needed(timeout=timeout_ms)
            except Exception:
                pass
            try:
                await el.click(timeout=timeout_ms, force=force)
                return True
            except Exception:
                return False

        # 1) 账号行容器（DevTools：li > div[role=link][jsname=W3oRb] 包裹整行）
        if await _try_click(p.locator('[role="link"]:has(div[data-email*="@gmail.com"])')):
            return
        if await _try_click(p.locator('li:has(div[data-email*="@gmail.com"]) [role="link"]')):
            return
        # 2) jsname=bQiQze 的邮箱格（与 data-email 同节点，部分环境需 force）
        if await _try_click(p.locator('div[jsname="bQiQze"][data-email*="@gmail.com"]'), force=True):
            return
        if await _try_click(p.locator('div[data-email*="@gmail.com"]'), force=True):
            return
        # 3) 旧版 / 纯文案
        if await _try_click(p.locator("div").filter(has_text=gmail_re), force=True):
            return
        if await _try_click(p.locator("[role='listitem']").filter(has_text=gmail_re)):
            return
        # 5) DOM 原生 click（部分覆盖层会挡住 Playwright 合成点击）
        for sel in (
            '[role="link"]:has(div[data-email*="@gmail.com"])',
            'div[data-email*="@gmail.com"]',
        ):
            loc = p.locator(sel)
            try:
                if await loc.count() <= 0:
                    continue
                el = loc.first
                try:
                    await el.scroll_into_view_if_needed(timeout=timeout_ms)
                except Exception:
                    pass
                await el.evaluate("node => (node instanceof HTMLElement && node.click())")
                return
            except Exception:
                continue
        raise RuntimeError("未找到可点击的 @gmail.com 账号行")

    async def _maybe_click_google_account_picker_if_present(self, open_pages: list[tuple[Any, Any, str]]) -> bool:
        """若某标签页为 Google 账号选择（标题或 URL），置前并点击 @gmail.com 账号行。"""
        for _c, p, _u in open_pages:
            try:
                if bool(getattr(p, "is_closed", lambda: False)()):
                    continue
            except Exception:
                continue
            try:
                title = (await p.title() or "").strip().lower()
            except Exception:
                title = ""
            u_low = (_u or "").strip().lower()
            looks_google_account_ui = (
                "sign in - google accounts" in title
                or "choose an account" in title
                or (
                    "accounts.google.com" in u_low
                    and any(x in u_low for x in ("signin", "oauth", "selectaccount", "identifier"))
                )
            )
            if not looks_google_account_ui:
                continue
            try:
                await p.bring_to_front()
            except Exception:
                pass
            try:
                await self._click_google_gmail_account_row(p)
                await self._push_debug_progress(p, "Google 账号选择页：已点击 @gmail.com 账号行", level="ok")
                try:
                    await asyncio.sleep(1.5)
                except Exception:
                    pass
                return True
            except Exception as e:
                await self._push_debug_progress(
                    p, f"Google 账号选择页：点击 @gmail.com 失败：{_short_err_msg(e)}", level="warn"
                )
                return False
        return False

    async def _try_google_accounts_login_autofill(
        self,
        open_pages: list[tuple[Any, Any, str]],
        *,
        db: Database,
        window_pk: int,
        timeout_ms: int = 90_000,
    ) -> None:
        """在 accounts.google.com 标签页上按窗口凭据自动填密码/邮箱/TOTP（EFA 可选）。"""
        from .sora_plus_register_executor import (
            google_accounts_autofill_login_steps,
            resolve_window_platform_login_creds_optional_efa,
        )

        def _pg_closed(pg: Any) -> bool:
            try:
                return bool(getattr(pg, "is_closed", lambda: False)())
            except Exception:
                return True

        acc_page: Any = None
        for _c, p, u in open_pages:
            if _pg_closed(p):
                continue
            if "accounts.google.com" in (u or "").lower():
                acc_page = p
                break
        if acc_page is None:
            return

        try:
            creds = await resolve_window_platform_login_creds_optional_efa(db, window_pk=int(window_pk))
        except Exception as e:
            await self._push_debug_progress(
                acc_page, f"Google 自动登录：读取窗口凭据失败：{_short_err_msg(e)}", level="warn"
            )
            return

        try:
            await acc_page.bring_to_front()
        except Exception:
            pass

        async def _pcb(_n: int, data: Dict[str, Any]) -> None:
            msg = str((data or {}).get("msg") or "").strip()
            if msg:
                await self._push_debug_progress(acc_page, f"Google 自动登录：{msg}", level="info")

        await google_accounts_autofill_login_steps(
            acc_page,
            platform_username=str(creds.get("platform_username") or ""),
            platform_password=str(creds.get("platform_password") or ""),
            platform_efa=creds.get("platform_efa"),
            timeout_ms=int(timeout_ms),
            progress_cb=_pcb,
        )

    @staticmethod
    def _veo_is_page_closed(p: Any) -> bool:
        try:
            return bool(getattr(p, "is_closed", lambda: False)())
        except Exception:
            return True

    @staticmethod
    def _google_url_indicates_relogin_required(u: str) -> bool:
        ul = (u or "").strip().lower()
        if "accounts.google.com" not in ul:
            return False
        return any(
            x in ul
            for x in (
                "/signin",
                "/oauth",
                "selectaccount",
                "servicelogin",
                "/identifier",
                "challenge",
                "speedbump",
                "/v3/signin",
            )
        )

    async def _veo_snapshot_contexts_pages(self) -> tuple[list[Any], list[tuple[Any, Any, str]]]:
        """返回 (contexts, open_pages[(ctx,page,url)])。"""
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
                if self._veo_is_page_closed(p0):
                    continue
                try:
                    u0 = str(getattr(p0, "url", "") or "").strip()
                except Exception:
                    u0 = ""
                open_pages0.append((c0, p0, u0))
        return ctxs0, open_pages0

    async def _veo_detect_google_login_and_gmail_visible(self) -> tuple[bool, bool]:
        """检测当前是否处于 Google 登录/选账号相关页，以及可见文本中是否出现 @gmail.com。

        用于管理台「开号」合并按钮：有 @gmail.com（账号列表）时走连接置前+点选；否则走完整开号登录。
        """
        _, open_pages = await self._veo_snapshot_contexts_pages()
        if not open_pages:
            return False, False

        is_google = False
        chunks: list[str] = []
        for _c, p, u in open_pages:
            if self._veo_is_page_closed(p):
                continue
            ul = str(u or "").strip().lower()
            url_hit = self._google_url_indicates_relogin_required(ul) or ("google.com" in ul) 
            title_hit = False
            try:
                t = (await p.title() or "").strip().lower()
                title_hit = "sign in - google accounts" in t or "choose an account" in t
            except Exception:
                pass
            if not url_hit and not title_hit:
                continue
            is_google = True
            try:
                chunks.append(str(await p.inner_text(timeout=5000) or ""))
            except Exception:
                chunks.append("")

        if not is_google:
            return False, False

        blob = "\n".join(chunks).lower()
        has_gmail = "@gmail.com" in blob
        return True, has_gmail

    async def _perform_google_relogin_if_stuck_on_accounts(
        self,
        *,
        db: Database,
        window_pk: int,
        timeout_ms: int,
    ) -> None:
        """调用方须已持有 ``_bring_drafts_lock``：检测 Google 登录/选账号页并自动登录。"""
        _, open_pages = await self._veo_snapshot_contexts_pages()
        if not open_pages:
            return
        need = False
        for _c, p, u in open_pages:
            if self._google_url_indicates_relogin_required(str(u)):
                need = True
                break
        if not need:
            for _c, p, _u in open_pages:
                try:
                    if self._veo_is_page_closed(p):
                        continue
                    t = (await p.title() or "").strip().lower()
                    if "sign in - google accounts" in t or "choose an account" in t:
                        need = True
                        break
                except Exception:
                    continue
        if not need:
            return
        if await self._maybe_click_google_account_picker_if_present(open_pages):
            _, open_pages = await self._veo_snapshot_contexts_pages()
        await self._try_google_accounts_login_autofill(
            open_pages,
            db=db,
            window_pk=int(window_pk),
            timeout_ms=int(timeout_ms),
        )

    async def _nonpenalized_raise_if_google_account_logged_out(self, drafts_page: Any) -> None:
        """未走管理台「连接置前」自动登录时，若仍停留在 Google 登录相关页则视为被登出。"""
        _, open_pages = await self._veo_snapshot_contexts_pages()
        for _ctx, p, u in open_pages:
            ul = str(u or "").strip().lower()
            if self._google_url_indicates_relogin_required(ul):
                raise NonPenalizedTaskError("账号被登出", status_code=401)
            if not self._veo_is_page_closed(p):
                try:
                    t = (await p.title() or "").strip().lower()
                    if "sign in - google accounts" in t or "choose an account" in t:
                        raise NonPenalizedTaskError("账号被登出", status_code=401)
                except NonPenalizedTaskError:
                    raise
                except Exception:
                    pass
        if drafts_page is None or self._veo_is_page_closed(drafts_page):
            return
        try:
            du = str(getattr(drafts_page, "url", "") or "").strip().lower()
            if self._google_url_indicates_relogin_required(du):
                raise NonPenalizedTaskError("账号被登出", status_code=401)
            t = (await drafts_page.title() or "").strip().lower()
            if "sign in - google accounts" in t or "choose an account" in t:
                raise NonPenalizedTaskError("账号被登出", status_code=401)
        except NonPenalizedTaskError:
            raise
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 核心：将目标页面置前（完整照搬 sora 的 _bring_sora_drafts_to_front）
    # ------------------------------------------------------------------
    async def _bring_target_page_to_front(
        self,
        refresh_target=True,
        *,
        drafts_url: str,
        acquire_bring_lock: bool = True,
        google_login_db: Optional[Database] = None,
        google_login_window_pk: Optional[int] = None,
        google_login_timeout_ms: Optional[int] = None,
    ) -> None:
        """将目标页面置前，并尽量确保整个指纹浏览器实例只保留一个目标页面。

        需求背景：指纹浏览器可能开了多个标签页/窗口；即使 ensure_open 选中了可用 page，也不一定是目标页面。
        这里确保 drafts_url 在每次 ensure_open 后都会被 bring_to_front，
        且会关闭同一个指纹浏览器（同一 CDP 连接）内除 drafts_url 外的其它页面（包括其它站点、about:blank、新窗口、重复 drafts 等），
        尽量只保留一个目标页面以节省内存。

        acquire_bring_lock：为 False 时表示调用方已持有 ``_bring_drafts_lock``（避免与外层 ``async with`` 死锁）。

        google_login_db / google_login_window_pk：仅管理台「连接置前」接口传入；若同时有值，会在合并/关闭标签前执行选 @gmail.com 账号 + 自动填密码/邮箱/TOTP。

        未传入时：若 bring 结束后仍停留在 Google 登录/选账号页，抛出 ``NonPenalizedTaskError("账号被登出")``。
        """
        try:
            target_host = urlparse(drafts_url).netloc.strip().lower()
        except Exception:
            target_host = ""

        gl_db = google_login_db
        gl_wpk = google_login_window_pk
        try:
            gl_to = int(google_login_timeout_ms) if google_login_timeout_ms is not None else 90_000
        except Exception:
            gl_to = 90_000

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

            async def _keep_only_one_drafts_page(keep_page: Any) -> Any:
                """关闭其它所有页面/多余 contexts，仅保留 keep_page。返回 keep_page。"""
                ctxs1, open_pages1 = await self._veo_snapshot_contexts_pages()
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

            ctxs, open_pages = await self._veo_snapshot_contexts_pages()
            if not ctxs:
                return

            if gl_db is not None and gl_wpk is not None:
                if await self._maybe_click_google_account_picker_if_present(open_pages):
                    ctxs, open_pages = await self._veo_snapshot_contexts_pages()
                    if not ctxs:
                        return
                try:
                    await self._try_google_accounts_login_autofill(
                        open_pages,
                        db=gl_db,
                        window_pk=int(gl_wpk),
                        timeout_ms=gl_to,
                    )
                except Exception as e:
                    hint = open_pages[0][1] if open_pages else None
                    if hint is not None:
                        await self._push_debug_progress(
                            hint, f"Google 自动登录异常：{_short_err_msg(e)}", level="warn"
                        )
                ctxs, open_pages = await self._veo_snapshot_contexts_pages()
                if not ctxs:
                    return

            drafts_page = None
            cur_page = getattr(self.pw_ctx, "page", None)
            if cur_page is not None and not _is_page_closed(cur_page):
                cur_u0 = _safe_page_url(cur_page)
                if cur_u0.startswith(drafts_url):
                    drafts_page = cur_page

            if drafts_page is None:
                for _c, p, u in open_pages:
                    if u.startswith(drafts_url):
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

            await self._push_debug_progress(drafts_page, "已选定目标页面，准备清理其它页面", level="info")

            drafts_page = await _keep_only_one_drafts_page(drafts_page)

            try:
                self.pw_ctx.page = drafts_page
            except Exception:
                pass
            try:
                await drafts_page.bring_to_front()
            except Exception:
                pass
            try:
                cur_u = str(getattr(drafts_page, "url", "") or "").strip()
            except Exception:
                cur_u = ""

            if refresh_target:
                try:
                    await drafts_page.reload(wait_until="domcontentloaded", timeout=30_000)
                    await self._push_debug_progress(drafts_page, "目标页面刷新完成", level="ok")
                except Exception:
                    await self._push_debug_progress(drafts_page, "目标页面刷新失败（将继续流程）", level="warn")
                    pass

                try:
                    await drafts_page.evaluate("() => { try { window.focus(); } catch(e) {} }")
                except Exception:
                    pass

                await asyncio.sleep(5.0)

            try:
                await drafts_page.mouse.click(5, 400)
                await asyncio.sleep(0.3)
            except Exception:
                pass

            # 若出现未登录提示，尽量先触发登录
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

            # Cloudflare interstitial 自愈
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
                                if u_n.startswith(drafts_url):
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
                                    drafts_page, "重启后已恢复目标页面并置前", level="ok"
                                )
                        except Exception:
                            pass
            except Exception:
                pass

            if gl_db is None or gl_wpk is None:
                await self._nonpenalized_raise_if_google_account_logged_out(drafts_page)

        if acquire_bring_lock:
            async with self._bring_drafts_lock:
                await _inner()
        else:
            await _inner()

    # ------------------------------------------------------------------
    # idle close / close_and_drop
    # ------------------------------------------------------------------
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

        async def _job():
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
        _drop_veo_session(self.cache_key)

    async def close(self) -> None:
        self._cancel_idle_close()
        await self.pw_ctx.close_and_drop()


def get_or_create_veo_session(
    *,
    vendor: str,
    base_url: str,
    access_key: Optional[str],
    space_id: str,
    window_key: str,
) -> VeoSession:
    """获取/创建 VEO 会话（按 window 维度缓存，避免重复开浏览器）。"""
    k = _veo_key(vendor, base_url, space_id, window_key)
    sess = _VEO_SESSIONS.get(k)
    if sess is None:
        pw_ctx = get_or_create_playwright_ctx(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            space_id=space_id,
            window_key=window_key,
        )
        sess = VeoSession(cache_key=k, pw_ctx=pw_ctx)
        _VEO_SESSIONS[k] = sess
    else:
        sess.pw_ctx.access_key = access_key
    return sess


VEO_FLOW_OPEN_ACCOUNT_DEFAULT_URL = "https://labs.google/fx/ja/tools/flow"


async def veo_flow_open_account(
    progress_cb: ProgressCB,
    *,
    db: Database,
    window_pk: int,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    timeout_seconds: float,
    flow_url: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
) -> Dict[str, Any]:
    """按窗口凭据完成 Google 登录，打开 Google Flow，再断开本地 CDP（保留指纹浏览器窗口）。"""
    from .sora_plus_register_executor import (
        _do_login_flow,
        _is_already_logged_in,
        _pick_platform_domain_page,
        _resolve_window_platform_credentials,
    )

    creds = await _resolve_window_platform_credentials(db, window_pk=int(window_pk))
    platform_url = str(creds["platform_url"] or "").strip()
    platform_username = str(creds["platform_username"] or "").strip()
    platform_password = str(creds["platform_password"] or "").strip()
    platform_efa = str(creds["platform_efa"] or "").strip()

    target_flow = str(flow_url or "").strip() or VEO_FLOW_OPEN_ACCOUNT_DEFAULT_URL
    timeout_ms = int(max(10_000, min(float(timeout_seconds) * 1000, 120_000)))

    await progress_cb(1, {"stage": "resolve_credentials", "window_pk": int(window_pk)})

    sess = get_or_create_veo_session(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = headless
    sess.browser_pure_mode = pure_mode
    sess.idle_close_disabled = True
    try:
        sess._cancel_idle_close()
    except Exception:
        pass

    await sess.ensure_open(args=sess.browser_open_args, force_open=False, headless=headless)

    ctx = sess.pw_ctx
    async with ctx.driver_lock:
        if ctx.context is None:
            raise RuntimeError("浏览器上下文不可用：context is None")
        page = await _pick_platform_domain_page(ctx.context, platform_url=platform_url)
        ctx.page = page

        await page.goto(platform_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await progress_cb(10, {"stage": "page_loaded", "url": platform_url})

        current_url = str(page.url or "").strip()
        if _is_already_logged_in(current_url):
            await progress_cb(55, {"stage": "already_logged_in", "current_url": current_url})
        else:
            await progress_cb(12, {"stage": "need_login", "current_url": current_url})
            await _do_login_flow(
                page,
                platform_username=platform_username,
                platform_password=platform_password,
                platform_efa=platform_efa,
                timeout_ms=timeout_ms,
                progress_cb=progress_cb,
            )
        print(f"target_flow: {target_flow}")
        await page.goto(target_flow, wait_until="domcontentloaded", timeout=timeout_ms)
        await progress_cb(90, {"stage": "flow_opened", "url": target_flow})

    try:
        br = getattr(ctx, "browser", None)
        pw = getattr(ctx, "playwright", None)
        try:
            ctx.browser = None
            ctx.context = None
            ctx.page = None
            ctx.cdp_endpoint = None
        except Exception:
            pass
        try:
            if br is not None:
                await br.close()
        except Exception:
            pass
        try:
            if pw is not None:
                await pw.stop()
        except Exception:
            pass
        try:
            ctx.playwright = None
        except Exception:
            pass
        await progress_cb(99, {"stage": "cdp_disconnected"})
    except Exception:
        pass

    return {
        "ok": True,
        "stage": "flow_ready",
        "platform_url": platform_url,
        "platform_username": platform_username,
        "flow_url": target_flow,
        "message": "已登录 Google 并打开 Flow，已断开 CDP",
    }


async def veo_admin_unified_open_or_connect(
    progress_cb: ProgressCB,
    *,
    db: Database,
    window_pk: int,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    timeout_seconds: float,
    headless: bool,
    default_target_url: str,
    google_login_timeout_ms: int,
    pure_mode: bool = True,
) -> Dict[str, Any]:
    """管理台合并逻辑：Google 登录页且页面可见 @gmail.com → 连接置前（与原 veo-connect-bring 一致）；否则若仍为 Google 登录相关 → 完整开号；其它情况 → 连接置前。"""
    target_url = "https://accounts.google.com/"
    gl_ms = int(google_login_timeout_ms)
    gl_ms = max(45_000, min(gl_ms, 240_000))
    sess = get_or_create_veo_session(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = headless
    sess.browser_pure_mode = pure_mode
    sess.idle_close_disabled = True
    try:
        sess._cancel_idle_close()
    except Exception:
        pass
    is_google = False
    has_gmail = False
    async with sess._bring_drafts_lock:
        await sess.ensure_open(
            args=sess.browser_open_args,
            force_open=sess.browser_force_open,
            headless=headless,
            acquire_bring_lock=False,
        )
        await sess._bring_target_page_to_front(
            refresh_target=True,
            drafts_url=target_url,
            acquire_bring_lock=False,
            google_login_db=db,
            google_login_window_pk=int(window_pk),
            google_login_timeout_ms=gl_ms,
        )
        print("--1");
        await progress_cb(5, {"stage": "detect_google_page"})
        is_google, has_gmail = await sess._veo_detect_google_login_and_gmail_visible()
        print(f"is_google: {is_google}, has_gmail: {has_gmail}")
        await progress_cb(
            8,
            {"stage": "detect_done", "is_google_login": is_google, "has_gmail_visible": has_gmail},
        )

        if is_google and not has_gmail:
            need_full_open_account = True
        elif is_google and has_gmail:
            need_full_open_account = False
            await sess._bring_target_page_to_front(
                refresh_target=False,
                drafts_url=target_url,
                acquire_bring_lock=False,
                google_login_db=db,
                google_login_window_pk=int(window_pk),
                google_login_timeout_ms=gl_ms,
            )
        else:
            need_full_open_account = False
            

    if need_full_open_account:
        target_url = str(default_target_url or "").strip() or "https://veo.google.com"
        out = await veo_flow_open_account(
            progress_cb,
            db=db,
            window_pk=window_pk,
            browser_vendor=browser_vendor,
            browser_base_url=browser_base_url,
            browser_access_key=browser_access_key,
            space_id=space_id,
            window_key=window_key,
            timeout_seconds=timeout_seconds,
            headless=headless,
            pure_mode=pure_mode,
        )
        if isinstance(out, dict):
            out = {**out, "branch": "full_open_account"}
        return out if isinstance(out, dict) else {"ok": True, "branch": "full_open_account", "raw": out}

    await sess.disconnect_playwright_under_bring_lock()
    branch = "gmail_picker_connect" if (is_google and has_gmail) else "connect_bring_default"
    return {
        "ok": True,
        "branch": branch,
        "is_google_login": is_google,
        "has_gmail_visible": has_gmail,
        "message": (
            "已连接并置前（检测到账号列表含 @gmail.com，已按自动点选/填表处理），已断开自动化连接"
            if branch == "gmail_picker_connect"
            else "已连接并置前目标页，已断开自动化连接"
        ),
    }


# ---------------------------------------------------------------------------
# Google Labs / Flow：余额与档位（与 flow2api flow_client.get_credits 对齐）
# ---------------------------------------------------------------------------
VEO_GOOG_API_KEY = "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY"
FLOW_LABS_CREDITS_URL = f"https://aisandbox-pa.googleapis.com/v1/credits?key={VEO_GOOG_API_KEY}"
# 刷新 VEO 额度时：新开标签页读取「Next update: Apr 22」或「Next update: Tomorrow」作为额度重置日
VEO_ONE_GOOGLE_AI_ACTIVITY_URL = "https://one.google.com/ai/activity?g1_landing_page=0"

FLOW_VIDEO_SUBMIT_T2V_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText"
FLOW_FLOW_UPLOAD_IMAGE_URL = "https://aisandbox-pa.googleapis.com/v1/flow/uploadImage"
FLOW_VIDEO_SUBMIT_I2V_START_IMAGE_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartImage"
FLOW_VIDEO_SUBMIT_I2V_START_END_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartAndEndImage"
FLOW_VIDEO_SUBMIT_R2V_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoReferenceImages"
FLOW_VIDEO_POLL_URL = "https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus"
FLOW_FLOW_UPSAMPLE_IMAGE_URL = "https://aisandbox-pa.googleapis.com/v1/flow/upsampleImage"
FLOW_FLOW_WORKFLOWS_BASE_URL = "https://aisandbox-pa.googleapis.com/v1/flowWorkflows"
# Flow / Labs 当前常用 enterprise site key；动态解析失败时兜底（与 flow2api 一致）
VEO_RECAPTCHA_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

# 与 flow2api generation_handler gemini-3.1-flash-image-*（NARWHAL）一致
IMAGE_ASPECT_RATIO_LANDSCAPE = "IMAGE_ASPECT_RATIO_LANDSCAPE"
IMAGE_ASPECT_RATIO_PORTRAIT = "IMAGE_ASPECT_RATIO_PORTRAIT"
IMAGE_ASPECT_RATIO_SQUARE = "IMAGE_ASPECT_RATIO_SQUARE"
IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE = "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR = "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
VEO_IMAGE_MODEL_NARWHAL = "NARWHAL"
# 与 flow2api generation_handler gemini-3.0-pro-image-*（GEM_PIX_2）一致
VEO_IMAGE_MODEL_GEM_PIX_2 = "GEM_PIX_2"
VEO_IMAGE_GENERATION_MAX_REFERENCE_IMAGES = 10
VEO_IMAGE_REFERENCE_MAX_PIXELS_4K = 3840 * 2160
UPSAMPLE_IMAGE_RESOLUTION_2K = "UPSAMPLE_IMAGE_RESOLUTION_2K"

PAYGATE_TIER_NOT_PAID = "PAYGATE_TIER_NOT_PAID"
PAYGATE_TIER_ONE = "PAYGATE_TIER_ONE"
PAYGATE_TIER_TWO = "PAYGATE_TIER_TWO"

def _veo_extract_project_id_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/tools/flow/project/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None

def _veo_project_page_url(*, project_id: str, hint_url: str) -> str:
    """构建 Flow 项目页 URL（保留 hint 中的语言前缀，如 /fx/zh/tools/flow/...）。"""
    pid = (project_id or "").strip()
    if not pid:
        return (hint_url or "").strip() or "https://labs.google/fx"
    hint = (hint_url or "").strip() or "https://labs.google/fx"
    try:
        p = urlparse(hint)
        origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else "https://labs.google"
    except Exception:
        origin = "https://labs.google"
    m = re.search(r"/fx/([a-z]{2})/tools/flow", hint, re.I)
    if m:
        return f"{origin}/fx/{m.group(1)}/tools/flow/project/{pid}"
    return f"{origin}/fx/tools/flow/project/{pid}"


def _veo_payload_looks_like_i2v(payload: Dict[str, Any]) -> bool:
    """仅用于图生视频判定。"""
    if _veo_payload_explicit_i2v(payload):
        return True
    if str(payload.get("first_image_url") or payload.get("firstImageUrl") or "").strip():
        return True
    if str(payload.get("image_url") or payload.get("imageUrl") or "").strip():
        return True
    imgs = payload.get("images")
    if isinstance(imgs, list) and len(imgs) > 0:
        return True
    last_u = str(payload.get("last_image_url") or payload.get("lastImageUrl") or "").strip()
    if last_u:
        return True
    return False


def _veo_payload_explicit_i2v(payload: Dict[str, Any]) -> bool:
    mode = str(
        payload.get("video_type")
        or payload.get("veo_video_type")
        or payload.get("video_mode")
        or payload.get("reference_mode")
        or ""
    ).strip().lower()
    return mode in ("i2v", "image_to_video", "img2vid", "img2video", "first_frame", "first-frame")


def _veo_payload_has_image_generation_references(payload: Dict[str, Any]) -> bool:
    """仅用于图生图判定：`images` 为多参考图输入，单图字段仅在未传 `images` 时兼容。"""
    payload = payload or {}
    imgs = payload.get("images")
    if isinstance(imgs, list) and len(imgs) > 0:
        return True
    if str(payload.get("first_image_url") or payload.get("firstImageUrl") or "").strip():
        return True
    if str(payload.get("image_url") or payload.get("imageUrl") or "").strip():
        return True
    return False


def _veo_pick_orientation_from_ratio(ratio: Optional[str]) -> Optional[str]:
    """与 `sora_task_executor._pick_orientation_from_ratio` 一致：16:9→landscape，9:16→portrait。"""
    if not ratio:
        return None
    s = str(ratio).strip().lower().replace("：", ":")
    if "16:9" in s:
        return "landscape"
    if "9:16" in s:
        return "portrait"
    return None


def _veo_parse_pixel_pair_from_payload(payload: Dict[str, Any]) -> Optional[tuple[int, int]]:
    """从 payload 的宽高字段解析像素尺寸（宽×高）。"""
    pairs = (
        ("width", "height"),
        ("video_width", "video_height"),
        ("w", "h"),
    )
    for wk, hk in pairs:
        try:
            if payload.get(wk) is None or payload.get(hk) is None:
                continue
            w = int(float(payload.get(wk)))
            h = int(float(payload.get(hk)))
            if w > 0 and h > 0:
                return w, h
        except Exception:
            continue
    return None


def _veo_orientation_from_pixel_dimensions(w: int, h: int) -> Optional[str]:
    if w > h:
        return "landscape"
    if h > w:
        return "portrait"
    return None


def _veo_try_parse_wh_in_ratio_string(ratio: str) -> Optional[tuple[int, int]]:
    """比例串中的 `1920x1080` / `1080*1920` / `1080×1920` → (宽, 高)。"""
    s = str(ratio or "").strip()
    if not s:
        return None
    m = re.search(r"(\d+)\s*[xX*×]\s*(\d+)", s)
    if not m:
        return None
    try:
        a = int(m.group(1))
        b = int(m.group(2))
        if a > 0 and b > 0:
            return a, b
    except Exception:
        pass
    return None


def _veo_resolve_orientation_str(payload: Dict[str, Any]) -> Optional[str]:
    """横竖屏语义 `portrait` / `landscape`，与 Sora 一致优先「比例/宽高」，再 `orientation` / 显式 API 字段。

    顺序：
    1. `width`×`height`（及 video_width / w、h 等）
    2. `size_ratio` / `aspect_ratio` / `ratio` / `尺寸` 中的 `WxH` 子串
    3. 同上字段中的 `16:9` / `9:16`（与 Sora `_pick_orientation_from_ratio` 一致）
    4. `orientation`
    5. `video_aspect_ratio` / `aspectRatio` / `veo_aspect_ratio`（含横竖、PORTRAIT/LANDSCAPE）
    """
    payload = payload or {}

    wh = _veo_parse_pixel_pair_from_payload(payload)
    if wh:
        o = _veo_orientation_from_pixel_dimensions(wh[0], wh[1])
        if o:
            return o

    ratio = str(
        payload.get("size_ratio") or payload.get("aspect_ratio") or payload.get("ratio") or payload.get("尺寸") or ""
    ).strip() or None
    if ratio:
        wh2 = _veo_try_parse_wh_in_ratio_string(ratio)
        if wh2:
            o2 = _veo_orientation_from_pixel_dimensions(wh2[0], wh2[1])
            if o2:
                return o2
        lo = _veo_pick_orientation_from_ratio(ratio)
        if lo:
            return lo

    ori = str(payload.get("orientation") or "").strip().lower()
    if ori in ("portrait", "landscape"):
        return ori

    raw_ar = str(
        payload.get("video_aspect_ratio") or payload.get("aspectRatio") or payload.get("veo_aspect_ratio") or ""
    ).strip()
    if raw_ar:
        u = raw_ar.upper()
        if "PORTRAIT" in u or "竖" in raw_ar:
            return "portrait"
        if "LANDSCAPE" in u or "横" in raw_ar:
            return "landscape"

    return None


VIDEO_ASPECT_RATIO_LANDSCAPE = "VIDEO_ASPECT_RATIO_LANDSCAPE"
VIDEO_ASPECT_RATIO_PORTRAIT = "VIDEO_ASPECT_RATIO_PORTRAIT"

# 与 flow2api generation_handler MODEL_CONFIG 中 veo_3_1_i2v_s_fast_*_fl 对齐
VEO_I2V_MODEL_LANDSCAPE_FL = "veo_3_1_i2v_s_fast_fl"
VEO_I2V_MODEL_PORTRAIT_FL = "veo_3_1_i2v_s_fast_portrait_fl"
# 与 flow2api generation_handler 中 veo_3_1_r2v_fast / veo_3_1_r2v_fast_portrait（Ingredients 多图）对齐
VEO_R2V_MODEL_LANDSCAPE = "veo_3_1_r2v_fast_landscape"
VEO_R2V_MODEL_PORTRAIT = "veo_3_1_r2v_fast_portrait"
VEO_OMNI_MODEL_ALIASES = {"gemini-omni", "gemini_omni", "gemini-omni-flash", "veo-omni", "veoomni", "omni", "omni-flash", "abra"}
VEO_OMNI_ALLOWED_DURATIONS = {4, 6, 8, 10}
VEO_3_1_FAST_MODEL_KEY = "veo_3_1_fast"
VEO_3_1_LITE_MODEL_KEY = "veo_3_1_lite"
VEO_3_1_QUALITY_MODEL_KEY = "veo_3_1_quality"
VEO_3_1_LITE_LOW_PRIORITY_MODEL_KEY = "veo_3_1_lite_low_priority"
VEO_3_1_MODEL_ALIASES = {
    "veo-3-1": VEO_3_1_FAST_MODEL_KEY,
    "veo-3-1-fast": VEO_3_1_FAST_MODEL_KEY,
    "veo-3-1-lite": VEO_3_1_LITE_MODEL_KEY,
    "veo-3-1-quality": VEO_3_1_QUALITY_MODEL_KEY,
    "veo-3-1-lite-low-priority": VEO_3_1_LITE_LOW_PRIORITY_MODEL_KEY,
    "veo-3-1-low-priority": VEO_3_1_LITE_LOW_PRIORITY_MODEL_KEY,
    "veo-31": VEO_3_1_FAST_MODEL_KEY,
    "veo-31-fast": VEO_3_1_FAST_MODEL_KEY,
    "veo-31-lite": VEO_3_1_LITE_MODEL_KEY,
    "veo-31-quality": VEO_3_1_QUALITY_MODEL_KEY,
    "veo-31-lite-low-priority": VEO_3_1_LITE_LOW_PRIORITY_MODEL_KEY,
    "veo3-1": VEO_3_1_FAST_MODEL_KEY,
    "veo3-1-fast": VEO_3_1_FAST_MODEL_KEY,
    "veo3-1-lite": VEO_3_1_LITE_MODEL_KEY,
    "veo3-1-quality": VEO_3_1_QUALITY_MODEL_KEY,
    "veo3-1-lite-low-priority": VEO_3_1_LITE_LOW_PRIORITY_MODEL_KEY,
    "veo-3-1-fast-portrait": VEO_3_1_FAST_MODEL_KEY,
}
VEO_3_1_DIRECT_MODEL_KEYS = {
    VEO_3_1_FAST_MODEL_KEY,
    VEO_3_1_LITE_MODEL_KEY,
    VEO_3_1_QUALITY_MODEL_KEY,
    VEO_3_1_LITE_LOW_PRIORITY_MODEL_KEY,
}


def _veo_resolve_i2v_aspect_ratio(payload: Dict[str, Any]) -> str:
    """I2V 的 aisandbox aspectRatio：与 T2V 相同规则，默认横屏。"""
    o = _veo_resolve_orientation_str(payload)
    if o == "portrait":
        return VIDEO_ASPECT_RATIO_PORTRAIT
    if o == "landscape":
        return VIDEO_ASPECT_RATIO_LANDSCAPE
    return VIDEO_ASPECT_RATIO_LANDSCAPE


def _veo_extract_url_from_image_item(item: Any) -> Optional[str]:
    if isinstance(item, str):
        u = item.strip()
        return u or None
    if isinstance(item, dict):
        for k in ("url", "image_url", "imageUrl", "src", "first_image_url", "firstImageUrl"):
            v = str(item.get(k) or "").strip()
            if v:
                return v
    return None


def _veo_payload_model_text(payload: Dict[str, Any]) -> str:
    return (
        str(
            (payload or {}).get("videoModelKey")
            or (payload or {}).get("model_key")
            or (payload or {}).get("veo_model")
            or (payload or {}).get("video_model")
            or (payload or {}).get("model")
            or ""
        )
        .strip()
        .lower()
    )


def _veo_payload_uses_omni(payload: Dict[str, Any]) -> bool:
    raw = _veo_payload_model_text(payload)
    if not raw:
        return False
    return raw in VEO_OMNI_MODEL_ALIASES or raw.startswith("abra_") or "omni" in raw


def _veo_resolve_3_1_model_key(payload: Dict[str, Any]) -> Optional[str]:
    raw = _veo_payload_model_text(payload)
    if not raw or _veo_payload_uses_omni(payload):
        return None
    if raw in VEO_3_1_DIRECT_MODEL_KEYS:
        return raw
    alias = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if alias in VEO_3_1_MODEL_ALIASES:
        return VEO_3_1_MODEL_ALIASES[alias]
    if raw in {"veo_3_1_t2v_fast", "veo_3_1_t2v_fast_portrait"}:
        return VEO_3_1_FAST_MODEL_KEY
    return None


def _veo_video_duration_seconds(payload: Dict[str, Any], *, default: int = 8) -> int:
    raw = (
        (payload or {}).get("duration")
        or (payload or {}).get("seconds")
        or (payload or {}).get("video_length")
        or (payload or {}).get("videoLengthSeconds")
        or default
    )
    try:
        return int(float(raw))
    except Exception:
        return int(default)


def _veo_omni_model_key(payload: Dict[str, Any], *, mode: str) -> str:
    raw = _veo_payload_model_text(payload)
    if raw.startswith("abra_"):
        want = "abra_r2v_" if str(mode or "").lower() == "r2v" else "abra_t2v_"
        if raw.startswith(want):
            return raw
    duration = _veo_video_duration_seconds(payload)
    if duration not in VEO_OMNI_ALLOWED_DURATIONS:
        raise NonPenalizedTaskError("Gemini Omni Flash 仅支持 4/6/8/10 秒", status_code=400)
    prefix = "abra_r2v" if str(mode or "").lower() == "r2v" else "abra_t2v"
    return f"{prefix}_{duration}s"


def _veo_collect_ingredients_image_urls(payload: Dict[str, Any]) -> List[str]:
    """Ingredients（R2V）参考图 URL：来自 `Ingredients_images`（或 `ingredients_images`），与 flow2api r2v 一致最多 3 张。"""
    payload = payload or {}
    raw = payload.get("Ingredients_images")
    if raw is None:
        raw = payload.get("ingredients_images")
    if not isinstance(raw, list):
        return []
    if len(raw) > 3:
        raise NonPenalizedTaskError("Ingredients 模式最多支持 3 张参考图", status_code=400)
    out: List[str] = []
    for it in raw:
        u = _veo_extract_url_from_image_item(it)
        if not u:
            raise NonPenalizedTaskError("Ingredients_images 中存在无法解析的图片地址", status_code=400)
        out.append(u)
    return out


def _veo_collect_images_reference_urls(payload: Dict[str, Any], *, max_count: int, label: str) -> List[str]:
    payload = payload or {}
    imgs = payload.get("images")
    if isinstance(imgs, list) and len(imgs) > 0:
        if len(imgs) > max_count:
            raise NonPenalizedTaskError(f"{label} 最多支持 {max_count} 张参考图", status_code=400)
        refs: List[str] = []
        for it in imgs:
            u = _veo_extract_url_from_image_item(it)
            if not u:
                raise NonPenalizedTaskError("images 数组中存在无法解析的图片地址", status_code=400)
            refs.append(u)
        return refs

    return []


def _veo_collect_omni_reference_image_urls(payload: Dict[str, Any]) -> List[str]:
    """Gemini Omni Flash reference inputs: accept Ingredients_images, images, or first_image_url."""
    out = _veo_collect_ingredients_image_urls(payload)
    if out:
        return out
    out = _veo_collect_images_reference_urls(payload, max_count=3, label="Gemini Omni Flash 参考图")
    if out:
        return out
    payload = payload or {}
    first = str(payload.get("first_image_url") or payload.get("firstImageUrl") or "").strip()
    if not first:
        first = str(payload.get("image_url") or payload.get("imageUrl") or "").strip()
    return [first] if first else []


def _veo_resolve_r2v_model(payload: Dict[str, Any]) -> tuple[str, str]:
    """R2V videoModelKey + aspectRatio，与 generation_handler 中 veo_3_1_r2v_fast* 一致。"""
    o = _veo_resolve_orientation_str(payload)
    if _veo_payload_uses_omni(payload):
        return _veo_omni_model_key(payload, mode="r2v"), (
            VIDEO_ASPECT_RATIO_PORTRAIT if o == "portrait" else VIDEO_ASPECT_RATIO_LANDSCAPE
        )
    if o is None:
        raw = _veo_payload_model_text(payload)
        if "r2v_fast_portrait" in raw or raw == "veo_3_1_r2v_fast_portrait":
            o = "portrait"
        elif "r2v" in raw and "portrait" in raw:
            o = "portrait"

    if o == "portrait":
        return VEO_R2V_MODEL_PORTRAIT, VIDEO_ASPECT_RATIO_PORTRAIT
    return VEO_R2V_MODEL_LANDSCAPE, VIDEO_ASPECT_RATIO_LANDSCAPE


def _veo_resolve_i2v_model_key(payload: Dict[str, Any], aspect_ratio: str) -> str:
    raw = _veo_payload_model_text(payload)
    if _veo_payload_uses_omni(payload):
        # Flow's public page exposes Omni-specific keys for T2V/R2V, but the
        # start-image endpoint continues to accept the standard Veo 3.1 I2V
        # keys. This keeps `images` / `first_image_url` on the start-image
        # path instead of the slower Ingredients R2V path.
        if aspect_ratio == VIDEO_ASPECT_RATIO_PORTRAIT:
            return VEO_I2V_MODEL_PORTRAIT_FL
        return VEO_I2V_MODEL_LANDSCAPE_FL
    if raw.startswith("veo_3_1_i2v_"):
        return raw
    if "i2v" in raw and "portrait" in raw:
        return VEO_I2V_MODEL_PORTRAIT_FL
    if "i2v" in raw:
        return VEO_I2V_MODEL_LANDSCAPE_FL
    if aspect_ratio == VIDEO_ASPECT_RATIO_PORTRAIT:
        return VEO_I2V_MODEL_PORTRAIT_FL
    return VEO_I2V_MODEL_LANDSCAPE_FL


def _veo_collect_i2v_image_urls(payload: Dict[str, Any]) -> List[str]:
    """解析 1～2 张图 URL：优先 `images` 数组（顺序=首帧、尾帧），否则 first_image_url / image_url + last/end。"""
    payload = payload or {}
    imgs = payload.get("images")
    if isinstance(imgs, list) and len(imgs) > 0:
        if len(imgs) > 2:
            raise NonPenalizedTaskError("图生视频最多支持 2 张图片（首帧与尾帧）", status_code=400)
        out: List[str] = []
        for it in imgs:
            u = _veo_extract_url_from_image_item(it)
            if not u:
                raise NonPenalizedTaskError("images 数组中存在无法解析的图片地址", status_code=400)
            out.append(u)
        return out

    first = str(payload.get("first_image_url") or payload.get("firstImageUrl") or "").strip()
    if not first:
        first = str(payload.get("image_url") or payload.get("imageUrl") or "").strip()
    last = str(
        payload.get("last_image_url")
        or payload.get("lastImageUrl")
        or payload.get("end_image_url")
        or payload.get("endImageUrl")
        or ""
    ).strip()
    if last and not first:
        raise NonPenalizedTaskError(
            "图生视频不能只提供尾图：请提供首图 first_image_url（或 images[0]），"
            "顺序为 [首帧, 尾帧]",
            status_code=400,
        )
    urls: List[str] = []
    if first:
        urls.append(first)
    if last:
        urls.append(last)
    return urls


def _veo_collect_image_generation_reference_urls(payload: Dict[str, Any]) -> List[str]:
    """解析图片生成参考图 URL：优先 `images` 数组，兼容单图字段 `first_image_url` / `image_url`。"""
    payload = payload or {}
    imgs = payload.get("images")
    if isinstance(imgs, list) and len(imgs) > 0:
        if len(imgs) > VEO_IMAGE_GENERATION_MAX_REFERENCE_IMAGES:
            raise NonPenalizedTaskError(
                f"多图生图最多支持 {VEO_IMAGE_GENERATION_MAX_REFERENCE_IMAGES} 张参考图",
                status_code=400,
            )
        out: List[str] = []
        for it in imgs:
            u = _veo_extract_url_from_image_item(it)
            if not u:
                raise NonPenalizedTaskError("images 数组中存在无法解析的图片地址", status_code=400)
            out.append(u)
        return out

    first = str(payload.get("first_image_url") or payload.get("firstImageUrl") or "").strip()
    if not first:
        first = str(payload.get("image_url") or payload.get("imageUrl") or "").strip()
    if first:
        return [first]
    return []

def _veo_resolve_t2v_model(payload: Dict[str, Any]) -> tuple[str, str]:
    """返回 (videoModelKey, aspectRatio)。横竖屏与 Sora 一致：优先宽高/比例字段，再 model 显式指定。

    与 `sora_gen_video` 对齐的输入：`size_ratio` / `aspect_ratio` / `ratio` / `尺寸`、`width`×`height`、
    比例串内 `WxH`、`16:9`/`9:16`、`orientation`；另支持 `video_aspect_ratio` 等 API 字段。
    若以上均未判定，则回退解析 `model` / `videoModelKey` 是否含 `t2v_fast_portrait`。
    默认横屏 `veo_3_1_t2v_fast`。
    """
    o = _veo_resolve_orientation_str(payload)
    if _veo_payload_uses_omni(payload):
        return _veo_omni_model_key(payload, mode="t2v"), (
            VIDEO_ASPECT_RATIO_PORTRAIT if o == "portrait" else VIDEO_ASPECT_RATIO_LANDSCAPE
        )
    veo31_key = _veo_resolve_3_1_model_key(payload)
    if veo31_key:
        return veo31_key, (
            VIDEO_ASPECT_RATIO_PORTRAIT if o == "portrait" else VIDEO_ASPECT_RATIO_LANDSCAPE
        )
    if o is None:
        raw = _veo_payload_model_text(payload)
        if "t2v_fast_portrait" in raw or raw == "veo_3_1_t2v_fast_portrait":
            o = "portrait"

    if o == "portrait":
        return "veo_3_1_t2v_fast_portrait", VIDEO_ASPECT_RATIO_PORTRAIT
    return "veo_3_1_t2v_fast", VIDEO_ASPECT_RATIO_LANDSCAPE

def veo_format_paygate_tier_label(tier: Optional[str]) -> str:
    """将 userPaygateTier 转为可读套餐名（与 flow2api manage.html formatAccountType 一致）。"""
    t = str(tier or "").strip()
    if not t or t == "PAYGATE_TIER_NOT_PAID":
        return "Google Labs · 普通"
    if t == "PAYGATE_TIER_ONE":
        return "Google Labs · Pro"
    if t == "PAYGATE_TIER_TWO":
        return "Google Labs · Ult"
    return f"Google Labs · {t}"


def _veo_normalize_credits_payload(data: Any) -> Dict[str, Any]:
    """解析 aisandbox /v1/credits 的 JSON（与 flow2api get_credits 字段一致）。"""
    if not isinstance(data, dict):
        raise RuntimeError("credits 返回格式异常")
    try:
        credits_i = int(data.get("credits") if data.get("credits") is not None else 0)
    except Exception:
        credits_i = 0
    tier_raw = data.get("userPaygateTier") or data.get("user_paygate_tier")
    tier_s = str(tier_raw).strip() if tier_raw is not None else None
    if tier_s == "":
        tier_s = None
    return {"credits": credits_i, "user_paygate_tier": tier_s, "raw": data}

def _veo_local_next_1305_datetime() -> datetime:
    """本地「下一次 01:05」：当前时刻若已过当天 01:05 则为明天 01:05，否则为今天 01:05。"""
    now = datetime.now()
    today_0105 = now.replace(hour=1, minute=5, second=0, microsecond=0)
    if now > today_0105:
        nd = now.date() + timedelta(days=1)
        return datetime(nd.year, nd.month, nd.day, 1, 5, 0)
    return today_0105


def _veo_local_next_0105_cooldown_str() -> str:
    """VEO 余额接口不返回到期时间时，按本地北京时间下一次 01:05:00 作为 cooldown_until。"""
    return _veo_local_next_1305_datetime().strftime("%Y-%m-%d %H:%M:%S")


def _veo_parse_proxy_url(proxy_url: str) -> Dict[str, str]:
    raw = str(proxy_url or "").strip()
    if not raw:
        return {}
    u = urlparse(raw if "://" in raw else f"http://{raw}")
    return {
        "scheme": (u.scheme or "http").lower(),
        "host": str(u.hostname or "").strip(),
        "port": str(u.port or (443 if (u.scheme or "").lower() == "https" else 80)),
        "username": unquote(u.username or ""),
        "password": unquote(u.password or ""),
    }


def _veo_chain_http_to_socks5h_get(
    *,
    system_proxy: str,
    window_proxy: str,
    url: str,
    headers: Dict[str, str],
    timeout: float = 45.0,
) -> tuple[int, str]:
    """本机 -> system_proxy(http/https CONNECT) -> window_proxy(socks5h) -> HTTPS GET。"""
    sp = _veo_parse_proxy_url(system_proxy)
    wp = _veo_parse_proxy_url(window_proxy)
    target = urlparse(url)
    if sp.get("scheme") not in ("http", "https"):
        raise RuntimeError("两层代理暂仅支持第一层 system_proxy 为 http/https")
    if wp.get("scheme") not in ("socks5", "socks5h", "socks"):
        raise RuntimeError("两层代理暂仅支持第二层 window_proxy 为 socks5/socks5h")
    if (target.scheme or "").lower() != "https":
        raise RuntimeError("VEO credits 两层代理仅支持 https 目标")
    if not sp.get("host") or not wp.get("host") or not target.hostname:
        raise RuntimeError("代理或目标 URL 缺少 host")

    def _read_until(sock: socket.socket, marker: bytes, limit: int = 65536) -> bytes:
        buf = b""
        while marker not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > limit:
                raise RuntimeError("读取代理响应超限")
        return buf

    def _recvn(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("连接提前关闭")
            buf += chunk
        return buf

    with socket.create_connection((sp["host"], int(sp["port"])), timeout=timeout) as raw_sock:
        raw_sock.settimeout(timeout)
        sock: socket.socket = raw_sock
        if sp.get("scheme") == "https":
            sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=sp["host"])
            sock.settimeout(timeout)
        connect_lines = [f"CONNECT {wp['host']}:{wp['port']} HTTP/1.1", f"Host: {wp['host']}:{wp['port']}"]
        if sp.get("username") or sp.get("password"):
            auth = base64.b64encode(f"{sp.get('username','')}:{sp.get('password','')}".encode()).decode()
            connect_lines.append(f"Proxy-Authorization: Basic {auth}")
        sock.sendall(("\r\n".join(connect_lines) + "\r\n\r\n").encode("ascii"))
        status_line = _read_until(sock, b"\r\n\r\n").split(b"\r\n", 1)[0].decode("iso-8859-1", "ignore")
        if " 200 " not in f" {status_line} ":
            raise RuntimeError(f"system_proxy CONNECT window_proxy 失败：{status_line}")

        sock.sendall(b"\x05\x02\x00\x02" if (wp.get("username") or wp.get("password")) else b"\x05\x01\x00")
        ver_method = _recvn(sock, 2)
        if ver_method[0] != 5 or ver_method[1] == 0xFF:
            raise RuntimeError("window_proxy SOCKS5 握手失败")
        if ver_method[1] == 2:
            user_b = wp.get("username", "").encode("utf-8")
            pwd_b = wp.get("password", "").encode("utf-8")
            sock.sendall(b"\x01" + bytes([len(user_b)]) + user_b + bytes([len(pwd_b)]) + pwd_b)
            if _recvn(sock, 2) != b"\x01\x00":
                raise RuntimeError("window_proxy SOCKS5 认证失败")
        elif ver_method[1] != 0:
            raise RuntimeError(f"window_proxy SOCKS5 不支持的认证方式：{ver_method[1]}")

        host_b = target.hostname.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + int(target.port or 443).to_bytes(2, "big"))
        rep = _recvn(sock, 4)
        if rep[0] != 5 or rep[1] != 0:
            raise RuntimeError(f"window_proxy SOCKS5 CONNECT 目标失败，rep={rep[1] if len(rep) > 1 else '?'}")
        if rep[3] == 1:
            _recvn(sock, 4)
        elif rep[3] == 3:
            _recvn(sock, _recvn(sock, 1)[0])
        elif rep[3] == 4:
            _recvn(sock, 16)
        _recvn(sock, 2)

        tls_sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.hostname)
        tls_sock.settimeout(timeout)
        path = (target.path or "/") + (f"?{target.query}" if target.query else "")
        req_headers = dict(headers or {})
        req_headers["Host"] = target.netloc
        req_headers["Connection"] = "close"
        req = f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in req_headers.items()) + "\r\n"
        tls_sock.sendall(req.encode("utf-8"))
        raw = b""
        while True:
            chunk = tls_sock.recv(65536)
            if not chunk:
                break
            raw += chunk
        head, _, resp_body = raw.partition(b"\r\n\r\n")
        head_text = head.decode("iso-8859-1", "ignore")
        try:
            status = int(head_text.split("\r\n", 1)[0].split()[1])
        except Exception:
            status = 0
        header_map: Dict[str, str] = {}
        for line in head_text.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                header_map[k.strip().lower()] = v.strip().lower()
        if header_map.get("transfer-encoding") == "chunked":
            out = b""
            rest = resp_body
            while True:
                line, _, rest = rest.partition(b"\r\n")
                size = int(line.split(b";", 1)[0], 16)
                if size <= 0:
                    break
                out += rest[:size]
                rest = rest[size + 2 :]
            resp_body = out
        enc = header_map.get("content-encoding", "")
        if "gzip" in enc:
            resp_body = gzip.decompress(resp_body)
        return status, resp_body.decode("utf-8", "replace")

async def veo_fetch_credits_by_proxy(
    *,
    sess: Optional["VeoSession"] = None,
    target_url: str,
    access_token: str,
    db: Any = None,
    picked: Any = None,
    log_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """本地经两层代理 GET aisandbox /v1/credits，不再打开指纹浏览器窗口。"""
    tok = str(access_token or "").strip()
    if not tok:
        raise RuntimeError("缺少 access_token（请先获取并保存 access_token）")

    log_file = log_file or (Path(sess.monitor_log_path) if sess is not None and getattr(sess, "monitor_log_path", None) else MONITOR_LOG_FILE)

    async def _resolve_system_proxy() -> str:
        if db is None:
            return ""
        try:
            syscfg = await db.get_system_config()
            if bool(getattr(syscfg, "proxy_enabled", False)):
                return str(getattr(syscfg, "proxy_url", "") or "").strip()
        except Exception:
            return ""
        return ""

    async def _resolve_window_proxy() -> str:
        if db is None or picked is None:
            return ""
        window_pk = int(getattr(picked, "window_pk", 0) or 0)
        if window_pk <= 0:
            return ""
        try:
            async with db._read_conn() as conn:  # type: ignore[attr-defined]
                conn.row_factory = __import__("aiosqlite").Row
                cur = await conn.execute(
                    """
                    SELECT p.protocol, p.host, p.port, p.proxy_username, p.proxy_password
                    FROM windows w
                    JOIN proxies p ON p.deleted = 0 AND (
                         (COALESCE(w.proxy_id, 0) > 0 AND p.proxy_id = w.proxy_id)
                      OR (COALESCE(w.proxy_id, 0) = 0 AND TRIM(COALESCE(w.proxy_addr, '')) <> '' AND (
                           TRIM(COALESCE(p.last_ip, '')) = TRIM(w.proxy_addr)
                        OR TRIM(COALESCE(p.host, '')) = TRIM(w.proxy_addr)
                        OR TRIM(COALESCE(p.host, '') || ':' || COALESCE(p.port, '')) = TRIM(w.proxy_addr)
                      ))
                    )
                    WHERE w.id = ? AND w.deleted = 0
                    LIMIT 1
                    """,
                    (window_pk,),
                )
                row = await cur.fetchone()
            if not row:
                return ""
            proto = str(row["protocol"] or "socks5").strip().lower()
            if proto in ("socks", "socks5") or proto not in ("socks5h", "http", "https"):
                proto = "socks5h"
            host = str(row["host"] or "").strip()
            port = str(row["port"] or "").strip()
            if not host or not port:
                return ""
            user = quote(str(row["proxy_username"] or "").strip(), safe="")
            pwd = quote(str(row["proxy_password"] or "").strip(), safe="")
            auth = f"{user}:{pwd}@" if user or pwd else ""
            return f"{proto}://{auth}{host}:{port}"
        except Exception as e:
            append_log(log_file, f"[veo] resolve window proxy failed: {safe_trim(str(e), 200)}")
            return ""

    system_proxy = await _resolve_system_proxy()
    window_proxy = await _resolve_window_proxy()
    proxy_url = window_proxy or system_proxy or None
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Authorization": f"Bearer {tok}",
        "Origin": "https://labs.google",
        "Referer": "https://labs.google/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "x-goog-api-key": VEO_GOOG_API_KEY,
        "x-client-data": "COPjygE=",
    }
    append_log(log_file, f"[veo] local credits proxy_system={'yes' if system_proxy else 'no'} proxy_window={'yes' if window_proxy else 'no'}")
    if system_proxy and window_proxy and _veo_parse_proxy_url(window_proxy).get("scheme") in ("socks", "socks5", "socks5h"):
        status, body_text = await asyncio.to_thread(
            _veo_chain_http_to_socks5h_get,
            system_proxy=system_proxy,
            window_proxy=window_proxy,
            url=FLOW_LABS_CREDITS_URL,
            headers=headers,
            timeout=45.0,
        )
        obj = json.loads(body_text) if body_text.strip() else {}
    else:
        client_kwargs: Dict[str, Any] = {"timeout": httpx.Timeout(45.0), "trust_env": True}
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        try:
            client = httpx.AsyncClient(**client_kwargs)
        except ImportError as e:
            if proxy_url and str(proxy_url).lower().startswith(("socks5://", "socks5h://", "socks4://")):
                raise RuntimeError("当前环境缺少 SOCKS 支持，请安装依赖：pip install 'httpx[socks]' socksio") from e
            raise
        async with client:
            try:
                resp = await client.get(FLOW_LABS_CREDITS_URL, headers=headers)
            except ImportError as e:
                if proxy_url and str(proxy_url).lower().startswith(("socks5://", "socks5h://", "socks4://")):
                    raise RuntimeError("当前环境缺少 SOCKS 支持，请安装依赖：pip install 'httpx[socks]' socksio") from e
                raise
            status, body_text = resp.status_code, resp.text
            obj = resp.json() if resp.text.strip() else {}
    if int(status or 0) >= 400:
        raise RuntimeError(f"查询 credits 失败：HTTP {status} {safe_trim(body_text, 400)}")
    out = _veo_normalize_credits_payload(obj)
    out["cooldown_until"] = _veo_local_next_0105_cooldown_str()
    return out


# ---------------------------------------------------------------------------
# 获取 token：长 token（浏览器 Cookie ST）与短 token（auth/session AT）
# ---------------------------------------------------------------------------
def _veo_extension_labs_url(target_url: str) -> str:
    """插件 content-script 只注入 labs.google；触发 WS 时统一落到 labs.google。"""
    raw = (target_url or "").strip() or "https://labs.google/fx"
    try:
        p = urlparse(raw)
        host = (p.netloc or "").lower()
        if p.scheme in ("http", "https") and (host == "labs.google" or host.endswith(".labs.google")):
            return raw
    except Exception:
        pass
    return "https://labs.google/fx"


VEO_FLOW_PAGE_URL_PREFIX = "https://labs.google/fx/tools/flow"


def _veo_input_token_params_result(
    *,
    access_token: Optional[str],
    access_expires: Optional[str],
    session_token: Optional[str],
    short_access_token: Optional[str],
    short_expires: Optional[str],
    target_url: str,
    current_url: str,
) -> Dict[str, Any]:
    session_tok = str(session_token or access_token or "").strip()
    short_tok = str(short_access_token or access_token or session_tok or "").strip()
    exp = str(access_expires or "").strip() or None
    short_exp = str(short_expires or exp or "").strip() or None
    return {
        "type": "veo_access_tokens",
        "source": "input_params",
        "access_token": session_tok or short_tok,
        "session_token": session_tok or short_tok,
        "expires": exp,
        "short_access_token": short_tok,
        "short_expires": short_exp,
        "target_url": target_url,
        "current_url": current_url,
        "required_url_prefix": VEO_FLOW_PAGE_URL_PREFIX,
        "token_fetch_skipped": True,
        "skip_reason": "current_page_not_flow",
    }


def _veo_extension_ids_from_session(
    sess: "VeoSession",
    *,
    space_id: Optional[str] = None,
    window_key: Optional[str] = None,
) -> tuple[str, str]:
    sid = str(space_id or getattr(getattr(sess, "pw_ctx", None), "space_id", "") or "").strip()
    wkey = str(window_key or getattr(getattr(sess, "pw_ctx", None), "window_key", "") or "").strip()
    if not sid or not wkey:
        raise RuntimeError("缺少插件连接标识：space_id/window_key")
    return sid, wkey

def is_accounts_google(url):
    parsed = urlparse(url)
    return parsed.netloc == "accounts.google.com"

async def veo_fetch_access_tokens_via_extension(
    *,
    sess: "VeoSession",
    target_url: str,
    space_id: Optional[str] = None,
    window_key: Optional[str] = None,
    connect_wait_seconds: float = 8.0,
    token_timeout_seconds: float = 45.0,
    log_file: Optional[Path] = None,
    auto_triger_connection: Optional[bool] = True,
    access_token: Optional[str] = None,
    access_expires: Optional[str] = None,
    session_token: Optional[str] = None,
    short_access_token: Optional[str] = None,
    short_expires: Optional[str] = None,
) -> Dict[str, Any]:
    """通过浏览器插件读取 VEO long session-token 与 short access_token。"""
    sid, wkey = _veo_extension_ids_from_session(sess, space_id=space_id, window_key=window_key)
    log_file = log_file or (Path(sess.monitor_log_path) if getattr(sess, "monitor_log_path", None) else MONITOR_LOG_FILE)
    client = await ensure_extension_connected_via_window(
        sess=sess,
        target_url=target_url,
        space_id=sid,
        window_key=wkey,
        wait_seconds=connect_wait_seconds,
        log_file=log_file,
        auto_triger_connection = auto_triger_connection,
    )
    if client is None:
        raise NonPenalizedTaskError(
            f"浏览器插件未连接：space_id={sid!r} window_key={wkey!r}",
            status_code=503,
        )

    labs_target = _veo_extension_labs_url(target_url)
    try:
        page_info = await submit_extension_task(
            space_id=sid,
            window_key=wkey,
            provider="veo",
            payload={
                "action": "get_current_page",
                "workflow_kind": "get_current_page",
            },
            progress_cb=_noop_progress_cb,
            timeout_seconds=max(3.0, min(10.0, float(connect_wait_seconds or 8.0))),
        )
        current_url = str((page_info or {}).get("url") or "").strip()
        print(f"current_url:{current_url}")
        if is_accounts_google(current_url):
            append_log(
                log_file,
                "[veo][token] current page is not flow page, skip extension token fetch "
                f"current_url={safe_trim(current_url, 300)!r} required_prefix={VEO_FLOW_PAGE_URL_PREFIX!r}",
            )
            return _veo_input_token_params_result(
                access_token=access_token,
                access_expires="1999-00-20T07:00:00.000Z",
                session_token=session_token,
                short_access_token=short_access_token,
                short_expires="1999-00-20T07:00:00.000Z",
                target_url=target_url,
                current_url=current_url,
            )
    except Exception as e:
        append_log(log_file, f"[veo][token] check current page via extension failed, continue token fetch: {e}")

    info = await submit_extension_task(
        space_id=sid,
        window_key=wkey,
        provider="veo",
        payload={
            "action": "fetch_tokens",
            "workflow_kind": "fetch_tokens",
            "target_url": labs_target,
            "project_page": labs_target,
        },
        progress_cb=_noop_progress_cb,
        timeout_seconds=max(5.0, float(token_timeout_seconds or 45.0)),
    )
    if not isinstance(info, dict):
        raise RuntimeError(f"插件返回 token 格式异常：{info!r}")
    session_token = str(info.get("session_token") or info.get("access_token") or "").strip()
    short_at = str(info.get("short_access_token") or "").strip()
    if not session_token:
        raise NonPenalizedTaskError("插件未返回 VEO long session_token", status_code=401)
    if not short_at:
        raise NonPenalizedTaskError("插件未返回 VEO short access_token", status_code=401)

    try:
        sess.veo_short_access_token = short_at
        sess.veo_short_access_expires = str(info.get("short_expires") or "").strip() or None
        sess.veo_short_session_token = session_token
        sess.veo_short_email = str(info.get("email") or "").strip() or None
    except Exception:
        pass
    out = dict(info)
    out["access_token"] = session_token
    out["session_token"] = session_token
    out["short_access_token"] = short_at
    append_log(
        log_file,
        f"[veo][token] extension returned long_len={len(session_token)} short_len={len(short_at)}",
    )
    return out


async def force_fetch_access_token_in_window(
    *,
    sess: "VeoSession",
    target_url: str,
) -> Dict[str, Any]:
    """通过浏览器插件读取长效 Cookie token；必要时先用带 fpb_* URL 触发 WS。"""
    sess._cancel_idle_close()
    info = await veo_fetch_access_tokens_via_extension(
        sess=sess,
        target_url=target_url,
        connect_wait_seconds=8.0,
        token_timeout_seconds=45.0,
        log_file=Path(sess.monitor_log_path) if sess.monitor_log_path else MONITOR_LOG_FILE,
    )
    return {
        "access_token": str((info or {}).get("short_access_token") or (info or {}).get("access_token") or "").strip() or None,
        "session_token": str((info or {}).get("short_access_token") or (info or {}).get("access_token") or "").strip() or None,
        "expires": str((info or {}).get("short_expires") or "").strip() or None,
        "email": str((info or {}).get("email") or "").strip() or None,
        "short_access_token": str((info or {}).get("short_access_token") or "").strip() or None,
        "short_expires": str((info or {}).get("short_expires") or "").strip() or None,
        "source": str((info or {}).get("source") or "extension").strip() or "extension",
    }


async def _veo_resolve_system_proxy(db: Any) -> str:
    if db is None:
        return ""
    try:
        syscfg = await db.get_system_config()
        if bool(getattr(syscfg, "proxy_enabled", False)):
            return str(getattr(syscfg, "proxy_url", "") or "").strip()
    except Exception:
        return ""
    return ""


async def _veo_resolve_window_proxy(db: Any, picked: Any, log_file: Path) -> str:
    if db is None or picked is None:
        return ""
    window_pk = int(getattr(picked, "window_pk", 0) or 0)
    if window_pk <= 0:
        return ""
    try:
        async with db._read_conn() as conn:  # type: ignore[attr-defined]
            conn.row_factory = __import__("aiosqlite").Row
            cur = await conn.execute(
                """
                SELECT p.protocol, p.host, p.port, p.proxy_username, p.proxy_password
                FROM windows w
                JOIN proxies p ON p.deleted = 0 AND (
                     (COALESCE(w.proxy_id, 0) > 0 AND p.proxy_id = w.proxy_id)
                  OR (COALESCE(w.proxy_id, 0) = 0 AND TRIM(COALESCE(w.proxy_addr, '')) <> '' AND (
                       TRIM(COALESCE(p.last_ip, '')) = TRIM(w.proxy_addr)
                    OR TRIM(COALESCE(p.host, '')) = TRIM(w.proxy_addr)
                    OR TRIM(COALESCE(p.host, '') || ':' || COALESCE(p.port, '')) = TRIM(w.proxy_addr)
                  ))
                )
                WHERE w.id = ? AND w.deleted = 0
                LIMIT 1
                """,
                (window_pk,),
            )
            row = await cur.fetchone()
        if not row:
            return ""
        proto = str(row["protocol"] or "socks5").strip().lower()
        if proto in ("socks", "socks5") or proto not in ("socks5h", "http", "https"):
            proto = "socks5h"
        host = str(row["host"] or "").strip()
        port = str(row["port"] or "").strip()
        if not host or not port:
            return ""
        user = quote(str(row["proxy_username"] or "").strip(), safe="")
        pwd = quote(str(row["proxy_password"] or "").strip(), safe="")
        auth = f"{user}:{pwd}@" if user or pwd else ""
        return f"{proto}://{auth}{host}:{port}"
    except Exception as e:
        append_log(log_file, f"[veo] resolve window proxy failed: {safe_trim(str(e), 200)}")
        return ""
    
async def fetch_short_access_token_by_proxy(
    *,
    session_token: str,
    target_url: str = "https://labs.google/fx",
    db: Any = None,
    picked: Any = None,
    log_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """通过两层代理 GET https://labs.google/fx/api/auth/session，用长效 session-token 换短效 access_token。"""
    st = str(session_token or "").strip()
    if not st:
        raise RuntimeError("缺少 __Secure-next-auth.session-token")
    log_file = log_file or MONITOR_LOG_FILE
    auth_session_url = "https://labs.google/fx/api/auth/session"
    referer = (target_url or "https://labs.google/fx").strip() or "https://labs.google/fx"
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Cookie": f"__Secure-next-auth.session-token={st}",
        "Referer": referer,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    }
    system_proxy = await _veo_resolve_system_proxy(db)
    window_proxy = await _veo_resolve_window_proxy(db, picked, log_file)
    proxy_url = window_proxy or system_proxy or None
    append_log(log_file, f"[veo][token] auth/session by proxy proxy_system={'yes' if system_proxy else 'no'} proxy_window={'yes' if window_proxy else 'no'}")
    if system_proxy and window_proxy and _veo_parse_proxy_url(window_proxy).get("scheme") in ("socks", "socks5", "socks5h"):
        status, body_text = await asyncio.to_thread(
            _veo_chain_http_to_socks5h_get,
            system_proxy=system_proxy,
            window_proxy=window_proxy,
            url=auth_session_url,
            headers=headers,
            timeout=45.0,
        )
        data = json.loads(body_text) if body_text.strip() else {}
    else:
        client_kwargs: Dict[str, Any] = {"timeout": httpx.Timeout(45.0), "trust_env": True}
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(auth_session_url, headers=headers)
            status, body_text = resp.status_code, resp.text
            data = resp.json() if resp.text.strip() else {}
    if int(status or 0) >= 400:
        raise RuntimeError(f"auth/session 失败：HTTP {status} {safe_trim(body_text, 400)}")
    if not isinstance(data, dict):
        raise RuntimeError("auth/session 返回格式异常")
    access_token = str(data.get("access_token") or data.get("accessToken") or "").strip()
    if not access_token:
        raise RuntimeError(f"auth/session 未返回 access_token：{safe_trim(str(data), 400)}")
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return {
        "access_token": access_token,
        "expires": str(data.get("expires") or "").strip() or None,
        "email": str((user or {}).get("email") or "").strip() or None,
        "session_token": st,
    }


def _veo_trpc_create_project_url(target_url: str) -> str:
    raw = (target_url or "").strip() or "https://labs.google/fx"
    try:
        p = urlparse(raw)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}/fx/api/trpc/project.createProject"
    except Exception:
        pass
    return "https://labs.google/fx/api/trpc/project.createProject"


def _veo_trpc_delete_project_url(target_url: str) -> str:
    # 删除项目按 Flow Web 当前接口固定走 labs.google。
    return "https://labs.google/fx/api/trpc/project.deleteProject"


def _parse_trpc_create_project_response(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    cur: Any = obj
    if isinstance(cur, list) and cur:
        cur = cur[0]
    if not isinstance(cur, dict):
        return None

    def _dig(d: Any, *keys: str) -> Any:
        x = d
        for k in keys:
            if not isinstance(x, dict):
                return None
            x = x.get(k)
        return x

    r = _dig(cur, "result", "data", "json", "result")
    if isinstance(r, dict):
        pid = r.get("projectId") or r.get("project_id")
        if pid:
            s = str(pid).strip()
            return s or None

    j = _dig(cur, "result", "data", "json")
    if isinstance(j, dict):
        pid2 = j.get("projectId") or j.get("project_id")
        if pid2:
            s2 = str(pid2).strip()
            return s2 or None

    pid3 = cur.get("projectId") or cur.get("project_id")
    if pid3:
        s3 = str(pid3).strip()
        return s3 or None
    return None


async def veo_create_flow_project_in_window(
    *,
    sess: "VeoSession",
    target_url: str,
    title: str,
    tool_name: str = "PINHOLE",
) -> str:
    """在指纹浏览器页面内调用 Flow `project.createProject`（与 flow2api flow_client.create_project 等价，走 Cookie）。"""
    title = str(title or "").strip()
    if not title:
        raise RuntimeError("项目标题不能为空")
    tn = str(tool_name or "PINHOLE").strip() or "PINHOLE"
    sess._cancel_idle_close()
    async with sess._bring_drafts_lock:
        await sess.ensure_open(args=sess.browser_open_args, force_open=sess.browser_force_open, headless=sess.browser_headless, acquire_bring_lock=False)
        await sess._bring_target_page_to_front(refresh_target=False, drafts_url=target_url, acquire_bring_lock=False)
        if sess.pw_ctx.page is None:
            raise RuntimeError("page 未初始化")

        log_file = Path(sess.monitor_log_path) if sess.monitor_log_path else (MONITOR_LOG_FILE)
        url = _veo_trpc_create_project_url(target_url)
        json_data = {"json": {"projectTitle": title, "toolName": tn}}

        tx = await page_fetch_json(
            sess.pw_ctx.page,
            url=url,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json_data=json_data,
            log_file=log_file,
        )
        st = tx.get("status")
        if st is not None and int(st) >= 400:
            body = safe_trim(str(tx.get("response_body") or ""), 500)
            raise RuntimeError(f"createProject 失败：HTTP {st} {body}")

        pid = _parse_trpc_create_project_response(tx.get("_json"))
        if not pid:
            raise RuntimeError(f"createProject 响应无效：{safe_trim(str(tx.get('response_body') or ''), 400)}")

        append_log(log_file, f"[veo][project] created title={title!r} project_id={pid}")
        project_url = f"https://labs.google/fx/tools/flow/project/{pid}"
        try:
            await sess.pw_ctx.page.goto(project_url, wait_until="domcontentloaded", timeout=60_000)
            append_log(log_file, f"[veo][project] navigated to created project url={project_url!r}")
        except Exception as e:
            # 项目已创建成功，导航失败不影响入库；仅记录日志，避免重复创建项目。
            append_log(log_file, f"[veo][project] goto created project failed url={project_url!r}: {e}")
        return pid


async def veo_delete_flow_project_in_window(
    *,
    sess: "VeoSession",
    target_url: str,
    project_id: str,
) -> Dict[str, Any]:
    """在指纹浏览器页面内调用 Flow `project.deleteProject`，删除指定 Flow 项目。"""
    pid = str(project_id or "").strip()
    if not pid:
        raise RuntimeError("project_id 不能为空")
    sess._cancel_idle_close()
    async with sess._bring_drafts_lock:
        await sess.ensure_open(
            args=sess.browser_open_args,
            force_open=sess.browser_force_open,
            headless=sess.browser_headless,
            acquire_bring_lock=False,
        )
        await sess._bring_target_page_to_front(refresh_target=False, drafts_url=target_url, acquire_bring_lock=False)
        if sess.pw_ctx.page is None:
            raise RuntimeError("page 未初始化")

        log_file = Path(sess.monitor_log_path) if sess.monitor_log_path else (MONITOR_LOG_FILE)
        url = _veo_trpc_delete_project_url(target_url)
        json_data = {"json": {"projectToDeleteId": pid}}

        tx = await page_fetch_json(
            sess.pw_ctx.page,
            url=url,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json_data=json_data,
            log_file=log_file,
        )
        st = tx.get("status")
        if st is not None and int(st) >= 400:
            body = safe_trim(str(tx.get("response_body") or ""), 500)
            raise RuntimeError(f"deleteProject 失败：HTTP {st} {body}")

        append_log(log_file, f"[veo][project] deleted project_id={pid}")
        resp = tx.get("_json")
        return {
            "success": True,
            "project_id": pid,
            "response": resp,
            "status": st,
        }


def _veo_resolve_n_frames(payload: Dict[str, Any]) -> int:
    """与 Sora 一致读取时长字段；显式为 1 时表示单帧 → 走文生图/图生图，其它值交给 `_pick_n_frames`（视频帧数语义）。"""
    duration_v = payload.get("n_frames") or payload.get("duration_frames") or payload.get("duration") or payload.get("时长")
    try:
        iv = int(float(duration_v))
    except Exception:
        iv = 0
    if iv == 1:
        return 1
    return _pick_n_frames(duration_v)


_VEO_KNOWN_IMAGE_ASPECT_RATIOS = frozenset(
    {
        IMAGE_ASPECT_RATIO_LANDSCAPE,
        IMAGE_ASPECT_RATIO_PORTRAIT,
        IMAGE_ASPECT_RATIO_SQUARE,
        IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE,
        IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR,
    }
)


def _veo_normalize_explicit_image_aspect_ratio(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip().upper().replace("-", "_")
    return s if s in _VEO_KNOWN_IMAGE_ASPECT_RATIOS else None


def _veo_image_aspect_from_dimensions(w: int, h: int) -> Optional[str]:
    """由像素宽高推断 Flow 图片 aspectRatio；无法归一到已知比例时按横竖给 16:9 / 9:16 类枚举。"""
    if w <= 0 or h <= 0:
        return None
    if w == h:
        return IMAGE_ASPECT_RATIO_SQUARE
    g = gcd(w, h)
    a, b = w // g, h // g
    if a > b:
        if a * 9 == b * 16:
            return IMAGE_ASPECT_RATIO_LANDSCAPE
        if a * 3 == b * 4:
            return IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE
        r = w / h
        if abs(r - 16 / 9) < 0.04:
            return IMAGE_ASPECT_RATIO_LANDSCAPE
        if abs(r - 4 / 3) < 0.04:
            return IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE
        return IMAGE_ASPECT_RATIO_LANDSCAPE
    if b > a:
        if b * 9 == a * 16:
            return IMAGE_ASPECT_RATIO_PORTRAIT
        if b * 3 == a * 4:
            return IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR
        r = h / w
        if abs(r - 16 / 9) < 0.04:
            return IMAGE_ASPECT_RATIO_PORTRAIT
        if abs(r - 4 / 3) < 0.04:
            return IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR
        return IMAGE_ASPECT_RATIO_PORTRAIT
    return IMAGE_ASPECT_RATIO_SQUARE


def _veo_pick_image_aspect_from_ratio_string(ratio: Optional[str]) -> Optional[str]:
    """从 `3:4`、`1920x1080` 等解析图片比例（不调用视频用的 `_veo_resolve_orientation_str`）。"""
    if not ratio:
        return None
    s = str(ratio).strip().lower().replace("：", ":")
    wh = _veo_try_parse_wh_in_ratio_string(s)
    if wh:
        got = _veo_image_aspect_from_dimensions(wh[0], wh[1])
        if got:
            return got
    for m in re.finditer(r"(\d+)\s*:\s*(\d+)", s):
        try:
            cw, ch = int(m.group(1)), int(m.group(2))
        except ValueError:
            continue
        got = _veo_image_aspect_from_dimensions(cw, ch)
        if got:
            return got
    return None


def _veo_resolve_image_aspect_ratio(payload: Dict[str, Any]) -> str:
    """文生图/图生图：支持 1:1、4:3、3:4、16:9、9:16（及 `IMAGE_ASPECT_RATIO_*` 显式值），默认横版 16:9 类。

    与视频的 `_veo_resolve_orientation_str` 分离：视频仅 16:9/9:16；图片多 Square / 4:3 / 3:4。
    """
    payload = payload or {}
    for key in ("image_aspect_ratio", "veo_image_aspect_ratio", "aspect_ratio"):
        got = _veo_normalize_explicit_image_aspect_ratio(payload.get(key))
        if got:
            return got

    wh = _veo_parse_pixel_pair_from_payload(payload)
    if wh:
        got = _veo_image_aspect_from_dimensions(wh[0], wh[1])
        if got:
            return got

    ratio = str(
        payload.get("size_ratio") or payload.get("aspect_ratio") or payload.get("ratio") or payload.get("尺寸") or ""
    ).strip() or None
    if ratio:
        got = _veo_pick_image_aspect_from_ratio_string(ratio)
        if got:
            return got

    ori = str(payload.get("orientation") or "").strip().lower()
    if ori == "portrait":
        return IMAGE_ASPECT_RATIO_PORTRAIT
    if ori == "landscape":
        return IMAGE_ASPECT_RATIO_LANDSCAPE

    raw_ar = str(
        payload.get("video_aspect_ratio") or payload.get("aspectRatio") or payload.get("veo_aspect_ratio") or ""
    ).strip()
    if raw_ar:
        got = _veo_pick_image_aspect_from_ratio_string(raw_ar)
        if got:
            return got
        u = raw_ar.upper()
        if "PORTRAIT" in u or "竖" in raw_ar:
            return IMAGE_ASPECT_RATIO_PORTRAIT
        if "LANDSCAPE" in u or "横" in raw_ar:
            return IMAGE_ASPECT_RATIO_LANDSCAPE

    return IMAGE_ASPECT_RATIO_LANDSCAPE


def _veo_truthy_payload_flag(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v or "").strip().lower()
    return s in ("1", "true", "yes", "on")


def _veo_resolve_image_model_name(payload: Dict[str, Any]) -> str:
    """文生图/图生图模型：默认 NARWHAL；`use_gem_pix_2` 或显式模型名为 GEM_PIX_2 时用 GEM_PIX_2（对齐 flow2api）。"""
    for key in ("veo_image_model", "image_model_name", "imageModelName"):
        raw = payload.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        s = str(raw).strip().upper().replace("-", "_")
        if s in ("GEM_PIX_2", "GEMPIX2", "GEM_PIX2"):
            return VEO_IMAGE_MODEL_GEM_PIX_2
        if s in ("NARWHAL",):
            return VEO_IMAGE_MODEL_NARWHAL
    if _veo_truthy_payload_flag(payload.get("use_gem_pix_2") or payload.get("veo_use_gem_pix_2")):
        return VEO_IMAGE_MODEL_GEM_PIX_2
    return VEO_IMAGE_MODEL_NARWHAL


def _veo_resolve_image_output_resolution(payload: Dict[str, Any]) -> tuple[str, bool]:
    """返回 (展示用标签 '1K'|'2K', 是否需要调用 flow/upsampleImage)。默认 1K，不放大。"""
    raw = payload.get("resolution") or payload.get("image_resolution") or payload.get("veo_image_resolution")
    if raw is None or str(raw).strip() == "":
        return ("1K", False)
    s = str(raw).strip().lower().replace(" ", "")
    if s in ("2k", "2048", "2k_output", "uhd_2k"):
        return ("2K", True)
    return ("1K", False)

# ---------------------------------------------------------------------------
# 入口函数
# ---------------------------------------------------------------------------
async def veo_workflow(
    payload: Dict[str, Any],
    progress_cb: ProgressCB,
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    timeout_seconds: float,
    access_token: Optional[str] = None,
    access_expires: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
    db: Any = None,
    task_type_window_id: Optional[int] = None,
) -> Dict[str, Any]:
    """VEO：指纹浏览器页面内 fetch aisandbox API。

    - 视频：`n_frames`（或 duration / duration_frames / 时长）经 `_pick_n_frames` 归一后 **>1**（如 300/450）
      时走文生视频 / 图生视频 / **Ingredients（R2V）多图**：若 `Ingredients_images`（或 `ingredients_images`）
      含至少 1 张可解析地址则走 `batchAsyncGenerateVideoReferenceImages`，模型与 flow2api
      `veo_3_1_r2v_fast` / `veo_3_1_r2v_fast_portrait` 一致，最多 3 张；否则再按首尾帧图判断 I2V 或 T2V。
      轮询 `batchCheckAsyncVideoGenerationStatus`。
    - 图片：当上述字段 **显式为 1** 时走文生图 / 图生图：`flow/uploadImage`（图生图仅首张）
      + `projects/{id}/flowMedia:batchGenerateImages`；默认模型 NARWHAL，`use_gem_pix_2`（或 `image_model_name`=`GEM_PIX_2`）时用 GEM_PIX_2（与 flow2api 一致）。
      比例支持 `IMAGE_ASPECT_RATIO_*` 显式值及 1:1 / 4:3 / 3:4 / 16:9 / 9:16（或宽高像素），默认横版。
      `resolution` / `veo_image_resolution` 等为 **2k** 时在生成后调用 `flow/upsampleImage`：`share_url` 为 2K 的 `data:image/jpeg;base64,...`，`origin_image_url` 为 1K fife 直链；放大失败时回退为 1K 并写入 `upsample_error`。

    project_id 解析顺序：payload（veo_project_id / project_id / current_project_id）
    → veo_url 中的 /tools/flow/project/{id}
    → 若传入 db 与 task_type_window_id（task_type_windows.id），则从 veo_flow_projects 随机一条。
    """
    payload = payload or {}
    project_id_from_db = False
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise NonPenalizedTaskError("payload.prompt 不能为空", status_code=400)

    n_frames = _veo_resolve_n_frames(payload)
    image_mode = n_frames == 1
    omni_mode = _veo_payload_uses_omni(payload)

    ingredients_urls: List[str] = []
    want_ingredients = False
    if not image_mode:
        if omni_mode and not _veo_payload_explicit_i2v(payload):
            ingredients_urls = _veo_collect_omni_reference_image_urls(payload)
        else:
            ingredients_urls = _veo_collect_ingredients_image_urls(payload)
        want_ingredients = len(ingredients_urls) >= 1
        #raise NonPenalizedTaskError("Veo3.1视频维护中，暂时下架", status_code=400,content_violation=True)

    want_i2v = False
    if not image_mode:
        want_i2v = (not want_ingredients) and _veo_payload_looks_like_i2v(payload)
    if want_ingredients:
        want_i2v = False

    i2v_urls: List[str] = []
    if want_i2v:
        i2v_urls = _veo_collect_i2v_image_urls(payload)
        if len(i2v_urls) == 0:
            raise NonPenalizedTaskError(
                "图生图需要提供至少一张参考图（first_image_url / image_url / images 等）"
                if image_mode
                else "图生视频需要提供 1-2 张图片（first_image_url / image_url / images 等）",
                status_code=400,
            )

    labs_hint = str(payload.get("veo_url") or payload.get("target_url") or "").strip() or "https://labs.google/fx"
    project_id = str(
        payload.get("veo_project_id") or payload.get("project_id") or payload.get("current_project_id") or ""
    ).strip()
    if not project_id:
        project_id = _veo_extract_project_id_from_url(labs_hint) or ""
    if not project_id and db is not None and task_type_window_id:
        try:
            mid = int(task_type_window_id)
            if mid > 0:
                picked_pid = await db.get_random_veo_flow_project_id(mid)
                if picked_pid:
                    project_id = str(picked_pid).strip()
                    project_id_from_db = True
        except Exception:
            project_id = project_id or ""
    if not project_id:
        raise NonPenalizedTaskError(
            "缺少 Flow projectId：请在本窗口绑定的「Veo 项目」中至少添加一个项目，"
            "或在 payload 中设置 veo_project_id（或 project_id），"
            "或让 veo_url 包含 /tools/flow/project/{id}",
            status_code=400,
        )

    monitor_log_path = str(payload.get("monitor_log_path") or "").strip() or None
    idle_close_seconds = float(payload.get("ctx_idle_close_seconds") or 30.0)
    max_wait_seconds = float(payload.get("veo_pending_max_wait_seconds") or max(60.0, min(float(timeout_seconds), 1800.0)))
    poll_interval_seconds = float(payload.get("veo_pending_poll_interval_seconds") or 5.0)

    sess = get_or_create_veo_session(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = headless
    sess.browser_pure_mode = pure_mode
    sess.monitor_log_path = monitor_log_path
    sess.idle_close_seconds = idle_close_seconds

    log_file = sess._log_file
    project_page = _veo_project_page_url(project_id=project_id, hint_url=labs_hint)
    bring_prefix = project_page
    print(f"bring_prefix: {bring_prefix}")
    if project_id_from_db and task_type_window_id:
        append_log(
            log_file,
            f"[veo] project_id from DB random pick mapping_id={int(task_type_window_id)} -> {project_id!r}",
        )
    _mode = (
        "IMAGE"
        if image_mode
        else ("INGREDIENTS_R2V" if want_ingredients else ("I2V" if want_i2v else "T2V"))
    )
    append_log(
        log_file,
        f"[veo] workflow {_mode} n_frames={n_frames} start project_id={project_id!r} "
        f"prompt={safe_trim(prompt, 200)!r} "
        f"ingredients={len(ingredients_urls) if want_ingredients else 0} "
        f"images={len(i2v_urls) if want_i2v else 0}",
    )
    await progress_cb(
        1,
        {
            "stage": "init",
            "workflow_kind": "image" if image_mode else "video",
            "n_frames": n_frames,
            "video_mode": (
                "r2v"
                if want_ingredients
                else ("i2v" if want_i2v else "t2v")
            ),
            "prompt": safe_trim(prompt, 200),
            "project_id": project_id,
            "image_count": len(ingredients_urls) if want_ingredients else (len(i2v_urls) if want_i2v else 0),
        },
    )

    if should_use_extension_executor(payload):
        # 插件模式下任务执行不再每次用带 fpb_* 参数的 URL 打开/导航窗口。
        # fpb_* 配置只在管理页“AccessToken / 过期 -> 更新”时写入一次，插件会持久化
        # 到 chrome.storage.local；之后只要指纹浏览器窗口启动、插件启动，就用缓存配置
        # 自动连接 WebSocket。这里保持目标页 URL 干净，避免额外 CDP/导航造成风控风险。
        annotated_project_page = project_page
        annotated_bring_prefix = project_page
        ext_at = str(access_token or "").strip() or None
        ext_session_token = str(access_token or "").strip() or None
        ext_exp = str(access_expires or "").strip() or None
        ext_tok_info: Optional[Dict[str, Any]] = None
        if db is not None and task_type_window_id:
            try:
                mid = int(task_type_window_id)
                if mid > 0:
                    row = await db.get_task_type_window_context(mid)
                    if row:
                        t2 = str(row.get("sora_access_token") or "").strip() or None
                        e2 = str(row.get("sora_access_expires") or "").strip() or None
                        if t2:
                            ext_session_token,ext_at, ext_exp = t2, t2, e2
            except Exception as e:
                append_log(log_file, f"[veo][extension] reload access_token from DB failed (use call args): {e}")
    
        try:
            client = await ensure_extension_connected_via_window(
                sess=sess,
                target_url=project_page,
                space_id=space_id,
                window_key=window_key,
                wait_seconds=10,
                log_file=log_file,
                auto_triger_connection = True,
            )
            if client is None:
                raise NonPenalizedTaskError(
                    f"浏览器插件未连接： window_key={window_key!r}",
                    status_code=503,
                )
            if not _veo_cached_access_still_valid(access_token, access_expires, margin_seconds=10):
                ext_tok_info = await veo_fetch_access_tokens_via_extension(
                    sess=sess,
                    target_url=project_page,
                    space_id=space_id,
                    window_key=window_key,
                    connect_wait_seconds=float(payload.get("extension_connect_wait_seconds") or 8.0),
                    token_timeout_seconds=float(payload.get("extension_token_timeout_seconds") or 45.0),
                    log_file=log_file,
                    access_token=ext_session_token,
                    access_expires=ext_exp,
                    session_token=ext_session_token,
                    short_access_token=ext_at,
                    short_expires=ext_exp,
                )
                ext_session_token = str((ext_tok_info or {}).get("session_token") or (ext_tok_info or {}).get("access_token") or "").strip() or None
                ext_exp = str((ext_tok_info or {}).get("expires") or "").strip() or None
                ext_at = str((ext_tok_info or {}).get("short_access_token") or "").strip() or ext_at
                if not _veo_cached_access_still_valid(ext_at, ext_exp, margin_seconds=10):
                    if db is not None and task_type_window_id:
                        try:
                            mid = int(task_type_window_id)
                            if mid > 0:
                                await db.update_task_type_window(mapping_id=mid, enabled=False)
                                append_log(log_file, f"[veo][extension] google account logged out; disabled task_type_window id={mid}")
                        except Exception as disable_e:
                            append_log(log_file, f"[veo][extension] disable task_type_window after logout failed: {disable_e}")
                    raise RuntimeError("google账号已登出")
        except Exception as e:
            append_log(log_file, f"[veo][extension] fetch long/short access_token via extension failed: {e}")
            if isinstance(e, NonPenalizedTaskError):
                raise
            ext_session_token = None
            ext_at = None
        if not ext_session_token:
            raise NonPenalizedTaskError(
                "missing usable session_token: please ensure the fingerprint window is logged in",
                status_code=401,
            )
        if not ext_at:
            raise NonPenalizedTaskError("missing usable short access_token: extension auth/session did not return access_token", status_code=401)
        if db is not None and task_type_window_id:
            try:
                mid = int(task_type_window_id)
                if mid > 0:
                    await db.update_task_type_window(
                        mapping_id=mid,
                        sora_access_token=ext_session_token,
                        sora_access_expires=ext_exp or None,
                    )
                    append_log(log_file, f"[veo][extension] persisted Labs session_token from extension to task_type_window id={mid}")
            except Exception as e:
                append_log(log_file, f"[veo][extension] persist access_token to DB failed (non-fatal): {e}")
        if image_mode:
            _ext_image_aspect = _veo_resolve_image_aspect_ratio(payload)
            _ext_image_model = _veo_resolve_image_model_name(payload)
            _ext_resolution_label, _ext_want_2k = _veo_resolve_image_output_resolution(payload)
            _ext_i2i_urls = _veo_collect_image_generation_reference_urls(payload) if _veo_payload_has_image_generation_references(payload) else []
            _ext_model_key = None
            _ext_video_aspect = None
        else:
            if want_ingredients:
                _ext_model_key, _ext_video_aspect = _veo_resolve_r2v_model(payload)
            elif want_i2v:
                _ext_video_aspect = _veo_resolve_i2v_aspect_ratio(payload)
                _ext_model_key = _veo_resolve_i2v_model_key(payload, _ext_video_aspect)
            else:
                _ext_model_key, _ext_video_aspect = _veo_resolve_t2v_model(payload)
            _ext_image_aspect = None
            _ext_image_model = None
            _ext_resolution_label, _ext_want_2k = ("1K", False)
            _ext_i2i_urls = []
        ext_payload = dict(payload)
        ext_payload.update(
            {
                "workflow_kind": "image" if image_mode else "video",
                "video_mode": (
                    "r2v"
                    if want_ingredients
                    else ("i2v" if want_i2v else "t2v")
                ),
                "project_id": project_id,
                "project_page": annotated_project_page,
                "bring_prefix": annotated_bring_prefix,
                "n_frames": n_frames,
                "image_mode": image_mode,
                "ingredients_urls": ingredients_urls,
                "i2v_urls": i2v_urls,
                "timeout_seconds": timeout_seconds,
                "max_wait_seconds": max_wait_seconds,
                "poll_interval_seconds": poll_interval_seconds,
                "access_token": ext_at,
                "access_expires": ext_exp,
                "ext_session_token": ext_session_token,
                "extension_model_key": _ext_model_key,
                "extension_video_aspect_ratio": _ext_video_aspect,
                "extension_image_aspect_ratio": _ext_image_aspect,
                "extension_image_model_name": _ext_image_model,
                "extension_image_reference_urls": _ext_i2i_urls,
                "extension_image_resolution_label": _ext_resolution_label,
                "extension_image_want_2k": _ext_want_2k,
            }
        )
        append_log(log_file, f"[veo][extension] dispatch workflow {_mode} project_id={project_id!r}")
        # 如插件在 token 获取后意外断开，仍走统一的“中转页 fpb_* URL 触发 WS”接口；
        # Python 只短暂连接 CDP 打开中转页，随后立刻断开；目标页由插件延迟跳转打开。
        if await wait_extension_client(space_id, window_key, timeout_seconds=0.2) is None:
            try:
                await ensure_extension_connected_via_window(
                    sess=sess,
                    target_url=project_page,
                    space_id=space_id,
                    window_key=window_key,
                    wait_seconds=float(payload.get("extension_connect_wait_seconds") or 8.0),
                    log_file=log_file,
                )
            except Exception as e:
                append_log(log_file, f"[veo][extension] ensure websocket for extension failed: {e}")
        try:
            _ext_result = await submit_extension_task(
                space_id=space_id,
                window_key=window_key,
                provider="veo",
                payload=ext_payload,
                progress_cb=progress_cb,
                timeout_seconds=max_wait_seconds + 120.0,
            )
        except Exception as e:
            if _veo_is_unsafe_generation_message(e):
                raise NonPenalizedTaskError(
                    f"VEO生成失败，内容包含（PUBLIC_ERROR_UNSAFE_GENERATION）：{safe_trim(str(e), 500)}",
                    status_code=getattr(e, "status_code", None) or 400,
                    content_violation=True,
                )
            raise
        if image_mode:
            _ext_result = await _veo_extension_upload_upsample_data_url_to_oss(
                _ext_result,
                project_id=str(project_id),
                log_file=log_file,
            )
        return _ext_result, project_page

    raise NonPenalizedTaskError(
        "VEO only supports extension/plugin mode: enable extension_executor or set payload.executor to extension/plugin",
        status_code=400,
    )
