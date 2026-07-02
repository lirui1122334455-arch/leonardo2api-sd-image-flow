"""Browser extension interaction helpers.

该模块只放“Python 侧主动与浏览器插件交互”的能力：

- 通过已注册的 WebSocket client 下发任务（``submit_extension_task``）；
- 通过指纹浏览器打开一个本地/内网页面，把 ``fpb_*`` 参数交给插件，
  再让插件自行跳转到真正目标站点（``trigger_veo_extension_ws_connection_via_window``）。

WebSocket 路由、client 注册表仍由 ``browser_extension_bridge.py`` 负责。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..core.config import config
from ..core.paths import MONITOR_LOG_FILE
from .browser_extension_bridge import (
    get_default_extension_bridge_url,
    get_extension_client,
    wait_extension_client,
)
from .playwright_broswer_context import append_log
from .task_executor_types import NonPenalizedTaskError, ProgressCB


def get_default_extension_launcher_url() -> str:
    """插件配置中转页 URL。

    ``trigger_veo_extension_ws_connection_via_window`` 不再直接打开目标站点，
    而是打开这个由我们控制的页面，并在 hash 中放入：
    ``fpb_space_id`` / ``fpb_window_key`` / ``fpb_bridge_url`` / ``redirect_url``。

    优先级：
    1. 环境变量 ``FPB_EXTENSION_LAUNCHER_URL``；
    2. ``config/setting.toml`` 的 ``[extension_executor].launcher_url``；
    3. 根据 ``bridge_url`` 推导出同 host 的 http(s) 首页；
    4. 本机 ``server_port``。
    """
    raw = config.extension_launcher_url
    if raw:
        return raw

    bridge = get_default_extension_bridge_url()
    try:
        p = urlsplit(bridge)
        if p.netloc and p.scheme in {"ws", "wss"}:
            return urlunsplit(("https" if p.scheme == "wss" else "http", p.netloc, "/", "", ""))
    except Exception:
        pass

    return f"http://127.0.0.1:{int(config.server_port)}/"


def _short_text(value: Any, *, max_len: int = 500) -> str:
    try:
        s = str(value or "").strip()
    except Exception:
        s = ""
    if len(s) <= max_len:
        return s
    return s[: max(10, max_len - 3)] + "..."


def _extension_ids_from_session(
    sess: Any,
    *,
    space_id: Optional[str] = None,
    window_key: Optional[str] = None,
) -> tuple[str, str]:
    sid = str(space_id or getattr(getattr(sess, "pw_ctx", None), "space_id", "") or "").strip()
    wkey = str(window_key or getattr(getattr(sess, "pw_ctx", None), "window_key", "") or "").strip()
    if not sid or not wkey:
        raise RuntimeError("缺少插件连接标识：space_id/window_key")
    return sid, wkey


def _normalize_http_url(raw: Any, fallback: str) -> str:
    s = str(raw or "").strip() or str(fallback or "").strip()
    if not s:
        return ""
    try:
        p = urlsplit(s)
        if p.scheme in {"http", "https"} and p.netloc:
            return s
    except Exception:
        pass
    return str(fallback or "").strip()


def build_extension_launcher_url(
    *,
    redirect_url: str,
    space_id: str,
    window_key: str,
    launcher_url: Optional[str] = None,
) -> str:
    """构建插件配置中转页 URL。

    示例：

    ``http://192.168.1.9:8000/#fpb_space_id=...&fpb_window_key=...``
    ``&fpb_bridge_url=ws%3A%2F%2F...&redirect_url=https%3A%2F%2F...``
    """
    base = str(launcher_url or get_default_extension_launcher_url() or "").strip()
    if not base:
        base = f"http://127.0.0.1:{int(config.server_port)}/"
    if "://" not in base:
        base = "http://" + base

    try:
        parts = urlsplit(base)
        fragment_items = dict(parse_qsl(parts.fragment, keep_blank_values=True))
        fragment_items.update(
            {
                "fpb_space_id": str(space_id or "").strip(),
                "fpb_window_key": str(window_key or "").strip(),
                "fpb_bridge_url": get_default_extension_bridge_url(),
                "redirect_url": str(redirect_url or "").strip(),
            }
        )
        token = config.extension_bridge_token
        if token:
            fragment_items["fpb_bridge_token"] = token
        else:
            fragment_items.pop("fpb_bridge_token", None)
        path = parts.path or "/"
        return urlunsplit((parts.scheme or "http", parts.netloc, path, parts.query, urlencode(fragment_items)))
    except Exception:
        sep = "&" if "#" in base else "#"
        return (
            f"{base}{sep}"
            f"fpb_space_id={space_id}&fpb_window_key={window_key}"
            f"&fpb_bridge_url={get_default_extension_bridge_url()}"
            f"&redirect_url={redirect_url}"
        )


async def submit_extension_task(
    *,
    space_id: str,
    window_key: str,
    provider: str,
    payload: Dict[str, Any],
    progress_cb: ProgressCB,
    timeout_seconds: float,
) -> Dict[str, Any]:
    """向已连接的浏览器插件派发任务并等待结果。"""
    client = await get_extension_client(space_id, window_key)
    if client is None:
        raise NonPenalizedTaskError(
            f"浏览器插件未连接：space_id={space_id!r} window_key={window_key!r}",
            status_code=503,
        )

    task_id = str((payload or {}).get("task_id") or uuid.uuid4())
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    client.pending[task_id] = fut
    client.progress_cbs[task_id] = progress_cb
    msg = {
        "type": "task.start",
        "task_id": task_id,
        "provider": provider,
        "payload": dict(payload or {}),
    }
    msg["payload"]["_bridge_task_id"] = task_id
    try:
        async with client.send_lock:
            await client.websocket.send_json(msg)
        await progress_cb(1, {"stage": "extension_dispatched", "provider": provider})
        result = await asyncio.wait_for(
            fut,
            timeout=max(1.0, float(timeout_seconds or config.extension_task_timeout_seconds)),
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"extension returned invalid result: {result!r}")
        return result
    finally:
        client.pending.pop(task_id, None)
        client.progress_cbs.pop(task_id, None)


async def _bring_launcher_page_to_front(
    *,
    sess: Any,
    launcher_url: str,
    force_open: Optional[bool],
    headless: Optional[bool],
    pure_mode: Optional[bool],
) -> None:
    """沿用原来的 _bring_target_page_to_front 打开/置前中转页。

    这里短暂连接 CDP 只访问我们控制的 launcher_url；目标站点仍由插件在
    Python 断开 CDP 后再跳转打开。
    """
    fo = bool(getattr(sess, "browser_force_open", False) if force_open is None else force_open)
    hd = bool(getattr(sess, "browser_headless", False) if headless is None else headless)
    pm = bool(getattr(sess, "browser_pure_mode", True) if pure_mode is None else pure_mode)

    ensure_open = getattr(sess, "ensure_open", None)
    bring_to_front = getattr(sess, "_bring_target_page_to_front", None)
    if not callable(ensure_open) or not callable(bring_to_front):
        raise RuntimeError("session does not support ensure_open/_bring_target_page_to_front")

    await ensure_open(
        args=getattr(sess, "browser_open_args", []) or [],
        force_open=fo,
        headless=hd,
        acquire_bring_lock=False,
        pure_mode=pm,
    )
    await bring_to_front(
        # 中转页只负责把 fpb_* 参数交给插件；不要触发 _bring_target_page_to_front
        # 的 reload + 5 秒等待，尽快返回并断开 CDP，目标页交给插件延迟跳转。
        refresh_target=False,
        drafts_url=launcher_url,
        acquire_bring_lock=False,
    )


async def _disconnect_launcher_cdp(sess: Any) -> None:
    """中转页打开后立即断开 Python 侧 CDP/Playwright 连接。"""
    disconnect = getattr(sess, "disconnect_playwright_under_bring_lock", None)
    if callable(disconnect):
        await disconnect()
        return

    pw_ctx = getattr(sess, "pw_ctx", None)
    disconnect_under_lock = getattr(pw_ctx, "disconnect_playwright_only_under_driver_lock", None)
    if callable(disconnect_under_lock):
        await disconnect_under_lock()
        return

    disconnect_only = getattr(pw_ctx, "disconnect_playwright_only", None)
    if callable(disconnect_only):
        await disconnect_only()


async def trigger_veo_extension_ws_connection_via_window(
    *,
    sess: Any,
    target_url: str,
    space_id: Optional[str] = None,
    window_key: Optional[str] = None,
    wait_seconds: float = 8.0,
    force_open: Optional[bool] = None,
    headless: Optional[bool] = None,
    pure_mode: Optional[bool] = None,
    log_file: Optional[Path] = None,
    launcher_url: Optional[str] = None,
) -> Any:
    """打开插件配置中转页，触发插件连接 WebSocket，再由插件跳转目标页。

    旧流程会直接打开：

    ``https://labs.google/...#fpb_space_id=...&fpb_bridge_url=...``

    新流程改为打开我们控制的页面：

    ``http://192.168.1.9:8000/#fpb_space_id=...&fpb_bridge_url=...&redirect_url=https://labs.google/...``

    这样 Python/Playwright 不再主动打开目标站点；目标站点导航由浏览器插件完成。
    """
    sid, wkey = _extension_ids_from_session(sess, space_id=space_id, window_key=window_key)
    log_file = log_file or (Path(getattr(sess, "monitor_log_path", "")) if getattr(sess, "monitor_log_path", None) else MONITOR_LOG_FILE)
    redirect = _normalize_http_url(target_url, "https://labs.google/fx")
    annotated_launcher = build_extension_launcher_url(
        redirect_url=redirect,
        space_id=sid,
        window_key=wkey,
        launcher_url=launcher_url,
    )
    print(f"trigger_veo_extension_ws_connection_via_window:{annotated_launcher}")

    lock = getattr(sess, "_bring_drafts_lock", None)
    if lock is not None:
        async with lock:
            await _bring_launcher_page_to_front(
                sess=sess,
                launcher_url=annotated_launcher,
                force_open=force_open,
                headless=headless,
                pure_mode=pure_mode,
            )
    else:
        await _bring_launcher_page_to_front(
            sess=sess,
            launcher_url=annotated_launcher,
            force_open=force_open,
            headless=headless,
            pure_mode=pure_mode,
        )
    try:
        await _disconnect_launcher_cdp(sess)
    except Exception:
        pass

    append_log(
        log_file,
        "[extension] opened launcher page via _bring_target_page_to_front and disconnected CDP "
        f"space_id={sid!r} window_key={wkey!r} "
        f"launcher={_short_text(annotated_launcher, max_len=350)!r} "
        f"redirect={_short_text(redirect, max_len=250)!r}",
    )

    client = await wait_extension_client(sid, wkey, timeout_seconds=max(0.1, float(wait_seconds or 0.1)))
    if client is None:
        append_log(log_file, f"[extension] websocket still not connected after trigger wait_s={float(wait_seconds or 0):.1f}")
    else:
        append_log(log_file, "[extension] websocket connected after trigger")
    return client


async def ensure_veo_extension_connected_via_window(
    *,
    sess: Any,
    target_url: str,
    space_id: Optional[str] = None,
    window_key: Optional[str] = None,
    wait_seconds: float = 8.0,
    log_file: Optional[Path] = None,
    force_open: Optional[bool] = None,
    headless: Optional[bool] = None,
    pure_mode: Optional[bool] = None,
    launcher_url: Optional[str] = None,
    auto_triger_connection: Optional[bool] = True,
) -> Any:
    """先检查插件 WS；未连接时打开中转页触发连接。"""
    sid, wkey = _extension_ids_from_session(sess, space_id=space_id, window_key=window_key)
    client = await wait_extension_client(sid, wkey, timeout_seconds=0.2)
    if client is not None:
        return client
    if auto_triger_connection:
        client = await trigger_veo_extension_ws_connection_via_window(
            sess=sess,
            target_url=target_url,
            space_id=sid,
            window_key=wkey,
            wait_seconds=wait_seconds,
            force_open=force_open,
            headless=headless,
            pure_mode=pure_mode,
            log_file=log_file,
            launcher_url=launcher_url,
        )
        if client is not None or bool(force_open):
            return client
        append_log(
            log_file or MONITOR_LOG_FILE,
            "[extension] soft reconnect failed; force reopening fingerprint window with extension args",
        )
        close_and_drop = getattr(sess, "close_and_drop", None)
        if callable(close_and_drop):
            try:
                await close_and_drop()
            except Exception as e:
                append_log(log_file or MONITOR_LOG_FILE, f"[extension] close before force reopen failed: {e}")
        else:
            pw_ctx = getattr(sess, "pw_ctx", None)
            pw_close_and_drop = getattr(pw_ctx, "close_and_drop", None)
            if callable(pw_close_and_drop):
                try:
                    await pw_close_and_drop()
                except Exception as e:
                    append_log(log_file or MONITOR_LOG_FILE, f"[extension] pw close before force reopen failed: {e}")
        return await trigger_veo_extension_ws_connection_via_window(
            sess=sess,
            target_url=target_url,
            space_id=sid,
            window_key=wkey,
            wait_seconds=max(25.0, float(wait_seconds or 0)),
            force_open=True,
            headless=headless,
            pure_mode=pure_mode,
            log_file=log_file,
            launcher_url=launcher_url,
        )
    return None


# 通用别名：后续 Dreamina/GPT 可逐步改用更语义化的名称。
trigger_extension_ws_connection_via_window = trigger_veo_extension_ws_connection_via_window
ensure_extension_connected_via_window = ensure_veo_extension_connected_via_window
