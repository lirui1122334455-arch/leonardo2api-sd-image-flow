"""
指纹浏览器管理与同步客户端（FPBrowserClient）。

本文件由 `sora2api/src/services/proxyBroserManage.py` 迁移/整理而来，并作为 fpbrowser2api 的统一入口。

当前重点实现：RoxyBrowser 的“空间(=workspaceId) -> 窗口同步”：
- 先 GET /browser/list_v3 分页拿到窗口列表（简要信息）
- 再 GET /browser/detail 逐个窗口拉取明细信息
- 最终仅抽取并返回以下字段（用于写入本地 windows 表）：
  - dirId -> window_key
  - windowName -> window_name
  - windowPlatformList[0].platformUserName -> platform_account
  - windowPlatformList[0].platformUrl -> platform_url
  - proxyInfo.lastIp -> proxy_addr
  - proxyInfo.lastCountry -> proxy_country

RoxyBrowser 官方接口文档：
- 获取窗口列表：`/browser/list_v3`
  https://faq.roxybrowser.com/zh/api-documentation/api-endpoint.html#%E8%8E%B7%E5%8F%96%E6%B5%8F%E8%A7%88%E5%99%A8%E7%AA%97%E5%8F%A3%E5%88%97%E8%A1%A8
- 获取窗口明细：`/browser/detail`
  https://faq.roxybrowser.com/zh/api-documentation/api-endpoint.html#%E8%8E%B7%E5%8F%96%E6%B5%8F%E8%A7%88%E5%99%A8%E7%AA%97%E5%8F%A3%E6%98%8E%E7%BB%86
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple
import http.client
from urllib.parse import urlsplit

import httpx


_SUPPRESS_PASSWORD_PROMPT_ARGS = (
    # 只关闭 Chromium 自带的「保存/生成密码」气泡，不禁用扩展，避免影响指纹浏览器里的插件。
    "--disable-save-password-bubble",
    "--disable-password-generation",
    "--remote-debugging-address=0.0.0.0",
)


def _append_unique_arg(args: List[str], arg: str) -> None:
    arg = str(arg or "").strip()
    if arg and arg not in args:
        args.append(arg)


def _normalize_browser_args(args: Optional[List[str]]) -> List[str]:
    """复制并去重 browser/open args，避免复用 session 参数时被反复 append。"""
    normalized: List[str] = []
    for raw in args or []:
        _append_unique_arg(normalized, str(raw or "").strip())
    return normalized


class RoxyRateLimiter:
    """RoxyBrowser API 速率限制：每分钟最多 200 次调用。留余量 180/分钟。"""

    def __init__(self, max_per_minute: int = 180, min_interval: float = 0.35):
        self.max_per_minute = max_per_minute
        self.min_interval = min_interval  # 秒，两次调用最小间隔
        self._calls: List[float] = []
        self._lock = asyncio.Lock()
        self._last_call_time = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            # 清理 60 秒前的记录
            cutoff = now - 60.0
            self._calls = [t for t in self._calls if t > cutoff]
            # 若已达上限，等待最老的一次过期
            if len(self._calls) >= self.max_per_minute:
                wait = 60.0 - (now - self._calls[0])
                if wait > 0.01:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
                    self._calls = [t for t in self._calls if t > now - 60.0]
            # 最小间隔
            elapsed = now - self._last_call_time
            if elapsed < self.min_interval and self._last_call_time > 0:
                await asyncio.sleep(self.min_interval - elapsed)
            self._last_call_time = time.monotonic()
            self._calls.append(self._last_call_time)


# 全局 Roxy 速率限制器（按 base_url 区分不同浏览器实例）
_roxy_limiters: Dict[str, RoxyRateLimiter] = {}
_roxy_limiters_lock = asyncio.Lock()


async def _get_roxy_limiter(base_url: str) -> RoxyRateLimiter:
    key = (base_url or "").strip().rstrip("/") or "_default"
    async with _roxy_limiters_lock:
        if key not in _roxy_limiters:
            _roxy_limiters[key] = RoxyRateLimiter(max_per_minute=180, min_interval=0.35)
        return _roxy_limiters[key]


class FPBrowserClient:
    def __init__(self, proxy_enabled: bool = False, proxy_url: Optional[str] = None) -> None:
        self.proxy_enabled = proxy_enabled
        self.proxy_url = (proxy_url or "").strip() or None

    def _client(self) -> httpx.AsyncClient:
        """
        httpx 版本兼容：
        - 新版本常用参数名为 proxy
        - 老版本常用参数名为 proxies
        """
        # 说明：显式拆分超时，避免某些版本/场景下默认值不生效导致“无限等待”
        timeout = httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=30.0)
        return httpx.AsyncClient(timeout=timeout)

    async def list_windows(
        self,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        project_ids: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        # 说明：本项目“同步窗口”当前按 RoxyBrowser 官方接口实现，
        # 以 `_roxy_get_browser_list_v3` + `_roxy_get_browser_detail` 为准。
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        if not base_url or not space_id:
            return []

        # RoxyBrowser：space_id 对应 workspaceId（int）
        if vendor not in ("roxy", "roxybrowser", "generic"):
            # 未来你要接入其他厂商时，再在此处扩展分支
            raise RuntimeError(f"暂不支持 vendor={vendor} 的同步，请设置为 roxy")

        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")

        return await self._roxy_list_windows(
            base_url=base_url,
            token=access_key,
            workspace_id=workspace_id,
            project_ids=(project_ids or "").strip() or None,
        )

    async def list_workspace_projects(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
    ) -> List[Dict[str, Any]]:
        """读取“空间 + 项目列表”（RoxyBrowser：GET /browser/workspace）。

        说明：
        - 仅用于 UI 展示，只读，不写入本地 DB。
        - 返回值尽量保持 RoxyBrowser 的 rows 结构：[{id, workspaceName, project_details:[{projectId, projectName}]}]
        """
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url:
            return []

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的空间项目列表查询，请设置为 roxy")

        return await self._roxy_list_workspace_projects(base_url=base_url, token=access_key)

    async def list_proxy_detect_channels(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
    ) -> List[Dict[str, Any]]:
        """读取代理检测渠道（RoxyBrowser：GET /proxy/detect_channel）。

        参考文档：
        - https://faq.roxybrowser.com/zh/api-documentation/api-endpoint.html#%E8%8E%B7%E5%8F%96%E6%A3%80%E6%B5%8B%E6%B8%A0%E9%81%93

        注意：文档标注“请求参数：无”，但实际本地 RoxyBrowser 服务会校验
        workspaceId；这里按当前空间 space_id 传入。
        """
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        if not base_url or not space_id:
            return []

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的代理检测渠道查询，请设置为 roxy")

        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")

        rsp = await self._roxy_get(
            base_url,
            access_key,
            "/proxy/detect_channel",
            {"workspaceId": int(workspace_id)},
        )
        if (rsp or {}).get("code") != 0:
            raise RuntimeError(f"Roxy proxy/detect_channel 失败：{(rsp or {}).get('msg')}")
        data = (rsp or {}).get("data") or []
        return [x for x in data if isinstance(x, dict)]

    async def browser_open(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        window_key: str,
        args: Optional[List[str]] = None,
        force_open: bool = False,
        headless: bool = False,
        pure_mode: bool = True,
    ) -> Dict[str, Any]:
        """打开指纹浏览器窗口，并返回自动化连接信息（主要是 CDP endpoint：data.http / data.ws）。

        约定：
        - 对于 RoxyBrowser：space_id = workspaceId（纯数字），window_key = dirId
        - 返回结构与 RoxyBrowser 接口一致：{code, msg, data:{http, driver, ws,...}}
        """
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        window_key = (window_key or "").strip()
        if not base_url or not space_id or not window_key:
            raise RuntimeError("browser_open 参数不足：base_url/space_id/window_key 不能为空")

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 browser_open，请设置为 roxy")

        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")
        args = _normalize_browser_args(args)
        for arg in _SUPPRESS_PASSWORD_PROMPT_ARGS:
            _append_unique_arg(args, arg)
        if headless:
            ##args.append("--headless=old") 容易触发机器人
            ##args.append("--disable-gpu") 容易触发机器人
            _append_unique_arg(args, "--disable-software-rasterizer")
            _append_unique_arg(args, "--disable-animations")
            _append_unique_arg(args, "--disable-threaded-animation")
            _append_unique_arg(args, "--disable-threaded-scrolling")
            _append_unique_arg(args, "--mute-audio")
            _append_unique_arg(args, "--disable-extensions")
            _append_unique_arg(args, "--autoplay-policy=user-gesture-required")
            _append_unique_arg(args, "--blink-settings=imagesEnabled=false")
        else:
            if pure_mode:
                ##args.append("--disable-software-rasterizer")
                ##args.append("--disable-animations")
                ##args.append("--disable-threaded-animation")
                ##args.append("--disable-threaded-scrolling")
                ##args.append("--mute-audio")
                ##args.append("--disable-extensions")
                ##args.append("--autoplay-policy=user-gesture-required")
                ##_append_unique_arg(args, "--blink-settings=imagesEnabled=false")
                pass

        return await self._roxy_open_browser(
            base_url=base_url,
            token=access_key,
            workspace_id=workspace_id,
            dir_id=window_key,
            args=args or [],
            force_open=bool(force_open),
            headless=bool(headless),
        )

    async def browser_close(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        window_key: str,
    ) -> Dict[str, Any]:
        """关闭指纹浏览器窗口（RoxyBrowser：POST /browser/close）。"""
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        window_key = (window_key or "").strip()
        if not base_url or not window_key:
            raise RuntimeError("browser_close 参数不足：base_url/window_key 不能为空")

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 browser_close，请设置为 roxy")
        return await self._roxy_close_browser(
            base_url=base_url,
            token=access_key,
            dir_id=window_key,
        )

    async def browser_random_env(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        window_key: str,
    ) -> Dict[str, Any]:
        """窗口随机指纹（RoxyBrowser：POST /browser/random_env）。

        参考文档：
        - https://faq.roxybrowser.com/zh/api-documentation/api-endpoint.html#%E7%AA%97%E5%8F%A3%E9%9A%8F%E6%9C%BA%E6%8C%87%E7%BA%B9
        """
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        window_key = (window_key or "").strip()
        if not base_url or not space_id or not window_key:
            raise RuntimeError("browser_random_env 参数不足：base_url/space_id/window_key 不能为空")

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 browser_random_env，请设置为 roxy")

        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")

        return await self._roxy_random_env(
            base_url=base_url,
            token=access_key,
            workspace_id=workspace_id,
            dir_id=window_key,
        )

    async def browser_clear_local_cache(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        window_keys: List[str],
    ) -> Dict[str, Any]:
        """清空窗口本地缓存（RoxyBrowser：POST /browser/clear_local_cache）。

        参考文档：
        - https://faq.roxybrowser.com/zh/api-documentation/api-endpoint.html#%E6%B8%85%E7%A9%BA%E7%AA%97%E5%8F%A3%E6%9C%AC%E5%9C%B0%E7%BC%93%E5%AD%98
        """
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        keys = [str(x or "").strip() for x in (window_keys or [])]
        keys = [x for x in keys if x]
        if not base_url or not keys:
            raise RuntimeError("browser_clear_local_cache 参数不足：base_url/window_keys 不能为空")

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 browser_clear_local_cache，请设置为 roxy")

        return await self._roxy_clear_local_cache(base_url=base_url, token=access_key, dir_ids=keys)

    async def browser_clear_server_cache(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        window_keys: List[str],
    ) -> Dict[str, Any]:
        """清空窗口服务器缓存（RoxyBrowser：POST /browser/clear_server_cache）。

        参考文档：
        - https://faq.roxybrowser.com/zh/api-documentation/api-endpoint.html#%E6%B8%85%E7%A9%BA%E7%AA%97%E5%8F%A3%E6%9C%8D%E5%8A%A1%E5%99%A8%E7%BC%93%E5%AD%98
        """
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        keys = [str(x or "").strip() for x in (window_keys or [])]
        keys = [x for x in keys if x]
        if not base_url or not keys:
            raise RuntimeError("browser_clear_server_cache 参数不足：base_url/window_keys 不能为空")
        try:
            workspace_id = int(str(space_id or "").strip())
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 browser_clear_server_cache，请设置为 roxy")

        return await self._roxy_clear_server_cache(
            base_url=base_url, token=access_key, workspace_id=workspace_id, dir_ids=keys
        )

    async def browser_mdf(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        window_key: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """修改浏览器窗口（RoxyBrowser：POST /browser/mdf）。

        说明：
        - 会强制注入 workspaceId/dirId（以 space_id/window_key 为准），其余字段透传给指纹浏览器。

        参考文档：
        - https://faq.roxybrowser.com/zh/api-documentation/api-endpoint.html#%E4%BF%AE%E6%94%B9%E6%B5%8F%E8%A7%88%E5%99%A8%E7%AA%97%E5%8F%A3
        """
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        window_key = (window_key or "").strip()
        if not base_url or not space_id or not window_key:
            raise RuntimeError("browser_mdf 参数不足：base_url/space_id/window_key 不能为空")

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 browser_mdf，请设置为 roxy")

        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")

        payload = dict(data or {})
        payload["workspaceId"] = int(workspace_id)
        payload["dirId"] = str(window_key)
        return await self._roxy_browser_mdf(base_url=base_url, token=access_key, data=payload)

    async def get_browser_detail(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        window_key: str,
    ) -> Dict[str, Any]:
        """获取窗口明细（RoxyBrowser：GET /browser/detail）。

        主要用途：在调用 /browser/mdf 做局部更新（例如只改 proxyInfo）时，
        先读取当前窗口信息并保留关键字段（如 windowPlatformList），避免某些 Roxy 版本把未传字段“清空”。

        参考文档：
        - https://faq.roxybrowser.com/zh/api-documentation/api-endpoint.html#%E8%8E%B7%E5%8F%96%E6%B5%8F%E8%A7%88%E5%99%A8%E7%AA%97%E5%8F%A3%E6%98%8E%E7%BB%86
        """
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        window_key = (window_key or "").strip()
        if not base_url or not space_id or not window_key:
            raise RuntimeError("get_browser_detail 参数不足：base_url/space_id/window_key 不能为空")

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 get_browser_detail，请设置为 roxy")

        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")

        return await self._roxy_get_browser_detail(
            base_url=base_url,
            token=access_key,
            workspace_id=int(workspace_id),
            dir_id=str(window_key),
        )

    async def list_proxies(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
    ) -> List[Dict[str, Any]]:
        """读取代理 IP 列表（RoxyBrowser：GET /proxy/list）。

        参考文档：
        - https://faq.roxybrowser.com/zh/api-documentation/api-endpoint.html#%E8%8E%B7%E5%8F%96%E4%BB%A3%E7%90%86-ip-%E5%88%97%E8%A1%A8
        """
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        if not base_url or not space_id:
            return []

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 list_proxies，请设置为 roxy")

        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")

        return await self._roxy_list_proxies(base_url=base_url, token=access_key, workspace_id=workspace_id)

    async def create_proxy(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        proxy: Dict[str, Any],
    ) -> Dict[str, Any]:
        """创建代理 IP（RoxyBrowser：POST /proxy/create）。"""
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        if not base_url or not space_id:
            raise RuntimeError("create_proxy 参数不足：base_url/space_id 不能为空")

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 create_proxy，请设置为 roxy")

        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")

        payload = dict(proxy or {})
        payload["workspaceId"] = int(workspace_id)
        return await self._roxy_proxy_create(base_url=base_url, token=access_key, data=payload)

    async def update_proxy(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        proxy_id: int,
        proxy: Dict[str, Any],
    ) -> Dict[str, Any]:
        """修改代理 IP（RoxyBrowser：POST /proxy/modify）。"""
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        if not base_url or not space_id:
            raise RuntimeError("update_proxy 参数不足：base_url/space_id 不能为空")

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 update_proxy，请设置为 roxy")

        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")

        pid = int(proxy_id or 0)
        if pid <= 0:
            raise RuntimeError("update_proxy 参数不足：proxy_id 必须大于 0")

        payload = dict(proxy or {})
        payload["workspaceId"] = int(workspace_id)
        payload["id"] = int(pid)
        return await self._roxy_proxy_modify(base_url=base_url, token=access_key, data=payload)

    async def delete_proxies(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        proxy_ids: List[int],
    ) -> Dict[str, Any]:
        """删除代理 IP（RoxyBrowser：POST /proxy/delete，支持批量）。"""
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        if not base_url or not space_id:
            raise RuntimeError("delete_proxies 参数不足：base_url/space_id 不能为空")

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 delete_proxies，请设置为 roxy")

        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")

        ids = [int(x) for x in (proxy_ids or []) if str(x).strip().isdigit() and int(x) > 0]
        if not ids:
            raise RuntimeError("delete_proxies 参数不足：proxy_ids 不能为空")

        payload = {
            "workspaceId": int(workspace_id),
            "ids": ids,
        }
        return await self._roxy_proxy_delete(base_url=base_url, token=access_key, data=payload)

    async def list_accounts(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
    ) -> List[Dict[str, Any]]:
        """读取平台账号列表（RoxyBrowser：GET /account/list，分页拉全量）。"""
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        if not base_url or not space_id:
            return []
        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 list_accounts，请设置为 roxy")
        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")
        return await self._roxy_list_accounts(base_url=base_url, token=access_key, workspace_id=workspace_id)

    async def create_accounts_batch(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        account_list: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """批量创建平台账号（RoxyBrowser：POST /account/batch_create）。"""
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        if not base_url or not space_id:
            raise RuntimeError("create_accounts_batch 参数不足：base_url/space_id 不能为空")
        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 create_accounts_batch，请设置为 roxy")
        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")
        payload = {
            "workspaceId": int(workspace_id),
            "accountList": account_list or [],
        }
        return await self._roxy_account_batch_create(base_url=base_url, token=access_key, data=payload)

    async def delete_accounts(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        account_ids: List[int],
    ) -> Dict[str, Any]:
        """删除平台账号（RoxyBrowser：POST /account/delete，支持批量）。"""
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        if not base_url or not space_id:
            raise RuntimeError("delete_accounts 参数不足：base_url/space_id 不能为空")
        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 delete_accounts，请设置为 roxy")
        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")
        ids = [int(x) for x in (account_ids or []) if str(x).strip().isdigit()]
        payload = {
            "workspaceId": int(workspace_id),
            "ids": ids,
        }
        return await self._roxy_account_delete(base_url=base_url, token=access_key, data=payload)

    async def delete_windows(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        window_keys: List[str],
        is_soft_deleted: bool = False,
    ) -> Dict[str, Any]:
        """删除浏览器窗口（RoxyBrowser：POST /browser/delete，支持批量）。"""
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        if not base_url or not space_id:
            raise RuntimeError("delete_windows 参数不足：base_url/space_id 不能为空")
        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 delete_windows，请设置为 roxy")
        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")

        dir_ids = [str(x or "").strip() for x in (window_keys or []) if str(x or "").strip()]
        if not dir_ids:
            raise RuntimeError("delete_windows 参数不足：window_keys 不能为空")

        payload = {
            "workspaceId": int(workspace_id),
            "dirIds": dir_ids,
            "isSoftDeleted": bool(is_soft_deleted),
        }
        return await self._roxy_browser_delete(base_url=base_url, token=access_key, data=payload)

    async def find_accounts_by_keys(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        space_id: str,
        keys: List[str],
        max_pages: int = 20,
        page_size: int = 200,
    ) -> List[Dict[str, Any]]:
        """按 (platformUrl||platformUserName) 键增量查找账号，避免全量同步。"""
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        space_id = (space_id or "").strip()
        if not base_url or not space_id:
            return []
        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 find_accounts_by_keys，请设置为 roxy")
        try:
            workspace_id = int(space_id)
        except Exception:
            raise RuntimeError("RoxyBrowser 的 space_id 请填写 workspaceId（纯数字）")
        key_set = {str(k or "").strip().lower() for k in (keys or []) if str(k or "").strip()}
        if not key_set:
            return []

        path = "/account/list"
        fallback_used = False
        page_index = 1
        found: Dict[str, Dict[str, Any]] = {}
        max_pages = max(1, int(max_pages or 1))
        page_size = max(1, int(page_size or 200))

        while page_index <= max_pages:
            try:
                _total, rows = await self._roxy_get_account_list_page(
                    base_url=base_url,
                    token=access_key,
                    workspace_id=workspace_id,
                    page_index=page_index,
                    page_size=page_size,
                    path=path,
                )
            except Exception:
                if (not fallback_used) and path == "/account/list":
                    path = "/browser/account"
                    fallback_used = True
                    continue
                raise

            if not rows:
                break
            for it in rows:
                if not isinstance(it, dict):
                    continue
                k = f"{str(it.get('platformUrl') or it.get('platform_url') or '').strip().lower()}||{str(it.get('platformUserName') or it.get('platform_username') or '').strip().lower()}"
                if k in key_set and k not in found:
                    found[k] = it
            if len(found) >= len(key_set):
                break
            page_index += 1

        return list(found.values())

    async def is_window_open(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        window_key: str,
    ) -> bool:
        """查询窗口是否已打开（RoxyBrowser：GET /browser/connection_info）。

        参考文档：
        - 已打开窗口进程信息：/browser/connection_info
          https://faq.roxybrowser.com/zh/api-documentation/api-endpoint.html#%E5%B7%B2%E6%89%93%E5%BC%80%E7%AA%97%E5%8F%A3%E8%BF%9B%E7%A8%8B%E4%BF%A1%E6%81%AF

        返回：
        - True：窗口已打开（能查到对应 dirId 的连接信息）
        - False：未打开或查询失败
        """
        info = await self.get_open_window_connection_info(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            window_key=window_key,
        )
        return info is not None

    async def get_open_window_connection_info(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        window_key: str,
    ) -> Optional[Dict[str, Any]]:
        """获取“已打开窗口”的连接信息条目（包含 http/ws/pid 等）。找不到则返回 None。"""
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        window_key = (window_key or "").strip()
        if not base_url or not window_key:
            raise RuntimeError("get_open_window_connection_info 参数不足：base_url/window_key 不能为空")

        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 connection_info，请设置为 roxy")

        rsp = await self._roxy_get(
            base_url,
            access_key,
            "/browser/connection_info",
            {"dirIds": window_key},
        )
        if (rsp or {}).get("code") != 0:
            return None
        data = (rsp or {}).get("data")
        if not isinstance(data, list):
            return None
        for it in data:
            if not isinstance(it, dict):
                continue
            did = str(it.get("dirId") or "").strip()
            if did and did == window_key:
                return it
        return None

    async def list_open_window_connection_infos(
        self,
        *,
        vendor: str,
        base_url: str,
        access_key: Optional[str],
        window_keys: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """批量查询已打开窗口进程信息（RoxyBrowser：GET /browser/connection_info）。"""
        vendor = (vendor or "roxy").strip().lower()
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url:
            raise RuntimeError("list_open_window_connection_infos 参数不足：base_url 不能为空")
        if vendor not in ("roxy", "roxybrowser", "generic"):
            raise RuntimeError(f"暂不支持 vendor={vendor} 的 connection_info，请设置为 roxy")

        keys = [str(x or "").strip() for x in (window_keys or []) if str(x or "").strip()]

        if not keys:
            # 不传 dirIds，查询全部已打开窗口
            rsp = await self._roxy_get(base_url, access_key, "/browser/connection_info", {})
            if (rsp or {}).get("code") != 0:
                return []
            data = (rsp or {}).get("data")
            if not isinstance(data, list):
                return []
            return [x for x in data if isinstance(x, dict)]

        # 分批请求，每批最多 50 个 dirId，避免 URL 过长导致服务端断开连接
        batch_size = 50
        all_results: List[Dict[str, Any]] = []
        for i in range(0, len(keys), batch_size):
            batch = keys[i : i + batch_size]
            params: Dict[str, Any] = {"dirIds": ",".join(batch)}
            try:
                rsp = await self._roxy_get(base_url, access_key, "/browser/connection_info", params)
            except Exception:
                continue
            if (rsp or {}).get("code") != 0:
                continue
            data = (rsp or {}).get("data")
            if isinstance(data, list):
                all_results.extend(x for x in data if isinstance(x, dict))
        return all_results

    # -------------------- RoxyBrowser --------------------
    def _roxy_headers(self, token: Optional[str]) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if token:
            # RoxyBrowser 文档要求请求头 token
            h["token"] = token
        return h

    async def _roxy_get(self, base_url: str, token: Optional[str], path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        limiter = await _get_roxy_limiter(base_url)
        await limiter.acquire()
        url = base_url.rstrip("/") + "/" + path.lstrip("/")
        async with self._client() as client:
            resp = await client.get(url, headers=self._roxy_headers(token), params={k: v for k, v in (params or {}).items() if v is not None and v != ""})
            resp.raise_for_status()
            return resp.json()

    async def _roxy_post(
        self,
        base_url: str,
        token: Optional[str],
        path: str,
        data: Dict[str, Any],
        *,
        timeout_seconds: Optional[float] = None,
        allow_non_json: bool = False,
    ) -> Dict[str, Any]:
        limiter = await _get_roxy_limiter(base_url)
        await limiter.acquire()
        url = base_url.rstrip("/") + "/" + path.lstrip("/")
        async with self._client() as client:
            headers = self._roxy_headers(token)
            payload = data or {}

            try:
                req_coro = client.post(url, headers=headers, json=payload)
                if timeout_seconds is not None and float(timeout_seconds) > 0:
                    resp = await asyncio.wait_for(req_coro, timeout=float(timeout_seconds))
                else:
                    resp = await req_coro
            except asyncio.TimeoutError as e:
                raise RuntimeError(f"Roxy POST 超时：{url} timeout={timeout_seconds}s") from e
            except httpx.TimeoutException as e:
                raise RuntimeError(f"Roxy POST 超时：httpx 超时异常 url={url}") from e

            # 先打印 status + body 片段，避免 resp.json() 因非 JSON/空响应出错导致“没有任何返回日志”
            body_text = ""
            try:
                body_text = resp.text
            except Exception:
                body_text = ""

            resp.raise_for_status()

            if not (body_text or "").strip():
                return {"code": 0, "msg": "empty response", "data": {}}

            try:
                return resp.json()
            except Exception as e:
                if allow_non_json:
                    return {"code": -1, "msg": f"non-json response: {e}", "data": {"text": body_text}}
                raise RuntimeError(f"Roxy POST 返回非 JSON：url={url} status={resp.status_code}") from e

    def _roxy_close_httpclient(
        self,
        *,
        base_url: str,
        token: Optional[str],
        dir_id: str,
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        """
        只为 /browser/close 定制的“更强健”实现：
        - 使用 http.client（标准库）并设置 socket timeout
        - 只等待响应头（getresponse），不读取 response body，避免服务端不结束 body 导致 read() 卡住
        - 强制 Connection: close
        """
        u = urlsplit((base_url or "").strip())
        if not u.scheme or not u.hostname:
            raise RuntimeError(f"base_url 非法：{base_url}")

        path = "/browser/close"
        host = u.hostname
        port = int(u.port or (443 if u.scheme == "https" else 80))

        headers = self._roxy_headers(token)
        headers["Connection"] = "close"
        body_dict = {"dirId": str(dir_id).strip()}
        body_bytes = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
        headers["Content-Length"] = str(len(body_bytes))

        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=float(timeout_seconds))
        try:
            conn.request("POST", path, body=body_bytes, headers=headers)
            resp = conn.getresponse()  # 只拿响应头就足够了
            status = int(getattr(resp, "status", 0) or 0)
            reason = str(getattr(resp, "reason", "") or "")
            # 不 resp.read()：避免服务端一直不结束 body 导致卡住
            try:
                resp.close()
            except Exception:
                pass
            return {"code": 0 if status == 200 else status, "msg": reason or "ok", "data": {"status": status}}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    async def _roxy_open_browser(
        self,
        *,
        base_url: str,
        token: Optional[str],
        workspace_id: int,
        dir_id: str,
        args: List[str],
        force_open: bool,
        headless: bool,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        data = {
            "dirId": str(dir_id),
            "args": args or [],
            "forceOpen": bool(force_open),
            "headless": bool(headless),
            "workspaceId": int(workspace_id),
        }
        last_err: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                rsp = await self._roxy_post(
                    base_url,
                    token,
                    "/browser/open",
                    data,
                    timeout_seconds=60.0,
                )
                return rsp or {}
            except RuntimeError as e:
                last_err = e
                if "超时" in str(e) and attempt < max_retries - 1:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                raise
        raise last_err  # type: ignore[misc]

    async def _roxy_random_env(self, *, base_url: str, token: Optional[str], workspace_id: int, dir_id: str) -> Dict[str, Any]:
        return await self._roxy_post(
            base_url,
            token,
            "/browser/random_env",
            {"workspaceId": int(workspace_id), "dirId": str(dir_id)},
        )

    async def _roxy_clear_local_cache(self, *, base_url: str, token: Optional[str], dir_ids: List[str]) -> Dict[str, Any]:
        return await self._roxy_post(
            base_url,
            token,
            "/browser/clear_local_cache",
            {"dirIds": [str(x).strip() for x in (dir_ids or []) if str(x or "").strip()]},
        )

    async def _roxy_clear_server_cache(
        self, *, base_url: str, token: Optional[str], workspace_id: int, dir_ids: List[str]
    ) -> Dict[str, Any]:
        ids = [str(x).strip() for x in (dir_ids or []) if str(x or "").strip()]
        return await self._roxy_post(
            base_url,
            token,
            "/browser/clear_server_cache",
            {"workspaceId": int(workspace_id), "dirIds": ids},
        )

    async def _roxy_browser_mdf(self, *, base_url: str, token: Optional[str], data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._roxy_post(base_url, token, "/browser/mdf", data or {})

    async def _roxy_close_browser(self, *, base_url: str, token: Optional[str], dir_id: str) -> Dict[str, Any]:
        dir_str = str(dir_id).strip();
        # 精简：优先走与 open 相同的 httpx POST。
        # 仍保留兜底：若 close 接口在某些环境下异常/超时，则回退到标准库 http.client 版本，确保不会无限挂起。
        try:
            rsp = await self._roxy_post(
                base_url,
                token,
                "/browser/close",
                {"dirId": dir_str},
                timeout_seconds=8.0,
                allow_non_json=True,
            )
        except Exception:
            close_timeout = 12.0
            limiter = await _get_roxy_limiter(base_url)
            await limiter.acquire()
            try:
                rsp = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._roxy_close_httpclient,
                        base_url=base_url,
                        token=token,
                        dir_id=dir_str,
                        timeout_seconds=close_timeout,
                    ),
                    timeout=close_timeout + 3.0,
                )
            except asyncio.TimeoutError:
                rsp = {"code": 504, "msg": f"browser_close timeout>{close_timeout}s", "data": {"dirId": dir_str}}

        return rsp or {}

    async def _roxy_get_browser_list_v3(
        self,
        *,
        base_url: str,
        token: Optional[str],
        workspace_id: int,
        project_ids: Optional[str] = None,
        page_index: int,
        page_size: int,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        rsp = await self._roxy_get(
            base_url,
            token,
            "/browser/list_v3",
            {
                "workspaceId": int(workspace_id),
                # 参考文档：projectIds 为可选（多个以英文逗号分隔）
                "projectIds": (project_ids or "").strip() or None,
                "page_index": int(page_index),
                "page_size": int(page_size),
            },
        )
        if (rsp or {}).get("code") != 0:
            raise RuntimeError(f"Roxy list_v3 失败：{(rsp or {}).get('msg')}")
        data = (rsp or {}).get("data") or {}
        total = int(data.get("total") or 0)
        rows = data.get("rows") or []
        rows = [x for x in rows if isinstance(x, dict)]
        return total, rows

    async def _roxy_get_proxy_bought_list_page(
        self,
        *,
        base_url: str,
        token: Optional[str],
        workspace_id: int,
        page_index: int,
        page_size: int,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        rsp = await self._roxy_get(
            base_url,
            token,
            "/proxy/bought_list",
            {
                "workspaceId": int(workspace_id),
                "page_index": int(page_index),
                "page_size": int(page_size),
            },
        )
        if (rsp or {}).get("code") != 0:
            raise RuntimeError(f"Roxy proxy/list 失败：{(rsp or {}).get('msg')}")
        data = (rsp or {}).get("data") or {}
        total = int(data.get("total") or 0)
        rows = data.get("rows") or []
        rows = [x for x in rows if isinstance(x, dict)]
        return total, rows

    async def _roxy_get_proxy_list_page(
        self,
        *,
        base_url: str,
        token: Optional[str],
        workspace_id: int,
        page_index: int,
        page_size: int,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        rsp = await self._roxy_get(
            base_url,
            token,
            "/proxy/list",
            {
                "workspaceId": int(workspace_id),
                "page_index": int(page_index),
                "page_size": int(page_size),
            },
        )
        if (rsp or {}).get("code") != 0:
            raise RuntimeError(f"Roxy proxy/list 失败：{(rsp or {}).get('msg')}")
        data = (rsp or {}).get("data") or {}
        total = int(data.get("total") or 0)
        rows = data.get("rows") or []
        rows = [x for x in rows if isinstance(x, dict)]
        return total, rows

    async def _roxy_get_account_list_page(
        self,
        *,
        base_url: str,
        token: Optional[str],
        workspace_id: int,
        page_index: int,
        page_size: int,
        path: str = "/account/list",
    ) -> Tuple[int, List[Dict[str, Any]]]:
        rsp = await self._roxy_get(
            base_url,
            token,
            path,
            {
                "workspaceId": int(workspace_id),
                "page_index": int(page_index),
                "page_size": int(page_size),
            },
        )
        if (rsp or {}).get("code") != 0:
            raise RuntimeError(f"Roxy {path} 失败：{(rsp or {}).get('msg')}")
        data = (rsp or {}).get("data") or {}
        total = int(data.get("total") or 0)
        rows = data.get("rows") or []
        rows = [x for x in rows if isinstance(x, dict)]
        return total, rows

    async def _roxy_list_proxies(self, *, base_url: str, token: Optional[str], workspace_id: int) -> List[Dict[str, Any]]:
        base_url = (base_url or "").strip().rstrip("/")
        page_size = 100
        by_proxy_id: Dict[int, Dict[str, Any]] = {}
        without_proxy_id: List[Dict[str, Any]] = []

        def append_proxy_row(row: Dict[str, Any], purchase_type: str) -> None:
            tagged = dict(row or {})
            tagged["purchase_type"] = purchase_type
            proxy_id_raw = tagged.get("id") if tagged.get("id") is not None else tagged.get("proxy_id")
            try:
                proxy_id = int(proxy_id_raw)
            except Exception:
                proxy_id = 0
            if proxy_id <= 0:
                without_proxy_id.append(tagged)
                return
            prev = by_proxy_id.get(proxy_id)
            if not prev:
                by_proxy_id[proxy_id] = tagged
                return
            prev_type = str(prev.get("purchase_type") or "").strip()
            # 两个接口都返回同一个 proxy_id 时，优先保留“内部购买”标记。
            if purchase_type == "内部购买" and prev_type != "内部购买":
                by_proxy_id[proxy_id] = tagged
                return
            if prev_type == purchase_type:
                by_proxy_id[proxy_id] = tagged

        async def collect_pages(purchase_type: str, page_fetcher) -> None:
            page_index = 1
            total = 0
            collected = 0
            while True:
                t, rows = await page_fetcher(
                    base_url=base_url,
                    token=token,
                    workspace_id=workspace_id,
                    page_index=page_index,
                    page_size=page_size,
                )
                if total <= 0:
                    total = t
                if not rows:
                    break
                for row in rows:
                    append_proxy_row(row, purchase_type)
                collected += len(rows)
                if total > 0 and collected >= total:
                    break
                page_index += 1
                if page_index > 200:
                    break

        await collect_pages("内部购买", self._roxy_get_proxy_bought_list_page)
        await collect_pages("外部购买", self._roxy_get_proxy_list_page)

        return list(by_proxy_id.values()) + without_proxy_id

    async def _roxy_list_accounts(self, *, base_url: str, token: Optional[str], workspace_id: int) -> List[Dict[str, Any]]:
        """兼容两套接口：
        - 新版：GET /account/list
        - 旧版：GET /browser/account
        """
        base_url = (base_url or "").strip().rstrip("/")
        page_size = 200
        page_index = 1
        total = 0
        all_rows: List[Dict[str, Any]] = []
        path = "/account/list"
        fallback_used = False
        while True:
            try:
                t, rows = await self._roxy_get_account_list_page(
                    base_url=base_url,
                    token=token,
                    workspace_id=workspace_id,
                    page_index=page_index,
                    page_size=page_size,
                    path=path,
                )
            except Exception:
                # 首次失败时自动回退到旧接口
                if (not fallback_used) and path == "/account/list":
                    path = "/browser/account"
                    fallback_used = True
                    continue
                raise
            if total <= 0:
                total = t
            if not rows:
                break
            all_rows.extend(rows)
            if total > 0 and len(all_rows) >= total:
                break
            page_index += 1
            if page_index > 200:
                break
        return all_rows

    async def _roxy_account_batch_create(self, *, base_url: str, token: Optional[str], data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._roxy_post(base_url, token, "/account/batch_create", data or {})

    async def _roxy_account_delete(self, *, base_url: str, token: Optional[str], data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._roxy_post(base_url, token, "/account/delete", data or {})

    async def _roxy_browser_delete(self, *, base_url: str, token: Optional[str], data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._roxy_post(base_url, token, "/browser/delete", data or {})

    async def _roxy_proxy_create(self, *, base_url: str, token: Optional[str], data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._roxy_post(base_url, token, "/proxy/create", data or {})

    async def _roxy_proxy_modify(self, *, base_url: str, token: Optional[str], data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._roxy_post(base_url, token, "/proxy/modify", data or {})

    async def _roxy_proxy_delete(self, *, base_url: str, token: Optional[str], data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._roxy_post(base_url, token, "/proxy/delete", data or {})

    async def _roxy_get_workspace_projects_page(
        self,
        *,
        base_url: str,
        token: Optional[str],
        page_index: int,
        page_size: int,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        rsp = await self._roxy_get(
            base_url,
            token,
            "/browser/workspace",
            {
                "page_index": int(page_index),
                "page_size": int(page_size),
            },
        )
        if (rsp or {}).get("code") != 0:
            raise RuntimeError(f"Roxy workspace 失败：{(rsp or {}).get('msg')}")
        data = (rsp or {}).get("data") or {}
        total = int(data.get("total") or 0)
        rows = data.get("rows") or []
        rows = [x for x in rows if isinstance(x, dict)]
        return total, rows

    async def _roxy_list_workspace_projects(self, *, base_url: str, token: Optional[str]) -> List[Dict[str, Any]]:
        base_url = (base_url or "").strip().rstrip("/")
        page_size = 50
        page_index = 1
        total = 0
        all_rows: List[Dict[str, Any]] = []

        while True:
            t, rows = await self._roxy_get_workspace_projects_page(
                base_url=base_url,
                token=token,
                page_index=page_index,
                page_size=page_size,
            )
            if total <= 0:
                total = t
            if not rows:
                break
            all_rows.extend(rows)
            if total > 0 and len(all_rows) >= total:
                break
            page_index += 1
            if page_index > 200:
                break

        return all_rows

    async def _roxy_get_browser_detail(
        self,
        *,
        base_url: str,
        token: Optional[str],
        workspace_id: int,
        dir_id: str,
    ) -> Dict[str, Any]:
        rsp = await self._roxy_get(
            base_url,
            token,
            "/browser/detail",
            {
                "workspaceId": int(workspace_id),
                "dirId": str(dir_id),
            },
        )
        if (rsp or {}).get("code") != 0:
            raise RuntimeError(f"Roxy detail 失败(dirId={dir_id})：{(rsp or {}).get('msg')}")
        data = (rsp or {}).get("data") or {}
        rows = data.get("rows") or []
        if isinstance(rows, list) and rows:
            if isinstance(rows[0], dict):
                return rows[0]
        # 兼容：有的实现可能直接返回 data 为 dict
        if isinstance(data, dict):
            return data
        return {}

    def _is_retryable_detail_error(self, err: Exception) -> bool:
        """识别可重试的明细请求错误（常见是 Roxy 内部 15s 超时）。"""
        msg = str(err or "").strip().lower()
        if not msg:
            return False
        keywords = (
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
            "temporarily unavailable",
            "network",
        )
        return any(k in msg for k in keywords)

    def _pick_platform(self, detail: Dict[str, Any]) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        lst = detail.get("windowPlatformList") or []
        if isinstance(lst, list):
            for it in lst:
                if not isinstance(it, dict):
                    continue
                aid = it.get("id")
                u = it.get("platformUserName")
                url = it.get("platformUrl")
                if u or url:
                    try:
                        account_id = int(aid) if aid not in (None, "", "-") else None
                    except Exception:
                        account_id = None
                    return (
                        account_id,
                        str(u).strip() if u is not None else None,
                        str(url).strip() if url is not None else None,
                    )
        return (None, None, None)

    async def _roxy_list_windows(
        self,
        *,
        base_url: str,
        token: Optional[str],
        workspace_id: int,
        project_ids: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        按你的需求“完整同步”：
        1) list_v3 分页拉全量窗口 dirId 列表
        2) detail 逐个 dirId 拉取明细
        3) 仅返回需要入库的字段（避免把 cookies 等超大字段写入 raw_json）
        """
        base_url = base_url.rstrip("/")
        page_size = 100
        page_index = 1
        total = 0
        all_rows: List[Dict[str, Any]] = []

        while True:
            t, rows = await self._roxy_get_browser_list_v3(
                base_url=base_url,
                token=token,
                workspace_id=workspace_id,
                project_ids=(project_ids or "").strip() or None,
                page_index=page_index,
                page_size=page_size,
            )
            if total <= 0:
                total = t
            if not rows:
                break
            all_rows.extend(rows)
            if total > 0 and len(all_rows) >= total:
                break
            page_index += 1
            if page_index > 500:  # 极端兜底
                break

        # 逐个 detail 补全
        result: List[Dict[str, Any]] = []
        for r in all_rows:
            dir_id = (r.get("dirId") or "").strip()
            if not dir_id:
                continue
            detail: Dict[str, Any] = {}
            detail_error: Optional[str] = None
            for attempt in range(2):
                try:
                    detail = await self._roxy_get_browser_detail(
                        base_url=base_url,
                        token=token,
                        workspace_id=workspace_id,
                        dir_id=dir_id,
                    )
                    detail_error = None
                    break
                except Exception as e:
                    detail_error = str(e or "").strip() or "unknown detail error"
                    # 仅在常见网络/超时场景下做一次快速重试，避免临时抖动导致整批失败。
                    if attempt == 0 and self._is_retryable_detail_error(e):
                        await asyncio.sleep(0.2)
                        continue
                    # 兜底：单个窗口 detail 失败时降级使用 list_v3 基础数据，继续同步其余窗口。
                    detail = {}
                    break
            window_name = (detail.get("windowName") or r.get("windowName") or "").strip() or dir_id

            platform_account_id, platform_user, platform_url = self._pick_platform(detail)
            proxy_info = detail.get("proxyInfo") or {}
            if not isinstance(proxy_info, dict):
                proxy_info = {}
            last_ip = proxy_info.get("lastIp")
            last_country = proxy_info.get("lastCountry")
            proxy_module_id = proxy_info.get("moduleId")
            proxy_method = proxy_info.get("proxyMethod")
            proxy_category = proxy_info.get("proxyCategory")
            proxy_ip_type = proxy_info.get("ipType")
            proxy_protocol = proxy_info.get("protocol")
            proxy_host = proxy_info.get("host")
            proxy_port = proxy_info.get("port")
            proxy_username = proxy_info.get("proxyUserName")
            proxy_password = proxy_info.get("proxyPassword")
            proxy_refresh_url = proxy_info.get("refreshUrl")

            # RoxyBrowser: windowSortNum（窗口序号，优先用于 UI 展示）
            window_sort_num_raw = detail.get("windowSortNum")
            if window_sort_num_raw is None:
                window_sort_num_raw = r.get("windowSortNum")
            try:
                window_sort_num = int(window_sort_num_raw) if window_sort_num_raw not in (None, "", "-") else None
            except Exception:
                window_sort_num = None

            core_ver = detail.get("coreVersion")
            if core_ver is None:
                core_ver = r.get("coreVersion")
            core_version_str = str(core_ver).strip() if core_ver not in (None, "") else None

            minimal_raw = {
                "dirId": dir_id,
                "windowSortNum": window_sort_num,
                "windowName": window_name,
                "coreVersion": core_version_str,
                "platformAccountId": platform_account_id,
                "platformUserName": platform_user,
                "platformUrl": platform_url,
                "lastIp": str(last_ip).strip() if last_ip is not None else None,
                "lastCountry": str(last_country).strip() if last_country is not None else None,
                "proxyModuleId": int(proxy_module_id) if str(proxy_module_id or "").strip().isdigit() else proxy_module_id,
                "proxyMethod": str(proxy_method).strip() if proxy_method is not None else None,
                "proxyCategory": str(proxy_category).strip() if proxy_category is not None else None,
                "proxyIpType": str(proxy_ip_type).strip() if proxy_ip_type is not None else None,
                "proxyProtocol": str(proxy_protocol).strip() if proxy_protocol is not None else None,
                "proxyHost": str(proxy_host).strip() if proxy_host is not None else None,
                "proxyPort": str(proxy_port).strip() if proxy_port is not None else None,
                "proxyUserName": str(proxy_username).strip() if proxy_username is not None else None,
                "proxyPassword": str(proxy_password).strip() if proxy_password is not None else None,
                "proxyRefreshUrl": str(proxy_refresh_url).strip() if proxy_refresh_url is not None else None,
                "detailError": detail_error,
            }

            proxy_addr = str(last_ip).strip() if last_ip is not None else None
            if not proxy_addr:
                h = str(proxy_host).strip() if proxy_host is not None else ""
                p = str(proxy_port).strip() if proxy_port is not None else ""
                if h and p:
                    proxy_addr = f"{h}:{p}"

            result.append(
                {
                    "window_key": dir_id,
                    "window_sort_num": window_sort_num,
                    "window_name": window_name,
                    "platform_account_id": platform_account_id,
                    "platform_account": platform_user,
                    "platform_url": platform_url,
                    "proxy_addr": proxy_addr,
                    "proxy_country": str(last_country).strip() if last_country is not None else None,
                    "enabled": True,
                    "deleted": False,
                    "raw": minimal_raw,
                }
            )
        return result

