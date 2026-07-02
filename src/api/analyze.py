"""网页数据采集分析 API。

采集指纹浏览器窗口数据并保存到 analyze/{window_id}/ 目录：
- network_log.json  — 所有页面的网络请求/响应（含 headers、body）
- source/           — 每个打开页面的 HTML 源码
- js/               — 内联脚本 + 外链 JS 文件内容
- cookies.json      — 当前 context 的全部 cookie
- summary.json      — 本次采集的元信息
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from ..core.database import Database
from ..core.logger import logger
from ..core.paths import ANALYZE_DIR
from ..services.fp_browser_client import FPBrowserClient
from ..services.playwright_broswer_context import normalize_cdp_endpoint

router = APIRouter()

db: Database | None = None

_ANALYZE_ROOT = ANALYZE_DIR


def set_dependencies(database: Database) -> None:
    global db
    db = database


def _verify_token(authorization: str = Header(default="")) -> str:
    from . import admin as admin_mod
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization")
    token = authorization[7:]
    if not admin_mod.active_admin_tokens.get(token):
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")
    return token


def _safe_filename(name: str, max_len: int = 180) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip(". ")
    return name[:max_len] or "unnamed"


def _url_to_filename(url: str) -> str:
    try:
        p = urlparse(url)
        host = p.hostname or "unknown"
        path = p.path.strip("/").replace("/", "__") or "index"
        return _safe_filename(f"{host}__{path}")
    except Exception:
        return _safe_filename(url[:100])


class CaptureRequest(BaseModel):
    window_pk: int
    include_static_js: bool = True
    capture_network: bool = True
    capture_source: bool = True
    capture_js: bool = True
    capture_cookies: bool = True


async def _get_window_open_info(window_pk: int):
    """从 DB 查询窗口信息并打开，返回 (cdp_endpoint, window_key, window_name)。"""
    if not db:
        raise RuntimeError("db not initialized")

    win = await db.get_window(window_pk)
    if not win:
        raise RuntimeError(f"window_pk={window_pk} 不存在")

    space_pk = int(win.space_pk)
    window_key = str(win.window_key or "")
    window_name = str(win.window_name or window_key)

    space = await db.get_space(space_pk)
    if not space:
        raise RuntimeError(f"space_pk={space_pk} 不存在")

    browser = await db.get_browser(int(space.browser_id))
    if not browser:
        raise RuntimeError(f"browser_id={space.browser_id} 不存在")

    vendor = str(browser.vendor or "roxy")
    base_url = str(browser.lan_addr or "")
    access_key = browser.access_key
    space_id = str(space.space_id or "")

    client = FPBrowserClient()
    result = await client.browser_open(
        vendor=vendor,
        base_url=base_url,
        access_key=access_key,
        space_id=space_id,
        window_key=window_key,
        force_open=False,
        headless=False,
        pure_mode=True,
    )

    data = result.get("data") or {}
    raw_ep = data.get("ws") or data.get("http") or data.get("driver") or ""
    if not raw_ep:
        raise RuntimeError(f"browser_open 未返回 CDP endpoint，响应：{result}")

    cdp_endpoint = normalize_cdp_endpoint(raw_ep, base_url=base_url)
    return cdp_endpoint, window_key, window_name


async def _cdp_capture_page(
    page,
    page_label: str,
    out_dir: Path,
    req: "CaptureRequest",
    files: List[str],
    errors: List[str],
) -> List[Dict[str, Any]]:
    """
    通过原始 CDP 协议采集单个页面的全部数据。
    策略：先挂 Network 监听器，再 reload 页面，等加载完成后
    用 getResponseBody 取所有响应体（含 async JS）。
    """
    req_map: Dict[str, Dict[str, Any]] = {}

    cdp = await page.context.new_cdp_session(page)

    try:
        await cdp.send("Network.enable", {
            "maxTotalBufferSize": 200 * 1024 * 1024,
            "maxResourceBufferSize": 20 * 1024 * 1024,
        })
    except Exception as e:
        errors.append(f"[{page_label}] Network.enable 失败: {e}")

    # ---- 先挂监听器，再 reload ----
    def _on_request(params):
        rid = params.get("requestId", "")
        r = params.get("request", {})
        req_map[rid] = {
            "requestId": rid,
            "url": r.get("url", ""),
            "method": r.get("method", ""),
            "request_headers": r.get("headers", {}),
            "post_data": r.get("postData"),
            "response_status": None,
            "response_headers": {},
            "response_body": None,
            "resource_type": params.get("type", ""),
        }

    def _on_response(params):
        rid = params.get("requestId", "")
        resp = params.get("response", {})
        entry = req_map.setdefault(rid, {
            "requestId": rid,
            "url": resp.get("url", ""),
            "method": "",
            "request_headers": {},
            "post_data": None,
            "response_status": None,
            "response_headers": {},
            "response_body": None,
            "resource_type": params.get("type", ""),
        })
        entry["response_status"] = resp.get("status")
        entry["response_headers"] = resp.get("headers", {})
        entry["url"] = entry["url"] or resp.get("url", "")

    cdp.on("Network.requestWillBeSent", _on_request)
    cdp.on("Network.responseReceived", _on_response)

    # 禁用缓存，确保第三方 CDN JS 等资源不从磁盘缓存读取，强制走网络
    try:
        await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
    except Exception:
        pass

    # reload 页面，让所有资源重新走一遍网络
    try:
        await page.reload(wait_until="networkidle", timeout=60000)
    except Exception:
        # networkidle 超时也没关系，继续采集已有数据
        pass

    # 额外等待 async 脚本执行完
    await asyncio.sleep(3.0)

    cdp.remove_listener("Network.requestWillBeSent", _on_request)
    cdp.remove_listener("Network.responseReceived", _on_response)

    # 恢复缓存
    try:
        await cdp.send("Network.setCacheDisabled", {"cacheDisabled": False})
    except Exception:
        pass

    # ---- 对所有已知 requestId 尝试获取 response body ----
    network_log: List[Dict[str, Any]] = []
    js_dir = out_dir / "js"
    src_dir = out_dir / "source"
    js_counter = 0

    for rid, entry in req_map.items():
        url = entry.get("url", "")
        rtype = (entry.get("resource_type") or "").lower()
        ct_hint = ""
        for v in entry.get("response_headers", {}).values():
            if "javascript" in str(v).lower() or "text/" in str(v).lower():
                ct_hint = str(v).lower()
                break

        is_js = (
            rtype in ("script", "javascript")
            or url.endswith(".js")
            or "javascript" in ct_hint
            or ".js?" in url
        )
        is_text = rtype in ("xhr", "fetch", "document", "stylesheet") or "text/" in ct_hint or "json" in ct_hint

        need_body = (req.capture_js and is_js) or (req.capture_network and (is_js or is_text))
        if need_body:
            try:
                result = await cdp.send("Network.getResponseBody", {"requestId": rid})
                body = result.get("body", "")
                if result.get("base64Encoded"):
                    import base64
                    body = base64.b64decode(body).decode("utf-8", errors="replace")
                entry["response_body"] = body

                # 保存 JS 文件
                if req.capture_js and is_js and body:
                    js_dir.mkdir(exist_ok=True)
                    fname_base = _url_to_filename(url)
                    fname = js_dir / f"{page_label}__cdp_{js_counter:03d}__{fname_base}.js"
                    fname.write_text(body, encoding="utf-8")
                    files.append(f"js/{fname.name}")
                    js_counter += 1
            except Exception:
                pass  # body 不可用（已被清除或未完成）

        network_log.append(entry)

    # ---- 内联脚本（仍需 DOM 扫描，但等页面稳定后） ----
    if req.capture_js:
        try:
            inline_scripts = await page.evaluate("""() => {
                const res = [];
                document.querySelectorAll('script:not([src])').forEach((s, i) => {
                    const t = (s.textContent || '').trim();
                    if (t) res.push({index: i, text: t});
                });
                return res;
            }""")
            for s in inline_scripts or []:
                js_dir.mkdir(exist_ok=True)
                fname = js_dir / f"{page_label}__inline_{s['index']:03d}.js"
                fname.write_text(s["text"], encoding="utf-8")
                files.append(f"js/{fname.name}")
        except Exception as e:
            errors.append(f"[{page_label}] 内联脚本采集失败: {e}")

    # ---- HTML 源码 ----
    if req.capture_source:
        try:
            html = await page.content()
            src_dir.mkdir(exist_ok=True)
            sf = src_dir / f"{page_label}.html"
            sf.write_text(html, encoding="utf-8")
            files.append(f"source/{sf.name}")
        except Exception as e:
            errors.append(f"[{page_label}] HTML 源码失败: {e}")

    try:
        await cdp.detach()
    except Exception:
        pass

    return network_log


async def _do_capture(req: CaptureRequest) -> Dict[str, Any]:
    t0 = time.monotonic()
    errors: List[str] = []
    files: List[str] = []

    cdp_endpoint, window_key, window_name = await _get_window_open_info(req.window_pk)

    # 输出目录：analyze/{window_key}/YYYYMMDD_HHMMSS/
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = _ANALYZE_ROOT / _safe_filename(window_key) / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_endpoint)
        except Exception as e:
            errors.append(f"CDP 连接失败: {e}")
            elapsed = time.monotonic() - t0
            return {
                "success": False,
                "window_id": window_key,
                "window_name": window_name,
                "output_dir": str(out_dir),
                "files": files,
                "errors": errors,
                "elapsed_seconds": round(elapsed, 2),
            }

        try:
            contexts = browser.contexts
            if not contexts:
                errors.append("浏览器无可用 context")
            else:
                ctx = contexts[0]
                pages = list(ctx.pages)

                if not pages:
                    errors.append("浏览器无打开的页面")
                else:
                    # cookies
                    if req.capture_cookies:
                        try:
                            cookies = await ctx.cookies()
                            cf = out_dir / "cookies.json"
                            cf.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
                            files.append("cookies.json")
                        except Exception as e:
                            errors.append(f"采集 cookies 失败: {e}")

                    network_all: List[Dict[str, Any]] = []

                    for page_idx, page in enumerate(pages):
                        try:
                            page_url = page.url or f"page_{page_idx}"
                        except Exception:
                            page_url = f"page_{page_idx}"

                        page_label = f"p{page_idx:02d}__{_url_to_filename(page_url)}"

                        try:
                            net = await _cdp_capture_page(
                                page, page_label, out_dir, req, files, errors
                            )
                            for entry in net:
                                entry["_page_index"] = page_idx
                                entry["_page_url"] = page_url
                            network_all.extend(net)
                        except Exception as e:
                            errors.append(f"[{page_label}] CDP 采集失败: {e}")

                    if req.capture_network:
                        nf = out_dir / "network_log.json"
                        nf.write_text(json.dumps(network_all, ensure_ascii=False, indent=2), encoding="utf-8")
                        files.insert(0, "network_log.json")

        finally:
            try:
                await browser.close()
            except Exception:
                pass

    elapsed = time.monotonic() - t0

    summary = {
        "window_pk": req.window_pk,
        "window_key": window_key,
        "window_name": window_name,
        "captured_at": datetime.now().isoformat(),
        "elapsed_seconds": round(elapsed, 2),
        "files": files,
        "errors": errors,
        "options": {
            "capture_network": req.capture_network,
            "capture_source": req.capture_source,
            "capture_js": req.capture_js,
            "capture_cookies": req.capture_cookies,
            "include_static_js": req.include_static_js,
        },
    }
    sf = out_dir / "summary.json"
    sf.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    files.append("summary.json")

    logger.info(
        "analyze capture done: window_key=%s out_dir=%s files=%d errors=%d elapsed=%.1fs",
        window_key, out_dir, len(files), len(errors), elapsed,
    )

    return {
        "success": True,
        "window_id": window_key,
        "window_name": window_name,
        "output_dir": str(out_dir),
        "files": files,
        "errors": errors,
        "elapsed_seconds": round(elapsed, 2),
    }


@router.get("/api/admin/analyze/windows")
async def list_windows_for_analyze(token: str = Depends(_verify_token)):
    """返回所有窗口的扁平列表，供前端选择。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    from . import admin as admin_mod
    tree = await db.get_project_tree(project_id=None, allowed_project_ids=None)
    flat: List[Dict[str, Any]] = []
    for p in tree:
        for b in p.get("browsers", []):
            for s in b.get("spaces", []):
                for w in s.get("windows", []):
                    flat.append({
                        "window_pk": w.get("id"),
                        "window_key": w.get("window_key"),
                        "window_name": w.get("window_name") or w.get("window_key"),
                        "window_sort_num": w.get("window_sort_num"),
                        "platform_account": w.get("platform_account"),
                        "platform_url": w.get("platform_url"),
                        "space_name": s.get("name"),
                        "browser_name": b.get("name"),
                        "project_name": p.get("name"),
                    })
    return {"success": True, "windows": flat}


@router.post("/api/admin/analyze/capture")
async def capture_window_data(req: CaptureRequest, token: str = Depends(_verify_token)):
    """一键采集指定窗口的所有数据并保存到 analyze/ 目录。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    try:
        result = await _do_capture(req)
        return result
    except Exception as e:
        logger.exception("analyze capture error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/admin/analyze/task-types")
async def list_task_types_for_analyze(token: str = Depends(_verify_token)):
    """返回任务类型列表供前端筛选窗口。"""
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    items = await db.list_task_types()
    return {
        "success": True,
        "task_types": [
            {"id": t.id, "code": t.code, "name": t.name}
            for t in items
            if not t.deleted and t.enabled
        ],
    }
