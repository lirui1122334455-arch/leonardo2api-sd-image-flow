"""Sora 执行器：专注于对 `https://sora.chatgpt.com` 的接口访问与任务编排。

拆分后的职责边界：
- `playwright_broswer_context.py`：通用“指纹浏览器自动化层”（开窗/连CDP/挑页/page内fetch）
- `sora_task_executor.py`：Sora 站点侧逻辑（鉴权抓取、nf/create、pending轮询、drafts/publish、nf_check、invite）

入口：
- `sora_gen_video`（由 `task_service.py` 发起调用）
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import io
import json
import os
import random
import re
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import quote, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from .playwright_broswer_context import (
    PlaywrightBrowserContext,
    acquire_browser_open_slot,
    append_log,
    get_or_create_ctx as get_or_create_playwright_ctx,
    page_fetch_json,
    page_fetch_tx,
    safe_trim,
)
from .task_executor_types import NonPenalizedTaskError, ProgressCB
from ..core.database import Database
from ..core.logger import logger
from ..core.paths import MONITOR_LOG_FILE


def _mask_secret(s: Optional[str], *, head: int = 8, tail: int = 6) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= head + tail + 3:
        return s[: max(1, min(len(s), head))] + "...(masked)"
    return s[:head] + "...(masked)..." + s[-tail:]


def _pick_orientation_from_ratio(ratio: Optional[str]) -> Optional[str]:
    """将尺寸比例（如 16:9 / 9:16）转换为 sora 的 orientation（landscape/portrait）。"""
    if not ratio:
        return None
    s = str(ratio).strip().lower().replace("：", ":")
    if "16:9" in s:
        return "landscape"
    if "9:16" in s:
        return "portrait"
    return None


def _pick_n_frames(v: Any) -> int:
    """将“时长参数”归一化为 n_frames（当前按你的需求仅支持 300/450）。"""
    try:
        iv = int(float(v))
    except Exception:
        iv = 300
    if iv in (300, 450):
        return iv
    # 常见：秒数 10/15
    if iv == 10:
        return 300
    if iv == 15:
        return 450
    return 450


def _normalize_progress(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        fv = float(v)
    except Exception:
        return None
    if fv > 1.0:
        return fv / 100.0
    return fv


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


def _build_debug_progress_panel_script() -> str:
    """返回调试进度面板注入脚本（单实例，重复调用只更新内容）。"""
    return r"""
(payload) => {
  try {
    const PANEL_ID = "__sora_debug_progress_panel__";
    const STYLE_ID = "__sora_debug_progress_panel_style__";
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

    ttl.textContent = data.title || "Sora 调试进度";
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


def _extract_task_obj(payload: Any, task_id: str) -> Optional[Dict[str, Any]]:
    if isinstance(payload, list):
        for it in payload:
            if isinstance(it, dict) and str(it.get("id", "")) == str(task_id):
                return it
        return None
    if isinstance(payload, dict):
        for k in ["data", "rows", "items", "tasks"]:
            v = payload.get(k)
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict) and str(it.get("id", "")) == str(task_id):
                        return it
        return None
    return None


def _sora_backend_url_from_target(target_url: str, path: str) -> str:
    """根据 target_url 组合 sora backend URL（默认 host 同源）。"""
    try:
        p = urlparse(str(target_url or "").strip())
        scheme = p.scheme or "https"
        netloc = p.netloc or "sora.chatgpt.com"
        return f"{scheme}://{netloc}{path}"
    except Exception:
        return "https://sora.chatgpt.com" + str(path)


async def refresh_sora_balance(
    *,
    db: Database,
    picked: Any,
    target_url: str,
    refresh_timeout_seconds: float,
    signal_window_pool_replenish: Optional[Callable[[], None]] = None,
) -> Optional[Dict[str, Any]]:
    handler = str(getattr(picked, "create_task_handler", "") or "").strip().lower()
    if not handler.startswith("sora_gen_video"):
        return None

    try:
        sess = get_or_create_sora_session(
            vendor=picked.browser_vendor,
            base_url=picked.browser_base_url,
            access_key=picked.browser_access_key,
            space_id=picked.space_id,
            window_key=picked.window_key,
        )
        sess.browser_headless = picked.headless
    except Exception:
        return None

    nf_check: Optional[Dict[str, Any]] = None
    nf_check_err: Optional[Exception] = None
    try:
        checked = await sess.api_nf_check(target_url=target_url)
        nf_check = checked if isinstance(checked, dict) else None
    except Exception as e:
        nf_check_err = e
        nf_check = None

    try:
        if nf_check and nf_check.get("remaining_count") is not None:
            await db.update_task_type_window(
                mapping_id=picked.mapping_id,
                remaining_quota=int(nf_check.get("remaining_count") or 0),
                sora_remaining_count=int(nf_check.get("remaining_count") or 0),
                sora_purchased_remaining_count=int(nf_check.get("purchased_remaining_count") or 0),
                sora_rate_limit_reached=bool(nf_check.get("rate_limit_reached", False)),
                sora_access_resets_in_seconds=int(nf_check.get("access_resets_in_seconds") or 0),
                cooldown_until=(str(nf_check.get("cooldown_until")) if nf_check.get("cooldown_until") else None),
            )
    except Exception:
        pass

    try:
        if nf_check is None or (nf_check and int(nf_check.get("remaining_count") or 0) == 0):
            info = await sora_fetch_access_token_in_window(sess=sess, target_url=target_url)
            access_token = str((info or {}).get("access_token") or "").strip() or None
            expires = str((info or {}).get("expires") or "").strip() or None
            if access_token:
                await db.update_task_type_window(
                    mapping_id=picked.mapping_id,
                    sora_access_token=access_token,
                    sora_access_expires=expires,
                )
                picked.sora_access_token = access_token
                picked.sora_access_expires = expires
                try:
                    sess.set_access_token(access_token, expires)
                except Exception:
                    pass

                try:
                    checked = await sess.api_nf_check(target_url=target_url)
                    nf_check = checked if isinstance(checked, dict) else None
                except Exception as e:
                    nf_check_err = e
                    nf_check = None

                try:
                    if nf_check and nf_check.get("remaining_count") is not None:
                        await db.update_task_type_window(
                            mapping_id=picked.mapping_id,
                            remaining_quota=int(nf_check.get("remaining_count") or 0),
                            sora_remaining_count=int(nf_check.get("remaining_count") or 0),
                            sora_purchased_remaining_count=int(nf_check.get("purchased_remaining_count") or 0),
                            sora_rate_limit_reached=bool(nf_check.get("rate_limit_reached", False)),
                            sora_access_resets_in_seconds=int(nf_check.get("access_resets_in_seconds") or 0),
                            cooldown_until=(str(nf_check.get("cooldown_until")) if nf_check.get("cooldown_until") else None),
                        )
                except Exception:
                    pass
    except Exception as e:
        logger.warning("refresh sora access token failed: %s", e)

    try:
        if nf_check_err is not None:
            if signal_window_pool_replenish is not None:
                signal_window_pool_replenish()
        else:
            remaining = int((nf_check or {}).get("remaining_count") or 0)
            if remaining <= 2:
                if signal_window_pool_replenish is not None:
                    signal_window_pool_replenish()
            else:
                sess._cancel_idle_close()
    except Exception:
        pass

    return nf_check


async def refresh_sora_balance_best_effort(
    *,
    db: Database,
    picked: Any,
    target_url: str,
    refresh_timeout_seconds: float,
    signal_window_pool_replenish: Optional[Callable[[], None]] = None,
    task_id: Optional[str] = None,
) -> None:
    """余额刷新只做尽力而为，不能影响任务终态写回。"""
    try:
        await asyncio.wait_for(
            refresh_sora_balance(
                db=db,
                picked=picked,
                target_url=target_url,
                refresh_timeout_seconds=refresh_timeout_seconds,
                signal_window_pool_replenish=signal_window_pool_replenish,
            ),
            timeout=refresh_timeout_seconds,
        )
    except Exception as e:
        logger.warning(
            "refresh_sora_balance skipped: task=%s mapping=%s err=%s",
            task_id,
            picked.mapping_id,
            e,
        )


async def force_refresh_sora_access_token(
    *,
    db: Database,
    picked: Any,
    target_url: str,
    refresh_timeout_seconds: float,
    task_id: Optional[str] = None,
) -> bool:
    """窗口内强制重抓 Sora access_token 并写回 mapping（不依赖余额为 0）。"""
    try:
        sess = get_or_create_sora_session(
            vendor=picked.browser_vendor,
            base_url=picked.browser_base_url,
            access_key=picked.browser_access_key,
            space_id=picked.space_id,
            window_key=picked.window_key,
        )
        sess.browser_headless = picked.headless
    except Exception:
        return False
    try:
        info = await asyncio.wait_for(
            sora_fetch_access_token_in_window(sess=sess, target_url=target_url),
            timeout=refresh_timeout_seconds,
        )
    except Exception as e:
        logger.warning(
            "force_refresh_sora_access_token failed: task=%s mapping=%s err=%s",
            task_id,
            picked.mapping_id,
            e,
        )
        return False
    access_token = str((info or {}).get("access_token") or "").strip() or None
    expires = str((info or {}).get("expires") or "").strip() or None
    if not access_token:
        return False
    try:
        await db.update_task_type_window(
            mapping_id=picked.mapping_id,
            sora_access_token=access_token,
            sora_access_expires=expires,
        )
    except Exception:
        return False
    picked.sora_access_token = access_token
    picked.sora_access_expires = expires
    try:
        sess.set_access_token(access_token, expires)
    except Exception:
        pass
    logger.info("sora access_token force-refreshed: task=%s mapping=%s", task_id, picked.mapping_id)
    return True

async def sora_fetch_access_token_in_window(
    *,
    sess: "SoraSession",
    target_url: str = "https://sora.chatgpt.com/drafts",
) -> Dict[str, Any]:
    """在“指纹浏览器窗口页面上下文”里调用 `/api/auth/session` 获取 access_token + expires。

    说明：
    - 使用 `page_fetch_json`（credentials: include），会自动携带该窗口 Cookie（含代理/指纹网络栈）。
    - 不允许手工传 Cookie header（page_fetch_tx 会屏蔽 cookie/ua/origin/referer 等头）。
    """
    sess._cancel_idle_close()
    async with sess._bring_drafts_lock:
        await sess.ensure_open(args=sess.browser_open_args, force_open=sess.browser_force_open, headless=sess.browser_headless, acquire_bring_lock=False)
        await sess._bring_sora_drafts_to_front(acquire_bring_lock=False)
        if sess.pw_ctx.page is None:
            raise RuntimeError("page 未初始化")

        log_file = Path(sess.monitor_log_path) if sess.monitor_log_path else (MONITOR_LOG_FILE)
        url = "https://sora.chatgpt.com/api/auth/session"
        tx = await page_fetch_json(sess.pw_ctx.page, url=url, method="GET", headers={"Accept": "application/json"}, json_data=None, log_file=log_file)
        data = tx.get("_json")
        await sess.pw_ctx.disconnect_playwright_only()
        if not isinstance(data, dict):
            raise RuntimeError(f"读取 session 失败：status={tx.get('status')} body={safe_trim(str(tx.get('response_body') or ''), 500)!r}")

        access_token = str(data.get("accessToken") or "").strip() or None
        expires = str(data.get("expires") or "").strip() or None
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        email = str((user or {}).get("email") or "").strip() or None
        if not access_token:
            raise RuntimeError("读取 session 失败：响应缺少 accessToken（请确认窗口已登录 Sora）")
        return {"access_token": access_token, "expires": expires, "email": email}


async def _pw_get_user_agent(page) -> Optional[str]:
    try:
        ua = await page.evaluate("() => navigator.userAgent")
        ua = str(ua or "").strip()
        return ua or None
    except Exception:
        return None


async def _sora_extract_bearer_from_any_post_pw(page, *, timeout_seconds: float, log_file: Path) -> Dict[str, Any]:
    """监听任意 POST/GET 请求，从 headers 提取 Authorization: Bearer <token>。"""
    result: Dict[str, Any] = {"seen": False, "authorization": None, "token": None, "user_agent": None, "url": None}
    loop = asyncio.get_running_loop()
    fut: "asyncio.Future[Dict[str, Any]]" = loop.create_future()

    def _on_request(req) -> None:
        if fut.done():
            return
        try:
            m = str(getattr(req, "method", "") or "").upper().strip()
            if m not in ("POST", "GET"):
                return
            headers = getattr(req, "headers", None) or {}
            auth = headers.get("authorization") or headers.get("Authorization")
            if not auth:
                return
            auth = str(auth).strip()
            if not auth.lower().startswith("bearer "):
                return
            tok = auth.split(" ", 1)[1].strip()
            if not tok:
                return
            ua = headers.get("user-agent") or headers.get("User-Agent")
            try:
                u = str(getattr(req, "url", "") or "")
            except Exception:
                u = ""
            fut.set_result(
                {
                    "seen": True,
                    "authorization": auth,
                    "token": tok,
                    "user_agent": str(ua or "") or None,
                    "url": u or None,
                }
            )
        except Exception:
            return

    try:
        page.on("request", _on_request)
    except Exception as e:
        append_log(log_file, f"[sora][token] attach request listener failed: {e}")
        return result

    try:
        data = await asyncio.wait_for(fut, timeout=max(1.0, float(timeout_seconds)))
        result.update(data or {})
        append_log(
            log_file,
            f"[sora][token] bearer_captured url={safe_trim(str(result.get('url') or ''), 240)!r} "
            f"ua={safe_trim(str(result.get('user_agent') or ''), 120)!r} "
            f"auth={_mask_secret(str(result.get('authorization') or ''), head=15, tail=15)!r}",
        )
        return result
    except Exception as e:
        append_log(log_file, f"[sora][token] bearer capture timeout/failed: {e}")
        return result
    finally:
        try:
            page.off("request", _on_request)
        except Exception:
            pass


async def _sora_generate_sentinel_token_in_fp_context_pw(page, *, device_id: Optional[str], log_file: Path) -> Optional[str]:
    """在“指纹浏览器的同一 context”中，用 __sentinel__ hack 生成 SentinelToken。"""
    try:
        ctx = page.context
    except Exception:
        ctx = None
    if ctx is None:
        append_log(log_file, "[sora][sentinel] page.context unavailable")
        return None

    did = (device_id or "").strip() if device_id else ""
    if not did:
        try:
            cookies = await ctx.cookies("https://sora.chatgpt.com")
        except Exception:
            cookies = []
        for c in cookies or []:
            try:
                if str(c.get("name") or "") == "oai-did" and c.get("value"):
                    did = str(c["value"])
                    break
            except Exception:
                continue
    if not did:
        did = str(uuid4())

    inject_html = "<!DOCTYPE html><html><head><script src=\"https://chatgpt.com/backend-api/sentinel/sdk.js\"></script></head><body></body></html>"

    try:
        p2 = await ctx.new_page()
    except Exception as e:
        append_log(log_file, f"[sora][sentinel] new_page failed: {e}")
        return None

    async def handle_route(route):
        try:
            url = str(route.request.url or "")
        except Exception:
            url = ""
        try:
            if "__sentinel__" in url:
                await route.fulfill(status=200, content_type="text/html", body=inject_html)
                return
            if ("/sentinel/" in url) or ("chatgpt.com" in url) or ("sora.chatgpt.com" in url):
                await route.continue_()
                return
            await route.abort()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        try:
            await p2.route("**/*", handle_route)
        except Exception:
            pass
        append_log(log_file, f"[sora][sentinel] loading sdk under did={did!r}")
        await p2.goto("https://sora.chatgpt.com/__sentinel__", wait_until="load", timeout=60_000)
        await p2.wait_for_function("typeof SentinelSDK !== 'undefined' && typeof SentinelSDK.token === 'function'", timeout=60_000)
        token = await p2.evaluate(
            """async (did) => {
              try {
                return await SentinelSDK.token('sora_2_create_task', did);
              } catch (e) {
                return 'ERROR: ' + (e && e.message ? e.message : String(e));
              }
            }""",
            did,
        )
        token_s = str(token or "")
        if token_s and not token_s.startswith("ERROR"):
            append_log(log_file, f"[sora][sentinel] token_ok value={_mask_secret(token_s, head=15, tail=15)!r}")
            return token_s
        append_log(log_file, f"[sora][sentinel] token_error value={safe_trim(token_s, 600)!r}")
        return None
    except Exception as e:
        append_log(log_file, f"[sora][sentinel] generate failed: {e}")
        return None
    finally:
        try:
            await p2.close()
        except Exception:
            pass


def _guess_image_filename_and_mime(headers: Dict[str, str], *, default_name: str = "image.png") -> Tuple[str, str]:
    ct = ""
    try:
        ct = str((headers or {}).get("content-type") or (headers or {}).get("Content-Type") or "").lower()
    except Exception:
        ct = ""
    if "image/jpeg" in ct or "image/jpg" in ct:
        return "image.jpg", "image/jpeg"
    if "image/webp" in ct:
        return "image.webp", "image/webp"
    if "image/png" in ct:
        return "image.png", "image/png"
    return default_name, "image/png"


def _prepare_first_frame_image_for_upload(
    image_bytes: bytes,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    target_bytes: int = 1_900_000,
) -> Tuple[bytes, str, str]:
    """将首帧图规范化为 JPEG/PNG，并尽量在清晰度优先前提下压到目标大小。"""
    try:
        from PIL import Image, ImageFile, ImageOps  # type: ignore[import-not-found]
    except Exception as e:
        raise NonPenalizedTaskError(f"图片处理依赖缺失：请安装 Pillow。err={e}") from e

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    soft_max_pixels = 48_000_000
    hard_max_pixels = 512_000_000

    if not image_bytes:
        raise NonPenalizedTaskError("首帧图片下载失败：下载内容为空")
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须大于 0")

    # 给上传留少量缓冲，避免边界抖动。
    limit_bytes = min(int(max_bytes), int(target_bytes) if int(target_bytes) > 0 else int(max_bytes))

    _NATIVE_FORMATS = {"JPEG", "JPG", "PNG"}
    # MPO（Multi-Picture Object）本质上是带 MPF 扩展的 JPEG，很多手机/相机会把它保存成 .jpg。
    # Pillow 会按文件内容识别为 "MPO"；上传前重编码为普通 JPEG，剥离多帧/MPF 数据。
    _CONVERTIBLE_FORMATS = {"WEBP", "BMP", "TIFF", "GIF", "MPO"}
    _ALL_SUPPORTED = _NATIVE_FORMATS | _CONVERTIBLE_FORMATS

    try:
        previous_max_pixels = getattr(Image, "MAX_IMAGE_PIXELS", None)
        Image.MAX_IMAGE_PIXELS = hard_max_pixels
        with Image.open(io.BytesIO(image_bytes)) as src:
            source_format = str(src.format or "").upper()
            if source_format not in _ALL_SUPPORTED:
                raise NonPenalizedTaskError(
                    f"不支持的首帧图片格式：检测到 {source_format!r}，"
                    f"仅支持 {'/'.join(sorted(_ALL_SUPPORTED))}（注意：格式由文件内容决定，而非 URL 扩展名）"
                )
            width, height = src.size
            total_pixels = int(width) * int(height)
            if total_pixels > hard_max_pixels:
                raise NonPenalizedTaskError(
                    f"首帧图片分辨率过高：{width}x{height}（{total_pixels} 像素），"
                    "请更换更小图片后重试。"
                )
            if source_format in {"JPEG", "JPG"} and total_pixels > soft_max_pixels:
                scale = (soft_max_pixels / float(total_pixels)) ** 0.5
                draft_size = (
                    max(1, int(width * scale)),
                    max(1, int(height * scale)),
                )
                # 对超大 JPEG 先在解码阶段降采样，避免占用过高内存。
                src.draft("RGB", draft_size)
            if source_format == "MPO":
                try:
                    src.seek(0)
                except Exception:
                    pass
            src.load()
            base_img = ImageOps.exif_transpose(src)
            if (base_img.size[0] * base_img.size[1]) > soft_max_pixels:
                scale = (soft_max_pixels / float(base_img.size[0] * base_img.size[1])) ** 0.5
                resized = (
                    max(1, int(base_img.size[0] * scale)),
                    max(1, int(base_img.size[1] * scale)),
                )
                base_img = base_img.resize(resized, Image.Resampling.LANCZOS)
    except NonPenalizedTaskError:
        raise
    except Exception as e:
        raise NonPenalizedTaskError(f"首帧图片解析失败（请检查图片地址/图片内容）：err={e}") from e
    finally:
        try:
            Image.MAX_IMAGE_PIXELS = previous_max_pixels
        except Exception:
            pass

    if source_format in ("PNG",):
        preferred_format = "PNG"
    else:
        preferred_format = "JPEG"

    def _save_bytes(img: Any, fmt: str, *, quality: Optional[int] = None) -> bytes:
        buf = io.BytesIO()
        if fmt == "JPEG":
            out = img.convert("RGB")
            q = int(quality if quality is not None else 88)
            out.save(buf, format="JPEG", quality=max(40, min(95, q)), optimize=True, progressive=True)
        elif fmt == "PNG":
            out = img.convert("RGBA") if ("A" in (img.mode or "")) else img.convert("RGB")
            out.save(buf, format="PNG", optimize=True, compress_level=9)
        else:
            raise ValueError(f"unsupported format: {fmt}")
        return buf.getvalue()

    def _fit_by_quality_and_scale(img: Any, fmt: str) -> Optional[bytes]:
        # 先固定分辨率降质量，不够再逐步降分辨率，尽量保清晰度。
        quality_steps = [92, 88, 84, 80, 76, 72, 68]
        min_long_edge = 1024
        max_rounds = 8
        current = img
        for _ in range(max_rounds):
            if fmt == "JPEG":
                for q in quality_steps:
                    out_b = _save_bytes(current, fmt, quality=q)
                    if len(out_b) <= limit_bytes:
                        return out_b
            else:
                out_b = _save_bytes(current, fmt)
                if len(out_b) <= limit_bytes:
                    return out_b

            w, h = current.size
            long_edge = max(w, h)
            if long_edge <= min_long_edge:
                break
            scale = 0.9
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))
            if new_w == w and new_h == h:
                break
            current = current.resize((new_w, new_h), Image.Resampling.LANCZOS)
        return None

    # 原始字节已经小于阈值且本身就是可直接上传的原生 JPEG/PNG 时，直接透传原图，避免不必要损失。
    # WEBP/BMP/TIFF/GIF/MPO 等可转换格式必须重编码；否则会出现“扩展名/mime 是 jpg，但内容仍是原格式”的问题。
    if len(image_bytes) <= limit_bytes and source_format in _NATIVE_FORMATS:
        if preferred_format == "JPEG":
            return image_bytes, "image.jpg", "image/jpeg"
        return image_bytes, "image.png", "image/png"

    out_bytes = _fit_by_quality_and_scale(base_img, preferred_format)
    out_format = preferred_format

    # PNG 在大图下经常难压，必要时回退到 JPEG（白底），保持可用性。
    if out_bytes is None and preferred_format == "PNG":
        rgb_bg = Image.new("RGB", base_img.size, (255, 255, 255))
        alpha = base_img.getchannel("A") if "A" in (base_img.mode or "") else None
        rgb_bg.paste(base_img.convert("RGB"), mask=alpha)
        out_bytes = _fit_by_quality_and_scale(rgb_bg, "JPEG")
        out_format = "JPEG"

    if out_bytes is None or len(out_bytes) > limit_bytes:
        raise NonPenalizedTaskError(
            f"首帧图片过大：压缩后仍超过限制（限制 {max_bytes} bytes，建议更换更小图片）"
        )

    if out_format == "JPEG":
        return out_bytes, "image.jpg", "image/jpeg"
    return out_bytes, "image.png", "image/png"


