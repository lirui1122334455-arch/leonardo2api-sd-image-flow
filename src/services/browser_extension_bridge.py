"""Browser extension executor bridge.

插件通过 FastAPI WebSocket 连接到本服务，按 space_id/window_key 注册；工作流入口
可把任务转发给插件，由浏览器内部扩展执行，避免 Playwright/CDP page.evaluate 控制目标站。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.config import config
from ..core.logger import logger
from .task_executor_types import NonPenalizedTaskError, ProgressCB


router = APIRouter()


@dataclass
class ExtensionClient:
    client_id: str
    websocket: WebSocket
    space_id: str = ""
    window_key: str = ""
    version: str = ""
    capabilities: list[str] = field(default_factory=list)
    connected_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    pending: Dict[str, asyncio.Future] = field(default_factory=dict)
    progress_cbs: Dict[str, ProgressCB] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def key(self) -> str:
        return _client_key(self.space_id, self.window_key)


_clients: Dict[str, ExtensionClient] = {}
_clients_by_id: Dict[str, ExtensionClient] = {}
_clients_lock = asyncio.Lock()
_PING_INTERVAL_SECONDS = 20.0


def _client_key(space_id: str, window_key: str) -> str:
    return f"{str(space_id or '').strip()}|{str(window_key or '').strip()}"


def _extension_requested(payload: Dict[str, Any]) -> bool:
    raw = str(
        (payload or {}).get("executor")
        or (payload or {}).get("browser_executor")
        or (payload or {}).get("execution_mode")
        or ""
    ).strip().lower()
    return raw in {"extension", "plugin", "browser_extension"}


def should_use_extension_executor(payload: Dict[str, Any]) -> bool:
    """True when global config or payload explicitly requests extension mode."""
    if _extension_requested(payload):
        return True
    raw = str((payload or {}).get("executor") or "").strip().lower()
    if raw in {"playwright", "cdp", "legacy"}:
        return False
    return bool(config.extension_executor_enabled)


def get_default_extension_bridge_url() -> str:
    """浏览器插件默认连接的 WebSocket 地址。

    优先读取 config/setting.toml 的 [extension_executor].bridge_url；
    也可用环境变量 FPB_EXTENSION_BRIDGE_URL 覆盖，例如：
    ws://192.168.2.10:8002/api/extension/ws
    """
    raw = config.extension_bridge_url
    if raw:
        return raw
    return f"ws://127.0.0.1:{int(config.server_port)}/api/extension/ws"


def annotate_url_with_extension_config(url: str, *, space_id: str, window_key: str) -> str:
    """把插件自动注册所需配置写入 URL hash，目标站服务端不可见。

    content script 会读取这些 fpb_* 字段并写入 chrome.storage.local，然后 background
    自动重连。hash 方式避免污染 query，降低影响目标站路由/接口的概率。
    """
    u = str(url or "").strip()
    if not u:
        return u
    try:
        parts = urlsplit(u)
        hash_items = dict(parse_qsl(parts.fragment, keep_blank_values=True))
        hash_items.update(
            {
                "fpb_space_id": str(space_id or "").strip(),
                "fpb_window_key": str(window_key or "").strip(),
                "fpb_bridge_url": get_default_extension_bridge_url(),
            }
        )
        token = config.extension_bridge_token
        if token:
            hash_items["fpb_bridge_token"] = token
        else:
            hash_items.pop("fpb_bridge_token", None)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, urlencode(hash_items)))
    except Exception:
        sep = "&" if "#" in u else "#"
        return (
            f"{u}{sep}"
            f"fpb_space_id={space_id}&fpb_window_key={window_key}"
            f"&fpb_bridge_url={get_default_extension_bridge_url()}"
        )


async def wait_extension_client(space_id: str, window_key: str, *, timeout_seconds: float = 20.0) -> Optional[ExtensionClient]:
    deadline = time.time() + max(0.1, float(timeout_seconds))
    while time.time() < deadline:
        c = await get_extension_client(space_id, window_key)
        if c is not None:
            return c
        await asyncio.sleep(0.5)
    return await get_extension_client(space_id, window_key)


async def _register_client(client: ExtensionClient) -> None:
    async with _clients_lock:
        old = _clients.get(client.key)
        if old and old is not client:
            for fut in list(old.pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("extension client replaced"))
        _clients[client.key] = client
        _clients_by_id[client.client_id] = client


async def _unregister_client(client: ExtensionClient) -> None:
    async with _clients_lock:
        if _clients.get(client.key) is client:
            _clients.pop(client.key, None)
        if _clients_by_id.get(client.client_id) is client:
            _clients_by_id.pop(client.client_id, None)
        for fut in list(client.pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("extension client disconnected"))


async def get_extension_client(space_id: str, window_key: str) -> Optional[ExtensionClient]:
    async with _clients_lock:
        return _clients.get(_client_key(space_id, window_key))


async def submit_extension_task(
    *,
    space_id: str,
    window_key: str,
    provider: str,
    payload: Dict[str, Any],
    progress_cb: ProgressCB,
    timeout_seconds: float,
) -> Dict[str, Any]:
    """兼容旧导入路径；实际实现已迁移到 browser_extension_interaction.py。"""
    from .browser_extension_interaction import submit_extension_task as _submit_extension_task

    return await _submit_extension_task(
        space_id=space_id,
        window_key=window_key,
        provider=provider,
        payload=payload,
        progress_cb=progress_cb,
        timeout_seconds=timeout_seconds,
    )


async def _handle_client_message(client: ExtensionClient, msg: Dict[str, Any]) -> None:
    typ = str((msg or {}).get("type") or "").strip()
    client.last_seen_at = time.time()

    if typ == "hello":
        client.space_id = str(msg.get("space_id") or "").strip()
        client.window_key = str(msg.get("window_key") or "").strip()
        client.version = str(msg.get("version") or "").strip()
        caps = msg.get("capabilities") or []
        client.capabilities = [str(x) for x in caps] if isinstance(caps, list) else []
        if not client.space_id or not client.window_key:
            await client.websocket.send_json({"type": "hello.error", "message": "space_id/window_key required"})
            return
        await _register_client(client)
        await client.websocket.send_json({"type": "hello.ok", "client_id": client.client_id})
        logger.info("extension connected: %s %s caps=%s", client.space_id, client.window_key, client.capabilities)
        return

    if typ == "pong":
        return

    if typ == "heartbeat":
        try:
            async with client.send_lock:
                await client.websocket.send_json({"type": "heartbeat.ok", "ts": int(time.time() * 1000)})
        except Exception:
            logger.debug("extension heartbeat ack failed client=%s", client.client_id, exc_info=True)
        return

    task_id = str(msg.get("task_id") or "").strip()
    if not task_id:
        return

    if typ == "task.progress":
        cb = client.progress_cbs.get(task_id)
        if cb:
            try:
                await cb(int(msg.get("progress") or 0), dict(msg.get("data") or {}))
            except Exception:
                logger.exception("extension progress callback failed task=%s", task_id)
        return

    fut = client.pending.get(task_id)
    if fut is None or fut.done():
        return

    if typ == "task.done":
        fut.set_result(dict(msg.get("result") or {}))
    elif typ == "task.error":
        err = msg.get("error") or {}
        message = str((err or {}).get("message") or "extension task failed")
        status_code = int((err or {}).get("status_code") or 502)
        if "PUBLIC_ERROR_UNUSUAL_ACTIVITY" in message or str((err or {}).get("reason") or "") == "PUBLIC_ERROR_UNUSUAL_ACTIVITY":
            fut.set_exception(RuntimeError(message))
        else:
            fut.set_exception(NonPenalizedTaskError(message, status_code=status_code))


async def _ping_client_loop(client: ExtensionClient) -> None:
    while True:
        await asyncio.sleep(_PING_INTERVAL_SECONDS)
        try:
            async with client.send_lock:
                await client.websocket.send_json({"type": "ping", "ts": int(time.time() * 1000)})
        except asyncio.CancelledError:
            raise
        except Exception:
            break


@router.websocket("/api/extension/ws")
async def extension_ws(websocket: WebSocket):
    token = websocket.query_params.get("token") or websocket.headers.get("x-extension-token") or ""
    expected = config.extension_bridge_token
    if expected and token != expected:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    client = ExtensionClient(client_id=str(uuid.uuid4()), websocket=websocket)
    ping_task = asyncio.create_task(_ping_client_loop(client))
    try:
        await websocket.send_json({"type": "welcome", "client_id": client.client_id})
        while True:
            msg = await websocket.receive_json()
            if isinstance(msg, dict):
                await _handle_client_message(client, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("extension websocket error")
    finally:
        ping_task.cancel()
        try:
            await ping_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        await _unregister_client(client)


@router.get("/api/extension/clients")
async def list_extension_clients():
    async with _clients_lock:
        return {
            "success": True,
            "clients": [
                {
                    "client_id": c.client_id,
                    "space_id": c.space_id,
                    "window_key": c.window_key,
                    "version": c.version,
                    "capabilities": c.capabilities,
                    "connected_at": c.connected_at,
                    "last_seen_at": c.last_seen_at,
                    "pending": len(c.pending),
                }
                for c in _clients.values()
            ],
        }
