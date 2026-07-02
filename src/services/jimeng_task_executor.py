"""Dreamina (dreamina.capcut.com) / 即梦 Seedance 视频生成执行器。

在已登录 dreamina.capcut.com 的指纹窗口内用 fetch(..., credentials: 'include') 调用官方接口，
复用浏览器 Cookie（ByteDance/TikTok 会话 Cookie）。

支持：
- 文生视频（first_last_frames，无图片）
- 首尾帧生成视频（first_last_frames，1～2 张图片，上传后写入 first_frame_image/end_frame_image）
- 多帧图片参考图生视频（omni_reference，最多 9 张图片，上传后写入 unified_edit_input.material_list）

入口：`dreamina_workflow`（由 `task_service._run_task` 调用）。

注意：API 端点与 payload 基于 jimeng-api/src/api/controllers/videos.ts 的 Seedance 2.0 流程转换。
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import io
import gzip
import random
import json
import os
import re
import socket
import ssl
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request as UrlRequest, urlopen

import httpx

from ..core.database import Database
from ..core.logger import logger
from ..core.paths import MONITOR_LOG_FILE
from .playwright_broswer_context import (
    append_log,
    build_jimeng_page_fetch_headers,
    build_jimeng_page_fetch_headers_body,
    get_or_create_ctx as get_or_create_playwright_ctx,
    page_fetch_json,
    safe_trim,
)
from .task_executor_types import NonPenalizedTaskError, ProgressCB
from .browser_extension_bridge import should_use_extension_executor
from .browser_extension_interaction import submit_extension_task
from .browser_automation_base import FingerprintBrowserAutomationBase
from .veo_workflow_executor import (
    _build_debug_progress_panel_script,
    _veo_resolve_orientation_str,
)
from .task_executor_types import NonPenalizedTaskError, ProgressCB

# ---- API 端点（逆向工程，如有变化请更新） ----
_DREAMINA_API_BASE = "https://dreamina-api.us.capcut.com"
_DREAMINA_API_BASE_CA = "https://mweb-api-sg.capcut.com"


_DREAMINA_GENERATE_PATH = "/mweb/v1/aigc_draft/generate"
_DREAMINA_GET_UPLOAD_TOKEN_PATH = "/mweb/v1/get_upload_token"
_DREAMINA_GET_HISTORY_BY_IDS_PATH = "/mweb/v1/get_history_by_ids"
_DREAMINA_REMOVE_HISTORY_PATH = "/mweb/v1/remove_history"
_DREAMINA_GET_LOCAL_ITEM_LIST_PATH = "/mweb/v1/get_local_item_list"
_DREAMINA_GET_IMAGE_BY_URI_PATH = "/mweb/v1/get_image_by_uri"
_DREAMINA_SUBJECT_CREATE_PATH = "/mweb/v1/dreamina_subject/create"
_DREAMINA_UPDATE_SETTINGS_PATH = "/mweb/v1/update_settings"
_DREAMINA_IMAGEX_BASE = "https://imagex16-normal-us-ttp.capcutapi.us"
_DREAMINA_IMAGEX_BASE_CA = "https://imagex-normal-sg.capcutapi.com"
_DREAMINA_AID = 513641
_DREAMINA_REGION = "US"
_DREAMINA_WEB_VERSION = "7.5.0"
_DREAMINA_DA_VERSION = "3.3.9"
_DREAMINA_DRAFT_VERSION = "3.3.17"
_APPVR = "8.4.0"
_DREAMINA_DRAFT_MIN_VERSION = "3.0.5"
_DREAMINA_AIGC_FEATURES = "app_lip_sync"
_DREAMINA_AWS_REGION = "ap-singapore-1"
_DREAMINA_IMAGE_SERVICE_ID_FALLBACK = "wopfjsm1ax"

DEFAULT_DREAMINA_TARGET = "https://dreamina.capcut.com/ai-tool/video/generate"

# Seedance 2.0 模型名称（来自 jimeng-api VIDEO_MODEL_MAP）
_MODEL_SEEDANCE_20_PRO = "dreamina_seedance_40_pro"
_MODEL_SEEDANCE_20_FAST = "dreamina_seedance_40"
_DREAMINA_MODEL_ALIASES: Dict[str, str] = {
    "seedance-2": _MODEL_SEEDANCE_20_PRO,
    "seedance-2-pro": _MODEL_SEEDANCE_20_PRO,
    "seedance-2-fast": _MODEL_SEEDANCE_20_FAST,
    "seedance_2": _MODEL_SEEDANCE_20_PRO,
    "seedance_2_pro": _MODEL_SEEDANCE_20_PRO,
    "seedance_2_fast": _MODEL_SEEDANCE_20_FAST,
    "jimeng-video-seedance-2.0": _MODEL_SEEDANCE_20_PRO,
    "jimeng-video-seedance-2.0-pro": _MODEL_SEEDANCE_20_PRO,
    "jimeng-video-seedance-2.0-fast": _MODEL_SEEDANCE_20_FAST,
    "seedance-2.0": _MODEL_SEEDANCE_20_PRO,
    "seedance-2.0-pro": _MODEL_SEEDANCE_20_PRO,
    "seedance-2.0-fast": _MODEL_SEEDANCE_20_FAST,
    "seedance_2_0": _MODEL_SEEDANCE_20_PRO,
    "seedance_2_0_fast": _MODEL_SEEDANCE_20_FAST,
    "dreamina_seedance_40_pro": _MODEL_SEEDANCE_20_PRO,
    "dreamina_seedance_40": _MODEL_SEEDANCE_20_FAST,
}
_SEEDANCE_BENEFIT_FAST_T2V_OUTPUT = "seedance_20_fast_720p_output"
_SEEDANCE_BENEFIT_PRO_OUTPUT = "seedance_20_pro_720p_output"

_DREAMINA_COMMERCE_BASE = "https://commerce.us.capcut.com"
_DREAMINA_COMMERCE_BASE_CA = "https://commerce-api-sg.capcut.com"
_DREAMINA_MIN_CREDIT = 255;#最小的dreamina积分才启动
_DREAMINA_GIFT_CREDIT = 120;

_DREAMINA_SESSIONS: Dict[str, "DreaminaSession"] = {}


def _dreamina_key(vendor: str, base_url: str, space_id: str, window_key: str) -> str:
    return f"dreamina|{vendor}|{base_url}|{space_id}|{window_key}"


def _one_str(v: Any) -> str:
    return str(v or "").strip()


def _compact_json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _drop_dreamina_session(cache_key: str) -> None:
    k = (cache_key or "").strip()
    if k:
        _DREAMINA_SESSIONS.pop(k, None)


def _dreamina_api_base_for_country(country_code: Any) -> str:
    if _one_str(country_code).lower() == "ca":
        return _DREAMINA_API_BASE_CA
    
    if _one_str(country_code).lower() == "tr":
        return _DREAMINA_API_BASE_CA
    return _DREAMINA_API_BASE


def _dreamina_imagex_base_for_country(country_code: Any) -> str:
    if _one_str(country_code).lower() == "ca":
        return _DREAMINA_IMAGEX_BASE_CA
    
    if _one_str(country_code).lower() == "tr":
        return _DREAMINA_IMAGEX_BASE_CA
    return _DREAMINA_IMAGEX_BASE



def _dreamina_commerce_base_for_country(country_code: Any) -> str:
    if _one_str(country_code).lower() == "ca":
        return _DREAMINA_COMMERCE_BASE_CA;
    if _one_str(country_code).lower() == "us":
        return _DREAMINA_COMMERCE_BASE;
    if _one_str(country_code).lower() == "tr":
        return _DREAMINA_COMMERCE_BASE_CA;
    return _DREAMINA_COMMERCE_BASE;


def _dreamina_header_loc_for_country(country_code: Any) -> str:
    code = _one_str(country_code).upper()
    return code;


def _dreamina_parse_proxy_url(proxy_url: str) -> Dict[str, str]:
    raw = _one_str(proxy_url)
    if not raw:
        return {}
    u = urlparse(raw if "://" in raw else f"http://{raw}")
    return {
        "scheme": (u.scheme or "http").lower(),
        "host": _one_str(u.hostname),
        "port": str(u.port or (443 if (u.scheme or "").lower() == "https" else 80)),
        "username": unquote(u.username or ""),
        "password": unquote(u.password or ""),
    }


def _dreamina_chain_http_to_socks5h_post_json(
    *,
    system_proxy: str,
    window_proxy: str,
    url: str,
    headers: Dict[str, str],
    json_data: Dict[str, Any],
    timeout: float = 45.0,
) -> Tuple[int, str]:
    """通过 HTTP 系统代理 CONNECT 到窗口 SOCKS5，再由 SOCKS5 访问 HTTPS 目标。

    链路：本机 -> system_proxy(http) -> window_proxy(socks5/socks5h) -> url。
    仅用于 Dreamina 余额接口这种简单 HTTPS POST JSON 场景。
    """
    sp = _dreamina_parse_proxy_url(system_proxy)
    wp = _dreamina_parse_proxy_url(window_proxy)
    target = urlparse(url)
    if sp.get("scheme") not in ("http", "https"):
        raise RuntimeError("两层代理暂仅支持第一层 system_proxy 为 http/https 本地代理")
    if wp.get("scheme") not in ("socks5", "socks5h", "socks"):
        raise RuntimeError("两层代理暂仅支持第二层 window_proxy 为 socks5/socks5h")
    if (target.scheme or "").lower() != "https":
        raise RuntimeError("两层代理 POST 暂仅支持 https 目标")
    if not sp.get("host") or not sp.get("port") or not wp.get("host") or not wp.get("port") or not target.hostname:
        raise RuntimeError("代理或目标 URL 缺少 host/port")

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
        resp_head = _read_until(sock, b"\r\n\r\n")
        status_line = resp_head.split(b"\r\n", 1)[0].decode("iso-8859-1", "ignore")
        if " 200 " not in f" {status_line} ":
            raise RuntimeError(f"system_proxy CONNECT window_proxy 失败：{status_line}")

        # SOCKS5 greeting + auth
        if wp.get("username") or wp.get("password"):
            sock.sendall(b"\x05\x02\x00\x02")
        else:
            sock.sendall(b"\x05\x01\x00")
        ver_method = _recvn(sock, 2)
        if ver_method[0] != 5 or ver_method[1] == 0xFF:
            raise RuntimeError("window_proxy SOCKS5 握手失败")
        if ver_method[1] == 2:
            user_b = wp.get("username", "").encode("utf-8")
            pwd_b = wp.get("password", "").encode("utf-8")
            if len(user_b) > 255 or len(pwd_b) > 255:
                raise RuntimeError("window_proxy SOCKS5 用户名/密码过长")
            sock.sendall(b"\x01" + bytes([len(user_b)]) + user_b + bytes([len(pwd_b)]) + pwd_b)
            auth_resp = _recvn(sock, 2)
            if auth_resp != b"\x01\x00":
                raise RuntimeError("window_proxy SOCKS5 认证失败")
        elif ver_method[1] != 0:
            raise RuntimeError(f"window_proxy SOCKS5 不支持的认证方式：{ver_method[1]}")

        host_b = target.hostname.encode("idna")
        port = target.port or 443
        if len(host_b) > 255:
            raise RuntimeError("目标域名过长")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + int(port).to_bytes(2, "big"))
        rep = _recvn(sock, 4)
        if rep[0] != 5 or rep[1] != 0:
            raise RuntimeError(f"window_proxy SOCKS5 CONNECT 目标失败，rep={rep[1] if len(rep) > 1 else '?'}")
        atyp = rep[3]
        if atyp == 1:
            _recvn(sock, 4)
        elif atyp == 3:
            ln = _recvn(sock, 1)[0]
            _recvn(sock, ln)
        elif atyp == 4:
            _recvn(sock, 16)
        _recvn(sock, 2)

        tls_sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.hostname)
        tls_sock.settimeout(timeout)
        body = json.dumps(json_data or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path = (target.path or "/") + (f"?{target.query}" if target.query else "")
        req_headers = dict(headers or {})
        req_headers["Host"] = target.netloc
        req_headers["Content-Type"] = req_headers.get("Content-Type") or "application/json"
        req_headers["Content-Length"] = str(len(body))
        req_headers["Connection"] = "close"
        if "Accept-Encoding" not in req_headers:
            req_headers["Accept-Encoding"] = "gzip, deflate"
        req = f"POST {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in req_headers.items()) + "\r\n"
        tls_sock.sendall(req.encode("utf-8") + body)

        raw = b""
        while True:
            chunk = tls_sock.recv(65536)
            if not chunk:
                break
            raw += chunk
        head, _, resp_body = raw.partition(b"\r\n\r\n")
        head_text = head.decode("iso-8859-1", "ignore")
        first = head_text.split("\r\n", 1)[0]
        try:
            status = int(first.split()[1])
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
        if "gzip" in header_map.get("content-encoding", ""):
            resp_body = gzip.decompress(resp_body)
        return status, resp_body.decode("utf-8", "replace")


# ---- 参数解析 ----

def _dreamina_resolve_aspect_ratio(payload: Dict[str, Any]) -> str:
    p = payload or {}
    o = _veo_resolve_orientation_str(p)
    if o == "portrait":
        return "9:16"
    if o == "landscape":
        return "16:9"
    raw = _one_str(
        p.get("aspect_ratio") or p.get("size_ratio") or p.get("ratio") or p.get("size")
    )
    if raw in ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"):
        return raw
    return "16:9"


def _dreamina_resolve_duration(payload: Dict[str, Any]) -> int:
    p = payload or {}
    if p.get("duration") is None or _one_str(p.get("duration")) == "":
        raise NonPenalizedTaskError("payload.duration 不能为空，仅支持 15 秒", status_code=422, content_violation=True)
    try:
        v = int(float(p.get("duration")))
    except (TypeError, ValueError):
        raise NonPenalizedTaskError("payload.duration 格式错误，仅支持 15 秒", status_code=422, content_violation=True)
    if v != 15:
        raise NonPenalizedTaskError("payload.duration 仅支持 15 秒", status_code=422, content_violation=True)
    return v


def _dreamina_resolve_resolution(payload: Dict[str, Any]) -> str:
    raw = _one_str((payload or {}).get("resolution") or (payload or {}).get("quality_resolution") or "720p").lower()
    return raw if raw in ("480p", "720p", "1080p") else "720p"


def _dreamina_resolution_size(resolution: str, ratio: str) -> Tuple[int, int]:
    table = {
        "480p": {"1:1": (480, 480), "4:3": (640, 480), "3:4": (480, 640), "16:9": (854, 480), "9:16": (480, 854)},
        "720p": {"1:1": (720, 720), "4:3": (960, 720), "3:4": (720, 960), "16:9": (1280, 720), "9:16": (720, 1280)},
        "1080p": {"1:1": (1080, 1080), "4:3": (1440, 1080), "3:4": (1080, 1440), "16:9": (1920, 1080), "9:16": (1080, 1920)},
    }
    return table.get(resolution, table["720p"]).get(ratio, table.get(resolution, table["720p"])["16:9"])


def _dreamina_resolve_model(payload: Dict[str, Any], has_image: bool) -> str:
    p = payload or {}
    del has_image  # Seedance 2.0 的模型 key 不再区分 t2v/i2v。
    explicit = _one_str(p.get("model_name") or p.get("model"))
    if explicit:
        key = explicit.strip().lower()
        return _DREAMINA_MODEL_ALIASES.get(key, explicit)
    return _MODEL_SEEDANCE_20_FAST


def _dreamina_is_seedance20_fast(model_name: str) -> bool:
    m = _one_str(model_name)
    return "40" in m and "40_pro" not in m


def _dreamina_is_seedance20_pro(model_name: str) -> bool:
    return "40_pro" in _one_str(model_name)


# ---- API 调用 ----

async def dreamina_fetch_sessionid_in_window(
    *,
    sess: "DreaminaSession",
    target_url: str = DEFAULT_DREAMINA_TARGET,
) -> Dict[str, Any]:
    """连接指纹浏览器打开 Dreamina 目标页后，读取 Cookie 中的 sessionid 值。

    管理台沿用 task_type_windows.sora_access_token 字段展示/保存令牌；对
    dreamina_workflow 来说该字段保存的是 Dreamina/CapCut Cookie ``sessionid``。
    """
    sess._cancel_idle_close()
    async with sess._bring_drafts_lock:
        await sess.ensure_open(
            args=sess.browser_open_args,
            force_open=sess.browser_force_open,
            headless=sess.browser_headless,
            acquire_bring_lock=False,
        )
        await sess._bring_target_page_to_front(
            refresh_target=False,
            drafts_url=(target_url or DEFAULT_DREAMINA_TARGET),
            acquire_bring_lock=False,
            close_other_pages = False,
        )
        page = sess.pw_ctx.page
        if page is None:
            raise RuntimeError("Dreamina 页面未初始化（pw_ctx.page 为空）")

        # 先读取/缓存窗口区域，再按区域选择 Dreamina API 域名：
        #   us -> _DREAMINA_API_BASE
        #   ca/tr -> _DREAMINA_API_BASE_CA（与其它 Dreamina 接口保持一致）
        try:
            await sess.refresh_store_country_code_from_cookies(target_url=target_url)
        except Exception as e:
            append_log(sess._log_file, f"[dreamina-update-settings] refresh store_country_code failed: {safe_trim(str(e), 200)}")

        try:
            update_settings_url = f"{sess.dreamina_api_base}{_DREAMINA_UPDATE_SETTINGS_PATH}"
            update_settings_tx = await page_fetch_json(
                page,
                url=update_settings_url,
                method="POST",
                headers=build_jimeng_page_fetch_headers_body(
                    uri=_DREAMINA_UPDATE_SETTINGS_PATH,
                    appid=_DREAMINA_AID,
                    appvr=_APPVR,
                    pf="7",
                    lan=sess.dreamina_header_loc,
                    loc=sess.dreamina_header_loc,
                    headers={"Referer": target_url or DEFAULT_DREAMINA_TARGET},
                ),
                json_data={"custom_settings": {"aigc_compliance_confirmed": True}},
                log_file=sess._log_file,
            )
            append_log(
                sess._log_file,
                "[dreamina-update-settings] "
                f"country={_one_str(sess.store_country_code) or 'default'} "
                f"url={update_settings_url} "
                f"response={safe_trim(_compact_json(update_settings_tx.get('_json')), 600)}",
            )
        except Exception as e:
            # 该接口只用于确认 AIGC 合规设置，失败不应阻断 sessionid 读取流程。
            append_log(sess._log_file, f"[dreamina-update-settings] failed: {safe_trim(str(e), 500)}")

        ctx = getattr(page, "context", None)
        if ctx is None:
            raise RuntimeError("Dreamina 浏览器 context 未初始化")
        try:
            cookies = await ctx.cookies("https://dreamina.capcut.com", "https://www.capcut.com")
        except TypeError:
            cookies = await ctx.cookies()
        except Exception as e:
            raise RuntimeError(f"读取 Cookies 失败：{e}")

        sessionid = ""
        expires: Optional[str] = None
        for item in cookies or []:
            if str((item or {}).get("name") or "") != "sessionid":
                continue
            val = _one_str((item or {}).get("value"))
            if not val:
                continue
            sessionid = val
            exp_raw = (item or {}).get("expires")
            try:
                exp_num = float(exp_raw)
                if exp_num > 0:
                    expires = datetime.datetime.fromtimestamp(exp_num, datetime.timezone.utc).isoformat()
            except Exception:
                expires = None
            break

        try:
            await sess.pw_ctx.disconnect_playwright_only()
        except Exception:
            pass

        if not sessionid:
            raise RuntimeError("读取 Cookies 失败：未找到 sessionid（请确认窗口已登录 Dreamina/CapCut）")
        return {"access_token": sessionid, "expires": expires, "cookie_name": "sessionid"}

def _dreamina_value_list(v: Any) -> List[Any]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        return [s]
    return [v]


def _dreamina_extract_image_source(v: Any) -> Tuple[str, str, Dict[str, Any]]:
    if isinstance(v, str):
        s = v.strip()
        # 兼容调用方把单个图片对象作为字符串传入（非数组 JSON）。
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return _dreamina_extract_image_source(parsed)
            except Exception:
                pass
        return _one_str(v), "", {}
    if isinstance(v, dict):
        # 兼容抓包原始层级：
        # subject_data.content.main_image / content.main_image / main_image / image_info
        nested = None
        if isinstance(v.get("subject_data"), dict):
            content = v.get("subject_data", {}).get("content")
            if isinstance(content, dict) and isinstance(content.get("main_image"), dict):
                nested = dict(content.get("main_image") or {})
                nested.setdefault("subject_id", v.get("subject_data", {}).get("subject_id"))
                nested.setdefault("data_id", v.get("subject_data", {}).get("data_id"))
                nested.setdefault("name", (content or {}).get("name"))
        if nested is None and isinstance(v.get("content"), dict) and isinstance(v.get("content", {}).get("main_image"), dict):
            nested = dict(v.get("content", {}).get("main_image") or {})
            nested.setdefault("subject_id", v.get("subject_id"))
            nested.setdefault("data_id", v.get("data_id"))
            nested.setdefault("name", v.get("content", {}).get("name"))
        if nested is None and isinstance(v.get("main_image"), dict):
            nested = dict(v.get("main_image") or {})
        if nested is None and isinstance(v.get("image_info"), dict):
            nested = dict(v.get("image_info") or {})
        if nested is not None:
            # 外层字段优先补给内层，便于保留 subject 信息。
            for k in ("name", "subject_id", "subjectId", "data_id", "dataId", "uid", "description"):
                if v.get(k) is not None and nested.get(k) is None:
                    nested[k] = v.get(k)
            return _dreamina_extract_image_source(nested)

        src = _one_str(
            v.get("url")
            or v.get("image_url")
            or v.get("imageUrl")
            or v.get("src")
            or v.get("first_image_url")
            or v.get("firstImageUrl")
            or v.get("uri")
            or v.get("image_uri")
        )
        alias = _one_str(v.get("name") or v.get("filename") or v.get("originalFilename") or v.get("field_name") or v.get("fieldName"))
        return src, alias, v
    return _one_str(v), "", {}


def _dreamina_make_image_ref(v: Any, *, field_name: Optional[str] = None, fallback_field_index: int = 1) -> Optional[Dict[str, Any]]:
    src, alias, meta = _dreamina_extract_image_source(v)
    if not src:
        return None
    fn = _one_str(field_name or meta.get("field_name") or meta.get("fieldName") or f"image_file_{fallback_field_index}")
    return {
        "source": src,
        "alias": alias or fn,
        "field_name": fn,
        "width": int(meta.get("width") or meta.get("imageWidth") or 0) if isinstance(meta, dict) else 0,
        "height": int(meta.get("height") or meta.get("imageHeight") or 0) if isinstance(meta, dict) else 0,
        "format": _one_str(meta.get("format") or meta.get("imageFormat")) if isinstance(meta, dict) else "",
        # 如果调用方已经提前保存了 Dreamina/TOS 内部图片 uri（例如
        # tos-useast5-i-wopfjsm1ax-tx/xxx），可以直接复用，不再走上传。
        # 兼容传完整 main_image / image_info 结构时保留原始字段，后续构造 omni subject material。
        "uri": _one_str(meta.get("uri") or meta.get("image_uri") or meta.get("imageUri")) if isinstance(meta, dict) else "",
        "uid": _one_str(meta.get("uid") or meta.get("user_id") or meta.get("userId")) if isinstance(meta, dict) else "",
        "subject_id": _one_str(meta.get("subject_id") or meta.get("subjectId")) if isinstance(meta, dict) else "",
        "data_id": _one_str(meta.get("data_id") or meta.get("dataId")) if isinstance(meta, dict) else "",
        "subject_name": _one_str(meta.get("subject_name") or meta.get("subjectName") or meta.get("name") or alias) if isinstance(meta, dict) else alias,
        "source_from": _one_str(meta.get("source_from") or meta.get("sourceFrom") or "upload") if isinstance(meta, dict) else "upload",
    }


def _dreamina_is_internal_image_uri(src: str) -> bool:
    """判断是否为可直接写入 draft 的 Dreamina/TOS 内部 uri，而不是外部 http/file 地址。"""
    s = _one_str(src)
    if not s:
        return False
    low = s.lower()
    return not (low.startswith("http://") or low.startswith("https://") or low.startswith("file://") or re.match(r"^[a-zA-Z]:[\\/]", s))


def _dreamina_ref_direct_uri(ref: Dict[str, Any]) -> str:
    """提取调用方提前保存的内部 uri；支持 source/url 直接就是 uri，也支持 uri/image_uri 字段。"""
    if not isinstance(ref, dict):
        return ""
    for k in ("uri", "image_uri", "imageUri"):
        v = _one_str(ref.get(k))
        if v and _dreamina_is_internal_image_uri(v):
            return v
    src = _one_str(ref.get("source"))
    return src if _dreamina_is_internal_image_uri(src) else ""


def _dreamina_require_https_image_ref(ref: Dict[str, Any], *, label: str = "参考图片") -> None:
    src = _one_str(ref.get("source") or ref.get("url") or ref.get("image_url") or ref.get("imageUrl"))
    if not src.lower().startswith("https://"):
        raise NonPenalizedTaskError(f"{label}仅支持 https 图片地址", status_code=422, content_violation=True)


def _dreamina_image_ref_dedupe_key(ref: Dict[str, Any]) -> str:
    """同一张外部图只上传/引用一次；忽略字段名差异（如 images[0] 与 first_image_url 相同）。"""
    src = _one_str((ref or {}).get("source") or (ref or {}).get("url") or (ref or {}).get("image_url") or (ref or {}).get("imageUrl"))
    if not src:
        return ""
    try:
        parts = urlsplit(src.strip())
        if parts.scheme and parts.netloc:
            scheme = parts.scheme.lower()
            netloc = parts.netloc.lower()
            # URL 片段不参与取图；去掉尾部空白，保留 query，避免签名 URL 被误判相同。
            return urlunsplit((scheme, netloc, parts.path, parts.query, ""))
    except Exception:
        pass
    return src.strip()


def _dreamina_collect_first_last_image_refs(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    p = payload or {}
    arr: List[Any] = []
    for k in ("images", "image_urls", "imageUrls", "file_paths", "filePaths"):
        vals = _dreamina_value_list(p.get(k))
        if vals:
            arr.extend(vals)
            break
    if arr:
        out: List[Dict[str, Any]] = []
        for i, item in enumerate(arr[:2], start=1):
            ref = _dreamina_make_image_ref(item, field_name=f"image_file_{i}", fallback_field_index=i)
            if ref:
                _dreamina_require_https_image_ref(ref, label=f"首尾帧图片 image_file_{i}")
                out.append(ref)
        return out

    first = (
        p.get("first_image_url")
        or p.get("firstImageUrl")
        or p.get("image_url")
        or p.get("imageUrl")
        or p.get("first_frame_image")
        or p.get("firstFrameImage")
        or p.get("image_file_1")
        or p.get("image_file")
    )
    last = (
        p.get("last_image_url")
        or p.get("lastImageUrl")
        or p.get("end_image_url")
        or p.get("endImageUrl")
        or p.get("last_frame_image")
        or p.get("lastFrameImage")
        or p.get("end_frame_image")
        or p.get("endFrameImage")
        or p.get("image_file_2")
    )
    out = []
    ref1 = _dreamina_make_image_ref(first, field_name="image_file_1", fallback_field_index=1)
    ref2 = _dreamina_make_image_ref(last, field_name="image_file_2", fallback_field_index=2)
    if ref1:
        _dreamina_require_https_image_ref(ref1, label="首帧图片")
        out.append(ref1)
    if ref2:
        _dreamina_require_https_image_ref(ref2, label="尾帧图片")
        out.append(ref2)
    return out


def _dreamina_collect_omni_image_refs(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    p = payload or {}
    refs: List[Dict[str, Any]] = []
    seen_fields: set[str] = set()
    seen_sources: set[str] = set()

    def add(v: Any, field_name: Optional[str] = None) -> None:
        idx = len(refs) + 1
        ref = _dreamina_make_image_ref(v, field_name=field_name or f"image_file_{idx}", fallback_field_index=idx)
        if not ref:
            return
        fn = _one_str(ref.get("field_name"))
        if fn in seen_fields:
            return
        source_key = _dreamina_image_ref_dedupe_key(ref)
        if source_key and source_key in seen_sources:
            return
        refs.append(ref)
        seen_fields.add(fn)
        if source_key:
            seen_sources.add(source_key)

    # 显式 image_file / image_file_N 优先，便于 prompt 中 @image_file_N 命中。
    if p.get("image_file") is not None:
        add(p.get("image_file"), "image_file")
    for i in range(1, 10):
        k = f"image_file_{i}"
        if p.get(k) is not None:
            add(p.get(k), k)

    # 数组式输入补齐剩余槽位。
    for k in ("images", "image_urls", "imageUrls", "reference_images", "reference_image_urls", "file_paths", "filePaths"):
        for item in _dreamina_value_list(p.get(k)):
            add(item, f"image_file_{len(refs) + 1}")

    # 兼容首尾帧字段；如果用户把 functionMode=omni_reference 但仍按首尾帧字段传图，也能进入 material_list。
    for item in (
        p.get("first_image_url") or p.get("firstImageUrl") or p.get("image_url") or p.get("imageUrl") or p.get("first_frame_image"),
        p.get("last_image_url") or p.get("lastImageUrl") or p.get("end_image_url") or p.get("endImageUrl") or p.get("end_frame_image"),
    ):
        add(item, f"image_file_{len(refs) + 1}")

    if len(refs) > 9:
        raise NonPenalizedTaskError("omni_reference 模式最多支持 9 张图片", status_code=400)
    return refs


def _dreamina_collect_external_omni_image_refs(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """只收集外部 https omni 参考图；内部 subject 必须通过 prompt 的 @数字传入。"""
    refs = []
    for ref in _dreamina_collect_omni_image_refs(payload):
        src = _one_str(ref.get("source") or ref.get("url") or ref.get("image_url") or ref.get("imageUrl"))
        if not src.lower().startswith("https://"):
            raise NonPenalizedTaskError("Omni reference 仅支持 https 图片地址", status_code=422, content_violation=True)
        refs.append(ref)
    return refs


def _dreamina_extract_subject_resource_ids_from_prompt(prompt: str) -> Tuple[List[int], str]:
    """提取 prompt 中 @token（到空白/结尾为止）的图片资源 id，同时删除命中的 @token。

    非纯数字 token 会以 -1 占位返回，交由调用方按“未找到资源”处理。
    """
    out: List[int] = []
    seen: set[int] = set()
    s = _one_str(prompt)

    def repl(m: re.Match[str]) -> str:
        token = _one_str(m.group(1))
        if not re.fullmatch(r"\d+", token):
            out.append(-1)
            return ""
        n = int(token)
        if n > 0 and n not in seen:
            out.append(n)
            seen.add(n)
        return ""

    cleaned = re.sub(r"@(\S+)(?=\s|$)", repl, s)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([，,。.!?！？；;：:])", r"\1", cleaned).strip()
    return out, cleaned


def _dreamina_parse_prompt_reference_tokens(prompt: str) -> List[Dict[str, str]]:
    """按 prompt 里的 @token 顺序切分文本和引用位。

    规则：
    - `@` 后紧跟空白前的内容视为 token；
    - 纯数字 token -> 内部 subject 引用；
    - 非数字 token -> 外部 https 参考图占位；
    """
    s = _one_str(prompt)
    parts: List[Dict[str, str]] = []
    last = 0
    for m in re.finditer(r"@(\S+)(?=\s|$)", s):
        if m.start() > last:
            text = s[last:m.start()]
            if text:
                parts.append({"type": "text", "text": text})
        token = _one_str(m.group(1))
        parts.append({
            "type": "subject" if re.fullmatch(r"\d+", token) else "image",
            "token": token,
            "raw": f"@{token}",
        })
        last = m.end()
    if last < len(s):
        text = s[last:]
        if text:
            parts.append({"type": "text", "text": text})
    return parts


def _dreamina_bind_prompt_materials(
    parts: List[Dict[str, str]],
    subject_refs: List[Dict[str, Any]],
    external_refs: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """把 prompt token 序列绑定到内部/外部图片，并生成 prompt/material/meta 结果。

    返回:
    - cleaned_prompt
    - material_list
    - meta_list
    - material_kinds
    """
    subject_iter = iter(subject_refs or [])
    external_iter = iter(external_refs or [])
    material_list: List[Dict[str, Any]] = []
    meta_list: List[Dict[str, Any]] = []
    material_kinds: List[str] = []
    cleaned_parts: List[str] = []
    image_idx = 0

    for part in parts or []:
        ptype = _one_str(part.get("type"))
        if ptype == "text":
            text = _one_str(part.get("text"))
            if text:
                cleaned_parts.append(text)
                meta_list.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "text", "text": text})
            continue

        if ptype == "subject":
            ref = next(subject_iter, None)
            if ref is None:
                raise NonPenalizedTaskError('@"数字"未找到图片', status_code=422, content_violation=True)
            uri = _one_str(ref.get("uri"))
            if not uri:
                raise NonPenalizedTaskError('@"数字"未找到图片', status_code=422, content_violation=True)
            material_list.append(_dreamina_subject_material(uri, ref, int(ref.get("width") or 0), int(ref.get("height") or 0)))
            material_kinds.append("subject")
            meta_list.append({
                "type": "",
                "id": str(uuid.uuid4()),
                "meta_type": "subject",
                "text": "",
                "material_ref": {"type": "", "id": str(uuid.uuid4()), "material_idx": image_idx},
            })
            image_idx += 1
            continue

        if ptype == "image":
            ref = next(external_iter, None)
            if ref is None:
                raise NonPenalizedTaskError('多余的那几个非数字"@xxx"找不到', status_code=422, content_violation=True)
            uri = _one_str(ref.get("uri"))
            if not uri:
                raise NonPenalizedTaskError('多余的那几个非数字"@xxx"找不到', status_code=422, content_violation=True)
            material_list.append({
                "type": "", "id": str(uuid.uuid4()), "material_type": "image",
                "image_info": {
                    **_dreamina_image_info(uri, int(ref.get("width") or 0), int(ref.get("height") or 0)),
                    "aigc_image": {"type": "", "id": str(uuid.uuid4())},
                    "title": "test",
                },
            })
            material_kinds.append("image")
            meta_list.append({
                "type": "",
                "id": str(uuid.uuid4()),
                "meta_type": "image",
                "text": "",
                "material_ref": {"material_idx": image_idx},
            })
            image_idx += 1
            continue

    if next(subject_iter, None) is not None:
        raise NonPenalizedTaskError('@"数字"未找到图片', status_code=422, content_violation=True)
    if next(external_iter, None) is not None:
        raise NonPenalizedTaskError('多余的那几个非数字"@xxx"找不到', status_code=422, content_violation=True)

    if not meta_list:
        meta_list.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "text", "text": ""})
    return "".join(cleaned_parts).strip(), material_list, meta_list, material_kinds


async def _dreamina_load_subject_refs_from_db(db: Any, ids: List[int], *, log_file: Optional[Path] = None) -> List[Dict[str, Any]]:
    """按 prompt 中 @id 从 dreamina_subject_images 表读取 subject 图片引用；不存在的 id 忽略。

    实际 DB 读取委托给 core.database.Database.list_dreamina_subject_images_by_ids，
    避免 service 层直接管理连接/锁。
    """
    safe_ids = [int(x) for x in (ids or []) if int(x) > 0]
    if not safe_ids or db is None:
        return []
    try:
        rows = await db.list_dreamina_subject_images_by_ids(safe_ids)
    except Exception as e:
        if log_file:
            append_log(log_file, f"[dreamina-subject-ref] load from db failed: {e}")
        return []
    by_id = {int(r["id"]): dict(r) for r in (rows or [])}
    refs: List[Dict[str, Any]] = []
    for rid in safe_ids:
        r = by_id.get(int(rid))
        if not r:
            if log_file:
                append_log(log_file, f"[dreamina-subject-ref] id={rid} not found, ignored")
            continue
        uri = _one_str(r.get("image_uri"))
        if not uri:
            continue
        refs.append({
            "resource_id": int(r.get("id") or rid),
            "name": _one_str(r.get("name")),
            "uri": uri,
            "width": int(r.get("width") or 0),
            "height": int(r.get("height") or 0),
            "subject_id": _one_str(r.get("subject_id")),
            "data_id": _one_str(r.get("data_id")),
            "uid": _one_str(r.get("uid")),
        })
    return refs

def _dreamina_normalize_download_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        parts._replace(
            path=quote(parts.path, safe="/:@!$&'()*+,;=-._~%"),
            query=quote(parts.query, safe="/:@!$&'()*+,;=-._~?%"),
        )
    )

def _dreamina_image_info(uri: str, width: int, height: int) -> Dict[str, Any]:
    return {
        "format": "", "height": height, "id": str(uuid.uuid4()), "image_uri": uri, "name": "",
        "platform_type": 1, "source_from": "upload", "type": "image", "uri": uri, "width": width,
    }


def _dreamina_frame_image_info(uri: str, width: int, height: int) -> Dict[str, Any]:
    info = _dreamina_image_info(uri, width, height)
    # Dreamina 标准首尾帧 payload 里首尾帧为 produced，并带 aigc_image。
    info["source_from"] = "produced"
    info["aigc_image"] = {"type": "", "id": str(uuid.uuid4())}
    return info


def _dreamina_build_meta_list(prompt: str, count: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    last = 0
    for m in re.finditer(r"@(?:图|image)?(\d+)", prompt or "", re.I):
        if m.start() > last and prompt[last:m.start()].strip():
            out.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "text", "text": prompt[last:m.start()]})
        idx = int(m.group(1)) - 1
        if 0 <= idx < count:
            out.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "image", "text": "", "material_ref": {"material_idx": idx}})
        last = m.end()
    if last < len(prompt or "") and prompt[last:].strip():
        out.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "text", "text": prompt[last:]})
    if not out:
        for i in range(count):
            if i == 0:
                out.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "text", "text": "使用"})
            out.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "image", "text": "", "material_ref": {"material_idx": i}})
            if i < count - 1:
                out.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "text", "text": "和"})
        out.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "text", "text": f"素材，{prompt}" if prompt.strip() else "素材生成视频"})
    return out


def _dreamina_build_subject_meta_list(prompt: str, count: int) -> List[Dict[str, Any]]:
    """构造内部 subject omni reference 的 meta_list。

    Dreamina UI 抓包里 subject 参考不是普通 image token：
    - 即使 sceneOptions.materialTypes 为 []，material_list 里仍有 material_type=subject；
    - meta_list 需要显式 subject material_ref，否则只剩纯文本时容易提交成功但生成失败/不采纳参考。
    """
    out: List[Dict[str, Any]] = []
    for i in range(max(0, count)):
        out.append({
            "type": "",
            "id": str(uuid.uuid4()),
            "meta_type": "subject",
            "text": "",
            "material_ref": {"type": "", "id": str(uuid.uuid4()), "material_idx": i},
        })
    if (prompt or "").strip():
        out.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "text", "text": prompt})
    elif not out:
        out.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "text", "text": ""})
    return out


def _dreamina_ref_is_subject_material(ref: Optional[Dict[str, Any]], *, omni_material_format: str = "") -> bool:
    """判断单个 ref 是否应按 Dreamina subject material 结构写入。

    混合场景下不能只看全局 omni_material_format：外部 http 图片上传后仍应是
    material_type=image；只有带 subject_id/data_id/uid 等 subject 信息的内部资源才
    应写成 material_type=subject。
    """
    if not isinstance(ref, dict):
        return False
    fmt = _one_str(omni_material_format).lower()
    if fmt not in ("subject", "main_image", "main-image", "captured", "reuse_uri"):
        return False
    if not _dreamina_ref_direct_uri(ref):
        return False
    return bool(_one_str(ref.get("subject_id") or ref.get("subjectId") or ref.get("data_id") or ref.get("dataId") or ref.get("uid")))


def _dreamina_build_mixed_meta_list(prompt: str, material_kinds: List[str]) -> List[Dict[str, Any]]:
    """为 subject/image 混合 material_list 构造 meta_list。"""
    out: List[Dict[str, Any]] = []
    for i, kind in enumerate(material_kinds or []):
        meta_type = "subject" if kind == "subject" else "image"
        item: Dict[str, Any] = {
            "type": "",
            "id": str(uuid.uuid4()),
            "meta_type": meta_type,
            "text": "",
            "material_ref": {"material_idx": i},
        }
        if meta_type == "subject":
            item["material_ref"] = {"type": "", "id": str(uuid.uuid4()), "material_idx": i}
        out.append(item)
    if (prompt or "").strip():
        out.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "text", "text": prompt})
    elif not out:
        out.append({"type": "", "id": str(uuid.uuid4()), "meta_type": "text", "text": ""})
    return out


def _dreamina_subject_material(uri: str, ref: Optional[Dict[str, Any]], width: int, height: int, *, uid: str = "") -> Dict[str, Any]:
    """按抓包中的 material_type=subject / subject_data.content.main_image 结构携带图片。"""
    ref = ref or {}
    w = int(ref.get("width") or width or 0)
    h = int(ref.get("height") or height or 0)
    name = _one_str(ref.get("subject_name") or ref.get("alias") or ref.get("name") or "reference image")
    main_image = {
        **_dreamina_image_info(uri, w, h),
        "title": _one_str(ref.get("title")),
        "source_from": _one_str(ref.get("source_from") or "upload"),
    }
    return {
        "type": "",
        "id": str(uuid.uuid4()),
        "material_type": "subject",
        "subject_data": {
            "type": "",
            "id": str(uuid.uuid4()),
            "uid": _one_str(ref.get("uid") or uid),
            "subject_id": _one_str(ref.get("subject_id")),
            "data_id": _one_str(ref.get("data_id")),
            "content": {
                "type": "",
                "id": str(uuid.uuid4()),
                "name": name,
                "description": _one_str(ref.get("description")),
                "main_image": main_image,
            },
            "subject_control": {
                "type": "",
                "id": str(uuid.uuid4()),
                "status": 0,
                "enabled": True,
                "deletable": True,
                "editable": True,
            },
        },
    }


def _dreamina_material_uri(material: Dict[str, Any]) -> str:
    if not isinstance(material, dict):
        return ""
    image_info = material.get("image_info") if isinstance(material.get("image_info"), dict) else {}
    uri = _one_str((image_info or {}).get("uri") or (image_info or {}).get("image_uri"))
    if uri:
        return uri
    subject_data = material.get("subject_data") if isinstance(material.get("subject_data"), dict) else {}
    content = subject_data.get("content") if isinstance(subject_data.get("content"), dict) else {}
    main_image = content.get("main_image") if isinstance(content.get("main_image"), dict) else {}
    return _one_str((main_image or {}).get("uri") or (main_image or {}).get("image_uri"))



def _dreamina_crc32_hex(data: bytes) -> str:
    import zlib
    return f"{zlib.crc32(data) & 0xffffffff:08x}"


def _dreamina_amz_timestamp() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def _dreamina_create_imagex_signature(
    method: str,
    url: str,
    headers: Dict[str, str],
    access_key_id: str,
    secret_access_key: str,
    session_token: Optional[str] = None,
    payload: str = "",
    *,
    aws_region: str = _DREAMINA_AWS_REGION,
    service_name: str = "imagex",
) -> str:
    parsed = urlparse(url)
    canonical_uri = parsed.path or "/"
    query_pairs = sorted(parse_qsl(parsed.query, keep_blank_values=True), key=lambda kv: kv[0])
    canonical_query = "&".join(f"{k}={v}" for k, v in query_pairs)
    timestamp = headers["x-amz-date"]
    date = timestamp[:8]
    payload_hash = hashlib.sha256((payload if (method.upper() == "POST" and payload) else "").encode("utf-8")).hexdigest()

    headers_to_sign: Dict[str, str] = {"x-amz-date": timestamp}
    if session_token:
        headers_to_sign["x-amz-security-token"] = session_token
    if method.upper() == "POST" and payload:
        headers_to_sign["x-amz-content-sha256"] = payload_hash
    signed_headers = ";".join(sorted(k.lower() for k in headers_to_sign))
    canonical_headers = "".join(f"{k.lower()}:{headers_to_sign[k].strip()}\n" for k in sorted(headers_to_sign, key=lambda x: x.lower()))
    canonical_request = "\n".join([method.upper(), canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash])
    scope = f"{date}/{aws_region}/{service_name}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        timestamp,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k_date = hmac.new(("AWS4" + secret_access_key).encode("utf-8"), date.encode("utf-8"), hashlib.sha256).digest()
    k_region = hmac.new(k_date, aws_region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service_name.encode("utf-8"), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"


def _dreamina_build_imagex_page_headers(
    *,
    uri: str,
    aws_headers: Dict[str, str],
    content_type: Optional[str] = None,
) -> Dict[str, str]:
    """ImageX XHR 请求头。

    不要把 Jimeng API 的 Appid/Sign/Pf/Lan 等头带到 ImageX。
    ImageX 是跨域 AWS 签名接口，额外自定义头会进入 CORS preflight 的
    Access-Control-Request-Headers，容易被服务端拒绝。

    另外 sec-fetch-*/origin/referer/user-agent/accept-encoding 是浏览器控制的
    forbidden/request metadata headers，JS/XHR 无法手动设置；Network 里看不到
    手动传入值是正常现象。
    """
    del uri
    base: Dict[str, str] = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if content_type:
        base["Content-Type"] = content_type
    else:
        base.pop("Content-Type", None)
    for k, v in (aws_headers or {}).items():
        if k and v is not None:
            base[str(k)] = str(v)
    return base


async def _dreamina_download_image_bytes(url: str, *, log_file: Path) -> bytes:
    """服务端直接下载用户传入的 HTTPS 图片，避免页面 fetch 受 CORS/Slardar/webmssdk 影响。"""
    src = _one_str(url)
    if not src.lower().startswith("https://"):
        raise NonPenalizedTaskError("Dreamina 参考图片仅支持 https 地址", status_code=400)

    def _download() -> bytes:
        req = UrlRequest(
            _dreamina_normalize_download_url(src),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
        )
        with urlopen(req, timeout=45) as resp:
            return resp.read(30 * 1024 * 1024 + 1)

    try:
        data = await asyncio.to_thread(_download)
    except NonPenalizedTaskError:
        raise
    except Exception as e:
        raise NonPenalizedTaskError(
            f"Dreamina 参考图片无法访问或下载失败：url={safe_trim(src, 160)} err={e}",
            status_code=400,
        ) from e
    if not data:
        raise NonPenalizedTaskError(
            f"Dreamina 参考图片无法访问或下载内容为空：url={safe_trim(src, 160)}",
            status_code=400,
        )
    append_log(log_file, f"[dreamina-api-upload] downloaded image bytes={len(data)} url={safe_trim(src, 120)}")
    return data


def _dreamina_prepare_image_jpeg_under_1mb(image_bytes: bytes, *, log_file: Path, index: int) -> bytes:
    """校验参考图分辨率不超过 4K，并在不缩放分辨率的前提下转为 <=1MB JPEG。"""
    try:
        from PIL import Image, ImageFile, ImageOps  # type: ignore[import-not-found]
    except Exception as e:
        raise NonPenalizedTaskError(f"图片处理依赖缺失：请安装 Pillow。err={e}", status_code=500) from e

    if not image_bytes:
        raise NonPenalizedTaskError("Dreamina 参考图片下载内容为空", status_code=400)

    max_edge = 4096
    max_bytes = 1 * 1024 * 1024
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    previous_max_pixels = getattr(Image, "MAX_IMAGE_PIXELS", None)
    try:
        # 4K 边长限制下像素上限约 16.8MP；这里放宽到 64MP 只为让 Pillow 能打开后给出明确错误。
        Image.MAX_IMAGE_PIXELS = 64_000_000
        with Image.open(io.BytesIO(image_bytes)) as src:
            src.load()
            img = ImageOps.exif_transpose(src)

        width, height = img.size
        if width > max_edge or height > max_edge:
            raise NonPenalizedTaskError(
                f"Dreamina 参考图片分辨率超过 4K：{width}x{height}，最大允许 {max_edge}x{max_edge}",
                status_code=400,
            )

        # JPEG 不支持透明通道；有 alpha 时合成到白底。保持 width/height 不变，不做 resize。
        if "A" in (img.mode or ""):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").getchannel("A"))
            img = bg
        else:
            img = img.convert("RGB")

        def _save_jpeg(quality: int) -> bytes:
            buf = io.BytesIO()
            img.save(
                buf,
                format="JPEG",
                quality=max(10, min(95, int(quality))),
                optimize=True,
                progressive=True,
                subsampling=2,
            )
            return buf.getvalue()

        best = b""
        for q in (95, 92, 90, 88, 85, 82, 80, 76, 72, 68, 64, 60, 56, 52, 48, 44, 40, 36, 32, 28, 24, 20, 16, 12, 10):
            out = _save_jpeg(q)
            best = out
            if len(out) <= max_bytes:
                append_log(
                    log_file,
                    f"[dreamina-api-upload] image {index} converted jpeg {width}x{height} quality={q} bytes={len(out)}",
                )
                return out
        raise NonPenalizedTaskError(
            f"Dreamina 参考图片无法在保持分辨率 {width}x{height} 的前提下压缩到 1MB 以内"
            f"（最低质量输出 {len(best)} bytes）",
            status_code=400,
        )
    except NonPenalizedTaskError:
        raise
    except Exception as e:
        raise NonPenalizedTaskError(f"Dreamina 参考图片解析/转换失败：err={e}", status_code=400) from e
    finally:
        try:
            Image.MAX_IMAGE_PIXELS = previous_max_pixels
        except Exception:
            pass


def _dreamina_env_int(name: str, default: int, *, minimum: int = 0, maximum: Optional[int] = None) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except Exception:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


async def _dreamina_page_fetch_upload_bytes(page: Any, *, url: str, headers: Dict[str, str], data: bytes, log_file: Path) -> None:
    """用 XMLHttpRequest 上传二进制；ImageX 跨域 fetch+credentials 会 CORS 失败。"""
    import base64
    safe_headers = {k: v for k, v in (headers or {}).items() if str(k).lower() not in {"host", "cookie", "content-length", "connection", "accept-encoding"}}
    timeout_ms = _dreamina_env_int("DREAMINA_IMAGEX_UPLOAD_TIMEOUT_MS", 300000, minimum=30000, maximum=900000)
    retries = _dreamina_env_int("DREAMINA_IMAGEX_UPLOAD_RETRIES", 2, minimum=0, maximum=5)
    payload = {"url": url, "headers": safe_headers, "base64": base64.b64encode(data).decode("ascii"), "timeoutMs": timeout_ms}
    last_err: Optional[BaseException] = None
    res: Optional[Dict[str, Any]] = None
    for attempt in range(1, retries + 2):
        try:
            append_log(log_file, f"[dreamina-api-upload] binary upload attempt={attempt}/{retries + 1} bytes={len(data)} timeout_ms={timeout_ms} url={safe_trim(url, 120)}")
            res = await page.evaluate(
        """async ({url, headers, base64, timeoutMs}) => {
          return await new Promise((resolve, reject) => {
            const bin = atob(base64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const xhr = new XMLHttpRequest();
            xhr.open('POST', url, true);
            xhr.withCredentials = false;
            for (const [k, v] of Object.entries(headers || {})) {
              try { xhr.setRequestHeader(k, String(v)); } catch (_) {}
            }
            xhr.onload = () => resolve({status: xhr.status, text: xhr.responseText || ''});
            xhr.onerror = () => reject(new Error('ImageX XHR binary upload network error'));
            xhr.ontimeout = () => reject(new Error(`ImageX XHR binary upload timeout after ${timeoutMs}ms`));
            xhr.timeout = timeoutMs;
            xhr.send(bytes);
          });
        }""",
                payload,
            )
            break
        except Exception as e:
            last_err = e
            msg = str(e)
            retryable = ("timeout" in msg.lower()) or ("network error" in msg.lower())
            if attempt > retries or not retryable:
                raise
            delay = min(2.0 * attempt, 8.0)
            append_log(log_file, f"[dreamina-api-upload] binary upload retry after error attempt={attempt} delay={delay}s err={safe_trim(msg, 300)}")
            await asyncio.sleep(delay)
    if res is None:
        raise last_err or RuntimeError("ImageX XHR binary upload failed without response")
    status = int((res or {}).get("status") or 0)
    if not (200 <= status < 300):
        raise NonPenalizedTaskError(f"Dreamina ImageX upload failed: status={status} body={safe_trim(_one_str((res or {}).get('text')), 500)}", status_code=502)
    append_log(log_file, f"[dreamina-api-upload] binary upload ok status={status} url={safe_trim(url, 120)}")


async def _dreamina_page_xhr_json(
    page: Any,
    *,
    url: str,
    method: str,
    headers: Dict[str, str],
    json_data: Optional[Dict[str, Any]],
    log_file: Path,
) -> Dict[str, Any]:
    """ImageX 专用 JSON 请求：用 XHR 且 withCredentials=false，避免 fetch 的 CORS credentials 限制。"""
    safe_headers = {
        str(k): str(v)
        for k, v in (headers or {}).items()
        if k and str(k).lower() not in {"host", "cookie", "content-length", "connection", "accept-encoding"}
    }
    body = json.dumps(json_data, ensure_ascii=False, separators=(",", ":")) if json_data is not None else None
    res = await page.evaluate(
        """async ({url, method, headers, body}) => {
          return await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open(method, url, true);
            xhr.withCredentials = false;
            for (const [k, v] of Object.entries(headers || {})) {
              try { xhr.setRequestHeader(k, String(v)); } catch (_) {}
            }
            xhr.onload = () => {
              const hdrs = {};
              try {
                const raw = xhr.getAllResponseHeaders() || '';
                raw.trim().split(/\\r?\\n/).forEach(line => {
                  const i = line.indexOf(':');
                  if (i > 0) hdrs[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
                });
              } catch (_) {}
              resolve({status: xhr.status, text: xhr.responseText || '', headers: hdrs});
            };
            xhr.onerror = () => resolve({status: xhr.status || 0, text: xhr.responseText || '', headers: {}, error: `ImageX XHR ${method} network error`});
            xhr.ontimeout = () => resolve({status: xhr.status || 0, text: xhr.responseText || '', headers: {}, error: `ImageX XHR ${method} timeout`});
            xhr.timeout = 120000;
            xhr.send(body === null || body === undefined ? null : body);
          });
        }""",
        {"url": url, "method": str(method or "GET").upper(), "headers": safe_headers, "body": body},
    )
    tx: Dict[str, Any] = {
        "seen": True,
        "request_id": None,
        "url": url,
        "method": str(method or "GET").upper(),
        "status": int((res or {}).get("status") or 0),
        "response_body": _one_str((res or {}).get("text")),
        "headers": dict((res or {}).get("headers") or {}),
        "log_file": str(log_file),
    }
    if (res or {}).get("error"):
        raise NonPenalizedTaskError(
            f"{(res or {}).get('error')}: status={tx['status']} url={safe_trim(url, 180)} headers={safe_trim(_compact_json(safe_headers), 700)}",
            status_code=502,
        )
    try:
        tx["_json"] = json.loads(tx["response_body"]) if tx["response_body"] else None
    except Exception:
        tx["_json"] = None
    if tx["_json"] is None and tx["status"] not in (204,):
        raise NonPenalizedTaskError(
            f"ImageX XHR JSON parse failed: status={tx['status']} body={safe_trim(tx['response_body'], 600)}",
            status_code=502,
        )
    append_log(log_file, f"[dreamina-api-upload] xhr_json {tx['method']} status={tx['status']} url={safe_trim(url, 160)}")
    return tx


async def _dreamina_upload_one_image_via_page_fetch(
    page: Any,
    ref: Dict[str, Any],
    *,
    token_data: Dict[str, Any],
    log_file: Path,
    index: int,
    return_info: bool = False,
    imagex_base: str = _DREAMINA_IMAGEX_BASE,
) -> Any:
    access_key_id = _one_str(token_data.get("access_key_id"))
    secret_access_key = _one_str(token_data.get("secret_access_key"))
    session_token = _one_str(token_data.get("session_token"))
    service_id = _one_str(token_data.get("service_id")) or _DREAMINA_IMAGE_SERVICE_ID_FALLBACK
    if not access_key_id or not secret_access_key or not session_token:
        raise NonPenalizedTaskError(f"Dreamina get_upload_token 失败 {safe_trim(_compact_json(token_data), 500)}", status_code=502)

    src = _one_str(ref.get("source") or ref.get("url") or ref.get("image_url") or ref.get("imageUrl"))
    if not src:
        raise NonPenalizedTaskError("Dreamina reference image source is empty", status_code=400)
    image_bytes = await _dreamina_download_image_bytes(src, log_file=log_file)
    if len(image_bytes) > 30 * 1024 * 1024:
        raise NonPenalizedTaskError("Dreamina 图片大小超过 30MB", status_code=400)
    image_bytes = _dreamina_prepare_image_jpeg_under_1mb(image_bytes, log_file=log_file, index=index)
    crc32 = _dreamina_crc32_hex(image_bytes)

    # 2) Action=ApplyImageUpload
    ts = _dreamina_amz_timestamp()
    random_s = uuid.uuid4().hex[:10]
    apply_path = "/"
    apply_url = f"{imagex_base}/?Action=ApplyImageUpload&Version=2018-08-01&ServiceId={service_id}&FileSize={len(image_bytes)}&s={random_s}&device_platform=web"
    apply_headers_base = {"x-amz-date": ts, "x-amz-security-token": session_token}
    apply_headers = _dreamina_build_imagex_page_headers(
        uri=apply_path,
        aws_headers={
            "authorization": _dreamina_create_imagex_signature("GET", apply_url, apply_headers_base, access_key_id, secret_access_key, session_token),
            "x-amz-date": ts,
            "x-amz-security-token": session_token,
        },
    )
    append_log(log_file, f"[dreamina-api-upload] apply image {index}: size={len(image_bytes)} crc32={crc32} service_id={service_id}")
    apply_tx = await _dreamina_page_xhr_json(page, url=apply_url, method="GET", headers=apply_headers, json_data=None, log_file=log_file)
    apply_obj = apply_tx.get("_json") or {}
    if isinstance(apply_obj, dict) and ((apply_obj.get("ResponseMetadata") or {}).get("Error")):
        raise NonPenalizedTaskError(f"Dreamina ApplyImageUpload failed: {safe_trim(_compact_json(apply_obj), 700)}", status_code=502)
    upload_address = ((apply_obj.get("Result") or {}) if isinstance(apply_obj, dict) else {}).get("UploadAddress") or {}
    store_infos = upload_address.get("StoreInfos") or []
    upload_hosts = upload_address.get("UploadHosts") or []
    if not store_infos or not upload_hosts:
        raise NonPenalizedTaskError(f"Dreamina ApplyImageUpload missing UploadAddress: {safe_trim(_compact_json(apply_obj), 700)}", status_code=502)
    store_info = store_infos[0]
    upload_url = f"https://{upload_hosts[0]}/upload/v1/{store_info.get('StoreUri')}"

    await _dreamina_page_fetch_upload_bytes(
        page,
        url=upload_url,
        headers={
            "Accept": "*/*",
            "Authorization": _one_str(store_info.get("Auth")),
            "Content-CRC32": crc32,
            "Content-Disposition": 'attachment; filename="image.jpg"',
            "Content-Type": "image/jpeg",
        },
        data=image_bytes,
        log_file=log_file,
    )

    # 4) Action=CommitImageUpload
    commit_path = "/"
    commit_url = f"{imagex_base}/?Action=CommitImageUpload&Version=2018-08-01&ServiceId={service_id}&device_platform=web"
    commit_payload = json.dumps({"SessionKey": upload_address.get("SessionKey"), "SuccessActionStatus": "200"}, separators=(",", ":"))
    cts = _dreamina_amz_timestamp()
    payload_hash = hashlib.sha256(commit_payload.encode("utf-8")).hexdigest()
    commit_base_headers = {"x-amz-date": cts, "x-amz-security-token": session_token, "x-amz-content-sha256": payload_hash}
    commit_headers = _dreamina_build_imagex_page_headers(
        uri=commit_path,
        content_type="application/json",
        aws_headers={
            "authorization": _dreamina_create_imagex_signature("POST", commit_url, commit_base_headers, access_key_id, secret_access_key, session_token, commit_payload),
            "x-amz-date": cts,
            "x-amz-security-token": session_token,
            "x-amz-content-sha256": payload_hash,
        },
    )
    commit_tx = await _dreamina_page_xhr_json(
        page,
        url=commit_url,
        method="POST",
        headers=commit_headers,
        json_data={"SessionKey": upload_address.get("SessionKey"), "SuccessActionStatus": "200"},
        log_file=log_file,
    )
    commit_obj = commit_tx.get("_json") or {}
    if isinstance(commit_obj, dict) and ((commit_obj.get("ResponseMetadata") or {}).get("Error")):
        raise NonPenalizedTaskError(f"Dreamina CommitImageUpload failed: {safe_trim(_compact_json(commit_obj), 700)}", status_code=502)
    result = (commit_obj.get("Result") or {}) if isinstance(commit_obj, dict) else {}
    plugin = (result.get("PluginResult") or [{}])[0]
    uri = _one_str(plugin.get("ImageUri"))
    if not uri:
        results = result.get("Results") or []
        if results and int((results[0] or {}).get("UriStatus") or 0) not in (0, 2000):
            raise NonPenalizedTaskError(f"Dreamina image upload UriStatus abnormal: {safe_trim(_compact_json(results[0]), 500)}", status_code=502)
        uri = _one_str((results[0] or {}).get("Uri") if results else "")
    if not uri:
        raise NonPenalizedTaskError(f"Dreamina CommitImageUpload missing Uri: {safe_trim(_compact_json(commit_obj), 700)}", status_code=502)
    append_log(log_file, f"[dreamina-api-upload] image {index} committed uri={uri}")
    if return_info:
        return {
            "uri": uri,
            "width": int(plugin.get("ImageWidth") or 0),
            "height": int(plugin.get("ImageHeight") or 0),
            "commit_response": commit_obj,
        }
    return uri


async def _dreamina_upload_image_refs_via_page_fetch(
    page: Any,
    refs: List[Dict[str, Any]],
    *,
    log_file: Path,
    api_base: str = _DREAMINA_API_BASE,
    imagex_base: str = _DREAMINA_IMAGEX_BASE,
    header_loc: str = "US",
) -> List[str]:
    if not refs:
        return []

    uris: List[str] = []
    for i, ref in enumerate(refs, start=1):
        # Dreamina/ImageX 的上传 token / SessionKey / StoreUri 按单张图片完整闭环使用。
        # 不复用上一张图片的 token，避免多图时后续 Apply/Commit 绑定到过期或已消费的上传会话。
        token_tx = await page_fetch_json(
            page,
            url=f"{api_base}{_DREAMINA_GET_UPLOAD_TOKEN_PATH}",
            method="POST",
            headers=build_jimeng_page_fetch_headers(uri=_DREAMINA_GET_UPLOAD_TOKEN_PATH, appid=_DREAMINA_AID, appvr=_APPVR, pf="7", lan="en", loc=header_loc),
            json_data={"scene": 2},
            log_file=log_file,
        )
        token_obj = token_tx.get("_json") or {}
        if _one_str(token_obj.get("ret")) not in ("", "0"):
            raise NonPenalizedTaskError(f"Dreamina get_upload_token failed for image {i}: {safe_trim(_compact_json(token_obj), 700)}", status_code=502)
        token_data = token_obj.get("data") if isinstance(token_obj.get("data"), dict) else token_obj
        append_log(log_file, f"[dreamina-api-upload] get_upload_token ok image={i}/{len(refs)} service_id={_one_str((token_data or {}).get('service_id'))}")
        uris.append(await _dreamina_upload_one_image_via_page_fetch(page, ref, token_data=token_data or {}, log_file=log_file, index=i, imagex_base=imagex_base))
    append_log(log_file, f"[dreamina-api-upload] uploaded {len(uris)} refs")
    return uris


def _dreamina_seedance_generate_body(
    *,
    prompt: str,
    model: str,
    ratio: str,
    resolution: str,
    duration: int,
    mode: str,
    material_list: List[Dict[str, Any]],
    meta_list: List[Dict[str, Any]],
    width: int,
    height: int,
) -> Tuple[str, Dict[str, Any], str]:
    component_id = str(uuid.uuid4())
    submit_id = str(uuid.uuid4())
    is_fast = _dreamina_is_seedance20_fast(model)
    is_omni_reference = mode != "first_last_frames"
    is_text_to_video = not material_list
    is_standard_output = is_omni_reference or mode == "first_last_frames"
    material_kinds = ["subject" if _one_str((m or {}).get("material_type")) == "subject" else "image" for m in (material_list or [])]
    subject_count = sum(1 for x in material_kinds if x == "subject")
    external_image_count = sum(1 for x in material_kinds if x != "subject")
    use_subject_material = subject_count > 0
    # 标准 Seedance 2.0（文生视频/omni 参考图/首尾帧）与“封装的 fast”扣费字段不同：
    # benefit_type 需要带输出规格，并带 amount/workspace_id；否则会命中旧的 dreamina_seedance_20_fast 路径。
    benefit = (
        _SEEDANCE_BENEFIT_FAST_T2V_OUTPUT
        if is_fast 
        else _SEEDANCE_BENEFIT_PRO_OUTPUT
    )
    function_mode = "first_last_frames" if mode == "first_last_frames" else "omni_reference"
    extra_vip_function_key = f"{model}-{resolution}" if is_standard_output else model
    scene_option: Dict[str, Any] = {
        "type": "video",
        "scene": "BasicVideoGenerateButton",
        "modelReqKey": model,
        "videoDuration": duration,
        "reportParams": {
            "enterSource": "generate",
            "vipSource": "generate",
            "extraVipFunctionKey": extra_vip_function_key,
            "useVipFunctionDetailsReporterHoc": True,
        },
        # materialTypes 只对应外部图片上传素材数量；内部 subject uri 不计入。
        "materialTypes": [1] * external_image_count,
    }
    if is_standard_output:
        scene_option["resolution"] = resolution
        scene_option["inputVideoDuration"] = 0
    metrics_extra = json.dumps({
        **({"promptSource": "custom"} if mode == "first_last_frames" else {}),
        "isDefaultSeed": 1, "originSubmitId": submit_id, "isRegenerate": False, "enterFrom": "click",
        "position": "page_bottom_box", "functionMode": function_mode,
        "sceneOptions": json.dumps([scene_option], separators=(",", ":")),
    }, separators=(",", ":"))
    draft_min_version = (
        "3.3.12" if use_subject_material
        else (_DREAMINA_DRAFT_MIN_VERSION if (is_text_to_video or mode == "first_last_frames") else (_DREAMINA_DA_VERSION if is_omni_reference else _DREAMINA_DRAFT_VERSION))
    )
    video_input: Dict[str, Any] = {
        "type": "", "id": str(uuid.uuid4()), "min_version": draft_min_version,
        "prompt": prompt if mode == "first_last_frames" else ("" if material_list else prompt),
        "video_mode": 2, "fps": 24, "duration_ms": duration * 1000,
    }
    if mode == "first_last_frames":
        uris = [_dreamina_material_uri(m) for m in (material_list or [])]
        video_input["first_frame_image"] = _dreamina_frame_image_info(uris[0], width, height) if len(uris) >= 1 and uris[0] else None
        video_input["end_frame_image"] = _dreamina_frame_image_info(uris[1], width, height) if len(uris) >= 2 and uris[1] else None
        video_input["resolution"] = resolution
    elif material_list:
        video_input["resolution"] = resolution
        video_input["idip_meta_list"] = []
        video_input["unified_edit_input"] = {
            "type": "",
            "id": str(uuid.uuid4()),
            "material_list": material_list,
            "meta_list": meta_list,
        }
    else:
        if is_standard_output:
            video_input["resolution"] = resolution
        video_input["idip_meta_list"] = []
    draft = {
        "type": "draft", "id": str(uuid.uuid4()), "min_version": draft_min_version,
        "min_features": (
            ["AIGC_Video_UnifiedEdit", "AIGC_UnifiedEditSubject"] if use_subject_material
            else ([] if (is_text_to_video or mode == "first_last_frames") else ["AIGC_Video_UnifiedEdit"])
        ), "is_from_tsn": True, "version": _DREAMINA_DRAFT_VERSION,
        "main_component_id": component_id,
        "component_list": [{"type": "video_base_component", "id": component_id, "min_version": "1.0.0", "aigc_mode": "workbench",
            "metadata": {"type": "", "id": str(uuid.uuid4()), "created_platform": 3, "created_platform_version": "", "created_time_in_ms": str(int(time.time()*1000)), "created_did": ""},
            "generate_type": "gen_video",
            "abilities": {"type": "", "id": str(uuid.uuid4()), "gen_video": {"type": "", "id": str(uuid.uuid4()),
                "text_to_video_params": {"type": "", "id": str(uuid.uuid4()), "video_gen_inputs": [video_input], "video_aspect_ratio": ratio, "seed": random.randint(1, 999999999), "model_req_key": model, "priority": 0},
                "video_task_extra": metrics_extra}},
            "process_type": 1}],
    }
    commerce_info: Dict[str, Any] = {"benefit_type": benefit, "resource_id": "generate_video", "resource_id_type": "str", "resource_sub_type": "aigc"}
    if is_standard_output:
        commerce_info["amount"] = duration
    body = {
        "extend": {"root_model": model, "m_video_commerce_info": commerce_info, "workspace_id": 0, "m_video_commerce_info_list": [dict(commerce_info)]},
        "submit_id": submit_id, "metrics_extra": metrics_extra, "draft_content": json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
        "http_common_info": {"aid": _DREAMINA_AID},
    }
    return submit_id, body, function_mode

# ---- Session 类 ----

class DreaminaSession:
    """按 window 缓存：复用指纹浏览器与 Playwright CDP（与 GrokSession 对齐）。"""

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

        # Dreamina 网页 Cookie 中的 store-country-code，表示当前窗口/IP 区域。
        # 常见值：us=美国，ca=加拿大。后续提交/查询接口可按该值选择不同分支。
        self.store_country_code: str = None

        self.debug_panel_seq: int = 0
        self.debug_panel_entries: list[Dict[str, str]] = []
    
    def get_credit_threshold(self):
        if self.is_US():
            return _DREAMINA_MIN_CREDIT;
        if self.is_TR():
            return 845;
        if self.is_CA():
            return 455;
        return _DREAMINA_MIN_CREDIT;


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

    async def _push_debug_progress(self, page: Any, text: str, *, level: str = "info") -> None:
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
            {"idx": str(self.debug_panel_seq), "ts": now_str, "level": str(level or "info"), "text": msg}
        )
        if len(self.debug_panel_entries) > 80:
            self.debug_panel_entries = self.debug_panel_entries[-80:]
        payload = {
            "title": "Dreamina 调试进度",
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "entries": list(self.debug_panel_entries),
        }
        script = _build_debug_progress_panel_script()
        try:
            await page.evaluate(script, payload)
        except Exception:
            pass

    @staticmethod
    def _is_page_closed(p: Any) -> bool:
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

    async def _snapshot_contexts_pages(self) -> Tuple[List[Any], List[Tuple[Any, Any, str]]]:
        ctx0 = getattr(self.pw_ctx, "context", None)
        br0 = getattr(self.pw_ctx, "browser", None)
        try:
            ctxs = list(getattr(br0, "contexts", []) or [])
        except Exception:
            ctxs = []
        if ctx0 is not None and ctx0 not in ctxs:
            ctxs.insert(0, ctx0)

        open_pages: List[Tuple[Any, Any, str]] = []
        for c in ctxs:
            try:
                pages = list(getattr(c, "pages", []) or [])
            except Exception:
                pages = []
            for p in pages:
                if self._is_page_closed(p):
                    continue
                try:
                    u = str(getattr(p, "url", "") or "").strip()
                except Exception:
                    u = ""
                open_pages.append((c, p, u))
        return ctxs, open_pages

    async def _detect_google_login_and_gmail_visible(self) -> Tuple[bool, bool]:
        """检测当前窗口是否在 Google 登录/选账号相关页，以及页面可见文本中是否有 @gmail.com。"""
        _, open_pages = await self._snapshot_contexts_pages()
        if not open_pages:
            return False, False

        is_google = False
        chunks: List[str] = []
        for _c, p, u in open_pages:
            if self._is_page_closed(p):
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
                chunks.append(str(await p.locator("body").inner_text(timeout=5000) or ""))
            except Exception:
                try:
                    chunks.append(str(await p.inner_text("body", timeout=5000) or ""))
                except Exception:
                    chunks.append("")

        if not is_google:
            return False, False
        return True, "@gmail.com" in "\n".join(chunks).lower()

    async def _click_google_gmail_account_row(self, page: Any) -> None:
        """Google「Choose an account」页：点击可见的 @gmail.com 账号行。"""
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

        if await _try_click(page.locator('[role="link"]:has(div[data-email*="@gmail.com"])')):
            return
        if await _try_click(page.locator('li:has(div[data-email*="@gmail.com"]) [role="link"]')):
            return
        if await _try_click(page.locator('div[jsname="bQiQze"][data-email*="@gmail.com"]'), force=True):
            return
        if await _try_click(page.locator('div[data-email*="@gmail.com"]'), force=True):
            return
        if await _try_click(page.locator("div").filter(has_text=gmail_re), force=True):
            return
        if await _try_click(page.locator("[role='listitem']").filter(has_text=gmail_re)):
            return
        for sel in (
            '[role="link"]:has(div[data-email*="@gmail.com"])',
            'div[data-email*="@gmail.com"]',
        ):
            loc = page.locator(sel)
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

    async def _maybe_click_google_account_picker_if_present(self, open_pages: List[Tuple[Any, Any, str]]) -> bool:
        """若存在 Google 账号选择页，置前并点击 @gmail.com 账号行。"""
        for _c, p, u in open_pages:
            if self._is_page_closed(p):
                continue
            try:
                title = (await p.title() or "").strip().lower()
            except Exception:
                title = ""
            u_low = (u or "").strip().lower()
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
                await asyncio.sleep(1.5)
                return True
            except Exception as e:
                await self._push_debug_progress(
                    p,
                    f"Google 账号选择页：点击 @gmail.com 失败：{safe_trim(str(e), 120)}",
                    level="warn",
                )
                return False
        return False

    async def _try_google_accounts_login_autofill(
        self,
        open_pages: List[Tuple[Any, Any, str]],
        *,
        db: Any,
        window_pk: int,
        timeout_ms: int = 90_000,
    ) -> None:
        """在 accounts.google.com 标签页按窗口凭据自动填邮箱/密码/TOTP（EFA 可选）。"""
        if db is None or int(window_pk or 0) <= 0:
            return
        from .sora_plus_register_executor import (
            google_accounts_autofill_login_steps,
            resolve_window_platform_login_creds_optional_efa,
        )

        acc_page: Any = None
        for _c, p, u in open_pages:
            if self._is_page_closed(p):
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
                acc_page,
                f"Google 自动登录：读取窗口凭据失败：{safe_trim(str(e), 120)}",
                level="warn",
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

    async def _bring_target_page_to_front(
        self,
        refresh_target: bool = True,
        *,
        drafts_url: str,
        acquire_bring_lock: bool = True,
        close_other_pages: bool = True,
    ) -> None:
        """将 Dreamina 目标页置前（与 GrokSession._bring_target_page_to_front 同源）。"""
        try:
            target_host = urlparse(drafts_url).netloc.strip().lower()
        except Exception:
            target_host = ""

        async def _inner() -> None:
            ctx0 = getattr(self.pw_ctx, "context", None)
            br0 = getattr(self.pw_ctx, "browser", None)
            try:
                ctxs = list(getattr(br0, "contexts", []) or [])
            except Exception:
                ctxs = []
            if ctx0 is not None and ctx0 not in ctxs:
                ctxs.insert(0, ctx0)
            if not ctxs:
                return

            def _safe_url(p: Any) -> str:
                try:
                    return str(getattr(p, "url", "") or "").strip()
                except Exception:
                    return ""

            def _url_matches(u: str) -> bool:
                if u.startswith(drafts_url):
                    return True
                try:
                    h = (urlparse(u).netloc or "").strip().lower()
                except Exception:
                    h = ""
                return bool(target_host and h == target_host)

            # 找目标页
            target_page = None
            cur = getattr(self.pw_ctx, "page", None)
            if cur is not None and not self._is_page_closed(cur) and _url_matches(_safe_url(cur)):
                target_page = cur

            if target_page is None:
                for c in ctxs:
                    try:
                        pages = list(getattr(c, "pages", []) or [])
                    except Exception:
                        pages = []
                    for p in pages:
                        if self._is_page_closed(p):
                            continue
                        if _url_matches(_safe_url(p)):
                            target_page = p
                            break
                    if target_page:
                        break

            if target_page is None:
                ctx_pref = ctx0 or (ctxs[0] if ctxs else None)
                if ctx_pref is None:
                    return
                try:
                    target_page = await ctx_pref.new_page()
                    await target_page.goto(drafts_url, wait_until="domcontentloaded")
                except Exception:
                    return

            # 关闭其它页面。
            # 注意：Chrome DevTools 面板在 CDP 下有时也会作为一个独立 page/target
            # 暴露给 Playwright。查余额等只需页面上下文 fetch 的场景不应该关掉用户
            # 手动打开的 DevTools，因此允许调用方通过 close_other_pages=False 跳过。
            if close_other_pages:
                for c in ctxs:
                    try:
                        pages = list(getattr(c, "pages", []) or [])
                    except Exception:
                        pages = []
                    for p in pages:
                        if p is target_page:
                            continue
                        try:
                            await p.close()
                        except Exception:
                            pass

            try:
                self.pw_ctx.page = target_page
            except Exception:
                pass
            try:
                await target_page.bring_to_front()
            except Exception:
                pass

            if refresh_target:
                try:
                    await target_page.goto(drafts_url, wait_until="domcontentloaded")
                    await self._push_debug_progress(target_page, "Dreamina 目标页面刷新完成", level="ok")
                except Exception:
                    await self._push_debug_progress(target_page, "Dreamina 目标页面刷新失败（将继续）", level="warn")
                try:
                    await target_page.evaluate("() => { try { window.focus(); } catch(e) {} }")
                except Exception:
                    pass
                await asyncio.sleep(2.0)

            # 检查是否在 Dreamina 域名下
            try:
                u = _safe_url(target_page).lower()
                if "capcut.com" not in u and "dreamina" not in u:
                    raise NonPenalizedTaskError(
                        "当前页面不在 Dreamina 域名（capcut.com）下，请先在指纹窗口登录 Dreamina",
                        status_code=401,
                    )
            except NonPenalizedTaskError:
                raise
            except Exception:
                pass

        if acquire_bring_lock:
            async with self._bring_drafts_lock:
                await _inner()
        else:
            await _inner()

    async def refresh_store_country_code_from_cookies(self, *, target_url: str = DEFAULT_DREAMINA_TARGET) -> str:
        """读取 Dreamina/CapCut Cookie 中的 store-country-code 并缓存到 session。

        该值代表当前窗口所在的 IP 区域，例如 us/ca。读取失败或 Cookie 不存在时不抛错，
        保留原缓存并返回空字符串，避免影响已有任务流程。
        """
        if self.store_country_code is not None and self.store_country_code != "":
            return self.store_country_code
        
        page = getattr(self.pw_ctx, "page", None)
        ctx = getattr(page, "context", None) if page is not None else None
        if ctx is None:
            ctx = getattr(self.pw_ctx, "context", None)
        if ctx is None:
            return self.store_country_code

        try:
            parsed = urlparse(target_url or DEFAULT_DREAMINA_TARGET)
            origin = f"{parsed.scheme or 'https'}://{parsed.netloc}" if parsed.netloc else "https://dreamina.capcut.com"
            cookies = await ctx.cookies(origin, "https://dreamina.capcut.com")
        except TypeError:
            cookies = await ctx.cookies()
        except Exception as e:
            append_log(self._log_file, f"[dreamina-store-country] read cookies failed: {safe_trim(str(e), 200)}")
            return self.store_country_code

        for item in cookies or []:
            if str((item or {}).get("name") or "").strip().lower() != "store-country-code":
                continue
            code = _one_str((item or {}).get("value")).lower()
            if code:
                self.store_country_code = code
                append_log(self._log_file, f"[dreamina-store-country] store_country_code={code}")
                return code
        append_log(self._log_file, "[dreamina-store-country] cookie store-country-code not found")
        return self.store_country_code

    def is_US(self) -> bool:
        """当前 Dreamina store-country-code 是否为美国。"""
        return _one_str(self.store_country_code).lower() == "us"

    def is_CA(self) -> bool:
        """当前 Dreamina store-country-code 是否为加拿大。"""
        return _one_str(self.store_country_code).lower() == "ca"
    
    def is_TR(self) -> bool:
        """当前 Dreamina store-country-code 是否为加拿大。"""
        return _one_str(self.store_country_code).lower() == "tr"

    def is_JP(self) -> bool:
        """当前 Dreamina store-country-code 是否为日本。"""
        return _one_str(self.store_country_code).lower() == "jp"

    @property
    def dreamina_api_base(self) -> str:
        return _dreamina_api_base_for_country(self.store_country_code)

    @property
    def dreamina_imagex_base(self) -> str:
        return _dreamina_imagex_base_for_country(self.store_country_code)

    @property
    def dreamina_commerce_base(self) -> str:
        return _dreamina_commerce_base_for_country(self.store_country_code)

    @property
    def dreamina_header_loc(self) -> str:
        return _dreamina_header_loc_for_country(self.store_country_code)

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
        _drop_dreamina_session(self.cache_key)

    async def close(self) -> None:
        self._cancel_idle_close()
        await self.pw_ctx.close_and_drop()


def get_or_create_dreamina_session(
    *,
    vendor: str,
    base_url: str,
    access_key: Optional[str],
    space_id: str,
    window_key: str,
) -> DreaminaSession:
    k = _dreamina_key(vendor, base_url, space_id, window_key)
    sess = _DREAMINA_SESSIONS.get(k)
    if sess is None:
        pw_ctx = get_or_create_playwright_ctx(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            space_id=space_id,
            window_key=window_key,
        )
        sess = DreaminaSession(cache_key=k, pw_ctx=pw_ctx)
        _DREAMINA_SESSIONS[k] = sess
    else:
        sess.pw_ctx.access_key = access_key
    return sess


async def dreamina_flow_open_account(
    progress_cb: ProgressCB,
    *,
    db: Any,
    window_pk: int,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    timeout_seconds: float,
    target_url: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
) -> Dict[str, Any]:
    """管理台完整开号：按窗口 Google 凭据登录后打开 Dreamina 目标页，并断开 CDP。"""
    if db is None or int(window_pk or 0) <= 0:
        raise RuntimeError("Dreamina 开号缺少 db/window_pk，无法读取窗口平台账号")

    from .sora_plus_register_executor import (
        _do_login_flow,
        _is_already_logged_in,
        _pick_platform_domain_page,
        _resolve_window_platform_credentials,
    )

    creds = await _resolve_window_platform_credentials(db, window_pk=int(window_pk))
    platform_url = _one_str(creds.get("platform_url"))
    platform_username = _one_str(creds.get("platform_username"))
    platform_password = _one_str(creds.get("platform_password"))
    platform_efa = _one_str(creds.get("platform_efa"))

    target = _one_str(target_url) or DEFAULT_DREAMINA_TARGET
    timeout_ms = int(max(10_000, min(float(timeout_seconds or 120.0) * 1000, 240_000)))

    await progress_cb(1, {"stage": "resolve_credentials", "window_pk": int(window_pk)})

    sess = get_or_create_dreamina_session(
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

    await sess.ensure_open(args=[], force_open=False, headless=headless, pure_mode=pure_mode)

    ctx = sess.pw_ctx
    async with ctx.driver_lock:
        if ctx.context is None:
            raise RuntimeError("浏览器上下文不可用：context is None")
        page = await _pick_platform_domain_page(ctx.context, platform_url=platform_url)
        ctx.page = page

        await page.goto(platform_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await progress_cb(10, {"stage": "platform_page_loaded", "url": platform_url})

        current_url = str(page.url or "").strip()
        if _is_already_logged_in(current_url):
            await progress_cb(55, {"stage": "already_logged_in", "current_url": current_url})
        else:
            await progress_cb(12, {"stage": "need_google_login", "current_url": current_url})
            await _do_login_flow(
                page,
                platform_username=platform_username,
                platform_password=platform_password,
                platform_efa=platform_efa,
                timeout_ms=timeout_ms,
                progress_cb=progress_cb,
            )

        await page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
        await progress_cb(90, {"stage": "dreamina_opened", "url": target})

    try:
        await sess.disconnect_playwright_under_bring_lock()
        await progress_cb(99, {"stage": "cdp_disconnected"})
    except Exception:
        pass

    return {
        "ok": True,
        "stage": "dreamina_ready",
        "branch": "full_open_account",
        "platform_url": platform_url,
        "platform_username": platform_username,
        "url": target,
        "message": "已登录 Google 并打开 Dreamina，已断开 CDP",
    }


async def dreamina_admin_open_connect_page(
    progress_cb: Optional[ProgressCB] = None,
    *,
    db: Any = None,
    window_pk: int = 0,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    headless: bool = False,
    default_target_url: Optional[str] = None,
    pure_mode: bool = True,
    timeout_seconds: float = 120.0,
    google_login_timeout_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """管理台合并逻辑：参考 Veo 的「开号/连接」。

    先探测 Google 登录态：
    - Google 登录页且没有可见 @gmail.com：走完整 Google 登录，再打开 Dreamina；
    - Google 账号选择页可见 @gmail.com：自动点选/补填密码或 TOTP，再打开 Dreamina；
    - 其它情况：直接连接并置前 Dreamina 目标页。
    """
    if progress_cb is None:
        async def _noop_progress(_pct: int, _meta: Optional[Dict[str, Any]] = None) -> None:
            return

        progress_cb = _noop_progress

    target = _one_str(default_target_url) or DEFAULT_DREAMINA_TARGET
    try:
        gl_ms = int(google_login_timeout_ms) if google_login_timeout_ms is not None else int(float(timeout_seconds or 120.0) * 1000)
    except Exception:
        gl_ms = 120_000
    gl_ms = max(45_000, min(gl_ms, 240_000))

    sess = get_or_create_dreamina_session(
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
    need_full_open_account = False

    # 与 veo_admin_unified_open_or_connect 一样：先打开 Google Accounts 探测登录态，
    # 并在账号选择页/登录页尝试自动点选与补填。
    async with sess._bring_drafts_lock:
        await sess.ensure_open(
            args=sess.browser_open_args,
            force_open=sess.browser_force_open,
            headless=headless,
            acquire_bring_lock=False,
            pure_mode=pure_mode,
        )
        ctx = getattr(sess.pw_ctx, "context", None)
        if ctx is None:
            raise RuntimeError("浏览器上下文不可用：context is None")

        page = getattr(sess.pw_ctx, "page", None)
        if page is None or sess._is_page_closed(page):
            try:
                pages = list(getattr(ctx, "pages", []) or [])
            except Exception:
                pages = []
            page = next((p for p in pages if not sess._is_page_closed(p)), None)
            if page is None:
                page = await ctx.new_page()
            sess.pw_ctx.page = page

        try:
            await page.goto("https://accounts.google.com/", wait_until="domcontentloaded", timeout=gl_ms)
        except Exception as e:
            await sess._push_debug_progress(page, f"Google 登录态探测页打开失败：{safe_trim(str(e), 120)}", level="warn")
        await progress_cb(5, {"stage": "detect_google_page"})
        await asyncio.sleep(1.0)

        _, open_pages = await sess._snapshot_contexts_pages()
        if await sess._maybe_click_google_account_picker_if_present(open_pages):
            _, open_pages = await sess._snapshot_contexts_pages()
        try:
            await sess._try_google_accounts_login_autofill(open_pages, db=db, window_pk=int(window_pk or 0), timeout_ms=gl_ms)
        except Exception as e:
            await sess._push_debug_progress(page, f"Google 自动登录异常：{safe_trim(str(e), 120)}", level="warn")
        await asyncio.sleep(1.0)

        is_google, has_gmail = await sess._detect_google_login_and_gmail_visible()
        await progress_cb(
            8,
            {"stage": "detect_done", "is_google_login": is_google, "has_gmail_visible": has_gmail},
        )

        if is_google and has_gmail:
            _, open_pages = await sess._snapshot_contexts_pages()
            if await sess._maybe_click_google_account_picker_if_present(open_pages):
                _, open_pages = await sess._snapshot_contexts_pages()
                try:
                    await sess._try_google_accounts_login_autofill(open_pages, db=db, window_pk=int(window_pk or 0), timeout_ms=gl_ms)
                except Exception as e:
                    await sess._push_debug_progress(page, f"Google 自动登录异常：{safe_trim(str(e), 120)}", level="warn")
                await asyncio.sleep(1.0)
        elif is_google and not has_gmail:
            need_full_open_account = True

    if need_full_open_account:
        return await dreamina_flow_open_account(
            progress_cb,
            db=db,
            window_pk=int(window_pk or 0),
            browser_vendor=browser_vendor,
            browser_base_url=browser_base_url,
            browser_access_key=browser_access_key,
            space_id=space_id,
            window_key=window_key,
            timeout_seconds=timeout_seconds,
            target_url=target,
            headless=headless,
            pure_mode=pure_mode,
        )

    await progress_cb(20, {"stage": "open_dreamina", "url": target})
    async with sess._bring_drafts_lock:
        await sess.ensure_open(
            args=sess.browser_open_args,
            force_open=sess.browser_force_open,
            headless=headless,
            acquire_bring_lock=False,
            pure_mode=pure_mode,
        )
        await sess._bring_target_page_to_front(
            refresh_target=True,
            drafts_url=target,
            acquire_bring_lock=False,
            close_other_pages=True,
        )

    try:
        await sess.disconnect_playwright_under_bring_lock()
        await progress_cb(99, {"stage": "cdp_disconnected"})
    except Exception:
        pass
    branch = "gmail_picker_connect" if (is_google and has_gmail) else "connect_bring_default"
    return {
        "ok": True,
        "branch": branch,
        "is_google_login": is_google,
        "has_gmail_visible": has_gmail,
        "message": (
            "已连接并置前 Dreamina（检测到 Google 账号列表含 @gmail.com，已按自动点选/填表处理），已断开自动化连接"
            if branch == "gmail_picker_connect"
            else "已连接并置前 Dreamina 目标页，已断开自动化连接"
        ),
        "url": target,
    }


async def dreamina_fetch_credits_in_window(
    *,
    target_url: str,
    access_token: Optional[str] = None,
    db: Any = None,
    picked: Any = None,
    country_code: Optional[str] = None,
    log_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """本地请求 Dreamina 余额接口，返回 {total_credit, cooldown_until}。

    不再通过指纹浏览器 page.fetch 读取余额；改为本地 httpx 调用 user_credit。
    代理链路要求为：系统固定代理（通常日本） -> 窗口独立 SOCKS5 代理（美国/加拿大等）
    -> Dreamina。httpx 只能配置最终一跳代理；第一层系统代理需要由运行环境/本机网络
    对窗口 SOCKS5 连接做透明转发或路由承载，本函数会优先把 httpx 出口设置为窗口代理。
    cooldown_until 来自 user_credit 返回的 credits_life_end，并转换为北京时间字符串。
    """
    log_file = log_file or MONITOR_LOG_FILE
    token = _one_str(access_token)
    if not token:
        raise RuntimeError("Dreamina 本地读取余额缺少 sessionid/access_token")

    async def _resolve_system_proxy() -> str:
        if db is None:
            return ""
        try:
            syscfg = await db.get_system_config()
            if bool(getattr(syscfg, "proxy_enabled", False)):
                return _one_str(getattr(syscfg, "proxy_url", ""))
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
            proto = _one_str(row["protocol"] or "socks5").lower()
            if proto in ("socks", "socks5"):
                # socks5h 让域名解析也走窗口代理，避免本机/第一层代理侧解析造成地区偏差。
                proto = "socks5h"
            elif proto not in ("socks5h", "http", "https"):
                proto = "socks5h"
            host = _one_str(row["host"])
            port = _one_str(row["port"])
            if not host or not port:
                return ""
            user = quote(_one_str(row["proxy_username"]), safe="")
            pwd = quote(_one_str(row["proxy_password"]), safe="")
            auth = f"{user}:{pwd}@" if user or pwd else ""
            return f"{proto}://{auth}{host}:{port}"
        except Exception as e:
            append_log(log_file, f"[dreamina] resolve window proxy failed: {safe_trim(str(e), 200)}")
            return ""

    # 无需打开浏览器；地区根据窗口已缓存/默认配置选择。未缓存时从目标 URL 默认按 US 分支处理。
    system_proxy = await _resolve_system_proxy()
    print(f"system_proxy:{system_proxy}")
    window_proxy = await _resolve_window_proxy()
    # 余额接口必须走窗口独立代理，才能匹配该窗口绑定 IP 的国家；没有窗口代理时才回退系统代理。
    # 若需要“两层代理”，请确保本机到 window_proxy 的 TCP 连接已经被系统固定代理透明承载。
    proxy_url = window_proxy or system_proxy or None
    cc = _one_str(country_code).lower()
    commerce_base = _dreamina_commerce_base_for_country(cc)
    header_loc = _dreamina_header_loc_for_country(cc)
    header_lan = header_loc or "en"
    user_credit_url = f"{commerce_base}/commerce/v1/benefits/user_credit"
    print(f"proxy_url:{proxy_url} cc:{cc} commerce_base:{commerce_base}");
    headers = build_jimeng_page_fetch_headers(
        uri="/commerce/v1/benefits/user_credit",
        appid=_DREAMINA_AID,
        appvr=_APPVR,
        pf="7",
        lan=header_lan,
        loc=header_loc,
        headers={
            "Origin": "https://dreamina.capcut.com",
            "Referer": "https://dreamina.capcut.com/",
            "Cookie": "; ".join([
                f"sid_tt={token}",
                f"sessionid={token}",
                f"sessionid_ss={token}",
            ]),
        },
    )
    append_log(log_file, f"[dreamina] local user_credit country={cc or '-'} base={commerce_base} lan={header_lan} loc={header_loc} proxy_system={'yes' if system_proxy else 'no'} proxy_window={'yes' if window_proxy else 'no'}")
    if system_proxy and window_proxy and _dreamina_parse_proxy_url(window_proxy).get("scheme") in ("socks", "socks5", "socks5h"):
        status, body_text = await asyncio.to_thread(
            _dreamina_chain_http_to_socks5h_post_json,
            system_proxy=system_proxy,
            window_proxy=window_proxy,
            url=user_credit_url,
            headers=headers,
            json_data={},
            timeout=45.0,
        )
        try:
            obj = json.loads(body_text)
        except Exception:
            obj = {}
        tx = {"status": status, "response_body": body_text[:2000]}
        data = obj.get("data") if isinstance(obj, dict) else None
        if not isinstance(data, dict) and isinstance(obj, dict):
            resp_s = obj.get("response")
            if isinstance(resp_s, str) and resp_s.strip():
                try:
                    parsed = json.loads(resp_s)
                    if isinstance(parsed, dict):
                        data = parsed
                except Exception:
                    data = None
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
                resp = await client.post(user_credit_url, headers=headers, json={})
            except ImportError as e:
                if proxy_url and str(proxy_url).lower().startswith(("socks5://", "socks5h://", "socks4://")):
                    raise RuntimeError("当前环境缺少 SOCKS 支持，请安装依赖：pip install 'httpx[socks]' socksio") from e
                raise
            obj = resp.json()
            tx = {"status": resp.status_code, "response_body": resp.text[:2000]}
        data = obj.get("data") if isinstance(obj, dict) else None
        if not isinstance(data, dict) and isinstance(obj, dict):
            # 兼容接口只返回 response 字符串 JSON、未展开 data 的情况。
            resp_s = obj.get("response")
            if isinstance(resp_s, str) and resp_s.strip():
                try:
                    parsed = json.loads(resp_s)
                    if isinstance(parsed, dict):
                        data = parsed
                except Exception:
                    data = None
    credit = (data or {}).get("credit") if isinstance(data, dict) else None
    if not isinstance(credit, dict):
        raise RuntimeError(f"Dreamina user_credit 返回缺少 credit：status={tx.get('status')} body={safe_trim(str(tx.get('response_body') or obj), 500)}")
    def _credit_int(v: Any) -> int:
        try:
            return int(float(v or 0))
        except Exception:
            return 0

    gift_credit = _credit_int(credit.get("gift_credit"))
    purchase_credit = _credit_int(credit.get("purchase_credit"))
    vip_credit = _credit_int(credit.get("vip_credit"))
    total_credit = gift_credit + purchase_credit + vip_credit

    cooldown_until: Optional[str] = None
    credits_life_end: Optional[int] = None
    if total_credit > 0 and isinstance(data, dict):
        def _positive_ts(v: Any) -> Optional[int]:
            try:
                ts = int(float(v or 0))
                return ts if ts > 0 else None
            except Exception:
                return None

        candidates: List[int] = []
        detail = data.get("credits_detail")
        if isinstance(detail, dict):
            for list_key in ("vip_credits", "gift_credits", "purchase_credits"):
                items = detail.get(list_key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    ts = _positive_ts(item.get("credits_life_end"))
                    if ts is not None:
                        candidates.append(ts)
        if not candidates:
            expiring = data.get("expiring_credits")
            if isinstance(expiring, list):
                for item in expiring:
                    if not isinstance(item, dict):
                        continue
                    ts = _positive_ts(item.get("expire_time"))
                    if ts is not None:
                        candidates.append(ts)
        if candidates:
            credits_life_end = min(candidates)
            bj_tz = datetime.timezone(datetime.timedelta(hours=8))
            cooldown_until = datetime.datetime.fromtimestamp(credits_life_end, bj_tz).strftime("%Y-%m-%d %H:%M:%S")

    append_log(
        log_file,
        f"[dreamina] user_credit gift={gift_credit} purchase={purchase_credit} vip={vip_credit} total={total_credit} cooldown_until={cooldown_until or ''}",
    )
    return {
        "gift_credit": gift_credit,
        "purchase_credit": purchase_credit,
        "vip_credit": vip_credit,
        "total_credit": total_credit,
        "cooldown_until": cooldown_until,
        "subscription_end_time": credits_life_end,
        "credits_life_end": credits_life_end,
        "source": "user_credit_local_sessionid",
    }


async def refresh_dreamina_balance(
    *,
    db: Database,
    picked: Any,
    refresh_timeout_seconds: float,
    signal_window_pool_replenish: Optional[Callable[[], None]] = None,
) -> Optional[Dict[str, Any]]:
    if str(picked.create_task_handler or "").strip().lower() != "dreamina_workflow":
        return None

    d_target = str(picked.default_target_url or "").strip() or "https://dreamina.capcut.com/"
    country_code = ""
    try:
        country_code = await db.get_window_bound_ip_last_country(window_pk=int(getattr(picked, "window_pk", 0) or 0))
        if not country_code:
            country_code = await db.get_window_bound_ip_last_country(
                space_id=str(getattr(picked, "space_id", "") or ""),
                window_key=str(getattr(picked, "window_key", "") or ""),
            )
    except Exception as e:
        logger.warning("refresh_dreamina_balance read country failed: mapping=%s err=%s", picked.mapping_id, e)
        country_code = ""
    d_info: Optional[Dict[str, Any]] = None
    try:
        d_info = await dreamina_fetch_credits_in_window(
            target_url=d_target,
            access_token=picked.sora_access_token,
            db=db,
            picked=picked,
            country_code=country_code,
        )
    except Exception as e:
        logger.warning("refresh_dreamina_balance failed: mapping=%s err=%s", picked.mapping_id, e)
        return None

    try:
        if d_info is not None and d_info.get("total_credit") is not None:
            update_kw: Dict[str, Any] = {
                "mapping_id": picked.mapping_id,
                "remaining_quota": int(d_info.get("total_credit") or 0),
                "sora_remaining_count": int(d_info.get("total_credit") or 0),
            }
            if d_info.get("cooldown_until"):
                update_kw["cooldown_until"] = str(d_info.get("cooldown_until"))
            await db.update_task_type_window(**update_kw)
            try:
                if _one_str(country_code).lower() == "tr":
                    credit_threshold = 845
                elif _one_str(country_code).lower() == "ca":
                    credit_threshold = 455
                else:
                    credit_threshold = _DREAMINA_MIN_CREDIT
                if int(d_info.get("total_credit") or 0) < credit_threshold and signal_window_pool_replenish is not None:
                    signal_window_pool_replenish()
            except Exception:
                pass
    except Exception:
        pass
    return d_info


async def refresh_dreamina_balance_best_effort(
    *,
    db: Database,
    picked: Any,
    refresh_timeout_seconds: float,
    signal_window_pool_replenish: Optional[Callable[[], None]] = None,
    task_id: Optional[str] = None,
) -> None:
    try:
        await asyncio.wait_for(
            refresh_dreamina_balance(
                db=db,
                picked=picked,
                refresh_timeout_seconds=refresh_timeout_seconds,
                signal_window_pool_replenish=signal_window_pool_replenish,
            ),
            timeout=refresh_timeout_seconds,
        )
    except Exception as e:
        logger.warning(
            "refresh_dreamina_balance skipped: task=%s mapping=%s err=%s",
            task_id,
            picked.mapping_id,
            e,
        )

# ---- 主入口 ----

_DREAMINA_UI_MODEL_FAST = "Dreamina Seedance 2.0 Fast"
_DREAMINA_UI_MODEL_PRO = "Dreamina Seedance 2.0"

def _dreamina_resolve_ui_model(payload: Dict[str, Any], *, has_image: bool = False) -> str:
    """把外部模型名规整为 Dreamina 页面中仅支持的两个视频模型显示名。"""
    model_key = _dreamina_resolve_model(payload, has_image=has_image)
    raw = _one_str((payload or {}).get("model_name") or (payload or {}).get("model"))
    if raw in (_DREAMINA_UI_MODEL_FAST, _DREAMINA_UI_MODEL_PRO):
        return raw
    if _dreamina_is_seedance20_fast(model_key) or "fast" in raw.lower():
        return _DREAMINA_UI_MODEL_FAST
    if _dreamina_is_seedance20_pro(model_key) or model_key == _MODEL_SEEDANCE_20_PRO:
        return _DREAMINA_UI_MODEL_PRO
    raise NonPenalizedTaskError(
        "Dreamina 视频生成仅支持 Dreamina Seedance 2.0 Fast / Dreamina Seedance 2.0",
        status_code=400,
    )


def _dreamina_resolve_ui_mode(payload: Dict[str, Any], image_refs: List[Dict[str, Any]]) -> Tuple[str, str]:
    """返回 (video_mode, reference_mode)。文生/多帧参考图均用 Omni reference；首尾帧用 First and last frames。"""
    raw = _one_str(
        (payload or {}).get("functionMode")
        or (payload or {}).get("function_mode")
        or (payload or {}).get("mode")
        or (payload or {}).get("video_mode")
    ).lower().replace("-", "_").replace(" ", "_")
    if raw in ("first_last_frames", "first_last", "first_and_last_frames", "首尾帧"):
        return "first_last_frames", "First and last frames"
    if image_refs:
        if len(image_refs) <= 2 and raw in ("first_last_frames", "first_last", "first_and_last_frames"):
            return "first_last_frames", "First and last frames"
        return "image_to_video", "Omni reference"
    return "text_to_video", "Omni reference"


class DreaminaUIAutomationController(FingerprintBrowserAutomationBase):
    """Dreamina 站点专用 UI 编排。

    通用的点击/输入兜底能力来自 FingerprintBrowserAutomationBase；
    这里仅保留 Dreamina 的业务选择器和参数选择流程。
    """

    def __init__(self, page: Any, log_file: Path):
        super().__init__(page, log_file=log_file, default_timeout=7000)

    async def _click_option(self, option_name: str) -> None:
        await self.page.wait_for_selector("role=listbox", timeout=7000)
        # 优先在页面 JS 中按文本点击当前可见 option，避免 Playwright locator 在
        # Dreamina 下拉浮层重绘时变成 stale/detached（尤其是时长下拉）。
        clicked_by_js = await self.page.evaluate(
            """(name) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const opts = Array.from(document.querySelectorAll('[role="option"]'))
                    .filter(el => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                    });
                const isTarget = (el) => {
                    const txt = norm(el.innerText || el.textContent || '');
                    const first = norm(txt.split('\\n')[0]);
                    if (first === name || txt === name) return true;
                    if (name === 'Dreamina Seedance 2.0' && first === 'Dreamina Seedance 2.0 Fast') return false;
                    return txt.startsWith(name);
                };
                const el = opts.find(isTarget);
                if (!el) return false;
                el.scrollIntoView({block:'center', inline:'center'});
                el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
                el.click();
                return true;
            }""",
            option_name,
        )
        if clicked_by_js:
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(200)
            return
        # Playwright 的 role name 默认是子串匹配；例如选择
        # "Dreamina Seedance 2.0" 会同时命中 "... 2.0 Fast" 和 "... 2.0 ..."。
        # 这里先按可见文本做精确/前缀筛选，避免 strict mode violation。
        options = self.page.get_by_role("option")
        count = await options.count()
        candidates: List[Tuple[int, str]] = []
        for i in range(count):
            opt = options.nth(i)
            try:
                txt = _one_str(await opt.inner_text(timeout=1000))
            except Exception:
                continue
            if not txt:
                continue
            first_line = _one_str(txt.splitlines()[0])
            if first_line == option_name or txt == option_name:
                await self.safe_click(opt, timeout=7000)
                await self.page.keyboard.press("Escape")
                return
            # 模型卡片的可访问名可能包含描述文案；保留为后备候选。
            if txt.startswith(option_name) and not (
                option_name == _DREAMINA_UI_MODEL_PRO and first_line == _DREAMINA_UI_MODEL_FAST
            ):
                candidates.append((i, txt))
        if candidates:
            await self.safe_click(options.nth(candidates[0][0]), timeout=7000)
            await self.page.keyboard.press("Escape")
            return
        try:
            await self.safe_click(self.page.get_by_role("option", name=option_name, exact=True), timeout=3000)
        except Exception:
            await self.safe_click(self.page.get_by_text(option_name, exact=True).last, timeout=3000)
        await self.page.keyboard.press("Escape")

    async def input_prompt(self, text: str) -> None:
        # Dreamina 的输入框有时不是标准 role=textbox，且历史页会停在中部。
        # 先尽量回到底部，再按多种选择器查找 textarea/input/contenteditable。
        for name in ("Go to bottom", "Bottom"):
            try:
                await self.page.get_by_role("button", name=name, exact=False).click(timeout=1500)
                await self.page.wait_for_timeout(500)
                break
            except Exception:
                pass
        try:
            await self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass

        locators = [
            self.page.get_by_role("textbox").last,
            self.page.locator("textarea").last,
            self.page.locator('input:not([type="hidden"]):not([type="file"])').last,
            self.page.locator('[contenteditable="true"]').last,
            self.page.locator('[role="textbox"]').last,
        ]
        last_err: Optional[Exception] = None
        for tb in locators:
            try:
                if await tb.count() == 0:
                    continue
                await tb.click(timeout=3000, force=True)
                await self.page.keyboard.press("Control+A")
                await self.page.keyboard.press("Backspace")
                try:
                    await tb.fill(text, timeout=2000)
                except Exception:
                    await self.page.keyboard.insert_text(text)
                return
            except Exception as e:
                last_err = e
                continue

        # 最后兜底：在页面内找可编辑元素并直接赋值/派发 input 事件。
        ok = await self.page.evaluate(
            """(text) => {
                const els = Array.from(document.querySelectorAll(
                    'textarea,input:not([type=hidden]):not([type=file]),[contenteditable=true],[role=textbox]'
                )).filter(el => {
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    return r.width > 20 && r.height > 10 && st.visibility !== 'hidden' && st.display !== 'none';
                });
                const el = els[els.length - 1];
                if (!el) return false;
                el.focus();
                if (el.isContentEditable) {
                    el.textContent = text;
                } else {
                    el.value = text;
                }
                el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:text}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                return true;
            }""",
            text,
        )
        if not ok:
            raise last_err or NonPenalizedTaskError("未找到 Dreamina 提示词输入框", status_code=502)

    async def select_ai_type(self, ai_type: str) -> None:
        await self.page.get_by_role("combobox").filter(has_text="AI").first.click(timeout=10000)
        await self._click_option(ai_type)

    async def select_model(self, model: str) -> None:
        await self.page.get_by_role("combobox").filter(has_text="Dreamina").first.click(timeout=10000)
        await self._click_option(model)

    async def select_reference_mode(self, mode: str) -> None:
        # 当前值可能是 "Omni reference" / "First and last frames" / "Multiframes"，
        # 只用 has_text="reference" 会在当前为 First/Multiframes 时找不到。
        pattern = re.compile(r"Omni reference|First and last frames|Multiframes|reference|frames", re.I)
        combo = self.page.get_by_role("combobox").filter(has_text=pattern).last
        try:
            if await combo.count() == 0:
                raise RuntimeError("reference mode combobox not found")
            await combo.click(timeout=5000)
        except Exception:
            # 兜底：从所有 combobox 中按文本排除 AI 类型/模型/时长，选最像参考模式的那个。
            boxes = self.page.get_by_role("combobox")
            count = await boxes.count()
            clicked = False
            for i in range(count - 1, -1, -1):
                b = boxes.nth(i)
                try:
                    txt = _one_str(await b.inner_text(timeout=800))
                except Exception:
                    continue
                low = txt.lower()
                if (
                    any(k in low for k in ("omni", "reference", "first", "last", "frame", "multiframe"))
                    and "dreamina" not in low
                    and "ai video" not in low
                    and not re.search(r"\b\d{1,2}s\b", low)
                ):
                    await b.click(timeout=5000)
                    clicked = True
                    break
            if not clicked:
                # 最后按底部栏常见顺序：AI 类型、模型、参考模式...
                await boxes.nth(2).click(timeout=5000)
        await self._click_option(mode)

    async def select_aspect_ratio(self, ratio: str) -> None:
        try:
            await self.page.get_by_role("button", name=re.compile(r":")).first.click(timeout=7000)
        except Exception:
            await self.page.get_by_text(re.compile(r"21:9|16:9|4:3|1:1|3:4|9:16")).first.click(timeout=7000)
        await self.page.wait_for_selector("role=radiogroup", timeout=7000)
        radio = self.page.get_by_role("radio", name=ratio)
        # Dreamina/Arco 的 radio 原生 input 经常是隐藏的；如果目标已经 checked，
        # 不需要点击。否则优先点可见文本/label，最后再 force click input。
        try:
            if await radio.first.is_checked(timeout=1000):
                await self.page.keyboard.press("Escape")
                return
        except Exception:
            pass
        try:
            await self.page.get_by_text(ratio, exact=True).last.click(timeout=3000)
        except Exception:
            await radio.first.click(timeout=3000, force=True)
        await self.page.keyboard.press("Escape")

    async def select_duration(self, duration: int) -> None:
        target = f"{int(duration)}s"
        await self.page.keyboard.press("Escape")
        # 底部参数栏在窗口较窄时会把时长折叠到右侧 “>” 更多面板里。
        # 先尝试直接找带 “4s/5s...” 的 combobox；找不到就点展开按钮后重试。
        combo = self.page.get_by_role("combobox").filter(has_text=re.compile(r"\b\d{1,2}s\b")).last
        try:
            if await combo.count() == 0:
                raise RuntimeError("duration combobox not found")
            cur_txt = _one_str(await combo.inner_text(timeout=1000))
            if target in cur_txt:
                return
            await combo.click(timeout=3000)
        except Exception:
            for name in (">", "More", "More settings"):
                try:
                    await self.page.get_by_role("button", name=name, exact=True).last.click(timeout=1500)
                    break
                except Exception:
                    pass
            combo = self.page.get_by_role("combobox").filter(has_text=re.compile(r"\b\d{1,2}s\b")).last
            cur_txt = ""
            try:
                cur_txt = _one_str(await combo.inner_text(timeout=1000))
            except Exception:
                pass
            if target in cur_txt:
                return
            await combo.click(timeout=5000)
        await self._click_option(target)

    async def click_submit(self) -> None:
        try:
            first = self.page.get_by_role("combobox").first
            parent = first.locator("xpath=..").locator("xpath=..").locator("xpath=..").locator("xpath=..")
            await parent.locator("button").last.click(timeout=10000)
            return
        except Exception:
            pass
        await self.page.get_by_role("button").last.click(timeout=10000)

def _dreamina_first_str_path(obj: Any, paths: List[Tuple[Any, ...]]) -> str:
    for path in paths:
        cur = obj
        ok = True
        for key in path:
            if isinstance(key, int):
                if not isinstance(cur, list) or key >= len(cur):
                    ok = False
                    break
                cur = cur[key]
            else:
                if not isinstance(cur, dict):
                    ok = False
                    break
                cur = cur.get(key)
        if ok:
            s = _one_str(cur)
            if s:
                return s
    return ""


def _dreamina_extract_history_data(result: Dict[str, Any], submit_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    item = result.get(submit_id)
    return item if isinstance(item, dict) else None


def _dreamina_decode_base64_url(v: Any) -> str:
    s = _one_str(v)
    if not s:
        return ""
    try:
        # Dreamina video_model.video_list.*.main_url/backup_url_* 是 base64 URL。
        # 某些返回可能省略 padding，这里自动补齐。
        raw = base64.b64decode(s + ("=" * (-len(s) % 4)), validate=False)
        url = raw.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""
    return url if url.lower().startswith(("http://", "https://")) else ""


def _dreamina_extract_video_model_url_from_item(item: Dict[str, Any]) -> str:
    """优先从 video.video_model 解析无水印地址。

    get_history_by_ids / get_local_item_list 返回中，transcoded_video.origin.video_url
    常是带水印地址；无水印地址位于 video_model(JSON 字符串) 的
    video_list.*.main_url，且以 base64 编码。
    """
    if not isinstance(item, dict):
        return ""
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    candidates = [
        video.get("video_model") if isinstance(video, dict) else None,
        item.get("video_model"),
    ]
    common_attr = item.get("common_attr") if isinstance(item.get("common_attr"), dict) else {}
    common_video = common_attr.get("video") if isinstance(common_attr.get("video"), dict) else {}
    if isinstance(common_video, dict):
        candidates.append(common_video.get("video_model"))

    for raw_model in candidates:
        model = raw_model
        if isinstance(raw_model, str):
            try:
                model = json.loads(raw_model)
            except Exception:
                continue
        if not isinstance(model, dict):
            continue
        video_list = model.get("video_list")
        if not isinstance(video_list, dict):
            continue
        entries = list(video_list.values())
        entries.sort(key=lambda x: 0 if isinstance(x, dict) and _one_str(x.get("definition")) == "origin" else 1)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for key in ("main_url", "backup_url_1", "backup_url_2", "backup_url_3"):
                url = _dreamina_decode_base64_url(entry.get(key))
                if url:
                    return url
    return ""


def _dreamina_extract_video_url_from_item(item: Dict[str, Any]) -> str:
    return _dreamina_extract_video_model_url_from_item(item) or _dreamina_first_str_path(item, [
        ("video", "transcoded_video", "origin", "video_url"),
        ("common_attr", "video", "transcoded_video", "origin", "video_url"),
        ("video", "download_url"),
        ("video", "play_url"),
        ("video", "url"),
        ("common_attr", "video", "download_url"),
        ("common_attr", "video", "play_url"),
        ("common_attr", "video", "url"),
        ("video_url",),
        ("download_url",),
        ("play_url",),
        ("url",),
    ])


def _dreamina_extract_preview_url_from_item(item: Dict[str, Any]) -> str:
    return _dreamina_first_str_path(item, [
        ("cover", "url"),
        ("cover", "image_url"),
        ("common_attr", "cover_url"),
        ("common_attr", "cover", "url"),
        ("common_attr", "cover", "image_url"),
        ("cover_url",),
        ("cover_image_url",),
        ("image", "url"),
        ("image", "image_url"),
        ("video", "cover", "url"),
        ("video", "cover_url"),
        ("common_attr", "video", "cover", "url"),
        ("common_attr", "video", "cover_url"),
        ("video", "thumbnail_url"),
        ("video", "poster_url"),
        ("common_attr", "video", "thumbnail_url"),
        ("common_attr", "video", "poster_url"),
        ("thumbnail_url",),
        ("poster_url",),
        ("preview_url",),
    ])


async def _dreamina_fetch_hq_video_url(
    page: Any,
    *,
    item_id: str,
    target_page: str,
    log_file: Path,
    api_base: str = _DREAMINA_API_BASE,
    header_loc: str = "US",
) -> str:
    query_url = (
        f"{api_base}{_DREAMINA_GET_LOCAL_ITEM_LIST_PATH}"
        f"?aid={_DREAMINA_AID}&device_platform=web&region={_DREAMINA_REGION}"
        f"&da_version={_DREAMINA_DRAFT_VERSION}&web_version={_DREAMINA_WEB_VERSION}"
        f"&aigc_features={_DREAMINA_AIGC_FEATURES}"
    )
    tx = await page_fetch_json(
        page,
        url=query_url,
        method="POST",
        headers=build_jimeng_page_fetch_headers_body(
            uri=_DREAMINA_GET_LOCAL_ITEM_LIST_PATH,
            appid=_DREAMINA_AID,
            appvr=_APPVR,
            pf="7",
            lan="en",
            loc=header_loc,
            headers={"Referer": target_page},
        ),
        json_data={
            "item_id_list": [item_id],
            "pack_item_opt": {"scene": 1, "need_data_integrity": True},
            "is_for_video_download": True,
        },
        log_file=log_file,
    )
    obj = tx.get("_json") or {}
    append_log(log_file, f"[dreamina-hq] response={safe_trim(_compact_json(obj), 1000)}")
    data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    items = (data or {}).get("item_list") or (data or {}).get("local_item_list") or []
    if isinstance(items, list) and items:
        url = _dreamina_extract_video_url_from_item(items[0] if isinstance(items[0], dict) else {})
        if url:
            return url
    text = _compact_json(obj)
    for pat in (
        r"https://v[0-9]+-dreamnia\.jimeng\.com/[^\"'\s\\]+",
        r"https://v[0-9]+-[^\"'\\]*\.jimeng\.com/[^\"'\s\\]+",
        r"https://v[0-9]+-[^\"'\\]*\.(?:vlabvod|jimeng)\.com/[^\"'\s\\]+",
        r"https://[^\"'\s\\]+\.(?:capcutapi|capcut|byteoversea|ibyteimg)\.[^\"'\s\\]+",
    ):
        m = re.search(pat, text)
        if m:
            return m.group(0)
    return ""


async def _dreamina_remove_history_record(
    sess: "DreaminaSession",
    *,
    task_id: str,
    target_page: str,
    log_file: Path,
) -> Dict[str, Any]:
    history_id = _one_str(task_id)
    if not history_id:
        return {}
    remove_url = (
        f"{sess.dreamina_api_base}{_DREAMINA_REMOVE_HISTORY_PATH}"
        f"?aid={_DREAMINA_AID}&web_version={_DREAMINA_WEB_VERSION}"
        f"&da_version={_DREAMINA_DRAFT_VERSION}&aigc_features={_DREAMINA_AIGC_FEATURES}"
    )
    async with sess._bring_drafts_lock:
        await sess.ensure_open(
            args=sess.browser_open_args,
            force_open=sess.browser_force_open,
            headless=sess.browser_headless,
            acquire_bring_lock=False,
        )
        await sess._bring_target_page_to_front(
            refresh_target=False,
            drafts_url=target_page,
            acquire_bring_lock=False,
            close_other_pages=False,
        )
        page = sess.pw_ctx.page
        if page is None:
            raise NonPenalizedTaskError("Dreamina 页面未打开", status_code=502)
        try:
            tx = await page_fetch_json(
                page,
                url=remove_url,
                method="POST",
                headers=build_jimeng_page_fetch_headers_body(
                    uri=_DREAMINA_REMOVE_HISTORY_PATH,
                    appid=_DREAMINA_AID,
                    appvr=_APPVR,
                    pf="7",
                    lan="en",
                    loc=sess.dreamina_header_loc,
                    headers={"Referer": target_page},
                ),
                json_data={"id_list": [history_id]},
                log_file=log_file,
            )
        finally:
            try:
                await sess.pw_ctx.disconnect_playwright_only()
            except Exception:
                pass
    obj = tx.get("_json") or {}
    append_log(log_file, f"[dreamina-remove-history] task_id={history_id} response={safe_trim(_compact_json(obj), 1000)}")
    if isinstance(obj, dict) and _one_str(obj.get("ret")) not in ("", "0"):
        append_log(log_file, f"[dreamina-remove-history] non-zero ret for task_id={history_id}: {safe_trim(_compact_json(obj), 600)}")
    return obj if isinstance(obj, dict) else {"response": obj}


async def _dreamina_poll_history_until_result(
    sess: "DreaminaSession",
    *,
    submit_id: str,
    task_id: str = "",
    target_page: str,
    log_file: Path,
    progress_cb: ProgressCB,
    max_wait_seconds: float,
) -> Dict[str, Any]:
    await asyncio.sleep(5.0)
    max_retries = max(3, int(float(max_wait_seconds or 600) / 60.0))
    status: Any = 20
    fail_code: Any = None
    history_data: Dict[str, Any] = {}
    item_list: List[Any] = []

    async def _remove_failed_history_record(reason: str) -> None:
        history_id = _one_str(task_id or (history_data.get("history_record_id") if isinstance(history_data, dict) else "") or submit_id)
        if not history_id:
            return
        try:
            await progress_cb(97, {"stage": "remove_failed_history", "task_id": history_id, "submit_id": submit_id, "reason": reason})
            await _dreamina_remove_history_record(
                sess,
                task_id=history_id,
                target_page=target_page,
                log_file=log_file,
            )
        except Exception as e:
            append_log(log_file, f"[dreamina-remove-history] failed failed-record task_id={history_id} submit_id={submit_id} reason={reason}: {e}")

    query_url = (
        f"{sess.dreamina_api_base}{_DREAMINA_GET_HISTORY_BY_IDS_PATH}"
        f"?aid={_DREAMINA_AID}&device_platform=web&region={_DREAMINA_REGION}"
        f"&da_version={_DREAMINA_DRAFT_VERSION}&web_version={_DREAMINA_WEB_VERSION}"
        f"&aigc_features={_DREAMINA_AIGC_FEATURES}"
    )
    for retry in range(max_retries):
        await progress_cb(min(95, 35 + retry), {"stage": "polling", "submit_id": submit_id, "attempt": retry + 1})
        try:
            async with sess._bring_drafts_lock:
                await sess.ensure_open(
                    args=sess.browser_open_args,
                    force_open=sess.browser_force_open,
                    headless=sess.browser_headless,
                    acquire_bring_lock=False,
                )
                await sess._bring_target_page_to_front(
                    refresh_target=True,
                    drafts_url=target_page,
                    acquire_bring_lock=False,
                    close_other_pages=False,
                )
                page = sess.pw_ctx.page
                if page is None:
                    raise NonPenalizedTaskError("Dreamina 页面未打开", status_code=502)
                try:
                    tx = await page_fetch_json(
                        page,
                        url=query_url,
                        method="POST",
                        headers=build_jimeng_page_fetch_headers_body(
                            uri=_DREAMINA_GET_HISTORY_BY_IDS_PATH,
                            appid=_DREAMINA_AID,
                            appvr=_APPVR,
                            pf="7",
                            lan="en",
                            loc=sess.dreamina_header_loc,
                            headers={"Referer": target_page},
                        ),
                        json_data={"submit_ids": [submit_id]},
                        log_file=log_file,
                    )
                finally:
                    try:
                        await sess.pw_ctx.disconnect_playwright_only()
                    except Exception:
                        pass
            obj = tx.get("_json") or {}
            result = obj.get("data") if isinstance(obj.get("data"), dict) else obj
            append_log(log_file, f"[dreamina-poll] attempt={retry + 1}/{max_retries} response={safe_trim(_compact_json(obj), 1000)}")
            history_data = _dreamina_extract_history_data(result or {}, submit_id) or {}
            if not history_data:
                await asyncio.sleep(60)
                continue
            status = history_data.get("status")
            fail_code = history_data.get("fail_code")
            item_list = history_data.get("item_list") or []
            await progress_cb(min(96, 40 + retry), {"stage": "polling", "submit_id": submit_id, "status": status, "fail_code": fail_code})
            if _one_str(status) == "30":
                msg = history_data.get("fail_starling_message") or "内容被过滤"
                await _remove_failed_history_record("status_30")
                raise NonPenalizedTaskError(msg, status_code=422 if _one_str(fail_code) == "2038" else 502)
            if _one_str(status) != "20":
                break
            await asyncio.sleep(60)
        except NonPenalizedTaskError:
            raise
        except Exception as e:
            append_log(log_file, f"[dreamina-poll] error attempt={retry + 1}: {e}")
            await asyncio.sleep(60)
    if _one_str(status) == "20":
        raise NonPenalizedTaskError("Dreamina 视频生成超时，未拿到预览图和视频地址", status_code=504)
    item0 = item_list[0] if isinstance(item_list, list) and item_list and isinstance(item_list[0], dict) else {}
    video_url = _dreamina_extract_video_url_from_item(item0)
    thumb_url = _dreamina_extract_preview_url_from_item(item0)
    item_id = _one_str(item0.get("item_id") or item0.get("id") or item0.get("local_item_id") or ((item0.get("common_attr") or {}).get("id") if isinstance(item0.get("common_attr"), dict) else ""))
    if item_id:
        try:
            async with sess._bring_drafts_lock:
                await sess.ensure_open(
                    args=sess.browser_open_args,
                    force_open=sess.browser_force_open,
                    headless=sess.browser_headless,
                    acquire_bring_lock=False,
                )
                await sess._bring_target_page_to_front(
                    refresh_target=False,
                    drafts_url=target_page,
                    acquire_bring_lock=False,
                    close_other_pages=False,
                )
                page = sess.pw_ctx.page
                if page is None:
                    raise NonPenalizedTaskError("Dreamina 页面未打开", status_code=502)
                try:
                    video_url = await _dreamina_fetch_hq_video_url(
                        page,
                        item_id=item_id,
                        target_page=target_page,
                        log_file=log_file,
                        api_base=sess.dreamina_api_base,
                        header_loc=sess.dreamina_header_loc,
                    ) or video_url
                finally:
                    try:
                        await sess.pw_ctx.disconnect_playwright_only()
                    except Exception:
                        pass
        except Exception as e:
            append_log(log_file, f"[dreamina-hq] fallback preview video url because hq failed: {e}")
    if not video_url:
        await _remove_failed_history_record("missing_video_url")
        raise NonPenalizedTaskError(f"Dreamina submit_id={submit_id}", status_code=502)
    return {"video_url": video_url, "thumb_url": thumb_url, "history_data": history_data, "item_list": item_list, "item_id": item_id}



def _dreamina_is_single_image_upload_payload(payload: Dict[str, Any]) -> bool:
    p = payload or {}
    raw = _one_str(p.get("workflow_kind") or p.get("workflowKind") or p.get("action") or p.get("type") or p.get("functionMode") or p.get("function_mode")).lower()
    norm = raw.replace("-", "_").replace(" ", "_")
    return norm in ("image_upload", "upload_image", "dreamina_image_upload", "dreamina_subject_create", "subject_image_upload") or bool(p.get("save_image_upload") or p.get("image_upload_save"))


def _dreamina_random_subject_name() -> str:
    return "mirr_" + uuid.uuid4().hex[:8]


async def _dreamina_save_subject_row(db: Any, *, uid: str, subject_id: str, data_id: str, name: str, image_uri: str, width: int, height: int) -> int:
    if db is None:
        raise NonPenalizedTaskError("Dreamina 未初始化 db", status_code=500)
    async with db._write_conn() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dreamina_subject_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT,
                subject_id TEXT,
                data_id TEXT,
                name TEXT NOT NULL,
                image_uri TEXT NOT NULL,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                updated_at TIMESTAMP DEFAULT (datetime('now','localtime'))
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_dreamina_subject_images_subject_id ON dreamina_subject_images(subject_id)")
        cur = await conn.execute(
            """INSERT INTO dreamina_subject_images (uid, subject_id, data_id, name, image_uri, width, height)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (uid, subject_id, data_id, name, image_uri, int(width or 0), int(height or 0)),
        )
        await conn.commit()
        return int(cur.lastrowid or 0)