def _download_bytes_local(
    url: str,
    *,
    timeout_seconds: float = 30.0,
    user_agent: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> Tuple[bytes, Dict[str, str]]:
    """本地下载资源（按当前实现：不走指纹浏览器下载首帧图）。"""
    u = str(url or "").strip()
    if not u:
        raise ValueError("url 不能为空")
    # 对 URL 中的非 ASCII 字符（如中文文件名）进行百分号编码，避免 ascii codec 报错
    parts = urlsplit(u)
    # quote 的 safe 必须包含 %，否则会把已存在的 %XX（如签名里的 %3D）二次编码成 %253D，导致 CDN 403。
    u = urlunsplit(parts._replace(
        path=quote(parts.path, safe="/:@!$&'()*+,;=-._~%"),
        query=quote(parts.query, safe="/:@!$&'()*+,;=-._~?%"),
    ))
    headers = {"Accept": "*/*"}
    if user_agent:
        headers["User-Agent"] = str(user_agent)
    req = Request(u, headers=headers, method="GET")
    with urlopen(req, timeout=max(1.0, float(timeout_seconds))) as resp:
        try:
            hdrs = dict(getattr(resp, "headers", {}) or {})
        except Exception:
            hdrs = {}
        total = 0
        chunks: list[bytes] = []
        if max_bytes is not None:
            try:
                cl = hdrs.get("Content-Length") or hdrs.get("content-length")
                if cl is not None and int(cl) > int(max_bytes):
                    raise RuntimeError(f"下载资源大小超过限制：Content-Length={cl} max_bytes={max_bytes}")
            except RuntimeError:
                raise
            except Exception:
                pass
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > int(max_bytes):
                raise RuntimeError(f"下载资源大小超过限制：max_bytes={max_bytes}")
            chunks.append(chunk)
        return b"".join(chunks), hdrs


async def _download_bytes_local_async(
    url: str,
    *,
    timeout_seconds: float = 30.0,
    user_agent: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> Tuple[bytes, Dict[str, str]]:
    """异步包装：将本地阻塞下载放入线程池，避免阻塞事件循环。"""
    return await asyncio.to_thread(
        _download_bytes_local,
        url,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
        max_bytes=max_bytes,
    )


REMOTE_IMAGE_MAX_BYTES = 10 * 1024 * 1024


async def _download_remote_image_bytes_with_limit_async(
    url: str,
    *,
    label: str,
    timeout_seconds: float = 30.0,
    user_agent: Optional[str] = None,
    max_bytes: int = REMOTE_IMAGE_MAX_BYTES,
) -> Tuple[bytes, Dict[str, str]]:
    """下载远程图片并在下载阶段提前拦截超大文件，避免后续 Pillow 解码超限。"""
    try:
        img_bytes, img_headers = await _download_bytes_local_async(
            url,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            max_bytes=max_bytes,
        )
    except Exception as e:
        msg = str(e or "")
        if "max_bytes=" in msg or "Content-Length=" in msg:
            limit_mb = max(1, int(max_bytes) // (1024 * 1024))
            raise NonPenalizedTaskError(
                f"{label}包含违规内容：图片大小超过 {limit_mb}MB 限制，请更换更小图片后重试。"
                f" url={safe_trim(str(url), 400)!r}",
                status_code=400,
                content_violation=True,
            ) from e
        raise
    if len(img_bytes) > int(max_bytes):
        limit_mb = max(1, int(max_bytes) // (1024 * 1024))
        raise NonPenalizedTaskError(
            f"{label}包含违规内容：图片大小超过 {limit_mb}MB 限制，请更换更小图片后重试。"
            f" url={safe_trim(str(url), 400)!r}",
            status_code=400,
            content_violation=True,
        )
    return img_bytes, img_headers


async def _prepare_first_frame_image_for_upload_async(
    image_bytes: bytes,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    target_bytes: int = 1_900_000,
) -> Tuple[bytes, str, str]:
    """异步包装：将 CPU 密集型图片压缩放入线程池。"""
    return await asyncio.to_thread(
        _prepare_first_frame_image_for_upload,
        image_bytes,
        max_bytes=max_bytes,
        target_bytes=target_bytes,
    )


def _write_bytes_to_tempfile_local(data: bytes, *, suffix: str) -> Path:
    """将字节写入临时文件并返回路径。"""
    fd = tempfile.NamedTemporaryFile(delete=False, suffix=str(suffix or ""))
    tmp_path = Path(fd.name)
    try:
        fd.close()
        with open(tmp_path, "wb") as f:
            f.write(bytes(data or b""))
        return tmp_path
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)  # type: ignore[call-arg]
        except Exception:
            try:
                os.unlink(str(tmp_path))
            except Exception:
                pass
        raise


async def _write_bytes_to_tempfile_local_async(data: bytes, *, suffix: str) -> Path:
    """异步包装：将阻塞文件写入放入线程池。"""
    return await asyncio.to_thread(_write_bytes_to_tempfile_local, data, suffix=suffix)


def _download_to_tempfile_local(
    url: str,
    *,
    suffix: str,
    timeout_seconds: float = 60.0,
    user_agent: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> Tuple[Path, Dict[str, str]]:
    """本地下载资源到临时文件（用于在浏览器上下文以 File 上传）。"""
    u = str(url or "").strip()
    if not u:
        raise ValueError("url 不能为空")

    headers = {"Accept": "*/*"}
    if user_agent:
        headers["User-Agent"] = str(user_agent)
    req = Request(u, headers=headers, method="GET")

    fd = tempfile.NamedTemporaryFile(delete=False, suffix=str(suffix or ""))
    tmp_path = Path(fd.name)
    try:
        fd.close()
        with urlopen(req, timeout=max(1.0, float(timeout_seconds))) as resp:
            try:
                hdrs = dict(getattr(resp, "headers", {}) or {})
            except Exception:
                hdrs = {}
            with open(tmp_path, "wb") as f:
                total = 0
                try:
                    if max_bytes is not None:
                        cl = hdrs.get("Content-Length") or hdrs.get("content-length")
                        if cl is not None:
                            try:
                                if int(cl) > int(max_bytes):
                                    raise RuntimeError(f"下载资源大小超过限制：Content-Length={cl} max_bytes={max_bytes}")
                            except Exception:
                                pass
                except Exception:
                    # 忽略 content-length 解析问题
                    pass
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    if max_bytes is not None:
                        total += len(chunk)
                        if total > int(max_bytes):
                            raise RuntimeError(f"下载资源大小超过限制：max_bytes={max_bytes}")
                    f.write(chunk)
        return tmp_path, hdrs
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)  # type: ignore[call-arg]
        except Exception:
            try:
                os.unlink(str(tmp_path))
            except Exception:
                pass
        raise


async def _download_to_tempfile_local_async(
    url: str,
    *,
    suffix: str,
    timeout_seconds: float = 60.0,
    user_agent: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> Tuple[Path, Dict[str, str]]:
    """异步包装：将阻塞下载到临时文件放入线程池。"""
    return await asyncio.to_thread(
        _download_to_tempfile_local,
        url,
        suffix=suffix,
        timeout_seconds=timeout_seconds,
        user_agent=user_agent,
        max_bytes=max_bytes,
    )


def _mp4_duration_seconds(path: Path) -> float:
    """解析 MP4 容器时长（秒）。

    仅用于“角色视频”校验（≤5秒），不引入第三方依赖。
    """
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"视频文件不存在：{p}")
    size = p.stat().st_size
    if size <= 0:
        raise RuntimeError("视频文件为空")

    CONTAINERS = {
        b"moov",
        b"trak",
        b"mdia",
        b"minf",
        b"stbl",
        b"edts",
        b"udta",
        b"meta",
        b"ilst",
        b"moof",
    }

    def _read(n: int) -> bytes:
        b = f.read(n)
        if len(b) != n:
            raise EOFError("unexpected eof")
        return b

    def _u32(b: bytes) -> int:
        return int.from_bytes(b, "big", signed=False)

    def _u64(b: bytes) -> int:
        return int.from_bytes(b, "big", signed=False)

    with open(p, "rb") as f:
        # 用栈模拟递归进入 container box
        stack = [int(size)]
        while stack and f.tell() < stack[-1]:
            start = f.tell()
            try:
                hdr = f.read(8)
            except Exception:
                break
            if len(hdr) < 8:
                break

            box_size = _u32(hdr[0:4])
            box_type = hdr[4:8]
            header_size = 8
            if box_size == 1:
                try:
                    box_size = _u64(_read(8))
                except Exception:
                    break
                header_size = 16
            elif box_size == 0:
                # extends to end of container
                box_size = int(stack[-1] - start)

            if box_size < header_size:
                # 无效 box，避免死循环
                break

            box_end = int(start + box_size)

            if box_type == b"mvhd":
                try:
                    version_flags = _read(4)
                    version = version_flags[0]
                    if version == 1:
                        _read(8)  # creation_time
                        _read(8)  # modification_time
                        timescale = _u32(_read(4))
                        duration = _u64(_read(8))
                    else:
                        _read(4)  # creation_time
                        _read(4)  # modification_time
                        timescale = _u32(_read(4))
                        duration = _u32(_read(4))
                    if timescale <= 0:
                        raise RuntimeError("timescale 无效")
                    return float(duration) / float(timescale)
                except Exception as e:
                    raise RuntimeError(f"解析 mvhd 失败：{e}")

            if box_type in CONTAINERS:
                # 进入 container：meta 需要跳过 4 bytes version/flags
                if box_type == b"meta":
                    try:
                        _read(4)
                    except Exception:
                        break
                stack.append(box_end)
                continue

            # 跳过 box 内容
            try:
                f.seek(box_end)
            except Exception:
                break

            # 弹出已结束的 container
            while stack and f.tell() >= stack[-1]:
                stack.pop()

    raise RuntimeError("无法解析 MP4 时长（未找到 mvhd）")


async def _sora_api_upload_file_via_input_fetch_pw(
    page,
    *,
    upload_url: str,
    bearer_token: str,
    file_path: Path,
    file_field_name: str = "file",
    extra_fields: Optional[Dict[str, str]] = None,
    log_file: Path,
    timeout_ms: int = 120_000,
) -> Tuple[int, str]:
    """在页面内用 `<input type=file>` + `fetch(FormData)` 上传本地文件（可带 Authorization 头）。

    说明：
    - 避免把大文件 bytes/base64 传入 page.evaluate（视频可能很大）。
    - 通过指纹浏览器窗口网络栈发起请求（credentials: include）。
    """
    p = Path(file_path)
    if not p.exists():
        raise RuntimeError(f"本地文件不存在：{p}")

    try:
        ctx = page.context
    except Exception:
        ctx = None
    if ctx is None:
        append_log(log_file, "[sora][upload] page.context unavailable")
        raise RuntimeError("page.context unavailable")

    try:
        upload_page = await ctx.new_page()
    except Exception as e:
        append_log(log_file, f"[sora][upload] new_page failed: {e}")
        raise RuntimeError(f"new_page 失败：{e}")

    try:
        # 关键：必须让上传发生在与 upload_url 同源的站点上下文，否则 fetch 可能被 CORS 以 “Failed to fetch” 拦截。
        try:
            up = urlparse(str(upload_url))
            up_host = str(up.netloc or "").strip().lower()
            up_scheme = str(up.scheme or "https").strip().lower() or "https"
        except Exception:
            up_host = ""
            up_scheme = "https"

        if up_host:
            try:
                await upload_page.goto(f"{up_scheme}://{up_host}/drafts", wait_until="domcontentloaded")
            except Exception as e:
                append_log(log_file, f"[sora][upload] goto same-origin failed: {e}")

        # 注入一个隐藏 input，供 set_input_files 使用
        input_id = "fp_upload_input"
        try:
            await upload_page.evaluate(
                """(args) => {
                  const { inputId } = args;
                  let el = document.getElementById(inputId);
                  if (!el) {
                    el = document.createElement('input');
                    el.type = 'file';
                    el.id = inputId;
                    el.style.position = 'fixed';
                    el.style.left = '-9999px';
                    el.style.top = '-9999px';
                    document.body.appendChild(el);
                  }
                }""",
                {"inputId": input_id},
            )
        except Exception as e:
            raise RuntimeError(f"注入 file input 失败：{e}")

        try:
            loc = upload_page.locator(f"#{input_id}")
            await loc.wait_for(state="attached", timeout=5_000)
            await loc.set_input_files(str(p), timeout=timeout_ms)
        except Exception as e:
            raise RuntimeError(f"set_input_files 失败：{e}")

        ef = extra_fields or {}
        res = await upload_page.evaluate(
            """async (args) => {
              const { uploadUrl, bearer, fieldName, extraFields, inputId } = args;
              try {
                const input = document.getElementById(inputId);
                const file = input && input.files && input.files[0];
                if (!file) return { status: 0, text: 'missing file' };
                const fd = new FormData();
                fd.append(fieldName || 'file', file, file.name || 'file.bin');
                const extras = extraFields || {};
                for (const k of Object.keys(extras)) {
                  try { fd.append(k, String(extras[k])); } catch (e) {}
                }
                try {
                  const resp = await fetch(uploadUrl, {
                    method: 'POST',
                    headers: { 'Authorization': 'Bearer ' + bearer },
                    body: fd,
                    credentials: 'include',
                  });
                  const text = await resp.text();
                  return { status: resp.status, text };
                } catch (e) {
                  return { status: 0, text: 'fetch_error: ' + (e && e.message ? e.message : String(e)) };
                }
              } catch (e) {
                return { status: 0, text: 'js_error: ' + (e && e.message ? e.message : String(e)) };
              }
            }""",
            {
                "uploadUrl": str(upload_url),
                "bearer": str(bearer_token),
                "fieldName": str(file_field_name),
                "extraFields": dict(ef),
                "inputId": input_id,
            },
        )
        status = int((res or {}).get("status") or 0)
        text = str((res or {}).get("text") or "")
        append_log(log_file, f"[sora][upload] url={str(upload_url)!r} status={status} body={safe_trim(text, 600)!r}")
        return status, text
    finally:
        try:
            await upload_page.close()
        except Exception:
            pass


async def _sora_api_upload_image_bytes_pw(
    page,
    *,
    target_url: str,
    bearer_token: str,
    image_bytes: bytes,
    filename: str,
    mime_type: str,
    log_file: Path,
) -> str:
    """上传首帧图片获取 media_id。"""
    candidates = [
        _sora_backend_url_from_target(target_url, "/backend/uploads"),
    ]
    try:
        b64 = base64.b64encode(image_bytes or b"").decode("ascii")
    except Exception:
        b64 = ""
    if not b64:
        raise RuntimeError("首帧图片为空或 base64 编码失败")

    last_err: Optional[str] = None
    for upload_url in candidates:
        try:
            res = await page.evaluate(
                """async (args) => {
                  const { uploadUrl, bearer, filename, mime, b64 } = args;
                  const bin = atob(b64);
                  const bytes = new Uint8Array(bin.length);
                  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                  const blob = new Blob([bytes], { type: mime || 'image/png' });
                  const file = new File([blob], filename || 'image.png', { type: mime || 'image/png' });
                  const fd = new FormData();
                  fd.append('file', file, filename || 'image.png');
                  fd.append('file_name', filename || 'image.png');
                  const resp = await fetch(uploadUrl, {
                    method: 'POST',
                    headers: { 'Authorization': 'Bearer ' + bearer },
                    body: fd,
                    credentials: 'include',
                  });
                  const text = await resp.text();
                  return { status: resp.status, text };
                }""",
                {"uploadUrl": upload_url, "bearer": bearer_token, "filename": filename, "mime": mime_type, "b64": b64},
            )
            status = int((res or {}).get("status") or 0)
            text = str((res or {}).get("text") or "")
            append_log(log_file, f"[sora][upload] url={upload_url!r} status={status} body={safe_trim(text, 1000)!r}")
            if status not in (200, 201):
                last_err = f"status={status} body={safe_trim(text, 2000)}"
                continue
            try:
                obj = json.loads(text) if text else {}
            except Exception:
                obj = {}
            media_id = str((obj or {}).get("id") or "").strip()
            if media_id:
                return media_id
            last_err = f"missing id body={safe_trim(text, 2000)}"
        except Exception as e:
            last_err = str(e)
            continue

    raise RuntimeError(f"上传首帧失败：{last_err}")


async def _sora_api_upload_character_video_pw(
    page,
    *,
    target_url: str,
    bearer_token: str,
    video_path: Path,
    timestamps: str,
    log_file: Path,
) -> str:
    """POST /characters/upload（multipart）：上传视频创建 cameo，返回 cameo_id。"""
    # sora2api 配置 base_url 默认为 /backend，因此这里优先尝试 /backend/characters/upload
    candidates = [
        _sora_backend_url_from_target(target_url, "/backend/characters/upload"),
    ]

    last_err: Optional[str] = None
    for upload_url in candidates:
        try:
            status, text = await _sora_api_upload_file_via_input_fetch_pw(
                page,
                upload_url=upload_url,
                bearer_token=bearer_token,
                file_path=Path(video_path),
                file_field_name="file",
                extra_fields={"timestamps": str(timestamps or "0,3")},
                log_file=log_file,
                timeout_ms=180_000,
            )
            if status not in (200, 201):
                last_err = f"url={upload_url!r} status={status} body={safe_trim(text, 600)}"
                continue
            try:
                obj = json.loads(text) if text else {}
            except Exception:
                obj = {}
            cameo_id = str((obj or {}).get("id") or "").strip()
            if cameo_id:
                return cameo_id
            last_err = f"url={upload_url!r} missing id body={safe_trim(text, 600)}"
        except Exception as e:
            last_err = f"url={upload_url!r} err={e}"
            continue

    raise RuntimeError(f"上传角色视频失败：{last_err}")


async def _sora_api_upload_character_image_pw(
    page,
    *,
    target_url: str,
    bearer_token: str,
    image_path: Path,
    log_file: Path,
) -> str:
    """POST /project_y/file/upload：上传头像，返回 asset_pointer。"""
    candidates = [
        _sora_backend_url_from_target(target_url, "/backend/project_y/file/upload"),
    ]

    last_err: Optional[str] = None
    for upload_url in candidates:
        try:
            status, text = await _sora_api_upload_file_via_input_fetch_pw(
                page,
                upload_url=upload_url,
                bearer_token=bearer_token,
                file_path=Path(image_path),
                file_field_name="file",
                extra_fields={"use_case": "profile"},
                log_file=log_file,
                timeout_ms=120_000,
            )
            if status not in (200, 201):
                last_err = f"url={upload_url!r} status={status} body={safe_trim(text, 600)}"
                continue
            try:
                obj = json.loads(text) if text else {}
            except Exception:
                obj = {}
            asset_pointer = str((obj or {}).get("asset_pointer") or "").strip()
            if asset_pointer:
                return asset_pointer
            last_err = f"url={upload_url!r} missing asset_pointer body={safe_trim(text, 600)}"
        except Exception as e:
            last_err = f"url={upload_url!r} err={e}"
            continue

    raise RuntimeError(f"上传角色头像失败：{last_err}")


async def _sora_api_get_video_drafts_pw(page, *, target_url: str, bearer_token: str, limit: int, log_file: Path) -> Dict[str, Any]:
    """GET /backend/project_y/profile/drafts?limit=..."""
    url = _sora_backend_url_from_target(target_url, f"/backend/project_y/profile/drafts/v2?limit={int(limit)}")
    headers = {"Authorization": f"Bearer {bearer_token}", "OAI-Language": "en-US"}
    tx = await page_fetch_json(page, url=url, method="GET", headers=headers, json_data=None, log_file=log_file)
    obj = tx.get("_json")
    return obj if isinstance(obj, dict) else {}


async def _sora_api_post_project_y_post_pw(
    page,
    *,
    target_url: str,
    bearer_token: str,
    sentinel_token: str,
    generation_id: str,
    log_file: Path,
) -> Dict[str, Any]:
    """POST /backend/project_y/post（发布草稿，获取 post_id）。"""
    url = _sora_backend_url_from_target(target_url, "/backend/project_y/post")
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "OpenAI-Sentinel-Token": str(sentinel_token),
        "Content-Type": "application/json",
        "OAI-Language": "en-US",
    }
    payload = {"attachments_to_create": [{"generation_id": str(generation_id), "kind": "sora"}], "post_text": ""}
    tx = await page_fetch_json(page, url=url, method="POST", headers=headers, json_data=payload, log_file=log_file)
    obj = tx.get("_json")
    return obj if isinstance(obj, dict) else {}


async def _sora_ui_fill_prompt_textarea(page, *, prompt: Any, log_file: Path) -> bool:
    """模拟用户在 textarea 输入 prompt（仅输入，不点击任何按钮；发送由后续 post 接口完成）。"""
    try:
        prompt_s = str(prompt or "")
    except Exception:
        prompt_s = ""
    prompt_s = prompt_s.strip()
    if not prompt_s:
        return False

    # 选择器按“更精确 -> 更通用”降级
    selectors = [
        'textarea[data-testid="prompt-textarea"]',
        'textarea[placeholder*="Describe"]',
        "textarea",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            await loc.first.wait_for(state="visible", timeout=2500)
            try:
                await loc.first.click(timeout=1500)
            except Exception:
                # click 不是必须；focus 失败也继续尝试 fill
                pass

            # 优先用 fill（不会触发点击发送）
            await loc.first.fill(prompt_s, timeout=5000)

            ok = False
            try:
                v = await loc.first.input_value(timeout=1500)
                if str(v or "").strip():
                    ok = True
            except Exception:
                ok = False

            if ok:
                append_log(log_file, f"[sora][ui] prompt filled into textarea selector={sel!r} len={len(prompt_s)}")
                return True
        except Exception:
            continue

    append_log(log_file, "[sora][ui] prompt fill skipped: textarea not found/visible")
    return False


async def _sora_create_task_pw(
    *,
    page,
    prompt: str,
    target_url: str,
    monitor_log_path: Optional[str],
    first_image_url: Optional[str],
    orientation: str,
    n_frames: int,
    bearer_token: Optional[str] = None,
    sentinel_token: Optional[str] = None,
    user_agent: Optional[str] = None,
    oai_device_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """通过指纹浏览器环境直接调用 /backend/nf/create（不再模拟 UI 输入/点击）。"""
    log_file = Path(monitor_log_path) if monitor_log_path else (MONITOR_LOG_FILE)

    # 先生成 SentinelToken（并用于填充 OAI-Device-Id），再确保 BearerToken
    if not sentinel_token:
        sentinel_token = await _sora_generate_sentinel_token_in_fp_context_pw(page, device_id=oai_device_id, log_file=log_file)

    if not oai_device_id:
        try:
            sentinel_data = json.loads(str(sentinel_token or ""))
            if isinstance(sentinel_data, dict):
                oai_device_id = str(sentinel_data.get("id") or "").strip() or None
        except Exception:
            oai_device_id = None
    if not oai_device_id:
        oai_device_id = str(uuid4())

    bearer_info: Dict[str, Any] = {}
    if not bearer_token:
        bearer_info = await _sora_extract_bearer_from_any_post_pw(page, timeout_seconds=20.0, log_file=log_file)
        bearer_token = str(bearer_info.get("token") or "").strip() or None

    if not user_agent:
        try:
            user_agent = str((bearer_info or {}).get("user_agent") or "").strip() or None
        except Exception:
            user_agent = None
    if not user_agent:
        user_agent = await _pw_get_user_agent(page)

    if not bearer_token:
        raise RuntimeError("未能读取到Bearer token（请确保已登录且页面会发出带 Authorization 的请求）")
    if not sentinel_token:
        raise RuntimeError("未能生成 SentinelToken，触发了429")

    await _sora_ui_fill_prompt_textarea(page, prompt=prompt, log_file=log_file)

    inpaint_items: list[Dict[str, Any]] = []
    if first_image_url:
        try:
            img_bytes, img_headers = await _download_remote_image_bytes_with_limit_async(
                first_image_url,
                label="首帧图片",
                timeout_seconds=30.0,
                user_agent=user_agent,
            )
        except Exception as e:
            if isinstance(e, NonPenalizedTaskError):
                raise
            raise NonPenalizedTaskError(
                f"首帧图片下载失败（请检查图片地址是否正确/可访问）：url={safe_trim(str(first_image_url), 400)!r} err={e}"
            ) from e

        if not img_bytes:
            raise NonPenalizedTaskError(
                f"首帧图片下载失败：下载内容为空（可能图片地址错误/无权限/已过期）：url={safe_trim(str(first_image_url), 400)!r}"
            )

        # 明显不是图片的响应（常见：返回 HTML 错误页/JSON 错误体）
        ct = ""
        try:
            ct = str((img_headers or {}).get("content-type") or (img_headers or {}).get("Content-Type") or "").lower()
        except Exception:
            ct = ""
        if ("text/html" in ct) or ("application/json" in ct):
            raise NonPenalizedTaskError(
                f"首帧图片下载失败：响应 Content-Type={safe_trim(ct, 120)!r}，疑似非图片（请检查图片地址）：url={safe_trim(str(first_image_url), 400)!r}"
            )
        img_bytes, filename, mime_type = await _prepare_first_frame_image_for_upload_async(
            img_bytes,
            max_bytes=2 * 1024 * 1024,
            target_bytes=1_900_000,
        )
        media_id = await _sora_api_upload_image_bytes_pw(
            page,
            target_url=target_url,
            bearer_token=str(bearer_token),
            image_bytes=img_bytes,
            filename=filename,
            mime_type=mime_type,
            log_file=log_file,
        )
        inpaint_items = [{"kind": "upload", "upload_id": str(media_id)}]

    create_payload: Dict[str, Any] = {
        "kind": "video",
        "prompt": prompt,
        "orientation": str(orientation or "portrait"),
        "size": "small",
        "n_frames": int(_pick_n_frames(n_frames)),
        "model": "sy_8",
        "inpaint_items": inpaint_items,
        "style_id": None,
    }

    create_url = _sora_backend_url_from_target(target_url, "/backend/nf/create")
    headers: Dict[str, str] = {
        "Authorization": f"Bearer {bearer_token}",
        "OpenAI-Sentinel-Token": str(sentinel_token),
        "Content-Type": "application/json",
        "OAI-Language": "en-US",
        "OAI-Device-Id": str(oai_device_id),
    }

    append_log(log_file, "\n" + "=" * 100)
    append_log(log_file, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [sora][api] nf/create start url={create_url!r}")
    append_log(log_file, f"[sora][api] ua={safe_trim(headers.get('User-Agent') or '', 140)!r} device_id={oai_device_id!r}")
    append_log(log_file, f"[sora][api] payload={safe_trim(json.dumps(create_payload, ensure_ascii=False), 1200)!r}")

    create_tx = await page_fetch_tx(page, url=create_url, method="POST", headers=headers, json_data=create_payload, log_file=log_file)

    status = create_tx.get("status")
    body_text = create_tx.get("response_body") or ""
    payload_obj: Any = {}
    task_id = None
    try:
        payload_obj = json.loads(body_text) if body_text else {}
        task_id = (payload_obj or {}).get("id") or (payload_obj or {}).get("task_id")
    except Exception:
        task_id = None

    try:
        status_i = int(status) if status is not None else None
    except Exception:
        status_i = None

    if status_i != 200 or not task_id:
        # 400 invalid_request 里有些属于“请求本身不合法/用户输入问题”，不应计入窗口连续错误，也不需要打堆栈日志
        try:
            err = (payload_obj or {}).get("error") if isinstance(payload_obj, dict) else None
            err_code = (err or {}).get("code") if isinstance(err, dict) else None
            err_msg = (err or {}).get("message") if isinstance(err, dict) else None
        except Exception:
            err_code = None
            err_msg = None

        bt_lower = str(body_text or "").lower()
        msg_lower = str(err_msg or "").lower()
        if status_i == 400:
            cameo_code = str(err_code or "").strip()
            is_cameo_not_found = (
                cameo_code == "cameo_not_found"
                or "cameo_not_found" in bt_lower
                or "does not have a cameo" in bt_lower
                or "does not have a cameo" in msg_lower
            )
            is_cameo_permission_denied = (
                cameo_code == "cameo_permission_denied"
                or "cameo_permission_denied" in bt_lower
                or "not allowed to access one or more mentioned cameos" in bt_lower
                or "not allowed to access one or more mentioned cameos" in msg_lower
            )
            if is_cameo_not_found or is_cameo_permission_denied:
                err_kind = "cameo_not_found" if is_cameo_not_found else "cameo_permission_denied"
                raise NonPenalizedTaskError(
                    f"create 失败（{err_kind}）：{safe_trim(str(err_msg or body_text), 400)}",
                    status_code=status_i,
                )
            if "too many users mentioned" in bt_lower or "too many users mentioned" in msg_lower:
                raise NonPenalizedTaskError(
                    f"create 失败，内容违规（too_many_users_mentioned）：{safe_trim(str(err_msg or body_text), 400)}",
                    status_code=status_i,
                )

        # token 过期：交给上层做“重新获取 access_token 并重试”的流程；
        # 且该类错误不应计入窗口连续错误。
        if status_i == 401 and (
            str(err_code or "").strip() == "token_expired"
            or "token_expired" in bt_lower
            or "token is expired" in bt_lower
            or "token_expired" in msg_lower
            or "token is expired" in msg_lower
        ):
            raise NonPenalizedTaskError(
                f"create 失败（token_expired）：{safe_trim(str(err_msg or body_text), 400)}",
                status_code=status_i,
            )

        if status_i == 403 and ("just a moment" in bt_lower or "cloudflare" in bt_lower or "/cdn-cgi/" in bt_lower):
            raise NonPenalizedTaskError(
                f"create 失败（cloudflare_challenge）：{safe_trim(body_text, 400)}",
                status_code=status_i,
            )

        if "rate_limit" in bt_lower or "rate_limit_exhausted" in bt_lower:
            raise NonPenalizedTaskError(
                f"create 失败（rate_limit）：{safe_trim(body_text, 400)}",
                status_code=status_i or 429,
            )

        raise RuntimeError(f"create failed：status={status_i} body={safe_trim(body_text, 400)}")

    auth_state: Dict[str, Any] = {
        "bearer_token": bearer_token,
        "sentinel_token": sentinel_token,
        "user_agent": user_agent,
        "oai_device_id": oai_device_id,
        "create_url": create_url,
    }
    return str(task_id), create_tx, auth_state


@dataclass
class _SoraWatcher:
    task_id: str
    deadline: float
    progress_cb: ProgressCB
    future: "asyncio.Future[Dict[str, Any]]"
    start_time: float
    max_wait_seconds: float
    last_sent_progress: int = -1
    last_status: Any = None
    last_progress_pct: Optional[float] = None
    miss_pending_count: int = 0
    has_progress: bool = False


@dataclass
class SoraSession:
    """按 window 维度缓存的 Sora 会话。

说明：
- 会话内复用同一个指纹浏览器窗口与 Playwright CDP 连接。
- create 必须串行（create_lock）；页面操作互斥（pw_ctx.driver_lock）。
"""

    cache_key: str
    pw_ctx: PlaywrightBrowserContext

    last_used_at: float = field(default_factory=lambda: time.time())
    create_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _bring_drafts_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    watchers: Dict[str, _SoraWatcher] = field(default_factory=dict)
    monitor_task: Optional[asyncio.Task] = None
    idle_close_task: Optional[asyncio.Task] = None
    idle_close_disabled: bool = False

    # 监控配置（watch 时更新）
    monitor_log_path: Optional[str] = None
    poll_interval_seconds: float = 5.0
    sniff_timeout_seconds: float = 4.0
    idle_close_seconds: float = 30.0

    # 最近一次 browser_open 参数（用于重连）
    browser_open_args: list[str] = field(default_factory=list)
    browser_force_open: bool = False
    browser_headless: bool = False
    browser_pure_mode: bool = True

    # 鉴权信息（从指纹浏览器网络抓取）
    bearer_token: Optional[str] = None
    # 由管理台转换并写入 DB 的 access_token（优先使用）
    access_token: Optional[str] = None
    access_expires: Optional[str] = None
    user_agent: Optional[str] = None
    oai_device_id: Optional[str] = None
    sentinel_token: Optional[str] = None
    invite_code: Optional[str] = None
    debug_panel_seq: int = 0
    debug_panel_entries: list[Dict[str, str]] = field(default_factory=list)

    def set_access_token(self, access_token: Optional[str], expires: Optional[str] = None) -> None:
        tok = str(access_token or "").strip() or None
        exp = str(expires or "").strip() or None
        self.access_token = tok
        self.access_expires = exp
        # 兼容旧代码路径：依旧使用 bearer_token 字段生成 Authorization 头
        if tok:
            self.bearer_token = tok

    def _get_bearer_token_required(self) -> str:
        tok = str(self.access_token or "").strip() or (str(self.bearer_token or "").strip() or "")
        if not tok:
            raise RuntimeError("缺少 access_token（请在任务类型管理页为该窗口转换并保存 access_token）")
        return tok

    async def _push_debug_progress(self, page: Any, text: str, *, level: str = "info") -> None:
        """向页面插件弹窗写入调试步骤；同一页面始终复用单个面板。"""
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
        # 仅保留最近 80 条，避免面板无限增长影响页面性能。
        if len(self.debug_panel_entries) > 80:
            self.debug_panel_entries = self.debug_panel_entries[-80:]
        payload = {
            "title": "Sora 调试进度",
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "entries": list(self.debug_panel_entries),
        }
        script = _build_debug_progress_panel_script()
        try:
            await page.evaluate(script, payload)
        except Exception:
            pass

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
        # 轮询等待时优先用 fast 判断；只有最终确认时才 deep 看 HTML（更耗时）
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

    async def _raise_if_cloudflare_page_nonpenalized(
        self, page, *, stage: str, drafts_url: str = "https://sora.chatgpt.com/drafts"
    ) -> None:
        """若为 Cloudflare 或未登录类提示，则最多 2 次：先尝试登录/错误页自愈，再 bring drafts；仍判定为 CF 再抛 NonPenalizedTaskError。"""
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            cur = self.pw_ctx.page
            for _ in range(2):
                is_cf = await self._is_cloudflare_page(cur, deep=False)
                if not is_cf and not await self._drafts_page_suggests_login_recovery(cur):
                    return
                await self._try_resolve_drafts_login_prompt_if_needed(cur, drafts_url)
                await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
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

        根因：
        - Turnstile iframe 在主页 closed shadow-root 内，query_selector 找不到
        - iframe 内部还有一层 closed shadow-root 包裹 checkbox
        - wait_for_selector 无法穿透 closed shadow-root，会超时

        策略：
        1. frame.locator()：CDP 原生可穿透 shadow-root，尝试直接点击
        2. 坐标法：frame_element().bounding_box() 拿到 iframe 屏幕位置，
           模拟拟人化鼠标移动后 mouse.click() 点击 checkbox 坐标

        返回 True 表示触发了点击动作，False 表示完全失败。
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
            # --- 找到 CF iframe frame ---
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

            # --- 策略1：frame.locator()（CDP 可穿 closed shadow-root）---
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

            # --- 策略2：坐标法 + 拟人化鼠标移动 ---
            # Cloudflare Turnstile widget 固定为 300×65px
            # checkbox 位于 widget 左侧，水平约 26px，垂直居中
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

                # 从随机偏移位置平滑移入，模拟人手移动
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
        # 两档等待：点击后给 Cloudflare 3s 处理时间；未点击时 1s 轮询
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

            # 尝试点击 Cloudflare Turnstile checkbox
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
            # 点击成功后多等一会，给 Cloudflare 时间处理；否则短轮询
            sleep_sec = poll_after_click if clicked else poll_idle
            try:
                await asyncio.sleep(min(sleep_sec, max(0.1, remain)))
            except Exception:
                break
        return True

    async def _restart_window_and_restore_single_drafts(self, *, drafts_url: str, sora_host: str) -> Any:
        """关闭并重开指纹浏览器窗口（仅打开窗口，不连接 CDP/不查找页面）。"""
        log_file = (
            Path(self.monitor_log_path)
            if self.monitor_log_path
            else (MONITOR_LOG_FILE)
        )
        try:
            append_log(log_file, "[sora][drafts] detected cloudflare interstitial, restarting fp window once")
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
            append_log(log_file, "[sora][drafts] reopen window only: skip cdp connect/page probing")
        except Exception:
            pass

        # browser_open + 稳定等待受并发信号量保护，按 base_url 分服务器限流
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
                    append_log(log_file, f"[sora][drafts] browser_open result code={code}")
                except Exception:
                    pass
            except Exception as e:
                try:
                    append_log(log_file, f"[sora][drafts] browser_open failed: {e}")
                except Exception:
                    pass

            # 清空旧句柄，避免 ensure_open 复用检查误判为"仍然连接"
            try:
                self.pw_ctx.browser = None
                self.pw_ctx.context = None
                self.pw_ctx.page = None
                self.pw_ctx.cdp_endpoint = None
            except Exception:
                pass

            # 等待指纹浏览器完成 Cloudflare 验证并稳定
            try:
                await asyncio.sleep(20.0)
            except Exception:
                pass

        # 重连 CDP：建立 browser+context，不操作 page
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
                append_log(log_file, f"[sora][drafts] CDP reconnect after restart failed: {e}")
            except Exception:
                pass
        return None

    async def _probe_log_in_cta_present(self, page) -> bool:
        """探测页面上是否存在 Log in 入口（不点击）。"""
        if page is None:
            return False
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
                            return True
                    loc_probe = sc.locator('button, a, [role="button"], [role="link"]').filter(has_text=login_name_re)
                    if (await loc_probe.count()) > 0:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def _drafts_page_suggests_login_recovery(self, page) -> bool:
        """草稿相关页是否呈现未登录/异常提示（用于与 Cloudflare 共用自愈循环）。"""
        if page is None:
            return False
        try:
            page_html = await page.content()
            if "Something went wrong. Please try again in a few minutes." in (page_html or ""):
                return True
        except Exception:
            pass
        return await self._probe_log_in_cta_present(page)

    async def _maybe_click_login_button_if_prompted(self, page) -> bool:
        has_login_button = False
        """尝试点击页面上的 Log in 按钮/链接（不依赖固定提示文案）。"""
        if page is None:
            return False, has_login_button

        has_login_button = await self._probe_log_in_cta_present(page)

        if not has_login_button:
            await self._push_debug_progress(page, "未发现 Log in 按钮/链接", level="info")
            return False, has_login_button
        await self._push_debug_progress(page, "发现 Log in 按钮/链接，准备点击", level="info")

        scopes: list[Any] = [page]
        try:
            for fr in list(getattr(page, "frames", []) or []):
                if fr is not page and fr not in scopes:
                    scopes.append(fr)
        except Exception:
            pass
        login_name_re = re.compile(r"log\s*in", re.IGNORECASE)

        # 点击 Log in（先 role，再文本兜底；同时遍历 page + frames）
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

    async def _try_resolve_drafts_login_prompt_if_needed(self, drafts_page, drafts_url: str) -> None:
        """若出现未登录/异常提示，尽量先触发登录（不改变「只保留 drafts 单页」的约束）。"""
        try:
            try:
                page_html = await drafts_page.content()
                if "Something went wrong. Please try again in a few minutes." in (page_html or ""):
                    await self._push_debug_progress(
                        drafts_page,
                        "检测到 Something went wrong 提示，先刷新 drafts 页面",
                        level="warn",
                    )
                    try:
                        await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
                    except Exception:
                        # 即使 goto 失败也继续，避免中断后续登录点击判断流程
                        pass
                    try:
                        await asyncio.sleep(2.0)
                    except Exception:
                        pass
            except Exception:
                pass

            clicked, has_login_button = await self._maybe_click_login_button_if_prompted(drafts_page)
            if clicked:
                try:
                    await asyncio.sleep(3.0)
                except Exception:
                    pass

                try:
                    await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
                except Exception:
                    # 即使 goto 失败，也继续尝试 bring_to_front（网络慢/被拦截时仍尽量保证窗口前置）
                    pass

            if not clicked and has_login_button:
                await self._push_debug_progress(drafts_page, "重新再试一次点击login", level="ok")
                try:
                    await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
                except Exception:
                    # 即使 goto 失败，也继续尝试 bring_to_front（网络慢/被拦截时仍尽量保证窗口前置）
                    pass

                clicked, has_login_button = await self._maybe_click_login_button_if_prompted(drafts_page)
                if clicked:
                    await self._push_debug_progress(drafts_page, "重新再试一次点击login成功", level="ok")
                else:
                    await self._push_debug_progress(drafts_page, "重新再试一次点击login失败", level="error")
        except Exception:
            pass

    async def _bring_sora_drafts_to_front(
        self,
        refresh_target=True,
        *,
        drafts_url: str = "https://sora.chatgpt.com/drafts",
        acquire_bring_lock: bool = True,
    ) -> None:
        """将目标页面置前，并尽量确保整个指纹浏览器实例只保留一个目标页面。

        需求背景：指纹浏览器可能开了多个标签页/窗口；即使 ensure_open 选中了可用 page，也不一定是目标页面。
        这里确保 drafts_url 在每次 ensure_open 后都会被 bring_to_front，
        且会关闭同一个指纹浏览器（同一 CDP 连接）内除 drafts_url 外的其它页面（包括其它站点、about:blank、新窗口、重复 drafts 等），
        尽量只保留一个目标页面以节省内存。

        acquire_bring_lock：为 False 时表示调用方已持有 ``_bring_drafts_lock``（避免与外层 ``async with`` 死锁）。
        """
        try:
            sora_host = urlparse(drafts_url).netloc.strip().lower()
        except Exception:
            sora_host = "sora.chatgpt.com"

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

            async def _snapshot_contexts_pages() -> tuple[list[Any], list[tuple[Any, Any, str]]]:
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
                        if _is_page_closed(p0):
                            continue
                        u0 = _safe_page_url(p0)
                        open_pages0.append((c0, p0, u0))
                return ctxs0, open_pages0

            async def _keep_only_one_drafts_page(keep_page: Any) -> Any:
                """关闭其它所有页面/多余 contexts，仅保留 keep_page。返回 keep_page。"""
                ctxs1, open_pages1 = await _snapshot_contexts_pages()
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

                # 先关页面：跨所有 contexts 关闭除 drafts 外的所有 page（最大化节省内存）
                for _c1, p1, _u1 in open_pages1:
                    if p1 is keep_page:
                        continue
                    try:
                        await p1.close()
                    except Exception:
                        pass

                # 再尽量关多余 context（有些 CDP 场景可能不允许 close，忽略即可）
                if keep_ctx is not None:
                    for c1 in ctxs1:
                        if c1 is keep_ctx:
                            continue
                        try:
                            await c1.close()
                        except Exception:
                            pass

                # 将 pw_ctx.context 指向保留页所在 context（若可用）
                try:
                    if keep_ctx is not None:
                        self.pw_ctx.context = keep_ctx
                except Exception:
                    pass
                return keep_page

            # 选定 drafts_page：优先复用 self.pw_ctx.page（若已是 drafts），否则从任意 context 中找 drafts；再不行则新开
            ctxs, open_pages = await _snapshot_contexts_pages()
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
                # 没有 drafts：在当前 context（若存在）或第一个 context 里新开一个
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
                    # 即使 goto 失败，也继续尝试 bring_to_front（网络慢/被拦截时仍尽量保证窗口前置）
                    pass

            await self._push_debug_progress(drafts_page, "已选定 drafts 页面，准备清理其它页面", level="info")

            # 关键需求：确保整个指纹浏览器实例只保留 drafts_page
            drafts_page = await _keep_only_one_drafts_page(drafts_page)

            # 强制将 drafts_page 置前并作为后续操作的统一 page
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
                    await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
                    await self._push_debug_progress(drafts_page, "drafts 页面刷新完成", level="ok")
                except Exception:
                    await self._push_debug_progress(drafts_page, "drafts 页面刷新失败（将继续流程）", level="warn")
                    pass

                try:
                    await drafts_page.evaluate("() => { try { window.focus(); } catch(e) {} }")
                except Exception:
                    pass

                await asyncio.sleep(3.0)

            await self._try_resolve_drafts_login_prompt_if_needed(drafts_page, drafts_url)

            # Cloudflare interstitial 自愈：先等最多 10 秒自动放行，超时仍是 Cloudflare 才重启一次。
            try:
                maybe_cf = await self._is_cloudflare_page(drafts_page, deep=False)
                if maybe_cf:
                    try:
                        await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
                    except Exception:
                        # 即使 goto 失败，也继续尝试 bring_to_front（网络慢/被拦截时仍尽量保证窗口前置）
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
                            drafts_url=drafts_url, sora_host=sora_host
                        )
                        # 重启后：从新 context 里找到 drafts page，关掉其它页面，置前
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

                            # 收集所有打开的页面
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

                            # 找到 drafts page（优先精确匹配，其次 sora_host）
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
                                    if h_n == sora_host:
                                        target_page_new = p_n
                                        break

                            if target_page_new is not None:
                                # 关掉其它页面
                                for p_n, _u_n in all_pages_new:
                                    if p_n is target_page_new:
                                        continue
                                    try:
                                        await p_n.close()
                                    except Exception:
                                        pass
                                # 更新 page 引用并置前
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
                                    drafts_page, "重启后已恢复 drafts 页面并置前", level="ok"
                                )
                        except Exception:
                            pass
            except Exception:
                pass

        if acquire_bring_lock:
            async with self._bring_drafts_lock:
                await _inner()
        else:
            await _inner()

    async def ensure_open(
        self,
        *,
        args: Optional[list[str]] = None,
        force_open: bool = False,
        headless: bool = False,
        acquire_bring_lock: bool = True,
        pure_mode: Optional[bool] = None,
    ) -> None:
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
        """先占 _bring_drafts_lock，再占 pw_ctx.driver_lock，再断开 CDP（与 bring 及 driver_lock 页面逻辑互斥）。"""
        async with self._bring_drafts_lock:
            async with self.pw_ctx.driver_lock:
                await self.pw_ctx.disconnect_playwright_only()

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
                if self.watchers:
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
        _drop_sora_session(self.cache_key)

    async def close(self) -> None:
        self._cancel_idle_close()
        t = self.monitor_task
        self.monitor_task = None
        if t and not t.done():
            t.cancel()
        await self.pw_ctx.close_and_drop()

    async def api_nf_check(self, *, target_url: str) -> Dict[str, Any]:
        """读取 Sora 余额：GET /backend/nf/check。"""
        self.last_used_at = time.time()
        # 余额查询属于“仍在使用窗口”的行为：不要触发倒计时关窗
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            url = _sora_backend_url_from_target(target_url, "/backend/nf/check")
            headers = {"Authorization": f"Bearer {token}", "OAI-Language": "en-US"}
            tx = await page_fetch_json(self.pw_ctx.page, url=url, method="GET", headers=headers, json_data=None, log_file=log_file)
            obj = tx.get("_json") or {}
            rate = (obj or {}).get("rate_limit_and_credit_balance") or {}

            remaining = int(rate.get("estimated_num_videos_remaining") or 0)
            purchased_remaining = int(rate.get("estimated_num_purchased_videos_remaining") or 0)
            resets = int(rate.get("access_resets_in_seconds") or 0)
            out: Dict[str, Any] = {
                "remaining_count": max(0, remaining),
                "purchased_remaining_count": purchased_remaining,
                "rate_limit_reached": bool(rate.get("rate_limit_reached", False)),
                "access_resets_in_seconds": resets,
                "raw": obj,
            }
            try:
                dt = datetime.now() + timedelta(seconds=max(0, int(resets or 0)))
                out["cooldown_until"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
            await self.pw_ctx.disconnect_playwright_only()
            return out

    async def api_subscription_info(self, *, target_url: str) -> Dict[str, Any]:
        """读取 Sora 订阅信息：GET /backend/billing/subscriptions。"""
        self.last_used_at = time.time()
        # 查询会员信息属于“仍在使用窗口”的行为：不要触发倒计时关窗
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless,acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            url = _sora_backend_url_from_target(target_url, "/backend/billing/subscriptions")
            headers = {"Authorization": f"Bearer {token}", "OAI-Language": "en-US"}
            tx = await page_fetch_json(self.pw_ctx.page, url=url, method="GET", headers=headers, json_data=None, log_file=log_file)
            obj = tx.get("_json") or {}
            rows = (obj or {}).get("data") or []
            first = rows[0] if isinstance(rows, list) and rows else {}
            plan = (first or {}).get("plan") if isinstance(first, dict) else {}
            plan = plan if isinstance(plan, dict) else {}
            return {
                "plan_type": str(plan.get("id") or "").strip(),
                "plan_title": str(plan.get("title") or "").strip(),
                "subscription_end": str((first or {}).get("end_ts") or "").strip(),
                "raw": obj,
            }

    async def api_clear_all_drafts(self, *, target_url: str) -> Dict[str, Any]:
        """获取 drafts/v2 列表并逐个 DELETE 删除所有草稿。"""
        self.last_used_at = time.time()
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless,acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            headers = {"Authorization": f"Bearer {token}", "OAI-Language": "en-US"}

            list_url = _sora_backend_url_from_target(target_url, "/backend/project_y/profile/drafts/v2?limit=45")
            tx = await page_fetch_json(self.pw_ctx.page, url=list_url, method="GET", headers=headers, json_data=None, log_file=log_file)
            obj = tx.get("_json")
            items = (obj or {}).get("items", []) if isinstance(obj, dict) else []

            deleted_ids: list[str] = []
            failed_ids: list[str] = []
            for it in (items if isinstance(items, list) else []):
                draft_id = str(it.get("id") or "").strip() if isinstance(it, dict) else ""
                if not draft_id:
                    continue
                del_url = _sora_backend_url_from_target(target_url, f"/backend/project_y/profile/drafts/{draft_id}")
                try:
                    del_tx = await page_fetch_json(self.pw_ctx.page, url=del_url, method="DELETE", headers=headers, json_data=None, log_file=log_file)
                    status = int(del_tx.get("status") or 0) if del_tx.get("status") is not None else 0
                    if 200 <= status < 300 or status == 404:
                        deleted_ids.append(draft_id)
                    else:
                        failed_ids.append(draft_id)
                    append_log(log_file, f"[sora][clear_drafts] DELETE {draft_id} status={status}")
                except Exception as e:
                    failed_ids.append(draft_id)
                    try:
                        append_log(log_file, f"[sora][clear_drafts] DELETE {draft_id} failed: {e}")
                    except Exception:
                        pass
            await self.pw_ctx.disconnect_playwright_only()
            return {
                "total": len(items) if isinstance(items, list) else 0,
                "deleted": deleted_ids,
                "failed": failed_ids,
            }

    async def api_invite_mine(self, *, target_url: str) -> Dict[str, Any]:
        """读取邀请码：GET /backend/project_y/invite/mine（必要时尝试 bootstrap 激活）。"""
        self.last_used_at = time.time()
        # 查询邀请码属于“仍在使用窗口”的行为：不要触发倒计时关窗
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless,acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            url = _sora_backend_url_from_target(target_url, "/backend/project_y/invite/mine")
            headers = {"Authorization": f"Bearer {token}", "OAI-Language": "en-US"}
            tx = await page_fetch_tx(self.pw_ctx.page, url=url, method="GET", headers=headers, json_data=None, log_file=log_file)
            status = int(tx.get("status") or 0) if tx.get("status") is not None else 0
            body = str(tx.get("response_body") or "")
            obj: Any = None
            try:
                obj = json.loads(body) if body else None
            except Exception:
                obj = None

            if status == 401:
                try:
                    boot_url = _sora_backend_url_from_target(target_url, "/backend/m/bootstrap")
                    await page_fetch_tx(self.pw_ctx.page, url=boot_url, method="GET", headers=headers, json_data=None, log_file=log_file)
                    tx2 = await page_fetch_json(self.pw_ctx.page, url=url, method="GET", headers=headers, json_data=None, log_file=log_file)
                    obj = tx2.get("_json")
                except Exception:
                    pass
            await self.pw_ctx.disconnect_playwright_only()
            data = obj if isinstance(obj, dict) else {}
            invite_code = (data or {}).get("invite_code")
            self.invite_code = str(invite_code).strip() if invite_code else None
            return {
                "supported": bool(self.invite_code),
                "invite_code": self.invite_code,
                "redeemed_count": int((data or {}).get("redeemed_count") or 0),
                "total_count": int((data or {}).get("total_count") or 0),
                "raw": data,
            }

    async def api_characters_upload_video(
        self,
        *,
        target_url: str,
        video_path: Path,
        timestamps: str = "0,3",
    ) -> str:
        """创建角色：POST /characters/upload，返回 cameo_id。"""
        self.last_used_at = time.time()
        self._cancel_idle_close()
        last_err: Exception | None = None
        for _attempt in range(2):
            self._cancel_idle_close()
            async with self._bring_drafts_lock:
                await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless,acquire_bring_lock=False)
                await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
                if self.pw_ctx.page is None:
                    raise RuntimeError("page 未初始化")
                log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
                token = self._get_bearer_token_required()
                try:
                    return await _sora_api_upload_character_video_pw(
                        self.pw_ctx.page,
                        target_url=target_url,
                        bearer_token=str(token),
                        video_path=Path(video_path),
                        timestamps=str(timestamps or "0,3"),
                        log_file=log_file,
                    )
                except Exception as exc:
                    last_err = exc
                    append_log(log_file, f"[sora][upload_character] 上传角色视频失败(第{_attempt + 1}次): {exc}, 尝试刷新治愈…")
            async with self._bring_drafts_lock:
                await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless,acquire_bring_lock=False)
                await self._bring_sora_drafts_to_front(acquire_bring_lock=False)
        self.disconnect_playwright_under_bring_lock()
        raise last_err  # type: ignore[misc]

    async def api_characters_from_generation(self, *, target_url: str, generation_id: str) -> Dict[str, Any]:
        """POST /backend/characters/from-generation：用 generation_id 创建 cameo，返回 cameo 对象（含 id）。"""
        self.last_used_at = time.time()
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless,acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            if self.pw_ctx.page is None:
                raise RuntimeError("page 未初始化")
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            headers: Dict[str, str] = {"Authorization": f"Bearer {token}", "OAI-Language": "en-US", "Content-Type": "application/json"}
            try:
                if self.oai_device_id:
                    headers["OAI-Device-Id"] = str(self.oai_device_id)
            except Exception:
                pass

            payload = {"generation_id": str(generation_id), "character_id": None, "timestamps": [0, 4]}
            urls = [
                _sora_backend_url_from_target(target_url, "/backend/characters/from-generation"),
            ]
            last_err: Optional[str] = None
            for url in urls:
                try:
                    tx = await page_fetch_json(self.pw_ctx.page, url=url, method="POST", headers=headers, json_data=payload, log_file=log_file)
                    status = int(tx.get("status") or 0) if tx.get("status") is not None else 0
                    obj = tx.get("_json")
                    data = obj if isinstance(obj, dict) else {}
                    if status == 404 and not data:
                        last_err = f"url={url!r} status=404"
                        continue
                    cameo_id = str((data or {}).get("id") or "").strip()
                    if cameo_id:
                        return data
                    last_err = f"url={url!r} status={status} missing id body={safe_trim(json.dumps(data, ensure_ascii=False), 600)}"
                except Exception as e:
                    last_err = f"url={url!r} err={e}"
                    continue
            await self.pw_ctx.disconnect_playwright_only()
            raise RuntimeError(f"from-generation 失败：{last_err}")

    async def api_cameo_owned_status(self, *, target_url: str, cameo_id: str) -> Dict[str, Any]:
        """GET /backend/project_y/cameos/in_progress/{cameo_id}：读取 from-generation cameo 处理进度。"""
        self.last_used_at = time.time()
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless,acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            if self.pw_ctx.page is None:
                raise RuntimeError("page 未初始化")
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            headers: Dict[str, str] = {"Authorization": f"Bearer {token}", "OAI-Language": "en-US"}
            try:
                if self.oai_device_id:
                    headers["OAI-Device-Id"] = str(self.oai_device_id)
            except Exception:
                pass

            urls = [
                _sora_backend_url_from_target(target_url, f"/backend/project_y/cameos/in_progress/{str(cameo_id)}"),
            ]
            last_err: Optional[str] = None
            for url in urls:
                try:
                    tx = await page_fetch_json(self.pw_ctx.page, url=url, method="GET", headers=headers, json_data=None, log_file=log_file)
                    obj = tx.get("_json")
                    data = obj if isinstance(obj, dict) else {}
                    status = int(tx.get("status") or 0) if tx.get("status") is not None else 0
                    if status == 404 and not data:
                        last_err = f"url={url!r} status=404"
                        continue
                    return data
                except Exception as e:
                    last_err = f"url={url!r} err={e}"
                    continue
            raise RuntimeError(f"获取 cameo owned 状态失败：{last_err}")

    async def api_username_check(self, *, target_url: str, username: str) -> Dict[str, Any]:
        """POST /backend/project_y/profile/username/check：检查 username 是否可用。"""
        self.last_used_at = time.time()
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            if self.pw_ctx.page is None:
                raise RuntimeError("page 未初始化")
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            headers: Dict[str, str] = {"Authorization": f"Bearer {token}", "OAI-Language": "en-US", "Content-Type": "application/json"}
            payload = {"username": str(username)}
            urls = [
                _sora_backend_url_from_target(target_url, "/backend/project_y/profile/username/check"),
            ]
            last_err: Optional[str] = None
            for url in urls:
                try:
                    tx = await page_fetch_json(self.pw_ctx.page, url=url, method="POST", headers=headers, json_data=payload, log_file=log_file)
                    obj = tx.get("_json")
                    data = obj if isinstance(obj, dict) else {}
                    status = int(tx.get("status") or 0) if tx.get("status") is not None else 0
                    if status == 404 and not data:
                        last_err = f"url={url!r} status=404"
                        continue
                    return data
                except Exception as e:
                    last_err = f"url={url!r} err={e}"
                    continue
            raise RuntimeError(f"username/check 失败：{last_err}")

    async def api_cameo_update_v2(self, *, target_url: str, cameo_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /backend/project_y/cameos/by_id/{cameo_id}/update_v2：更新 cameo（如 instruction_set、visibility）。"""
        self.last_used_at = time.time()
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            if self.pw_ctx.page is None:
                raise RuntimeError("page 未初始化")
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            headers = {"Authorization": f"Bearer {token}", "OAI-Language": "en-US", "Content-Type": "application/json"}
            urls = [
                _sora_backend_url_from_target(target_url, f"/backend/project_y/cameos/by_id/{str(cameo_id)}/update_v2"),
            ]
            last_err: Optional[str] = None
            for url in urls:
                try:
                    tx = await page_fetch_json(self.pw_ctx.page, url=url, method="POST", headers=headers, json_data=dict(payload or {}), log_file=log_file)
                    obj = tx.get("_json")
                    data = obj if isinstance(obj, dict) else {}
                    status = int(tx.get("status") or 0) if tx.get("status") is not None else 0
                    if status in (200, 201):
                        return data
                    if status == 404 and not data:
                        last_err = f"url={url!r} status=404"
                        continue
                    last_err = f"url={url!r} status={status} body={safe_trim(json.dumps(data, ensure_ascii=False), 500)}"
                except Exception as e:
                    last_err = f"url={url!r} err={e}"
                    continue
            raise RuntimeError(f"update_v2 失败：{last_err}")

    async def api_cameo_status(self, *, target_url: str, cameo_id: str) -> Dict[str, Any]:
        """GET /project_y/cameos/in_progress/{cameo_id}。"""
        self.last_used_at = time.time()
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            if self.pw_ctx.page is None:
                raise RuntimeError("page 未初始化")
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            headers: Dict[str, str] = {"Authorization": f"Bearer {token}", "OAI-Language": "en-US"}
            try:
                if self.oai_device_id:
                    headers["OAI-Device-Id"] = str(self.oai_device_id)
            except Exception:
                pass

            # 优先 /backend 前缀（与 sora2api base_url 一致），失败再降级
            urls = [
                _sora_backend_url_from_target(target_url, f"/backend/project_y/cameos/in_progress/{str(cameo_id)}"),
            ]
            last_err: Optional[str] = None
            for url in urls:
                try:
                    tx = await page_fetch_json(self.pw_ctx.page, url=url, method="GET", headers=headers, json_data=None, log_file=log_file)
                    obj = tx.get("_json")
                    data = obj if isinstance(obj, dict) else {}
                    # 若接口不存在，有时会返回 HTML 字符串（_json 解析失败 -> None/{}），这里用 status 兜底
                    status = int(tx.get("status") or 0) if tx.get("status") is not None else 0
                    if status == 404 and not data:
                        last_err = f"url={url!r} status=404"
                        continue
                    return data
                except Exception as e:
                    last_err = f"url={url!r} err={e}"
                    continue
            raise RuntimeError(f"获取 cameo 状态失败：{last_err}")

    async def api_character_upload_image(self, *, target_url: str, image_path: Path) -> str:
        """POST /project_y/file/upload，返回 asset_pointer。"""
        self.last_used_at = time.time()
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            if self.pw_ctx.page is None:
                raise RuntimeError("page 未初始化")
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            # 不新开页面：直接复用当前已登录的 drafts 页面，降低 429/机器人风控概率
            return await _sora_api_upload_character_image_pw(
                self.pw_ctx.page,
                target_url=target_url,
                bearer_token=str(token),
                image_path=Path(image_path),
                log_file=log_file,
            )

    async def api_character_finalize(
        self,
        *,
        target_url: str,
        cameo_id: str,
        username: str,
        display_name: str,
        profile_asset_pointer: str,
    ) -> str:
        """POST /characters/finalize，返回 character_id。"""
        self.last_used_at = time.time()
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            if self.pw_ctx.page is None:
                raise RuntimeError("page 未初始化")
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            headers = {"Authorization": f"Bearer {token}", "OAI-Language": "en-US", "Content-Type": "application/json"}
            payload = {
                "cameo_id": str(cameo_id),
                "username": str(username),
                "display_name": str(display_name),
                "profile_asset_pointer": str(profile_asset_pointer),
                "instruction_set": None,
                "safety_instruction_set": None,
            }

            urls = [
                _sora_backend_url_from_target(target_url, "/backend/characters/finalize"),
            ]
            last_err: Optional[str] = None
            for url in urls:
                try:
                    tx = await page_fetch_json(self.pw_ctx.page, url=url, method="POST", headers=headers, json_data=payload, log_file=log_file)
                    obj = tx.get("_json")
                    data = obj if isinstance(obj, dict) else {}
                    status = int(tx.get("status") or 0) if tx.get("status") is not None else 0
                    if status == 404 and not data:
                        last_err = f"url={url!r} status=404"
                        continue
                    character_id = None
                    try:
                        character_id = (data or {}).get("character", {}).get("character_id")
                    except Exception:
                        character_id = None
                    cid = str(character_id or "").strip()
                    if cid:
                        return cid
                    last_err = f"url={url!r} missing character_id body={safe_trim(json.dumps(data, ensure_ascii=False), 600)}"
                except Exception as e:
                    last_err = f"url={url!r} err={e}"
                    continue
            raise RuntimeError(f"finalize 失败：{last_err}")

    async def api_character_set_public(self, *, target_url: str, cameo_id: str) -> bool:
        """POST /project_y/cameos/by_id/{cameo_id}/update_v2（visibility=public）。"""
        self.last_used_at = time.time()
        self._cancel_idle_close()
        async with self._bring_drafts_lock:
            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
            
            if self.pw_ctx.page is None:
                raise RuntimeError("page 未初始化")
            log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
            token = self._get_bearer_token_required()
            headers = {"Authorization": f"Bearer {token}", "OAI-Language": "en-US", "Content-Type": "application/json"}
            payload = {"visibility": "public"}

            urls = [
                _sora_backend_url_from_target(target_url, f"/backend/project_y/cameos/by_id/{str(cameo_id)}/update_v2"),
            ]
            last_err: Optional[str] = None
            for url in urls:
                try:
                    tx = await page_fetch_tx(self.pw_ctx.page, url=url, method="POST", headers=headers, json_data=payload, log_file=log_file)
                    status = int(tx.get("status") or 0) if tx.get("status") is not None else 0
                    body = str(tx.get("response_body") or "")
                    if status in (200, 201):
                        return True
                    if status == 404 and body and "<!DOCTYPE html" in body:
                        last_err = f"url={url!r} status=404"
                        continue
                    last_err = f"url={url!r} status={status} body={safe_trim(body, 300)}"
                except Exception as e:
                    last_err = f"url={url!r} err={e}"
                    continue
            raise RuntimeError(f"设置角色公开失败：{last_err}")

    async def _ensure_bearer_token(self, *, target_url: str, log_file: Path) -> str:
        if self.pw_ctx.page is None:
            raise RuntimeError("page 未初始化")
        if self.access_token:
            self.bearer_token = self.access_token
            return str(self.access_token)
        if self.bearer_token:
            return str(self.bearer_token)
        try:
            await self.pw_ctx.page.goto(target_url, wait_until="domcontentloaded")
        except Exception:
            pass
        try:
            await self.pw_ctx.page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        info = await _sora_extract_bearer_from_any_post_pw(self.pw_ctx.page, timeout_seconds=20.0, log_file=log_file)
        tok = str(info.get("token") or "").strip()
        if not tok:
            raise RuntimeError("未抓到 Bearer token（请确认窗口已登录 Sora）")
        self.bearer_token = tok
        try:
            self.user_agent = str(info.get("user_agent") or "").strip() or self.user_agent
        except Exception:
            pass
        return tok

    async def create_task(
        self,
        *,
        prompt: str,
        target_url: str,
        monitor_log_path: Optional[str],
        first_image_url: Optional[str],
        orientation: str,
        n_frames: int,
        browser_open_args: list[str],
        browser_force_open: bool,
        browser_headless: bool,
    ) -> Tuple[str, Dict[str, Any]]:
        """串行创建任务：只有 create 拿到结果后才放行下一个。"""
        self.last_used_at = time.time()
        self._cancel_idle_close()
        self.monitor_log_path = monitor_log_path

        self.browser_open_args = browser_open_args or []
        self.browser_force_open = bool(browser_force_open)
        self.browser_headless = bool(browser_headless)

        async with self.create_lock:
            try:
                log_file = Path(monitor_log_path) if monitor_log_path else (MONITOR_LOG_FILE)
                sentinel_token = None
                # 先生成 sentinel_token，并填充/更新 oai_device_id
                async with self._bring_drafts_lock:
                    await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
                    await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
                    if self.pw_ctx.page is None:
                        raise RuntimeError("无法获取可用 page（context/pages 不可用或 drafts 打开失败）")
                    sentinel_token = await _sora_generate_sentinel_token_in_fp_context_pw(
                        self.pw_ctx.page, device_id=self.oai_device_id, log_file=log_file
                    )
                    if not sentinel_token:
                        raise RuntimeError("未能生成 SentinelToken，触发了429")
                    try:
                        sentinel_data = json.loads(str(sentinel_token))
                        if isinstance(sentinel_data, dict):
                            did = str(sentinel_data.get("id") or "").strip() or None
                            if did:
                                self.oai_device_id = did
                    except Exception:
                        pass
                    if not self.oai_device_id:
                        self.oai_device_id = str(uuid4())

                # bearer_token 优先复用会话缓存；缺失则 bring_to_front + refresh 一次后再抓取
                self._cancel_idle_close()
                async with self._bring_drafts_lock:
                    await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
                    await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
                    if self.pw_ctx.page is None:
                        raise RuntimeError("page 未初始化")
                    await _sora_ui_fill_prompt_textarea(self.pw_ctx.page, prompt=prompt, log_file=log_file)

                if not self.bearer_token:
                    self._cancel_idle_close()
                    async with self._bring_drafts_lock:
                        await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
                        await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
                        if self.pw_ctx.page is None:
                            raise RuntimeError("page 未初始化")
                        info = await _sora_extract_bearer_from_any_post_pw(self.pw_ctx.page, timeout_seconds=20.0, log_file=log_file)
                        tok = str(info.get("token") or "").strip()
                        if not tok:
                            raise RuntimeError("未抓到 Bearer token（请确认窗口已登录 Sora）")
                        self.bearer_token = tok
                        try:
                            ua = str(info.get("user_agent") or "").strip() or None
                            if ua:
                                self.user_agent = ua
                        except Exception:
                            pass
                        if not self.user_agent:
                            try:
                                self.user_agent = await _pw_get_user_agent(self.pw_ctx.page)
                            except Exception:
                                pass

                max_create_attempts = 2 if str(first_image_url or "").strip() else 1
                task_id: Optional[str] = None
                create_tx: Dict[str, Any] = {}
                auth_state: Dict[str, Any] = {}
                last_create_err: Optional[Exception] = None
                token_refresh_used = False
                await self._raise_if_cloudflare_page_nonpenalized(
                    self.pw_ctx.page, stage="创建 Sora 任务", drafts_url=target_url
                )
                for attempt in range(1, max_create_attempts + 1):
                    try:
                        self._cancel_idle_close()
                        async with self._bring_drafts_lock:
                            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
                            await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
                            task_id, create_tx, auth_state = await _sora_create_task_pw(
                                page=self.pw_ctx.page,
                                prompt=prompt,
                                target_url=target_url,
                                monitor_log_path=monitor_log_path,
                                first_image_url=first_image_url,
                                orientation=orientation,
                                n_frames=n_frames,
                                bearer_token=str(self.bearer_token or "") or None,
                                sentinel_token=str(sentinel_token or "") or None,
                                user_agent=str(self.user_agent or "") or None,
                                oai_device_id=str(self.oai_device_id or "") or None,
                            )
                        break
                    except Exception as e:
                        last_create_err = e
                        err_msg = str(e or "")
                        err_msg_lower = err_msg.lower()

                        is_token_expired = (
                            isinstance(e, NonPenalizedTaskError)
                            and (
                                "token_expired" in err_msg_lower
                                or "token is expired" in err_msg_lower
                            )
                        )
                        if is_token_expired:
                            if token_refresh_used:
                                raise NonPenalizedTaskError(
                                    f"create 失败（token_expired）：刷新 access_token 后仍失败：{safe_trim(err_msg, 400)}",
                                    status_code=401,
                                ) from e
                            token_refresh_used = True
                            append_log(
                                log_file,
                                f"[sora][create] 命中 token_expired，准备刷新 access_token 并重试（{attempt + 1}/{max_create_attempts}）",
                            )
                            async with self._bring_drafts_lock:
                                await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless,acquire_bring_lock=False)
                                await self._bring_sora_drafts_to_front(acquire_bring_lock=False)
                            # 注意：_bring_sora_drafts_to_front 内部会拿 _bring_drafts_lock，
                            # 所以这里必须在未持锁状态下调用，避免锁重入。
                            try:
                                token_info = await sora_fetch_access_token_in_window(
                                    sess=self,
                                    target_url=target_url,
                                )
                                self.set_access_token(
                                    token_info.get("access_token"),
                                    token_info.get("expires"),
                                )
                            except Exception as te:
                                raise NonPenalizedTaskError(
                                    f"create 失败（token_expired）：刷新 access_token 失败：{safe_trim(str(te), 400)}",
                                    status_code=401,
                                ) from te
                            continue

                        is_cloudflare = (
                            isinstance(e, NonPenalizedTaskError)
                            and "cloudflare_challenge" in err_msg_lower
                        )
                        if is_cloudflare:
                            append_log(
                                log_file,
                                f"[sora][create] 命中 Cloudflare 挑战页（403），准备刷新页面并自愈（{attempt}/{max_create_attempts}）",
                            )
                            async with self._bring_drafts_lock:
                                await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless,acquire_bring_lock=False)
                                await self._bring_sora_drafts_to_front(acquire_bring_lock=False)
                            if attempt >= max_create_attempts:
                                raise
                            continue

                        is_upload_err = "上传首帧失败" in err_msg
                        if (not is_upload_err) or attempt >= max_create_attempts:
                            raise
                        append_log(
                            log_file,
                            f"[sora][create] 首帧上传失败，准备第 {attempt + 1}/{max_create_attempts} 次重试：{safe_trim(err_msg, 400)!r}",
                        )
                        async with self._bring_drafts_lock:
                            await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless,acquire_bring_lock=False)
                            await self._bring_sora_drafts_to_front(acquire_bring_lock=False)

                if task_id is None:
                    if last_create_err:
                        raise last_create_err
                    raise RuntimeError("create 失败：未知错误（未生成 task_id）")

                try:
                    self.bearer_token = auth_state.get("bearer_token") or self.bearer_token
                    self.sentinel_token = auth_state.get("sentinel_token") or sentinel_token
                    self.user_agent = auth_state.get("user_agent") or self.user_agent
                    self.oai_device_id = auth_state.get("oai_device_id") or self.oai_device_id
                except Exception:
                    pass
                return task_id, create_tx
            finally:
                # create_task 后通常仍会继续监控/发布，需要窗口保持前端状态
                self._cancel_idle_close()

    async def watch_task_progress(
        self,
        *,
        task_id: str,
        progress_cb: ProgressCB,
        max_wait_seconds: float,
        poll_interval_seconds: float,
        sniff_timeout_seconds: float,
        idle_close_seconds: float,
    ) -> Dict[str, Any]:
        self.last_used_at = time.time()
        self._cancel_idle_close()

        self.poll_interval_seconds = max(0.2, float(poll_interval_seconds))
        self.sniff_timeout_seconds = max(0.2, float(sniff_timeout_seconds))
        self.idle_close_seconds = max(0.0, float(idle_close_seconds))

        start_time = time.time()
        max_wait = max(1.0, float(max_wait_seconds))
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        w = _SoraWatcher(
            task_id=str(task_id),
            deadline=start_time + max_wait,
            progress_cb=progress_cb,
            future=fut,
            start_time=start_time,
            max_wait_seconds=max_wait,
        )
        self.watchers[w.task_id] = w
        if self.monitor_task is None or self.monitor_task.done():
            self.monitor_task = asyncio.create_task(self._monitor_loop())

        try:
            return await fut
        finally:
            self.watchers.pop(w.task_id, None)
            if not self.watchers:
                # 监控结束不代表“窗口不用了”，不要自动进入倒计时关窗
                self._cancel_idle_close()

    async def _monitor_loop(self) -> None:
        try:
            while True:
                if not self.watchers:
                    return

                now = time.time()
                for tid, w in list(self.watchers.items()):
                    if w.future.done():
                        continue
                    elapsed = now - w.start_time
                    timeout_seconds = w.max_wait_seconds * (2.0 if w.has_progress else 1.0)
                    if elapsed > timeout_seconds:
                        w.future.set_exception(RuntimeError(f"进度监控超时：task_id={tid}"))
                        self.watchers.pop(tid, None)

                if not self.watchers:
                    return

                try:
                    self._cancel_idle_close()
                    async with self._bring_drafts_lock:
                        await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
                        await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
                        # monitor_loop 内也要保证有可用 page（ensure_open 已不再强制挑 page）
                        page_closed = False
                        if self.pw_ctx.page is not None:
                            try:
                                page_closed = bool(getattr(self.pw_ctx.page, "is_closed", lambda: False)())
                            except Exception:
                                page_closed = False
                        if self.pw_ctx.page is None:
                            raise RuntimeError("无法获取可用 page（context/pages 不可用或 drafts 打开失败）")
                except Exception as e:
                    for tid, w in list(self.watchers.items()):
                        if not w.future.done():
                            w.future.set_exception(RuntimeError(f"浏览器/driver 不可用：{e}"))
                        self.watchers.pop(tid, None)
                    return

                log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
                if not (self.access_token or self.bearer_token):
                    for tid, w in list(self.watchers.items()):
                        if not w.future.done():
                            w.future.set_exception(RuntimeError("缺少 access_token，无法轮询 pending（请在管理台为该窗口转换并保存 access_token）"))
                        self.watchers.pop(tid, None)
                    return

                token = self._get_bearer_token_required()
                try:
                    pending_url = _sora_backend_url_from_target(
                        getattr(self.pw_ctx.page, "url", "") or "https://sora.chatgpt.com", "/backend/nf/pending/v2"
                    )
                except Exception:
                    pending_url = "https://sora.chatgpt.com/backend/nf/pending/v2"

                headers: Dict[str, str] = {
                    "Authorization": f"Bearer {token}",
                    "OAI-Language": "en-US",
                    "OAI-Device-Id": str(self.oai_device_id or ""),
                }

                tx: Optional[Dict[str, Any]] = None
                async with self._bring_drafts_lock:
                    self._cancel_idle_close()
                    await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
                    await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
                    try:
                        tx = await page_fetch_tx(self.pw_ctx.page, url=pending_url, method="GET", headers=headers, json_data=None, log_file=log_file)
                    except Exception as e:
                        append_log(log_file, f"[sora][api] pending poll failed: {e}")
                        tx = None

                body = (tx or {}).get("response_body") or ""
                payload_obj: Any = None
                try:
                    payload_obj = json.loads(body) if body else None
                except Exception:
                    payload_obj = None

                index: Dict[str, Dict[str, Any]] = {}
                if isinstance(payload_obj, list):
                    for it in payload_obj:
                        if isinstance(it, dict) and it.get("id") is not None:
                            index[str(it.get("id"))] = it

                missing_tids: list[str] = []
                for tid, w in list(self.watchers.items()):
                    task_obj = index.get(str(tid)) if index else _extract_task_obj(payload_obj, str(tid))
                    if not task_obj:
                        w.miss_pending_count += 1
                        if w.miss_pending_count >= 2:
                            missing_tids.append(str(tid))
                        continue
                    w.miss_pending_count = 0

                    status = task_obj.get("status")
                    progress_pct = _normalize_progress(task_obj.get("progress_pct"))
                    w.last_status = status
                    w.last_progress_pct = progress_pct

                    if progress_pct is not None:
                        p_int = min(int(max(0.15, min(1.0, float(progress_pct))) * 100.0), 85)
                        if p_int != w.last_sent_progress:
                            w.last_sent_progress = p_int
                            w.has_progress = True
                            try:
                                await w.progress_cb(p_int, {"task_id": tid, "status": status})
                            except Exception:
                                pass

                if missing_tids:
                    self._cancel_idle_close()
                    async with self._bring_drafts_lock:
                        await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
                        await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
                        try:
                            drafts = await _sora_api_get_video_drafts_pw(
                                self.pw_ctx.page,
                                target_url=str(getattr(self.pw_ctx.page, "url", "") or "https://sora.chatgpt.com/drafts"),
                                bearer_token=str(token),
                                limit=15,
                                log_file=log_file,
                            )
                            items = drafts.get("items", []) if isinstance(drafts, dict) else []
                        except Exception as e:
                            items = []
                            try:
                                append_log(log_file, f"[sora][drafts] fetch failed: {e}")
                            except Exception:
                                pass

                    if isinstance(items, list) and items:
                        by_task: Dict[str, Dict[str, Any]] = {}
                        for it in items:
                            if isinstance(it, dict) and it.get("task_id") is not None:
                                by_task[str(it.get("task_id"))] = it
                        for tid in list(missing_tids):
                            it = by_task.get(str(tid))
                            if not it:
                                continue
                            if tid in self.watchers:
                                w = self.watchers.get(tid)
                                if w and not w.future.done():
                                    w.future.set_result({"task_id": tid, "status": "completed", "progress_pct": 1.0, "done": True, "draft": it})
                                self.watchers.pop(tid, None)
                if not self.watchers:
                    return
                self._cancel_idle_close()
                await self.disconnect_playwright_under_bring_lock()
                await asyncio.sleep(float(self.poll_interval_seconds))
        finally:
            if not self.watchers:
                # monitor loop 退出不代表“窗口不用了”，不要自动进入倒计时关窗
                self._cancel_idle_close()

    async def finalize_video_and_publish(
        self,
        *,
        task_id: str,
        max_wait_seconds: float,
        prompt: str,
        target_url: str,
        drafts_limit: int = 100,
    ) -> Dict[str, Any]:
        """任务完成后：从 drafts 找到对应视频 → 发布草稿（去水印）→ 返回 {post_id, urls, draft}。"""
        _ = prompt  # 预留：未来扩展（例如校验 prompt 匹配）
        self.last_used_at = time.time()
        # 发布/轮询 drafts 期间仍在使用窗口：不要触发倒计时关窗
        self._cancel_idle_close()        
        log_file = Path(self.monitor_log_path) if self.monitor_log_path else (MONITOR_LOG_FILE)
        token = self._get_bearer_token_required()


        def _get_item_task_id(it: Dict[str, Any]) -> str:
            try:
                v = it.get("task_id")
                if v:
                    return str(v)
            except Exception:
                pass
            try:
                v = it.get("taskId")
                if v:
                    return str(v)
            except Exception:
                pass
            try:
                t = it.get("task")
                if isinstance(t, dict) and t.get("id"):
                    return str(t.get("id"))
            except Exception:
                pass
            return ""

        drafts_wait_seconds = max_wait_seconds
        drafts_poll_interval = 15.0
        deadline = time.time() + drafts_wait_seconds
        draft_item: Optional[Dict[str, Any]] = None
        last_items_sample: list[str] = []
        last_drafts_count: int = 0
        attempt = 0
        while time.time() < deadline and draft_item is None:
            attempt += 1
            items = []
            self._cancel_idle_close()
            async with self._bring_drafts_lock:
                try:
                    await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
                    await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
                    drafts = await _sora_api_get_video_drafts_pw(
                        self.pw_ctx.page,
                        target_url=target_url,
                        bearer_token=str(token),
                        limit=int(drafts_limit),
                        log_file=log_file,
                    )
                    items = drafts.get("items", []) if isinstance(drafts, dict) else []
                    if not isinstance(items, list):
                        items = []
                    last_drafts_count = len(items)
                except Exception as e:
                    items = []
                    try:
                        append_log(log_file, f"[sora][drafts] fetch failed: {e}")
                    except Exception:
                        pass

            last_items_sample = []
            for it in items[:20]:
                if isinstance(it, dict):
                    tid = _get_item_task_id(it)
                    if tid:
                        last_items_sample.append(tid)

            for it in items:
                if not isinstance(it, dict):
                    continue
                if _get_item_task_id(it) == str(task_id):
                    draft_item = it
                    break

            append_log(
                log_file,
                f"[sora][drafts] poll attempt={attempt} found={bool(draft_item)} items={len(items)} "
                f"sample_task_ids={safe_trim(','.join(last_items_sample), 260)!r}",
            )

            if draft_item is not None:
                break
            await asyncio.sleep(float(drafts_poll_interval))

        if not draft_item:
            raise RuntimeError(
                f"草稿箱未找到任务对应视频（已轮询 {drafts_wait_seconds:.0f}s）：task_id={task_id} "
                f"sample_task_ids={safe_trim(','.join(last_items_sample), 260)}"
            )

        generation_id = str(draft_item.get("id") or "").strip()
        if not generation_id:
            raise RuntimeError("草稿箱记录缺少 generation_id（draft_item.id）")

        err_kind = str(draft_item.get("kind") or "").strip()
        reason_str = str(draft_item.get("reason_str") or "").strip()
        markdown_reason_str = str(draft_item.get("markdown_reason_str") or "").strip()
            
        max_publish_attempts = 3
        post_resp: Dict[str, Any] = {}
        for publish_attempt in range(1, max_publish_attempts + 1):
            try:
                self._cancel_idle_close()
                async with self._bring_drafts_lock:
                    await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless, acquire_bring_lock=False)
                    await self._bring_sora_drafts_to_front(refresh_target=False, acquire_bring_lock=False)
                    sentinel_token = await _sora_generate_sentinel_token_in_fp_context_pw(
                        self.pw_ctx.page,
                        device_id=self.oai_device_id,
                        log_file=log_file,
                    )
                    if not sentinel_token:
                        raise RuntimeError("缺少 sentinel_token，无法发布草稿")
                    post_resp = await _sora_api_post_project_y_post_pw(
                        self.pw_ctx.page,
                        target_url=target_url,
                        bearer_token=str(self.bearer_token),
                        sentinel_token=str(sentinel_token),
                        generation_id=generation_id,
                        log_file=log_file,
                    )
                break
            except Exception as e:
                err_msg = str(e or "")
                should_retry = (
                    publish_attempt < max_publish_attempts
                    and "fetch_json 解析失败：status=" in err_msg
                )
                append_log(
                    log_file,
                    f"[sora][publish] attempt={publish_attempt}/{max_publish_attempts} failed "
                    f"retry={bool(should_retry)} err={safe_trim(err_msg, 300)!r}",
                )
                if not should_retry:
                    try:
                        del_url = _sora_backend_url_from_target(target_url, f"/backend/project_y/profile/drafts/{generation_id}")
                        del_headers = {"Authorization": f"Bearer {self.bearer_token}", "OAI-Language": "en-US"}
                        del_tx = await page_fetch_json(self.pw_ctx.page, url=del_url, method="DELETE", headers=del_headers, json_data=None, log_file=log_file)
                        del_status = int(del_tx.get("status") or 0) if del_tx.get("status") is not None else 0
                        append_log(log_file, f"[sora][publish] cleanup DELETE draft {generation_id} status={del_status}")
                    except Exception as del_e:
                        try:
                            append_log(log_file, f"[sora][publish] cleanup DELETE draft {generation_id} failed: {del_e}")
                        except Exception:
                            pass
                    raise NonPenalizedTaskError(f"发布草稿失败: {err_kind}-{reason_str}-{markdown_reason_str}-{err_msg}", status_code=400)
                async with self._bring_drafts_lock:
                    await self.ensure_open(args=self.browser_open_args, force_open=self.browser_force_open, headless=self.browser_headless,acquire_bring_lock=False)
                    await self._bring_sora_drafts_to_front(acquire_bring_lock=False)
        
        post_id = ""
        try:
            post_id = str(((post_resp or {}).get("post") or {}).get("id") or "").strip()
        except Exception:
            post_id = ""
        if not post_id:
            try:
                del_url = _sora_backend_url_from_target(target_url, f"/backend/project_y/profile/drafts/{generation_id}")
                del_headers = {"Authorization": f"Bearer {self.bearer_token}", "OAI-Language": "en-US"}
                del_tx = await page_fetch_json(self.pw_ctx.page, url=del_url, method="DELETE", headers=del_headers, json_data=None, log_file=log_file)
                del_status = int(del_tx.get("status") or 0) if del_tx.get("status") is not None else 0
                append_log(log_file, f"[sora][publish] cleanup DELETE draft {generation_id} status={del_status}")
            except Exception as del_e:
                try:
                    append_log(log_file, f"[sora][publish] cleanup DELETE draft {generation_id} failed: {del_e}")
                except Exception:
                    pass
            _resp_str = safe_trim(json.dumps(post_resp, ensure_ascii=False), 600)
            _resp_lower = _resp_str.lower()
            if "cameo_not_found" in _resp_lower or "does not have a cameo" in _resp_lower:
                raise NonPenalizedTaskError(f"发布草稿失败（cameo_not_found）：{_resp_str}", status_code=400)
            if "sora_content_violation" in err_kind:
                raise NonPenalizedTaskError(f"发布草稿失败，生成视频包含违规内容: {err_kind}-{reason_str}-{markdown_reason_str}", status_code=400)
            if "may violate our feed policies" in _resp_lower:
                raise NonPenalizedTaskError(f"发布草稿失败，内容包含违规内容（feed_policies_violation）：{_resp_str}", status_code=400)
            if "authentication service is temporarily unavailable" in _resp_lower:
                raise NonPenalizedTaskError(f"发布草稿失败（auth_service_unavailable）：{_resp_str}", status_code=400)
            raise RuntimeError(f"发布草稿失败：未返回 post_id resp={_resp_str}-{err_kind}-{reason_str}-{markdown_reason_str}")

        # 优先回传可下载的视频直链（downloadable_url）；取不到再回退到帖子链接
        downloadable_url = ""
        thumb_url = ""
        try:
            post_obj = (post_resp or {}).get("post") or {}
            attachments = post_obj.get("attachments")
            if isinstance(attachments, list):
                for att in attachments:
                    if not isinstance(att, dict):
                        continue
                    du = str(att.get("downloadable_url") or "").strip()
                    if du:
                        downloadable_url = du
                    encodings = att.get("encodings") or {}
                    thumbnail = encodings.get("thumbnail") or {}
                    tu = str(thumbnail.get("path") or "").strip()
                    if tu:
                        thumb_url = tu
                    if downloadable_url:
                        break
        except Exception:
            downloadable_url = ""
            raise RuntimeError(f"发布草稿失败,生成视频包含违规内容")

        if not downloadable_url:
            err_kind = str(draft_item.get("kind") or "").strip()
            reason_str = str(draft_item.get("reason_str") or "").strip()
            markdown_reason_str = str(draft_item.get("markdown_reason_str") or "").strip()
            raise NonPenalizedTaskError(f"create 失败，内容中包含违禁画面{err_kind}-{reason_str}-{markdown_reason_str}", status_code=400)
        share_url = downloadable_url or f"https://sora.chatgpt.com/p/{post_id}"
        # https://oscdn2.dyysy.com/MP4/{post_id}.mp4
        watermark_free_url = ""
        return {
            "task_id": str(task_id),
            "generation_id": generation_id,
            "post_id": post_id,
            "share_url": share_url,
            "watermark_free_url": watermark_free_url,
            "draft": draft_item,
            "thumb_url": thumb_url,
            "drafts_count": last_drafts_count,
        }


_SORA_LOCK = asyncio.Lock()
_SORA_SESSIONS: Dict[str, SoraSession] = {}


def _sora_key(vendor: str, base_url: str, space_id: str, window_key: str) -> str:
    return "|".join([(vendor or "").strip().lower(), (base_url or "").strip().lower(), (space_id or "").strip(), (window_key or "").strip()])


def _drop_sora_session(cache_key: str) -> None:
    k = (cache_key or "").strip()
    if not k:
        return
    _SORA_SESSIONS.pop(k, None)


def get_or_create_sora_session(
    *,
    vendor: str,
    base_url: str,
    access_key: Optional[str],
    space_id: str,
    window_key: str,
) -> SoraSession:
    """对外提供：用于 admin/registry 等复用同一 window 的 Sora 会话。"""
    k = _sora_key(vendor, base_url, space_id, window_key)
    # 说明：这里用简单字典缓存；并发创建窗口时由内部 ensure_open/create_lock 兜底
    sess = _SORA_SESSIONS.get(k)
    if sess is None:
        pw_ctx = get_or_create_playwright_ctx(vendor=vendor, base_url=base_url, access_key=access_key, space_id=space_id, window_key=window_key)
        sess = SoraSession(cache_key=k, pw_ctx=pw_ctx)
        _SORA_SESSIONS[k] = sess
    else:
        sess.pw_ctx.access_key = access_key
    return sess


async def window_pool_guard_unknown_handler_page(page: Any, *, stage: str, target_url: str) -> None:
    """非 Sora/Veo 专用会话时的窗口池 CF 巡检：两次 goto 目标页，仍疑似 CF 则抛 NonPenalizedTaskError。"""
    if page is None:
        return
    tu = (target_url or "").strip()
    if not tu:
        return

    async def _looks_cf(p: Any, *, deep: bool) -> bool:
        if p is None:
            return False
        try:
            u = str(getattr(p, "url", "") or "").strip().lower()
        except Exception:
            u = ""
        if "/cdn-cgi/" in u:
            return True
        try:
            title = await p.title()
        except Exception:
            title = ""
        tl = (title or "").strip().lower()
        if "just a moment" in tl or "attention required" in tl:
            return True
        if not deep:
            return False
        try:
            html = await p.content()
        except Exception:
            html = ""
        hl = (html or "").lower()
        if "cloudflare" in hl and ("just a moment" in hl or "/cdn-cgi/" in hl or "cf-ray" in hl):
            return True
        if ("turnstile" in hl or "cf-challenge" in hl) and ("/cdn-cgi/" in hl or "cloudflare" in hl):
            return True
        return False

    cur = page
    for _ in range(2):
        if not await _looks_cf(cur, deep=False):
            return
        try:
            await cur.goto(tu, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            pass
        await asyncio.sleep(2.0)
    if await _looks_cf(cur, deep=True):
        raise NonPenalizedTaskError(
            f"当前页面为 Cloudflare 验证/拦截页，无法继续：{stage}",
            status_code=503,
        )


async def sora_gen_video(
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
) -> Dict[str, Any]:
    """Sora 生视频：复用同一指纹浏览器窗口 + Playwright(CDP) 轻量连接，拆分“创建任务”和“进度轮询”。"""
    payload = payload or {}
    video_url = str(payload.get("video_url") or payload.get("videoUrl") or payload.get("videoURL") or "").strip() or None
    prompt = str(payload.get("prompt") or "").strip()
    generation_id = str(payload.get("generation_id") or "").strip()
    head_url = str(payload.get("head_url") or "").strip() or None
    if not video_url and not prompt and not generation_id:
        raise NonPenalizedTaskError("payload.prompt 或payload.video_url（用于创建角色） 或payload.generation_id 不能为空（用于创建角色）", status_code=400)

    first_image_url = str(payload.get("first_image_url") or payload.get("firstImageUrl") or "").strip() or None
    ratio = str(payload.get("size_ratio") or payload.get("aspect_ratio") or payload.get("ratio") or payload.get("尺寸") or "").strip() or None
    orientation = _pick_orientation_from_ratio(ratio) or str(payload.get("orientation") or "").strip() or None
    duration_v = payload.get("n_frames") or payload.get("duration_frames") or payload.get("duration") or payload.get("时长")
    n_frames = _pick_n_frames(duration_v)

    target_url = str(payload.get("sora_url") or "https://sora.chatgpt.com/drafts").strip()
    monitor_log_path = (str(payload.get("sora_monitor_log_path") or "").strip() or None)

    max_wait_seconds = float(payload.get("sora_pending_max_wait_seconds") or max(30.0, min(float(timeout_seconds), 90.0 * 10)))
    poll_interval_seconds = float(payload.get("sora_pending_poll_interval_seconds") or 30.0)
    sniff_timeout_seconds = float(payload.get("sora_pending_sniff_timeout_seconds") or 4.0)
    idle_close_seconds = float(payload.get("ctx_idle_close_seconds") or 30.0)

    sess = get_or_create_sora_session(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = headless
    # 由管理台预先转换并写入 DB 的 access_token：直接写入会话，避免抓包/ensure
    try:
        if access_token:
            sess.set_access_token(access_token, access_expires)
    except Exception:
        pass

    # 新分支：若提供 video_url，则先创建角色（Character Creation Only）
    if generation_id:
        if not head_url:
            raise NonPenalizedTaskError("payload.head_url 不能为空（用于创建角色）", status_code=400)
        sess.monitor_log_path = monitor_log_path
        sess.idle_close_seconds = max(0.0, float(idle_close_seconds))

        # 依你的约束：最多轮询 360 秒，每 2 秒一次
        character_max_wait_seconds = 600.0
        character_poll_interval_seconds = 2.0

        tmp_img: Optional[Path] = None
        cameo_status: Dict[str, Any] = {}
        try:
            
            await progress_cb(5, {"stage": "character_download_avatar", "head_url": head_url})
            try:
                avatar_bytes_raw, hdrs = await _download_bytes_local_async(
                    head_url,
                    timeout_seconds=float(payload.get("avatar_download_timeout_seconds") or 60.0),
                    user_agent=sess.user_agent,
                )
            except Exception as e:
                raise NonPenalizedTaskError(
                    f"头像下载失败（请检查 head_url 是否正确/可访问）：url={safe_trim(str(head_url), 400)!r} err={e}",
                    status_code=400,
                ) from e

            if not avatar_bytes_raw:
                raise NonPenalizedTaskError("头像下载失败：文件为空", status_code=400)

            ct = ""
            try:
                ct = str((hdrs or {}).get("content-type") or (hdrs or {}).get("Content-Type") or "").lower()
            except Exception:
                ct = ""
            if ("text/html" in ct) or ("application/json" in ct):
                raise NonPenalizedTaskError(
                    f"头像下载失败：响应 Content-Type={safe_trim(ct, 120)!r}，疑似非图片（请检查 head_url）：url={safe_trim(str(head_url), 400)!r}",
                    status_code=400,
                )

            avatar_bytes, _avatar_filename, avatar_mime = await _prepare_first_frame_image_for_upload_async(
                avatar_bytes_raw,
                max_bytes=100 * 1024,
                target_bytes=95 * 1024,
            )
            tmp_suffix = ".jpg" if avatar_mime == "image/jpeg" else ".png"
            tmp_img = await _write_bytes_to_tempfile_local_async(avatar_bytes, suffix=tmp_suffix)

            await progress_cb(10, {"stage": "character_from_generation_submit", "generation_id": generation_id})
            await sess._raise_if_cloudflare_page_nonpenalized(
                sess.pw_ctx.page, stage="角色创建（from_generation 前）", drafts_url=target_url
            )

            cameo_obj = None
            for _from_gen_attempt in range(2):
                try:
                    cameo_obj = await sess.api_characters_from_generation(target_url=target_url, generation_id=str(generation_id))
                    break
                except Exception as e:
                    async with sess._bring_drafts_lock:
                        await sess.ensure_open(args=sess.browser_open_args, force_open=sess.browser_force_open, headless=sess.browser_headless,acquire_bring_lock=False)
                        await sess._bring_sora_drafts_to_front(acquire_bring_lock=False)
                    if _from_gen_attempt == 2:
                        raise
            cameo_id = str((cameo_obj or {}).get("id") or "").strip()
            if not cameo_id:
                raise RuntimeError(f"from-generation 响应缺少 id：body={safe_trim(json.dumps(cameo_obj, ensure_ascii=False), 600)}")

            await progress_cb(15, {"stage": "character_processing", "cameo_id": cameo_id})

            start = time.time()
            last_msg = None
            last_pct: Optional[float] = None
            consecutive_errors = 0
            while True:
                if time.time() - start > character_max_wait_seconds:
                    raise RuntimeError(f"角色处理超时：cameo_id={cameo_id} waited={int(time.time() - start)}s")
                try:
                    await asyncio.sleep(character_poll_interval_seconds)
                except Exception:
                    pass

                try:
                    cameo_status = await sess.api_cameo_status(target_url=target_url, cameo_id=cameo_id)
                    consecutive_errors = 0
                except Exception as e:
                    consecutive_errors += 1
                    async with sess._bring_drafts_lock:
                        await sess.ensure_open(args=sess.browser_open_args, force_open=sess.browser_force_open, headless=sess.browser_headless,acquire_bring_lock=False)
                        await sess._bring_sora_drafts_to_front(acquire_bring_lock=False)
                    if consecutive_errors >= 5:
                        raise RuntimeError(f"轮询 cameo owned 状态失败{consecutive_errors}次：{e}")
                    continue

                msg = str((cameo_status or {}).get("status_message") or "").strip()
                pct = _normalize_progress((cameo_status or {}).get("progress_pct"))
                if pct is not None:
                    try:
                        pct = float(max(0.0, min(1.0, pct)))
                    except Exception:
                        pct = None

                if msg != last_msg or pct != last_pct:
                    last_msg = msg
                    last_pct = pct
                    if pct is None:
                        prog = 10 + int(min(55.0, (time.time() - start) / max(1.0, character_max_wait_seconds) * 55.0))
                    else:
                        prog = 5 + int(pct * 60.0)
                    await progress_cb(
                        int(max(20, min(65, prog))),
                        {"stage": "character_processing", "cameo_id": cameo_id, "status_message": msg, "progress_pct": pct},
                    )

                if str((cameo_status or {}).get("status") or "") == "failed":
                    raise NonPenalizedTaskError(f"角色创建失败：{msg or 'failed'}", status_code=400)
                if msg == "Completed":
                    break

            display_name = str((cameo_status or {}).get("display_name_hint") or "Character").strip() or "Character"
            username_hint = str((cameo_status or {}).get("username_hint") or "character").strip().lstrip("@").strip() or "character"
            await progress_cb(70, {"stage": "character_identified", "cameo_id": cameo_id, "display_name": display_name, "username_hint": username_hint})

            def _sanitize_username(s: str) -> str:
                s = str(s or "").strip().lstrip("@").strip().lower()
                out = "".join([ch for ch in s if (ch.isalnum() or ch in ("_", "."))])
                out = out.strip(".")
                return out or "character"

            sanitized = _sanitize_username(username_hint)
            if len(sanitized) >= 7:
                username = "mrr_" + sanitized[4:-3] + f"{random.randint(0, 999):03d}"
            else:
                username = "mrr_" + sanitized + f"{random.randint(0, 999):03d}"

            await progress_cb(75, {"stage": "character_username_checked", "cameo_id": cameo_id, "username_hint": username})


            await progress_cb(80, {"stage": "character_upload_avatar"})
            asset_pointer = await sess.api_character_upload_image(target_url=target_url, image_path=tmp_img)

            await progress_cb(85, {"stage": "character_finalize"})
            character_id = None
            for _finalize_attempt in range(2):
                try:
                    character_id = await sess.api_character_finalize(
                        target_url=target_url,
                        cameo_id=cameo_id,
                        username=username,
                        display_name=display_name,
                        profile_asset_pointer=asset_pointer,
                    )
                    break
                except Exception as e:
                    async with sess._bring_drafts_lock:
                        await sess.ensure_open(args=sess.browser_open_args, force_open=sess.browser_force_open, headless=sess.browser_headless,acquire_bring_lock=False)
                        await sess._bring_sora_drafts_to_front(acquire_bring_lock=False)
                    if _finalize_attempt == 2:
                        raise NonPenalizedTaskError(f"角色 finalize 失败：{e}", status_code=400)

            await progress_cb(90, {"stage": "character_set_public"})
            update_payload: Dict[str, Any] = {"visibility": "public"}
            hint = (cameo_status or {}).get("instruction_set_hint")
            if isinstance(hint, dict) and hint.get("value") is not None:
                update_payload["instruction_set"] = hint
                update_payload["value"] = hint.get("value")
            elif isinstance(hint, list):
                update_payload["instruction_set"] = {"value": hint}
                update_payload["value"] = hint
            await sess.api_cameo_update_v2(target_url=target_url, cameo_id=cameo_id, payload=update_payload)

            await progress_cb(100, {"stage": "done", "cameo_id": cameo_id, "character_id": character_id, "username_hint": username})
            return {
                "type": "character",
                "message": "Sora角色创建完成",
                "username": username,
                "character_id": character_id,
                "cameo_id": cameo_id,
                "display_name": display_name,
                "raw_cameo_status": cameo_status,
                "profile_asset_url": head_url
            }
        finally:
            if tmp_img is not None:
                try:
                    tmp_img.unlink(missing_ok=True)  # type: ignore[call-arg]
                except Exception:
                    try:
                        os.unlink(str(tmp_img))
                    except Exception:
                        pass
    elif video_url:
        target_url = str(payload.get("sora_url") or "https://sora.chatgpt.com/drafts").strip()
        monitor_log_path = (str(payload.get("sora_monitor_log_path") or "").strip() or None)
        max_wait_seconds = float(payload.get("character_pending_max_wait_seconds") or payload.get("sora_pending_max_wait_seconds") or max(60.0, min(float(timeout_seconds), 60.0 * 15)))
        poll_interval_seconds = float(payload.get("character_pending_poll_interval_seconds") or 10.0)
        idle_close_seconds = float(payload.get("ctx_idle_close_seconds") or 30.0)
        sess.monitor_log_path = monitor_log_path
        sess.idle_close_seconds = max(0.0, float(idle_close_seconds))

        tmp_video: Optional[Path] = None
        tmp_img: Optional[Path] = None
        cameo_status: Dict[str, Any] = {}
        try:
            await progress_cb(5, {"stage": "character_download_video", "video_url": video_url})
            # 下载到本地临时文件（用于浏览器上下文上传）
            tmp_video, _hdrs = await _download_to_tempfile_local_async(
                video_url,
                suffix=".mp4",
                timeout_seconds=float(payload.get("video_download_timeout_seconds") or 120.0),
                user_agent=sess.user_agent,
                max_bytes=20 * 1024 * 1024,  # 20MB
            )

            # 校验大小（<=20MB）与时长（<=16秒）
            try:
                sz = int(tmp_video.stat().st_size)
            except Exception:
                sz = -1
            if sz < 0:
                raise NonPenalizedTaskError("无法读取视频文件大小", status_code=400)
            if sz > 20 * 1024 * 1024:
                raise NonPenalizedTaskError(f"视频文件过大：{sz} bytes（限制 20MB）", status_code=400)

            try:
                dur = _mp4_duration_seconds(tmp_video)
            except Exception as e:
                raise NonPenalizedTaskError(f"无法解析视频时长（仅支持 MP4）：{e}", status_code=400)
            if float(dur) > 16.0 + 1e-6:
                raise NonPenalizedTaskError(f"视频时长过长：{dur:.3f}s（限制 ≤16s）", status_code=400)

            await sess._raise_if_cloudflare_page_nonpenalized(
                sess.pw_ctx.page, stage="角色创建（上传角色视频前）", drafts_url=target_url
            )

            await progress_cb(10, {"stage": "character_upload_video"})

            cameo_id = await sess.api_characters_upload_video(
                target_url=target_url,
                video_path=tmp_video,
                timestamps=str(payload.get("character_video_timestamps") or "0,3"),
            )
            await progress_cb(15, {"stage": "character_processing", "cameo_id": cameo_id})

            # 轮询 cameo 状态
            start = time.time()
            consecutive_errors = 0
            last_status = None
            while True:
                if time.time() - start > max(1.0, float(max_wait_seconds)):
                    raise RuntimeError(f"角色处理超时：cameo_id={cameo_id} waited={int(time.time() - start)}s")
                try:
                    await asyncio.sleep(max(0.8, float(poll_interval_seconds)))
                except Exception:
                    pass

                try:
                    cameo_status = await sess.api_cameo_status(target_url=target_url, cameo_id=cameo_id)
                    consecutive_errors = 0
                except Exception as e:
                    consecutive_errors += 1
                    if consecutive_errors >= 4:
                        raise RuntimeError(f"轮询 cameo 状态失败{consecutive_errors}次数过多：{e}")
                    continue

                cur = cameo_status.get("status")
                msg = str(cameo_status.get("status_message") or "")
                if cur != last_status:
                    last_status = cur
                    try:
                        pct = 10 + int(min(55.0, (time.time() - start) / max(1.0, float(max_wait_seconds)) * 55.0))
                    except Exception:
                        pct = 20
                    await progress_cb(int(max(20, min(65, pct))), {"stage": "character_processing", "cameo_id": cameo_id, "status": cur, "status_message": msg})

                if str(cur) == "failed":
                    raise NonPenalizedTaskError(f"角色创建失败：{msg or 'failed'}", status_code=400)
                if msg == "Completed" or str(cur) == "finalized":
                    break

            username_hint = str(cameo_status.get("username_hint") or "character")
            display_name = str(cameo_status.get("display_name_hint") or "Character")
            base_username = username_hint.split(".")[-1] if "." in username_hint else username_hint
            base_username = str(base_username or "").strip().lstrip("@").strip() or "character"
            # Sora 对 username 约束更严格：尽量只保留 [a-z0-9_]
            safe_base = "".join([ch for ch in base_username.lower() if (ch.isalnum() or ch == "_")])
            if not safe_base:
                safe_base = "character"
            if len(safe_base) >= 7:
                username = "mrr_" + safe_base[4:-3] + f"{random.randint(0, 999):03d}"
            else:
                username = f"mrr_{safe_base}{random.randint(0, 999):03d}"
            await progress_cb(70, {"stage": "character_identified", "cameo_id": cameo_id, "display_name": display_name, "username": username})

            profile_asset_url = str(cameo_status.get("profile_asset_url") or "").strip() or None
            if not profile_asset_url:
                raise RuntimeError("cameo_status 缺少 profile_asset_url，无法完成角色创建")

            await progress_cb(75, {"stage": "character_download_avatar"})
            tmp_img, _hdrs2 = await _download_to_tempfile_local_async(
                profile_asset_url,
                suffix=".webp",
                timeout_seconds=float(payload.get("avatar_download_timeout_seconds") or 60.0),
                user_agent=sess.user_agent,
            )

            await progress_cb(80, {"stage": "character_upload_avatar"})
            asset_pointer = await sess.api_character_upload_image(target_url=target_url, image_path=tmp_img)

            await progress_cb(85, {"stage": "character_finalize"})
            character_id = None
            for _finalize_attempt in range(2):
                try:
                    character_id = await sess.api_character_finalize(
                        target_url=target_url,
                        cameo_id=cameo_id,
                        username=username,
                        display_name=display_name,
                        profile_asset_pointer=asset_pointer,
                    )
                    break
                except Exception as e:
                    if _finalize_attempt == 2:
                        raise NonPenalizedTaskError(f"角色 finalize 失败：{e}", status_code=400)

            await progress_cb(90, {"stage": "character_set_public"})
            await sess.api_character_set_public(target_url=target_url, cameo_id=cameo_id)

            await progress_cb(100, {"stage": "done", "cameo_id": cameo_id, "character_id": character_id})
            return {
                "type": "character",
                "message": "Sora角色创建完成",
                "cameo_id": cameo_id,
                "character_id": character_id,
                "display_name": display_name,
                "username": username,
                "profile_asset_url": profile_asset_url,
                "raw_cameo_status": cameo_status,
            }
        finally:
            for p in [tmp_video, tmp_img]:
                if p is None:
                    continue
                try:
                    p.unlink(missing_ok=True)  # type: ignore[call-arg]
                except Exception:
                    try:
                        os.unlink(str(p))
                    except Exception:
                        pass

    await progress_cb(5, {"stage": "create_task"})
    task_id, _create_tx = await sess.create_task(
        prompt=prompt,
        target_url=target_url,
        monitor_log_path=monitor_log_path,
        first_image_url=first_image_url,
        orientation=str(orientation or "portrait"),
        n_frames=int(n_frames),
        browser_open_args=[],
        browser_force_open=False,
        browser_headless=headless,
    )
    await sess.disconnect_playwright_under_bring_lock()
    await progress_cb(10, {"stage": "created", "task_id": task_id})
    await progress_cb(10, {"stage": "monitor_progress", "task_id": task_id})
    progress_result = await sess.watch_task_progress(
        task_id=task_id,
        progress_cb=progress_cb,
        max_wait_seconds=max_wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sniff_timeout_seconds=sniff_timeout_seconds,
        idle_close_seconds=idle_close_seconds,
    )

    await sess.disconnect_playwright_under_bring_lock()
    await progress_cb(90, {"stage": "drafts_and_publish", "task_id": task_id})
    publish_result = await sess.finalize_video_and_publish(
        task_id=task_id,
        max_wait_seconds=max_wait_seconds,
        prompt=prompt,
        target_url=target_url,
        drafts_limit=int(payload.get("sora_drafts_limit") or 15),
    )

    await progress_cb(100, {"stage": "done", "task_id": task_id, "post_id": publish_result.get("post_id")})
    _ = progress_result

    return {
        "type": "video",
        "message": "Sora创建完成",
        "task_id": task_id,
        "post_id": publish_result.get("post_id"),
        "generation_id": publish_result.get("generation_id"),
        "share_url": publish_result.get("share_url"),
        "watermark_free_url": publish_result.get("watermark_free_url"),
        "thumb_url": publish_result.get("thumb_url"),
        "drafts_count": publish_result.get("drafts_count", 0),
    }
