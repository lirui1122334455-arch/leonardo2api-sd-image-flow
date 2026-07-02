"""Playwright 指纹浏览器上下文（通用能力）。

注意：文件名按项目历史约定使用 `broswer`（拼写保留），用于承载“指纹浏览器自动化”的通用能力。

目标：
- 仅负责“指纹浏览器窗口自动化”的通用能力：打开/关闭窗口、连接 CDP、选择可用页面、在页面上下文里 fetch。
- 不包含任何特定站点（如 sora.chatgpt.com / jimeng.jianying.com）的业务逻辑。

说明：
- `FPBrowserClient` 是对指纹浏览器局域网 API 的适配层，本模块只调用它，**不要改动 fp_browser_client.py**。
- 站点侧执行器（如 `sora_task_executor.py` / 未来的 `jimeng_task_executor.py`）应组合使用本模块提供的上下文能力。
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from ..core.database import Database
from .fp_browser_client import FPBrowserClient
from .task_executor_types import NonPenalizedTaskError

# ---------------------------------------------------------------------------
# 浏览器打开并发控制：按 browser_base_url 区分服务器，每台服务器独立限制 N 个并发
# ---------------------------------------------------------------------------
_BROWSER_OPEN_SEMS: Dict[str, asyncio.Semaphore] = {}
_BROWSER_OPEN_SEM_LOCK = threading.Lock()
_BROWSER_OPEN_SEM_LIMIT: int = 3
_BROWSER_OPEN_QUEUE_TIMEOUT: float = 120.0


def _normalize_base_url_for_key(base_url: str) -> str:
    """规范化 base_url 用作并发池 key（同一台指纹浏览器服务器共享一个信号量）。"""
    s = (base_url or "").strip().rstrip("/").lower()
    return s or "__default__"


def _get_or_create_semaphore_for_base_url(base_url: str) -> asyncio.Semaphore:
    """按 base_url 获取或创建信号量（每台服务器独立 3 个并发）。"""
    key = _normalize_base_url_for_key(base_url)
    with _BROWSER_OPEN_SEM_LOCK:
        sem = _BROWSER_OPEN_SEMS.get(key)
        if sem is None:
            sem = asyncio.Semaphore(_BROWSER_OPEN_SEM_LIMIT)
            _BROWSER_OPEN_SEMS[key] = sem
    return sem


def set_browser_open_concurrency(limit: int) -> None:
    """动态调整每台服务器的浏览器打开并发上限（热更新；新建的服务器池使用新值，已有池不变）。"""
    global _BROWSER_OPEN_SEM_LIMIT
    _BROWSER_OPEN_SEM_LIMIT = max(1, int(limit))
    with _BROWSER_OPEN_SEM_LOCK:
        _BROWSER_OPEN_SEMS.clear()


def set_browser_open_queue_timeout(seconds: float) -> None:
    """动态调整排队等待超时（秒）。"""
    global _BROWSER_OPEN_QUEUE_TIMEOUT
    _BROWSER_OPEN_QUEUE_TIMEOUT = max(10.0, float(seconds))


def get_browser_open_concurrency() -> int:
    """返回当前每台服务器的浏览器打开并发上限。"""
    return _BROWSER_OPEN_SEM_LIMIT


def get_browser_open_queue_timeout() -> float:
    """返回当前排队等待超时（秒）。"""
    return _BROWSER_OPEN_QUEUE_TIMEOUT


@asynccontextmanager
async def acquire_browser_open_slot(base_url: str):
    """带超时的浏览器打开信号量上下文管理器。

    - 按 base_url 区分服务器，每台服务器独立限制并发
    - 排队等待最多 _BROWSER_OPEN_QUEUE_TIMEOUT 秒
    - 超时后抛出 NonPenalizedTaskError 让任务快速失败
    """
    sem = _get_or_create_semaphore_for_base_url(base_url)
    try:
        await asyncio.wait_for(sem.acquire(), timeout=_BROWSER_OPEN_QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        raise NonPenalizedTaskError(
            f"browser_open 打开排队超时（该服务器 base_url={safe_trim(base_url, 80)!r}，"
            f"每台并发上限={_BROWSER_OPEN_SEM_LIMIT}，排队已超过 {_BROWSER_OPEN_QUEUE_TIMEOUT:.0f}s），请稍后重试"
        )
    try:
        yield
    finally:
        sem.release()


def safe_trim(s: Optional[str], max_len: int = 300) -> str:
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= max_len else s[:max_len] + "...(truncated)"


def append_log(log_file: Path, s: str) -> None:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8", newline="\n") as f:
            f.write(s)
            if not s.endswith("\n"):
                f.write("\n")
    except Exception:
        pass


def normalize_cdp_endpoint(endpoint: str, *, base_url: Optional[str] = None) -> str:
    """将指纹浏览器返回的 http/ws 调试地址规范化为 Playwright 可连接的 endpoint。"""
    s = (endpoint or "").strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://", "ws://", "wss://")):
        normalized = s
    else:
        # 常见返回：127.0.0.1:9222
        normalized = "http://" + s

    # 将 debugger endpoint 的主机对齐到 base_url，避免 API 在远端机器时仍回传 127.0.0.1 导致 ECONNREFUSED。
    base = (base_url or "").strip()
    if not base:
        return normalized
    if "://" not in base:
        base = "http://" + base

    try:
        endpoint_u = urlparse(normalized)
        base_u = urlparse(base)
        host = (base_u.hostname or "").strip()
        if not host or not endpoint_u.scheme:
            return normalized

        if ":" in host and not host.startswith("["):
            host = f"[{host}]"

        userinfo = ""
        if endpoint_u.username:
            userinfo = endpoint_u.username
            if endpoint_u.password:
                userinfo += f":{endpoint_u.password}"
            userinfo += "@"

        netloc = f"{userinfo}{host}"
        if endpoint_u.port is not None:
            netloc += f":{endpoint_u.port}"
        return urlunparse(endpoint_u._replace(netloc=netloc))
    except Exception:
        return normalized


def _is_probably_navigable_url(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u:
        return True
    if u.startswith(("http://", "https://", "about:blank")):
        return True
    # 不建议用这些页面做自动化入口页（通常无法 goto 或 DOM 不符合预期）
    if u.startswith(("chrome://", "edge://", "chrome-extension://", "moz-extension://", "devtools://", "view-source:")):
        return False
    return False


async def pick_working_page_from_context(ctx) -> Any:
    """从 context.pages 中挑一个“可用页面”；否则创建新页。"""
    try:
        pages = list(getattr(ctx, "pages", []) or [])
    except Exception:
        pages = []

    best = None
    best_score = -10
    for p in pages:
        try:
            is_closed = bool(getattr(p, "is_closed", lambda: False)())
        except Exception:
            is_closed = False
        if is_closed:
            continue
        try:
            u = str(getattr(p, "url", "") or "")
        except Exception:
            u = ""
        if not _is_probably_navigable_url(u):
            continue
        score = 0
        if u.startswith(("http://", "https://")):
            score += 10
        if u.startswith("about:blank") or not u:
            score += 3
        if score > best_score:
            best_score = score
            best = p

    if best is not None:
        return best
    return await ctx.new_page()


async def page_fetch_tx(
    page,
    *,
    url: str,
    method: str,
    headers: Dict[str, str],
    json_data: Optional[Dict[str, Any]],
    log_file: Path,
) -> Dict[str, Any]:
    """在“浏览器页面上下文”里 fetch（走指纹浏览器自己的网络栈/代理/DNS），返回兼容 tx 结构。"""
    if page is None:
        raise RuntimeError("page 为 None（窗口可能已被自动回收/关闭），无法执行 page.fetch")
    tx: Dict[str, Any] = {
        "seen": True,
        "request_id": None,
        "url": url,
        "method": str(method or "GET").upper().strip(),
        "status": None,
        "response_body": None,
        "headers": None,
        "log_file": str(log_file),
    }

    blocked = {"user-agent", "host", "cookie", "content-length", "accept-encoding", "connection", "origin", "referer"}
    safe_headers: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        if not k:
            continue
        lk = str(k).strip().lower()
        if lk in blocked:
            continue
        safe_headers[str(k)] = str(v)

    try:
        res = await page.evaluate(
            """async (args) => {
              const { url, method, headers, body } = args;
              const init = {
                method,
                headers: headers || {},
                credentials: 'include',
              };
              if (body !== null && body !== undefined) {
                init.body = JSON.stringify(body);
              }
              const resp = await fetch(url, init);
              const text = await resp.text();
              const hdrs = {};
              try {
                for (const [k, v] of resp.headers.entries()) hdrs[k] = v;
              } catch (e) {}
              return { status: resp.status, text, headers: hdrs };
            }""",
            {"url": url, "method": tx["method"], "headers": safe_headers, "body": json_data},
        )
    except Exception as e:
        append_log(log_file, f"[browser][page_fetch] fetch failed url={url!r} err={e}")
        raise

    try:
        tx["status"] = int((res or {}).get("status")) if (res or {}).get("status") is not None else None
    except Exception:
        tx["status"] = None
    try:
        tx["response_body"] = str((res or {}).get("text") or "")
    except Exception:
        tx["response_body"] = ""
    try:
        tx["headers"] = dict((res or {}).get("headers") or {})
    except Exception:
        tx["headers"] = None

    append_log(log_file, f"[browser][page_fetch] {tx['method']} url={url!r} status={tx['status']}")
    append_log(log_file, f"[browser][page_fetch] body={safe_trim(str(tx.get('response_body') or ''), 800)!r}")
    return tx

def build_jimeng_page_fetch_headers(
    *,
    uri: str,
    headers: Optional[Dict[str, str]] = None,
    appid: int = 513641,
    appvr: str = "8.4.0",
    pf: str = "7",
    lan: str = "en",
    loc: str = "US",
    tdid: Optional[str] = None,
    device_time: Optional[int] = None,
) -> Dict[str, str]:
    """构造 Jimeng/Dreamina 请求通用 headers。

    参考 jimeng2me_newapi/src/api/controllers/core.ts 的 request()：
    Sign = md5(`9e2c|${uri.slice(-7)}|${pf}|${appvr}|${deviceTime}||11ac`)

    这里只封装 header；Cookie 仍由 page.fetch 的 credentials: include 携带当前指纹窗口 Cookie。
    """
    dt = int(device_time or time.time())
    path = str(uri or "")
    try:
        parsed_path = urlparse(path).path
        if parsed_path:
            path = parsed_path
    except Exception:
        pass
    sign_src = f"9e2c|{path[-7:]}|{pf}|{appvr}|{dt}||11ac"
    sign = hashlib.md5(sign_src.encode("utf-8")).hexdigest()
    out: Dict[str, str] = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Lan": str(lan),
        "Appid": str(appid),
        "Appvr": str(appvr),
        "Device-Time": str(dt),
        "Sign": sign,
        "Sign-Ver": "1",
        "Pf": str(pf),
        "loc": str(loc or "US"),
        "tdid": str(tdid or ""),
    }
    for k, v in (headers or {}).items():
        if k:
            out[str(k)] = str(v)
    return out

def build_jimeng_page_fetch_headers_body(
    *,
    uri: str,
    headers: Optional[Dict[str, str]] = None,
    appid: int = 513641,
    appvr: str = "8.4.0",
    pf: str = "7",
    lan: str = "en",
    loc: str = "US",
    tdid: Optional[str] = None,
    device_time: Optional[int] = None,
) -> Dict[str, str]:
    """构造 Jimeng/Dreamina 请求通用 headers。

    参考 jimeng2me_newapi/src/api/controllers/core.ts 的 request()：
    Sign = md5(`9e2c|${uri.slice(-7)}|${pf}|${appvr}|${deviceTime}||11ac`)

    这里只封装 header；Cookie 仍由 page.fetch 的 credentials: include 携带当前指纹窗口 Cookie。
    """
    dt = int(device_time or time.time())
    path = str(uri or "")
    try:
        parsed_path = urlparse(path).path
        if parsed_path:
            path = parsed_path
    except Exception:
        pass
    sign_src = f"9e2c|{path[-7:]}|{pf}|{appvr}|{dt}||11ac"
    sign = hashlib.md5(sign_src.encode("utf-8")).hexdigest()
    out: Dict[str, str] = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Lan": str(lan),
        "app-sdk-version": "48.0.0",
        "Appid": str(appid),
        "Appvr": str(appvr),
        "Device-Time": str(dt),
        "Sign": sign,
        "Sign-Ver": "1",
        "Pf": str(pf),
        "loc": str(loc or "US"),
        "tdid": str(tdid or ""),
    }
    for k, v in (headers or {}).items():
        if k:
            out[str(k)] = str(v)
    return out

def _api_response_location_header(resp: Any) -> str:
    """从 Playwright APIResponse 取 Location（headers 多为小写 key；必要时用 headers_array）。"""
    try:
        h = getattr(resp, "headers", None)
        if isinstance(h, dict):
            for k, v in h.items():
                if str(k or "").lower() == "location" and v:
                    return str(v).strip()
    except Exception:
        pass
    try:
        arr = getattr(resp, "headers_array", None) or []
        for item in arr:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").lower() != "location":
                continue
            v = str(item.get("value") or "").strip()
            if v:
                return v
    except Exception:
        pass
    return ""


async def _resolve_redirect_via_page_goto(
    context: Any,
    url: str,
    *,
    referer: Optional[str],
    log_file: Path,
) -> Optional[str]:
    """APIRequestContext.get 在部分环境会优先连 IPv6 导致 ETIMEDOUT；用真实页面 goto 跟随重定向，与地址栏一致。"""
    u = str(url or "").strip()
    if not u:
        return None
    p2: Any = None
    try:
        p2 = await context.new_page()
        kw: Dict[str, Any] = {"wait_until": "commit", "timeout": 60_000}
        ref = str(referer or "").strip()
        if ref.startswith(("http://", "https://")):
            kw["referer"] = ref
        await p2.goto(u, **kw)
        final_u = str(getattr(p2, "url", None) or "").strip()
        if not final_u or urlparse(final_u).scheme not in ("http", "https"):
            append_log(
                log_file,
                f"[browser][page_resolve_redirect] goto_fallback 无效 final url={safe_trim(final_u, 80)!r}",
            )
            return None
        if final_u.rstrip("/") == u.rstrip("/"):
            append_log(
                log_file,
                f"[browser][page_resolve_redirect] goto_fallback 未发生跳转 url={safe_trim(u, 100)!r}",
            )
            return None
        append_log(
            log_file,
            f"[browser][page_resolve_redirect] goto_fallback ok -> {safe_trim(final_u, 120)!r}",
        )
        return final_u
    except Exception as e:
        append_log(
            log_file,
            f"[browser][page_resolve_redirect] goto_fallback err url={safe_trim(u, 90)!r} err={e!r}",
        )
        return None
    finally:
        if p2 is not None:
            try:
                await p2.close()
            except Exception:
                pass


async def page_resolve_redirect_url(
    page,
    *,
    url: str,
    log_file: Path,
    max_hops: int = 8,
) -> Optional[str]:
    """用 Playwright `page.request`（与页面共享 Cookie / UA）解析重定向，读取 302 的 Location。

    页面内 `fetch(..., redirect: 'manual')` 对跨站请求常得到 status=0（opaque / 被策略拦截），
    而 `page.request` 不受页面 CORS 限制；但若其底层连 IPv6 超时，则回退为同上下文新建标签页 `goto`
    自动跟随重定向，取最终地址栏 URL。
    """
    if page is None:
        raise RuntimeError("page 为 None，无法解析重定向")
    u = str(url or "").strip()
    if not u:
        return None
    context = getattr(page, "context", None)
    if context is None:
        append_log(log_file, "[browser][page_resolve_redirect] page.context 为 None")
        return None

    referer = ""
    try:
        pu = str(getattr(page, "url", "") or "").strip()
        if pu.startswith(("http://", "https://")):
            referer = pu
    except Exception:
        pass

    current = u
    n_hops = int(max(1, max_hops))
    for hop in range(n_hops):
        extra_headers: Dict[str, str] = {}
        if referer:
            extra_headers["Referer"] = referer
        extra_headers.setdefault(
            "Accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        )
        try:
            _req_kw: Dict[str, Any] = {"max_redirects": 0}
            if extra_headers:
                _req_kw["headers"] = extra_headers
            resp = await page.request.get(current, **_req_kw)
        except Exception as e:
            append_log(
                log_file,
                f"[browser][page_resolve_redirect] request.get 失败 hop={hop}，尝试 goto 回退: {e!r}",
            )
            return await _resolve_redirect_via_page_goto(
                context, current, referer=referer or None, log_file=log_file
            )

        try:
            status = int(resp.status)
            if 300 <= status < 400:
                loc = _api_response_location_header(resp)
                if not loc:
                    try:
                        hk = list((getattr(resp, "headers", None) or {}).keys())
                    except Exception:
                        hk = []
                    append_log(
                        log_file,
                        f"[browser][page_resolve_redirect] no Location hop={hop} status={status} header_keys={hk!r}",
                    )
                    return None
                nxt = urljoin(current, loc)
                cur_host = (urlparse(current).hostname or "").lower()
                nex_host = (urlparse(nxt).hostname or "").lower()
                if nex_host and nex_host != cur_host:
                    append_log(
                        log_file,
                        f"[browser][page_resolve_redirect] ok url={safe_trim(u, 80)!r} -> {safe_trim(nxt, 120)!r}",
                    )
                    return nxt
                current = nxt
                continue
            if 200 <= status < 300:
                final_u = str(getattr(resp, "url", None) or current).strip()
                if final_u:
                    append_log(
                        log_file,
                        f"[browser][page_resolve_redirect] ok(200) url={safe_trim(u, 80)!r} -> {safe_trim(final_u, 120)!r}",
                    )
                    return final_u
                return None
            append_log(
                log_file,
                f"[browser][page_resolve_redirect] bad status={status} hop={hop} url={safe_trim(current, 100)!r}",
            )
            return None
        finally:
            try:
                await resp.dispose()
            except Exception:
                pass

    append_log(log_file, f"[browser][page_resolve_redirect] too_many_hops url={safe_trim(u, 80)!r}")
    return None


async def page_fetch_json(
    page,
    *,
    url: str,
    method: str,
    headers: Dict[str, str],
    json_data: Optional[Dict[str, Any]],
    log_file: Path,
) -> Dict[str, Any]:
    """页面内 fetch 并解析 JSON（失败则抛出带 response 文本摘要的异常）。"""
    tx = await page_fetch_tx(page, url=url, method=method, headers=headers, json_data=json_data, log_file=log_file)
    body = str(tx.get("response_body") or "")
    try:
        obj = json.loads(body) if body else None
    except Exception:
        obj = None
    if obj is None and (tx.get("status") not in (204,)):
        raise RuntimeError(f"fetch_json 解析失败：status={tx.get('status')} body={safe_trim(body, 600)}")
    tx["_json"] = obj
    return tx


@dataclass
class PlaywrightBrowserContext:
    """通用的 Playwright + 指纹浏览器窗口上下文。"""

    cache_key: str
    vendor: str
    base_url: str
    access_key: Optional[str]
    space_id: str
    window_key: str
    fp_client: FPBrowserClient
    db: Database = field(default_factory=Database)

    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    cdp_endpoint: Optional[str] = None
    last_used_at: float = field(default_factory=lambda: time.time())

    driver_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def _sync_window_status(self, opened: bool) -> None:
        try:
            await self.db.update_window_status_by_space_and_key(
                space_id=self.space_id,
                window_key=self.window_key,
                window_status=1 if opened else 0,
            )
        except Exception:
            pass

    async def ensure_open(
        self,
        *,
        args: Optional[list[str]] = None,
        force_open: bool = False,
        headless: bool = False,
        require_page: bool = True,
        pure_mode: bool = True,
    ) -> None:
        self.last_used_at = time.time()

        # 优先走“复用已连接的 browser/context”。
        # 但如果用户手动关掉了指纹浏览器，缓存中的句柄会变成“非空但失效”，
        # 这里先做活性检查，避免后续在 new_page/goto 时报 TargetClosedError。
        if not force_open and self.browser is not None and self.context is not None:
            browser_ok = True
            try:
                is_connected_fn = getattr(self.browser, "is_connected", None)
                if callable(is_connected_fn):
                    browser_ok = bool(is_connected_fn())
            except Exception:
                browser_ok = False

            context_ok = True
            if browser_ok:
                try:
                    _ = list(getattr(self.context, "pages", []) or [])
                except Exception:
                    context_ok = False
            else:
                context_ok = False

            if browser_ok and context_ok:
                await self._sync_window_status(True)
                return

            # 失效句柄：清空后走下面的重连流程
            self.browser = None
            self.context = None
            self.page = None

        try:
            # 优先使用 patchright（playwright 的 stealth fork）：
            # patch 掉 CDP Runtime.consoleAPICalled 触发的 stack getter 检测、
            # isolated world 泄漏、--enable-automation 等典型自动化指纹，
            # 用来降低 reCAPTCHA Enterprise / Google 风控对 CDP 连接的识别概率。
            # 若未安装则回退到原版 playwright，保证旧环境仍能跑。
            #try:
            #    from patchright.async_api import async_playwright  # type: ignore
            #except Exception:
            from playwright.async_api import async_playwright  # type: ignore
        except Exception as e:
            await self._sync_window_status(False)
            raise RuntimeError(
                f"Playwright/Patchright 未安装或导入失败，请先安装依赖："
                f"pip install patchright（推荐）或 pip install playwright；错误：{e}"
            )

        if force_open:
            await self.disconnect_playwright_only()
            try:
                await self.fp_client.browser_close(
                    vendor=self.vendor,
                    base_url=self.base_url,
                    access_key=self.access_key,
                    window_key=self.window_key,
                )
            except Exception:
                pass
            for _ in range(20):
                try:
                    conn = await self.fp_client.get_open_window_connection_info(
                        vendor=self.vendor,
                        base_url=self.base_url,
                        access_key=self.access_key,
                        window_key=self.window_key,
                    )
                    if not conn:
                        break
                except Exception:
                    break
                await asyncio.sleep(0.5)

        raw_endpoint = ""
        try:
            if not force_open:
                conn = await self.fp_client.get_open_window_connection_info(
                    vendor=self.vendor,
                    base_url=self.base_url,
                    access_key=self.access_key,
                    window_key=self.window_key,
                )
                if conn:
                    raw_endpoint = str(conn.get("http") or conn.get("ws") or "").strip()
        except Exception:
            pass

        if not raw_endpoint:
            # 需要打开新浏览器窗口 —— 按 base_url 分服务器限流，每台独立 N 个并发
            async with acquire_browser_open_slot(self.base_url):
                rsp = await self.fp_client.browser_open(
                    vendor=self.vendor,
                    base_url=self.base_url,
                    access_key=self.access_key,
                    space_id=self.space_id,
                    window_key=self.window_key,
                    args=args or [],
                    force_open=bool(force_open),
                    headless=bool(headless),
                    pure_mode=bool(pure_mode),
                )
                if (rsp or {}).get("code") != 0:
                    try:
                        conn = await self.fp_client.get_open_window_connection_info(
                            vendor=self.vendor,
                            base_url=self.base_url,
                            access_key=self.access_key,
                            window_key=self.window_key,
                        )
                        if conn:
                            raw_endpoint = str(conn.get("http") or conn.get("ws") or "").strip()
                    except Exception:
                        pass
                    if not raw_endpoint:
                        await self._sync_window_status(False)
                        raise RuntimeError(f"browser_open 失败：{rsp}")

                if not raw_endpoint:
                    data = (rsp or {}).get("data") or {}
                    raw_endpoint = str(data.get("http") or data.get("ws") or "").strip()

                debugger_address = normalize_cdp_endpoint(raw_endpoint, base_url=self.base_url)
                if not debugger_address:
                    await self._sync_window_status(False)
                    raise RuntimeError(f"无法获取 http/ws(CDP endpoint)：raw={raw_endpoint!r}")
                self.cdp_endpoint = debugger_address
                await asyncio.sleep(3)
            await asyncio.sleep(15)
        else:
            debugger_address = normalize_cdp_endpoint(raw_endpoint, base_url=self.base_url)
            if not debugger_address:
                await self._sync_window_status(False)
                raise RuntimeError(f"无法获取 http/ws(CDP endpoint)：raw={raw_endpoint!r}")
            self.cdp_endpoint = debugger_address
        if self.playwright is None:
            try:
                self.playwright = await async_playwright().start()
            except Exception as e:
                await self._sync_window_status(False)
                raise RuntimeError(f"Playwright 启动失败：{e}") from e

        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(debugger_address)
        except Exception as e:
            await self._sync_window_status(False)
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
            self.browser = None
            raise RuntimeError(f"连接CDP失败：endpoint={debugger_address} err={e}") from e

        try:
            ctxs = list(getattr(self.browser, "contexts", []) or [])
        except Exception:
            ctxs = []
        if ctxs:
            best_ctx = None
            best_score = -1
            for c in ctxs:
                try:
                    pages = list(getattr(c, "pages", []) or [])
                except Exception:
                    pages = []
                score = 0
                for p in pages:
                    try:
                        u = str(getattr(p, "url", "") or "")
                    except Exception:
                        u = ""
                    if u.startswith(("http://", "https://")):
                        score += 2
                    elif u.startswith("about:blank") or not u:
                        score += 1
                if score > best_score:
                    best_score = score
                    best_ctx = c
            self.context = best_ctx or ctxs[0]
        else:
            self.context = await self.browser.new_context()
        await self._sync_window_status(True)

    async def disconnect_playwright_only(self) -> None:
        """关闭本地 Playwright/CDP 连接，不调用指纹 API 关窗（指纹侧窗口可保持打开）。"""
        br = self.browser
        self.browser = None
        self.context = None
        self.page = None
        self.cdp_endpoint = None
        try:
            if br is not None:
                await br.close()
        except Exception:
            pass
        pw = self.playwright
        self.playwright = None
        try:
            if pw is not None:
                await pw.stop()
        except Exception:
            pass

    async def disconnect_playwright_only_under_driver_lock(self) -> None:
        """在 driver_lock 下断开 CDP，与通过 driver_lock 进行的页面操作串行。"""
        async with self.driver_lock:
            await self.disconnect_playwright_only()

    async def open_fingerprint_window_only(
        self,
        *,
        args: Optional[List[str]] = None,
        force_open: bool = False,
        headless: bool = False,
        pure_mode: bool = True,
    ) -> None:
        """仅通过指纹 API 打开/唤起窗口：不 connect_over_cdp、不挑选或初始化 page。"""
        await self.disconnect_playwright_only()
        if force_open:
            try:
                await self.fp_client.browser_close(
                    vendor=self.vendor,
                    base_url=self.base_url,
                    access_key=self.access_key,
                    window_key=self.window_key,
                )
            except Exception:
                pass
            for _ in range(20):
                try:
                    conn = await self.fp_client.get_open_window_connection_info(
                        vendor=self.vendor,
                        base_url=self.base_url,
                        access_key=self.access_key,
                        window_key=self.window_key,
                    )
                    if not conn:
                        break
                except Exception:
                    break
                await asyncio.sleep(0.5)
        async with acquire_browser_open_slot(self.base_url):
            rsp = await self.fp_client.browser_open(
                vendor=self.vendor,
                base_url=self.base_url,
                access_key=self.access_key,
                space_id=self.space_id,
                window_key=self.window_key,
                args=list(args or []),
                force_open=bool(force_open),
                headless=bool(headless),
                pure_mode=bool(pure_mode),
            )
            if (rsp or {}).get("code") != 0:
                raw_endpoint = ""
                try:
                    conn = await self.fp_client.get_open_window_connection_info(
                        vendor=self.vendor,
                        base_url=self.base_url,
                        access_key=self.access_key,
                        window_key=self.window_key,
                    )
                    if conn:
                        raw_endpoint = str(conn.get("http") or conn.get("ws") or "").strip()
                except Exception:
                    pass
                if not raw_endpoint:
                    await self._sync_window_status(False)
                    raise RuntimeError(f"browser_open 失败：{rsp}")
        await self._sync_window_status(True)

    async def close(self) -> None:
        self.last_used_at = time.time()
        try:
            await self.fp_client.browser_close(
                vendor=self.vendor,
                base_url=self.base_url,
                access_key=self.access_key,
                window_key=self.window_key,
            )
        except Exception:
            pass
        await self._sync_window_status(False)

        br = self.browser
        self.browser = None
        self.context = None
        self.page = None
        try:
            if br is not None:
                await br.close()
        except Exception:
            pass

        pw = self.playwright
        self.playwright = None
        try:
            if pw is not None:
                await pw.stop()
        except Exception:
            pass

    async def close_and_drop(self) -> None:
        await self.close()
        drop_ctx(self.cache_key)


_CTX_LOCK = threading.Lock()
_CTX_POOL: Dict[str, PlaywrightBrowserContext] = {}


def _ctx_key(vendor: str, base_url: str, space_id: str, window_key: str) -> str:
    return "|".join([(vendor or "").strip().lower(), (base_url or "").strip().lower(), (space_id or "").strip(), (window_key or "").strip()])


def drop_ctx(cache_key: str) -> None:
    k = (cache_key or "").strip()
    if not k:
        return
    with _CTX_LOCK:
        _CTX_POOL.pop(k, None)


def get_or_create_ctx(
    *,
    vendor: str,
    base_url: str,
    access_key: Optional[str],
    space_id: str,
    window_key: str,
) -> PlaywrightBrowserContext:
    """获取/创建通用 PlaywrightBrowserContext（按 window 维度缓存）。"""
    k = _ctx_key(vendor, base_url, space_id, window_key)
    with _CTX_LOCK:
        ctx = _CTX_POOL.get(k)
        if ctx is None:
            ctx = PlaywrightBrowserContext(
                cache_key=k,
                vendor=(vendor or "roxy").strip().lower(),
                base_url=(base_url or "").strip().rstrip("/"),
                access_key=access_key,
                space_id=(space_id or "").strip(),
                window_key=(window_key or "").strip(),
                fp_client=FPBrowserClient(),
            )
            _CTX_POOL[k] = ctx
        else:
            ctx.access_key = access_key
        return ctx