async def _dreamina_single_image_upload_save_workflow(
    page: Any,
    payload: Dict[str, Any],
    *,
    target_page: str,
    log_file: Path,
    db: Any,
    progress_cb: ProgressCB,
    api_base: str = _DREAMINA_API_BASE,
    imagex_base: str = _DREAMINA_IMAGEX_BASE,
    header_loc: str = "US",
) -> Dict[str, Any]:
    src = _one_str(payload.get("image_url") or payload.get("imageUrl") or payload.get("url") or payload.get("image") or payload.get("http_url"))
    if not src.lower().startswith(("http://", "https://")):
        raise NonPenalizedTaskError("图片地址必须以以http(s)开头", status_code=400)
    name = _one_str(payload.get("name") or payload.get("subject_name") or payload.get("subjectName")) or _dreamina_random_subject_name()
    await progress_cb(2, {"stage": "upload_image", "workflow_kind": "image_upload"})

    token_tx = await page_fetch_json(
        page,
        url=f"{api_base}{_DREAMINA_GET_UPLOAD_TOKEN_PATH}",
        method="POST",
        headers=build_jimeng_page_fetch_headers(uri=_DREAMINA_GET_UPLOAD_TOKEN_PATH, appid=_DREAMINA_AID, appvr=_APPVR, pf="7", lan="en", loc=header_loc),
        json_data={"scene": 2},
        log_file=log_file,
    )
    token_obj = token_tx.get("_json") or {}
    if _one_str(token_obj.get("ret")) not in ("", "0"):
        raise NonPenalizedTaskError(f"Dreamina get_upload_token failed: {safe_trim(_compact_json(token_obj), 700)}", status_code=502)
    token_data = token_obj.get("data") if isinstance(token_obj.get("data"), dict) else token_obj
    upload_info = await _dreamina_upload_one_image_via_page_fetch(page, {"source": src}, token_data=token_data or {}, log_file=log_file, index=1, return_info=True, imagex_base=imagex_base)
    uri = _one_str(upload_info.get("uri"))
    width = int(upload_info.get("width") or 0)
    height = int(upload_info.get("height") or 0)
    if not uri or width <= 0 or height <= 0:
        raise NonPenalizedTaskError(f"Dreamina CommitImageUpload 失败 uri/width/height: {safe_trim(_compact_json(upload_info.get('commit_response')), 700)}", status_code=502)

    await progress_cb(45, {"stage": "get_image_by_uri", "uri": uri})
    get_url = f"{api_base}{_DREAMINA_GET_IMAGE_BY_URI_PATH}?aid={_DREAMINA_AID}&device_platform=web&region={_DREAMINA_REGION}&web_version={_DREAMINA_WEB_VERSION}&da_version={_DREAMINA_DRAFT_VERSION}&aigc_features={_DREAMINA_AIGC_FEATURES}"
    img_tx = await page_fetch_json(
        page, url=get_url, method="POST",
        headers=build_jimeng_page_fetch_headers_body(uri=_DREAMINA_GET_IMAGE_BY_URI_PATH, appid=_DREAMINA_AID, appvr=_APPVR, pf="7", lan="en", loc=header_loc, headers={"Referer": target_page}),
        json_data={"uris": [uri]}, log_file=log_file,
    )
    img_obj = img_tx.get("_json") or {}
    image_url = _one_str((((img_obj.get("uri2image") or {}) if isinstance(img_obj, dict) else {}).get(uri) or {}).get("image_url"))
    if not image_url:
        raise NonPenalizedTaskError(f"Dreamina get_image_by_uri missing image_url: {safe_trim(_compact_json(img_obj), 700)}", status_code=502)

    await progress_cb(70, {"stage": "create_subject", "name": name})
    create_url = f"{api_base}{_DREAMINA_SUBJECT_CREATE_PATH}?aid={_DREAMINA_AID}&web_version={_DREAMINA_WEB_VERSION}&da_version={_DREAMINA_DRAFT_VERSION}&aigc_features={_DREAMINA_AIGC_FEATURES}"
    create_body = {"content": {"description": "", "name": name, "main_image": {"width": width, "height": height, "image_uri": uri, "image_url": image_url}}}
    create_tx = await page_fetch_json(
        page, url=create_url, method="POST",
        headers=build_jimeng_page_fetch_headers_body(uri=_DREAMINA_SUBJECT_CREATE_PATH, appid=_DREAMINA_AID, appvr=_APPVR, pf="7", lan="en", loc=header_loc, headers={"Referer": target_page}),
        json_data=create_body, log_file=log_file,
    )
    create_obj = create_tx.get("_json") or {}
    if _one_str(create_obj.get("ret")) not in ("", "0"):
        raise NonPenalizedTaskError(f"Dreamina subject create failed: {safe_trim(_compact_json(create_obj), 700)}", status_code=502)
    data = create_obj.get("data") if isinstance(create_obj.get("data"), dict) else {}
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    main_image = content.get("main_image") if isinstance(content.get("main_image"), dict) else {}
    final_name = _one_str(content.get("name") or name)
    row_id = await _dreamina_save_subject_row(db, uid=_one_str(data.get("uid")), subject_id=_one_str(data.get("subject_id")), data_id=_one_str(data.get("data_id")), name=final_name, image_uri=_one_str(main_image.get("image_uri") or uri), width=int(main_image.get("width") or width), height=int(main_image.get("height") or height))
    await progress_cb(100, {"stage": "done", "id": row_id, "name": final_name})
    return {"type": "dreamina_image_upload", "workflow_kind": "image_upload", "id": row_id, "name": final_name}

async def dreamina_workflow(
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
    pure_mode: bool = True,
    access_token: Optional[str] = None,
    access_expires: Optional[str] = None,
    db: Any = None,
    task_type_window_id: Optional[int] = None,
) -> Dict[str, Any]:
    del task_type_window_id, access_expires

    p = dict(payload or {})
    is_image_upload_save = _dreamina_is_single_image_upload_payload(p)
    prompt = _one_str(p.get("prompt"))
    if not prompt and not is_image_upload_save:
        raise NonPenalizedTaskError("payload.prompt 为空", status_code=400)

    aspect_ratio = _dreamina_resolve_aspect_ratio(p)
    duration = _dreamina_resolve_duration(p)
    omni_refs = _dreamina_collect_external_omni_image_refs(p)
    first_last_refs = _dreamina_collect_first_last_image_refs(p)
    raw_mode = _one_str(p.get("function_mode")).lower()
    if raw_mode.replace("-", "_").replace(" ", "_") in ("first_last_frames", "first_last", "first_and_last_frames"):
        image_refs = first_last_refs[:2]
    else:
        image_refs = omni_refs
    video_mode, reference_mode = _dreamina_resolve_ui_mode(p, image_refs)
    if reference_mode == "First and last frames":
        image_refs = first_last_refs[:2]
        if not image_refs:
            raise NonPenalizedTaskError("参考图请使用https地址", status_code=400, content_violation=True)
    elif len(image_refs) > 9:
        raise NonPenalizedTaskError("Omni reference 超过9张参考图", status_code=400, content_violation=True)
    ui_model = _dreamina_resolve_ui_model(p, has_image=bool(image_refs))
    target_page = (
        _one_str(default_target_url)
        or _one_str(p.get("dreamina_url") or p.get("target_url"))
        or DEFAULT_DREAMINA_TARGET
    )
    monitor_log_path = _one_str(p.get("monitor_log_path")) or None

    sess = get_or_create_dreamina_session(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = headless
    sess.browser_pure_mode = pure_mode
    sess.monitor_log_path = monitor_log_path
    sess.idle_close_seconds = float(p.get("ctx_idle_close_seconds") or 30.0)
    log_file = sess._log_file
    started = time.time()
    await progress_cb(1, {"stage": "prepare upload picture"})
    prompt_parts = _dreamina_parse_prompt_reference_tokens(prompt)
    prompt_subject_ids = [int(x.get("token") or 0) for x in prompt_parts if x.get("type") == "subject" and re.fullmatch(r"\d+", _one_str(x.get("token")))]
    prompt_image_tokens = [x for x in prompt_parts if x.get("type") == "image"]
    prompt_subject_refs = await _dreamina_load_subject_refs_from_db(db, prompt_subject_ids, log_file=log_file)
    if prompt_subject_ids:
        found_ids = {int(x.get("resource_id") or x.get("id") or 0) for x in prompt_subject_refs if isinstance(x, dict)}
        missing_ids = [rid for rid in prompt_subject_ids if rid not in found_ids]
        if missing_ids:
            raise NonPenalizedTaskError(f'"使用了未授权的图片角色【@{missing_ids[0]}】 "', status_code=422, content_violation=True)
        p["omni_material_format"] = "subject"
        p["use_subject_material"] = True
        p["functionMode"] = "omni_reference"

    if prompt_image_tokens:
        p["functionMode"] = "omni_reference"

    if prompt_subject_refs:
        append_log(log_file, f"[dreamina-subject-ref] prompt_ids={prompt_subject_ids} matched={len(prompt_subject_refs)} refs={safe_trim(_compact_json(prompt_subject_refs), 1200)}")
        image_refs = omni_refs
        video_mode, reference_mode = _dreamina_resolve_ui_mode(p, image_refs + prompt_subject_refs)
        ui_model = _dreamina_resolve_ui_model(p, has_image=True)
    elif prompt_subject_ids:
        append_log(log_file, f"[dreamina-subject-ref] prompt_ids={prompt_subject_ids} matched=0, ignored")

    if prompt_image_tokens and len(prompt_image_tokens) != len(omni_refs):
        raise NonPenalizedTaskError(f'参考图片【{prompt_image_tokens[-1].get("raw")}】 未找到', status_code=422, content_violation=True)
    if prompt_subject_refs or prompt_image_tokens:
        p["functionMode"] = "omni_reference"
        reference_mode = "Omni reference"
        video_mode = "image_to_video"
        image_refs = omni_refs
        ui_model = _dreamina_resolve_ui_model(p, has_image=True)

    await progress_cb(2, {
        "stage": "init",
        "workflow_kind": "video",
        "video_mode": video_mode,
        "reference_mode": reference_mode,
        "prompt": safe_trim(prompt, 200),
        "model_name": ui_model,
        "aspect_ratio": aspect_ratio,
        "duration": duration,
        "image_count": len(image_refs) + len(prompt_subject_refs),
    })
    '''
    if should_use_extension_executor(p):
        ext_payload = dict(p)
        ext_payload.update(
            {
                "workflow_kind": "video",
                "video_mode": video_mode,
                "reference_mode": reference_mode,
                "target_page": target_page,
                "aspect_ratio": aspect_ratio,
                "duration": duration,
                "image_refs": image_refs,
                "prompt_subject_refs": prompt_subject_refs,
                "prompt_subject_ids": prompt_subject_ids,
                "timeout_seconds": timeout_seconds,
            }
        )
        append_log(log_file, f"[dreamina][extension] dispatch video_mode={video_mode!r} refs={len(image_refs)}")
        return await submit_extension_task(
            space_id=space_id,
            window_key=window_key,
            provider="dreamina",
            payload=ext_payload,
            progress_cb=progress_cb,
            timeout_seconds=max(60.0, float(timeout_seconds or 600.0)) + 120.0,
        )

    '''
    async with sess.create_lock:
        try:
            sess.idle_close_disabled = True
            sess._cancel_idle_close()
            async with sess._bring_drafts_lock:
                await sess.ensure_open(args=sess.browser_open_args, force_open=sess.browser_force_open, headless=headless, acquire_bring_lock=False, pure_mode=pure_mode)
                await sess._bring_target_page_to_front(refresh_target=False, drafts_url=target_page, close_other_pages=False, acquire_bring_lock=False)
                # --- reload 后深度拟人行为：给 reCAPTCHA 足够的行为信号积累 ---
                try:
                    _ha_page = getattr(sess.pw_ctx, "page", None)
                    if _ha_page and not _ha_page.is_closed():
                        from .window_human_activity import perform_deep_human_activity
                        _deep_result = await perform_deep_human_activity(
                            _ha_page, min_seconds=5.0, max_seconds=15.0,
                        )
                        append_log(log_file, f"[veo] post-inject deep human activity done: {_deep_result}")
                except Exception as _e_post_ha:
                    append_log(log_file, f"[veo] post-inject human activity error: {_e_post_ha}")

                page = sess.pw_ctx.page
                if page is None:
                    raise NonPenalizedTaskError("Dreamina 页面未打开", status_code=502)

                store_country_code = ""
                try:
                    if db is not None:
                        store_country_code = await db.get_window_bound_ip_last_country(space_id=space_id, window_key=window_key)
                except Exception as e:
                    append_log(log_file, f"[dreamina-store-country] read bound ip last_country failed: {safe_trim(str(e), 200)}")
                sess.store_country_code = store_country_code
                await progress_cb(2, {
                    "stage": "store_country_code",
                    "store_country_code": store_country_code,
                })

                if is_image_upload_save:
                    try:
                        return await _dreamina_single_image_upload_save_workflow(
                            page,
                            p,
                            target_page=target_page,
                            log_file=log_file,
                            db=db,
                            progress_cb=progress_cb,
                            api_base=sess.dreamina_api_base,
                            imagex_base=sess.dreamina_imagex_base,
                            header_loc=sess.dreamina_header_loc,
                        )
                    finally:
                        try:
                            await sess.pw_ctx.disconnect_playwright_only()
                        except Exception:
                            pass

                model_key = _dreamina_resolve_model(p, has_image=bool(image_refs))
                resolution = _dreamina_resolve_resolution(p)
                width, height = _dreamina_resolution_size(resolution, aspect_ratio)

                external_prompt_ref_count = len(prompt_image_tokens)
                upload_uris: List[str] = []
                if image_refs:
                    append_log(log_file, f"[dreamina-api-upload] external_refs={safe_trim(_compact_json(image_refs), 1200)}")
                    if external_prompt_ref_count and external_prompt_ref_count != len(image_refs):
                        raise NonPenalizedTaskError('多余的那几个非数字"@xxx"找不到', status_code=422, content_violation=True)
                    await progress_cb(3, {"stage": "upload_images_api", "count": len(image_refs)})
                    upload_uris = await _dreamina_upload_image_refs_via_page_fetch(
                        page,
                        image_refs,
                        log_file=log_file,
                        api_base=sess.dreamina_api_base,
                        imagex_base=sess.dreamina_imagex_base,
                        header_loc=sess.dreamina_header_loc,
                    )
                    if len(upload_uris) < len(image_refs):
                        raise NonPenalizedTaskError("Dreamina reference image upload failed: returned too few URIs", status_code=502)
                    for ref, uri in zip(image_refs, upload_uris):
                        ref["uri"] = uri
                    await progress_cb(5, {"stage": "upload_images_done", "count": len(upload_uris)})

                has_prompt_refs = bool(prompt_subject_refs or prompt_image_tokens)
                if has_prompt_refs:
                    prompt_for_body, material_list, meta_list, material_kinds = _dreamina_bind_prompt_materials(
                        prompt_parts,
                        prompt_subject_refs,
                        image_refs,
                    )
                    append_log(log_file, f"[dreamina-material-bind] kinds={material_kinds} prompt={safe_trim(prompt_for_body, 300)} material_count={len(material_list)} meta_count={len(meta_list)}")
                elif image_refs:
                    prompt_for_body = prompt
                    material_list = []
                    for ref in image_refs:
                        uri = _one_str(ref.get("uri"))
                        material_list.append({
                            "type": "", "id": str(uuid.uuid4()), "material_type": "image",
                            "image_info": {**_dreamina_image_info(uri, int(ref.get("width") or width), int(ref.get("height") or height)), "aigc_image": {"type": "", "id": str(uuid.uuid4())}, "title": "test"},
                        })
                    meta_list = _dreamina_build_meta_list(prompt, len(image_refs))
                else:
                    prompt_for_body = prompt
                    material_list = []
                    meta_list = []

                api_mode = "first_last_frames" if reference_mode == "First and last frames" else "omni_reference"
                submit_id, generate_body, function_mode = _dreamina_seedance_generate_body(
                    prompt=prompt_for_body,
                    model=model_key,
                    ratio=aspect_ratio,
                    resolution=resolution,
                    duration=duration,
                    mode=api_mode,
                    material_list=material_list,
                    meta_list=meta_list,
                    width=width,
                    height=height,
                )
                generate_url = (
                    f"{sess.dreamina_api_base}{_DREAMINA_GENERATE_PATH}"
                    f"?aid={_DREAMINA_AID}&device_platform=web&region={_DREAMINA_REGION}"
                    f"&da_version={_DREAMINA_DRAFT_VERSION}&os=windows&web_component_open_flag=0&commerce_with_input_video=1"
                    f"&web_version={_DREAMINA_WEB_VERSION}&aigc_features={_DREAMINA_AIGC_FEATURES}"
                )
                await progress_cb(6, {"stage": "submit_api", "model_name": model_key, "function_mode": function_mode})
                try:
                    submit_tx = await page_fetch_json(
                        page,
                        url=generate_url,
                        method="POST",
                        headers=build_jimeng_page_fetch_headers_body(
                            uri=_DREAMINA_GENERATE_PATH,
                            appid=_DREAMINA_AID,
                            appvr=_APPVR,
                            pf="7",
                            lan="en",
                            loc=sess.dreamina_header_loc,
                            headers={"Referer": target_page},
                        ),
                        json_data=generate_body,
                        log_file=log_file,
                    )
                    await sess._bring_target_page_to_front(refresh_target=True, drafts_url=target_page, close_other_pages=False, acquire_bring_lock=False)
                finally:
                    try:
                        await sess.pw_ctx.disconnect_playwright_only()
                    except Exception:
                        pass
            submit_obj = submit_tx.get("_json") or {}
            append_log(log_file, f"[dreamina-api-submit] response={safe_trim(_compact_json(submit_obj), 1000)}")
            if _one_str(submit_obj.get("ret")) not in ("", "0"):
                raise NonPenalizedTaskError(f"Dreamina submit failed: {safe_trim(_compact_json(submit_obj), 600)}", status_code=502)
            aigc = ((submit_obj.get("data") or {}) if isinstance(submit_obj, dict) else {}).get("aigc_data") or submit_obj.get("aigc_data") or {}
            task_id = _one_str(aigc.get("history_record_id") or (aigc.get("task") or {}).get("task_id"))
            if not task_id:
                raise NonPenalizedTaskError("Dreamina submit succeeded but history_record_id was not found", status_code=502)
            submit_result = {"response": submit_obj, "submit_id": submit_id, "task_id": task_id, "generate_id": aigc.get("generate_id")}

            task_id = _one_str(submit_result.get("task_id"))
            submit_id = _one_str(submit_result.get("submit_id"))
            await progress_cb(10, {"stage": "submitted", "task_id": task_id, "submit_id": submit_id})
            
            await asyncio.sleep(30);
            poll_result = await _dreamina_poll_history_until_result(
                sess,
                submit_id=submit_id,
                task_id=task_id,
                target_page=target_page,
                log_file=log_file,
                progress_cb=progress_cb,
                max_wait_seconds=max(60.0, float(timeout_seconds or 600.0)),
            )
            video_url = _one_str(poll_result.get("video_url"))
            thumb_url = _one_str(poll_result.get("thumb_url"))
            elapsed_ms = int(max(0.0, (time.time() - started) * 1000.0))
            #任务成功了，删除掉历史记录，使用task_id
            try:
                await progress_cb(98, {"stage": "remove_history", "task_id": task_id, "submit_id": submit_id})
                await _dreamina_remove_history_record(
                    sess,
                    task_id=task_id,
                    target_page=target_page,
                    log_file=log_file,
                )
            except Exception as e:
                append_log(log_file, f"[dreamina-remove-history] failed task_id={task_id}: {e}")
            await progress_cb(100, {
                "stage": "done",
                "task_id": task_id,
                "submit_id": submit_id,
                "elapsed_ms": elapsed_ms,
                "video_url": video_url,
                "thumb_url": thumb_url,
            })

            return {
                "type": "dreamina_workflow_video",
                "message": "Dreamina 视频完成",
                "share_url": video_url,
                "thumb_url": thumb_url,
                "video_type": "i2v" if (image_refs or prompt_subject_refs) else "t2v",
                "model_key": model_key,
                "workflow_kind": "video",
                "video_mode": video_mode,
                "function_mode": "first_last_frames" if reference_mode == "First and last frames" else "omni_reference",
                "reference_mode": reference_mode,
                "model_name": ui_model,
                "aspect_ratio": aspect_ratio,
                "duration": duration,
                "task_id": task_id,
                "history_id": task_id,
                "submit_id": submit_id,
                "generate_id": submit_result.get("generate_id"),
                "image_count": len(image_refs) + len(prompt_subject_refs),
                "item_id": poll_result.get("item_id"),
                "elapsed_ms": elapsed_ms,
            }
        finally:
            try:
                await sess.disconnect_playwright_under_bring_lock()
            except Exception:
                pass
