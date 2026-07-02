"""GPT/ChatGPT 图片/视频工作流执行器。

对齐 ``veo_workflow_executor.py`` 的插件模式：
- Python 侧负责触发/等待浏览器插件、读取/缓存 ChatGPT 长 ``access_token``；
- 余额/会员信息优先走插件，失败后回退到本机 + 窗口代理；
- 图片/视频提交和进度轮询下发到 ``browser_extension/providers/gpt_provider.js``。
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import math
import re
import socket
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from ..core.config import config as app_config
from ..core.logger import logger
from ..core.paths import MONITOR_LOG_FILE
from .browser_extension_bridge import should_use_extension_executor
from .browser_extension_interaction import ensure_extension_connected_via_window, submit_extension_task, wait_extension_client
from .playwright_broswer_context import append_log, safe_trim
from .task_executor_types import NonPenalizedTaskError, ProgressCB
from .veo_workflow_executor import (
    _veo_parse_proxy_url,
    _veo_resolve_system_proxy,
    _veo_resolve_window_proxy,
    get_or_create_veo_session,
)

DEFAULT_GPT_TARGET = "https://chatgpt.com/"
GPT_AUTH_SESSION_PATH = "/api/auth/session"
GPT_BALANCE_INIT_PATH = "/backend-api/conversation/init"
GPT_IMAGE2_PUBLIC_MODELS: Dict[str, str] = {
    "gpt-image2-1k": "1k",
    "gpt-image2-2k": "2k",
    "gpt-image2-4k": "4k",
}
GPT_IMAGE2_MIN_PIXELS = 655_360
GPT_IMAGE2_MAX_PIXELS = 8_294_400
GPT_IMAGE2_MAX_EDGE = 3_840
GPT_IMAGE2_SIZE_TABLE: Dict[str, Dict[str, str]] = {
    "1k": {
        "1:1": "1024x1024",
        "3:2": "1216x832",
        "2:3": "832x1216",
        "4:3": "1152x864",
        "3:4": "864x1152",
        "5:4": "1120x896",
        "4:5": "896x1120",
        "16:9": "1344x768",
        "9:16": "768x1344",
        "21:9": "1536x640",
    },
    "2k": {
        "1:1": "1248x1248",
        "3:2": "1536x1024",
        "2:3": "1024x1536",
        "4:3": "1440x1088",
        "3:4": "1088x1440",
        "5:4": "1392x1120",
        "4:5": "1120x1392",
        "16:9": "1664x928",
        "9:16": "928x1664",
        "21:9": "1904x816",
    },
    "4k": {
        "1:1": "2480x2480",
        "3:2": "3056x2032",
        "2:3": "2032x3056",
        "4:3": "2880x2160",
        "3:4": "2160x2880",
        "5:4": "2784x2224",
        "4:5": "2224x2784",
        "16:9": "3312x1872",
        "9:16": "1872x3312",
        "21:9": "3808x1632",
    },
}


async def _noop_progress_cb(_progress: int, _data: Dict[str, Any]) -> None:
    return None


def _one_str(v: Any) -> str:
    return str(v or "").strip()


def _int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return int(default)
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip()
        return int(float(s)) if s else int(default)
    except Exception:
        return int(default)


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return bool(default)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off", "", "none", "null"}:
        return False
    return bool(v)


def _gpt_target_url(raw: Any = None) -> str:
    s = _one_str(raw) or DEFAULT_GPT_TARGET
    try:
        p = urlparse(s)
        host = (p.hostname or "").lower()
        if p.scheme in {"http", "https"} and host in {"chatgpt.com", "chat.openai.com"}:
            return s
    except Exception:
        pass
    return DEFAULT_GPT_TARGET


def _gpt_origin(target_url: Any = None) -> str:
    target = _gpt_target_url(target_url)
    try:
        p = urlparse(target)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}".rstrip("/")
    except Exception:
        pass
    return "https://chatgpt.com"


def _is_video_payload(payload: Dict[str, Any]) -> bool:
    raw = _one_str((payload or {}).get("workflow_kind") or (payload or {}).get("kind") or (payload or {}).get("type") or (payload or {}).get("mode")).lower()
    model0 = _one_str((payload or {}).get("gpt_image2_model") or (payload or {}).get("model") or (payload or {}).get("model_code") or (payload or {}).get("upstream_model")).lower()
    if model0 in GPT_IMAGE2_PUBLIC_MODELS or model0 in {"gpt-image-2", "gpt-image2"}:
        return False
    if "video" in raw or raw in {"t2v", "i2v", "v2v"}:
        return True
    model = _one_str((payload or {}).get("model") or (payload or {}).get("model_code") or (payload or {}).get("upstream_model")).lower()
    return "video" in model or model.startswith("sora") or model.startswith("vid-")


def _gpt_image2_parse_size(size: Any) -> Optional[Tuple[int, int]]:
    s = _one_str(size)
    m = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", s, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _gpt_image2_size_is_valid(size: str) -> bool:
    if _one_str(size).lower() == "auto":
        return True
    dims = _gpt_image2_parse_size(size)
    if not dims:
        return False
    w, h = dims
    if w <= 0 or h <= 0:
        return False
    if w % 16 != 0 or h % 16 != 0:
        return False
    if max(w, h) > GPT_IMAGE2_MAX_EDGE:
        return False
    short_edge = min(w, h)
    if short_edge <= 0 or (max(w, h) / short_edge) > 3:
        return False
    pixels = w * h
    return GPT_IMAGE2_MIN_PIXELS <= pixels <= GPT_IMAGE2_MAX_PIXELS


def _gpt_image2_normalize_size(size: Any) -> str:
    s = _one_str(size)
    if not s:
        return ""
    if s.lower() == "auto":
        return "auto"
    dims = _gpt_image2_parse_size(s)
    if dims:
        s = f"{dims[0]}x{dims[1]}"
    return s if _gpt_image2_size_is_valid(s) else ""


def _gpt_image2_resolution_tier_for_size(size: Any) -> str:
    dims = _gpt_image2_parse_size(size)
    if not dims:
        return ""
    w, h = dims
    pixels = w * h
    if pixels > 2_400_000 or max(w, h) > 2_048:
        return "4k"
    if pixels > 1_100_000 or max(w, h) > 1_280:
        return "2k"
    return "1k"


def _gpt_image2_resolution(payload: Dict[str, Any]) -> str:
    p = payload or {}
    size = _gpt_image2_normalize_size(p.get("size"))
    if size:
        tier = _gpt_image2_resolution_tier_for_size(size)
        if tier:
            return tier
    for key in ("resolution", "size_tier", "gpt_image2_resolution", "image_resolution"):
        s = _one_str(p.get(key)).lower().replace(" ", "")
        if s in {"1", "1k", "k1"}:
            return "1k"
        if s in {"2", "2k", "k2"}:
            return "2k"
        if s in {"4", "4k", "k4"}:
            return "4k"
    public = _one_str(p.get("gpt_image2_model") or p.get("model")).lower()
    if public in GPT_IMAGE2_PUBLIC_MODELS:
        return GPT_IMAGE2_PUBLIC_MODELS[public]
    return "1k"


def _gpt_image2_ratio_from_size(size: str) -> str:
    s = _one_str(size)
    if not s or s.lower() == "auto":
        return ""
    for by_ratio in GPT_IMAGE2_SIZE_TABLE.values():
        for ratio, candidate in by_ratio.items():
            if candidate == s:
                return ratio
    dims = _gpt_image2_parse_size(s)
    if not dims:
        return ""
    w, h = dims
    g = math.gcd(w, h) or 1
    return f"{w // g}:{h // g}"


def _gpt_image2_ratio(payload: Dict[str, Any]) -> str:
    p = payload or {}
    ratio = _one_str(p.get("ratio") or p.get("aspect_ratio") or p.get("size_ratio") or p.get("aspectRatio"))
    if ratio:
        return ratio
    return _gpt_image2_ratio_from_size(_one_str(p.get("size"))) or "1:1"


def _gpt_image2_size(payload: Dict[str, Any], resolution: Optional[str] = None) -> str:
    p = payload or {}
    size = _gpt_image2_normalize_size(p.get("size"))
    if size:
        return size
    tier = (resolution or _gpt_image2_resolution(p)).lower()
    ratio = _gpt_image2_ratio(p)
    return (GPT_IMAGE2_SIZE_TABLE.get(tier) or GPT_IMAGE2_SIZE_TABLE["1k"]).get(ratio) or GPT_IMAGE2_SIZE_TABLE[tier].get("1:1") or "1024x1024"


def _gpt_is_image2_payload(payload: Dict[str, Any]) -> bool:
    p = payload or {}
    vals = [
        p.get("gpt_image2_model"),
        p.get("model"),
        p.get("model_code"),
        p.get("image_model_name"),
        p.get("upstream_model"),
    ]
    for v in vals:
        s = _one_str(v).lower()
        if s in GPT_IMAGE2_PUBLIC_MODELS or s in {"gpt-image-2", "gpt-image2"}:
            return True
    return False


def _gpt_apply_image2_payload_defaults(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    if not _gpt_is_image2_payload(p):
        return p
    resolution = _gpt_image2_resolution(p)
    size = _gpt_image2_size(p, resolution)
    public_model = _one_str(p.get("gpt_image2_model") or p.get("model")).lower()
    if GPT_IMAGE2_PUBLIC_MODELS.get(public_model) != resolution:
        public_model = f"gpt-image2-{resolution}"
    p["workflow_kind"] = "image"
    p["gpt_image2_model"] = public_model
    p["model_code"] = "gpt-image-2"
    p["image_model_name"] = "gpt-image-2"
    p["resolution"] = resolution
    p["size_tier"] = resolution.upper()
    p["size"] = size
    return p


def _gpt_collect_reference_urls(payload: Dict[str, Any]) -> List[str]:
    p = payload or {}
    out: List[str] = []

    def add(v: Any) -> None:
        if isinstance(v, str):
            s = v.strip()
        elif isinstance(v, dict):
            s = _one_str(v.get("url") or v.get("image_url") or v.get("src"))
        else:
            s = ""
        if s and s not in out:
            out.append(s)

    for key in (
        "image",
        "image_url",
        "imageUrl",
        "first_image_url",
        "firstImageUrl",
        "last_image_url",
        "lastImageUrl",
        "end_image_url",
        "endImageUrl",
        "mask_image_url",
        "mask",
        "reference_image",
    ):
        add(p.get(key))
    for key in (
        "images",
        "image_urls",
        "imageUrls",
        "ref_assets",
        "reference_images",
        "reference_image_urls",
        "input_images",
        "Ingredients_images",
        "ingredients_images",
    ):
        raw = p.get(key)
        if isinstance(raw, list):
            for item in raw:
                add(item)
    return out


def _gpt_count(payload: Dict[str, Any]) -> int:
    for key in ("n", "count", "batch_size"):
        n = _int((payload or {}).get(key), 0)
        if n > 0:
            return max(1, min(n, 4))
    return 1


def _gpt_parse_dt(raw: Any) -> Optional[datetime]:
    s = _one_str(raw)
    if not s:
        return None
    if s.endswith("Z") and len(s) > 1:
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s.replace(" ", "T", 1))
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


def _gpt_jwt_payload(token: Any) -> Dict[str, Any]:
    parts = _one_str(token).split(".")
    if len(parts) < 2:
        return {}
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        obj = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8", "replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _gpt_jwt_exp_dt(token: Any) -> Optional[datetime]:
    exp = _int(_gpt_jwt_payload(token).get("exp"), 0)
    if exp <= 0:
        return None
    try:
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        return None


def _gpt_plan_from_jwt(token: Any) -> str:
    payload = _gpt_jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        for key in ("chatgpt_plan_type", "plan_type", "account_plan_type"):
            s = _one_str(auth.get(key))
            if s:
                return s
    for key in ("plan_type", "account_plan", "chatgpt_plan_type"):
        s = _one_str(payload.get(key))
        if s:
            return s
    return ""


def _gpt_access_token_still_valid(token: Optional[str], expires_raw: Optional[str], *, margin_seconds: float) -> bool:
    at = _one_str(token)
    if not at:
        return False
    dt = _gpt_parse_dt(expires_raw) or _gpt_jwt_exp_dt(at)
    if dt is None:
        return True
    margin = timedelta(seconds=max(0.0, float(margin_seconds or 0.0)))
    if dt.tzinfo is None:
        return datetime.now() + margin < dt
    return datetime.now(timezone.utc) + margin < dt.astimezone(timezone.utc)


def _gpt_auth_headers(access_token: str, *, target_url: str = DEFAULT_GPT_TARGET, path: str = "/") -> Dict[str, str]:
    origin = _gpt_origin(target_url)
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        "OAI-Language": "zh-CN",
        "OAI-Client-Version": "prod-81e0c5cdf6140e8c5db714d613337f4aeab94029",
        "OAI-Client-Build-Number": "6128297",
        "OAI-Device-Id": "00000000-0000-4000-8000-000000000000",
        "X-OpenAI-Target-Path": path,
        "X-OpenAI-Target-Route": path,
    }


def _gpt_chain_http_to_socks5h_request(
    *,
    system_proxy: str,
    window_proxy: str,
    method: str,
    url: str,
    headers: Dict[str, str],
    body: bytes = b"",
    timeout: float = 45.0,
) -> Tuple[int, str]:
    """本机 -> system_proxy(http/https CONNECT) -> window_proxy(socks5h) -> HTTPS request。"""
    sp = _veo_parse_proxy_url(system_proxy)
    wp = _veo_parse_proxy_url(window_proxy)
    target = urlparse(url)
    if sp.get("scheme") not in ("http", "https"):
        raise RuntimeError("两层代理暂仅支持第一层 system_proxy 为 http/https")
    if wp.get("scheme") not in ("socks5", "socks5h", "socks"):
        raise RuntimeError("两层代理暂仅支持第二层 window_proxy 为 socks5/socks5h")
    if (target.scheme or "").lower() != "https":
        raise RuntimeError("GPT 两层代理仅支持 https 目标")
    if not sp.get("host") or not wp.get("host") or not target.hostname:
        raise RuntimeError("代理或目标 URL 缺少 host")

    def read_until(sock: socket.socket, marker: bytes, limit: int = 65536) -> bytes:
        buf = b""
        while marker not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > limit:
                raise RuntimeError("读取代理响应超限")
        return buf

    def recvn(sock: socket.socket, n: int) -> bytes:
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
        status_line = read_until(sock, b"\r\n\r\n").split(b"\r\n", 1)[0].decode("iso-8859-1", "ignore")
        if " 200 " not in f" {status_line} ":
            raise RuntimeError(f"system_proxy CONNECT window_proxy 失败：{status_line}")

        sock.sendall(b"\x05\x02\x00\x02" if (wp.get("username") or wp.get("password")) else b"\x05\x01\x00")
        ver_method = recvn(sock, 2)
        if ver_method[0] != 5 or ver_method[1] == 0xFF:
            raise RuntimeError("window_proxy SOCKS5 握手失败")
        if ver_method[1] == 2:
            user_b = wp.get("username", "").encode("utf-8")
            pwd_b = wp.get("password", "").encode("utf-8")
            sock.sendall(b"\x01" + bytes([len(user_b)]) + user_b + bytes([len(pwd_b)]) + pwd_b)
            if recvn(sock, 2) != b"\x01\x00":
                raise RuntimeError("window_proxy SOCKS5 认证失败")
        elif ver_method[1] != 0:
            raise RuntimeError(f"window_proxy SOCKS5 不支持的认证方式：{ver_method[1]}")

        host_b = target.hostname.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + int(target.port or 443).to_bytes(2, "big"))
        rep = recvn(sock, 4)
        if rep[0] != 5 or rep[1] != 0:
            raise RuntimeError(f"window_proxy SOCKS5 CONNECT 目标失败，rep={rep[1] if len(rep) > 1 else '?'}")
        if rep[3] == 1:
            recvn(sock, 4)
        elif rep[3] == 3:
            recvn(sock, recvn(sock, 1)[0])
        elif rep[3] == 4:
            recvn(sock, 16)
        recvn(sock, 2)

        tls_sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.hostname)
        tls_sock.settimeout(timeout)
        req_path = (target.path or "/") + (f"?{target.query}" if target.query else "")
        req_headers = dict(headers or {})
        req_headers["Host"] = target.netloc
        req_headers["Connection"] = "close"
        if body:
            req_headers["Content-Length"] = str(len(body))
        req = f"{method.upper()} {req_path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in req_headers.items()) + "\r\n"
        tls_sock.sendall(req.encode("utf-8") + (body or b""))
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
            if not line:
                break
            size = int(line.split(b";", 1)[0], 16)
            if size <= 0:
                break
            out += rest[:size]
            rest = rest[size + 2 :]
        resp_body = out
    if "gzip" in header_map.get("content-encoding", ""):
        resp_body = gzip.decompress(resp_body)
    return status, resp_body.decode("utf-8", "replace")


async def _gpt_proxy_request(
    *,
    method: str,
    url: str,
    headers: Dict[str, str],
    json_data: Optional[Dict[str, Any]] = None,
    db: Any = None,
    picked: Any = None,
    log_file: Optional[Path] = None,
    timeout: float = 45.0,
) -> Tuple[int, str, Any]:
    log_file = log_file or MONITOR_LOG_FILE
    system_proxy = await _veo_resolve_system_proxy(db)
    window_proxy = await _veo_resolve_window_proxy(db, picked, log_file)
    proxy_url = window_proxy or system_proxy or None
    body = json.dumps(json_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if json_data is not None else b""
    append_log(log_file, f"[gpt] proxy request {method.upper()} {url} proxy_system={'yes' if system_proxy else 'no'} proxy_window={'yes' if window_proxy else 'no'}")

    if system_proxy and window_proxy and _veo_parse_proxy_url(window_proxy).get("scheme") in ("socks", "socks5", "socks5h"):
        status, text = await asyncio.to_thread(
            _gpt_chain_http_to_socks5h_request,
            system_proxy=system_proxy,
            window_proxy=window_proxy,
            method=method,
            url=url,
            headers=headers,
            body=body,
            timeout=timeout,
        )
        try:
            obj = json.loads(text) if text.strip() else {}
        except Exception:
            obj = None
        return status, text, obj

    client_kwargs: Dict[str, Any] = {"timeout": httpx.Timeout(timeout), "trust_env": True, "follow_redirects": True}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    try:
        client = httpx.AsyncClient(**client_kwargs)
    except ImportError as e:
        if proxy_url and str(proxy_url).lower().startswith(("socks5://", "socks5h://", "socks4://")):
            raise RuntimeError("当前环境缺少 SOCKS 支持，请安装依赖：pip install 'httpx[socks]' socksio") from e
        raise
    async with client:
        resp = await client.request(method.upper(), url, headers=headers, content=body if body else None)
        text = resp.text
        try:
            obj = resp.json() if text.strip() else {}
        except Exception:
            obj = None
        return resp.status_code, text, obj


def _gpt_is_image_quota_feature(name: Any) -> bool:
    n = _one_str(name).lower()
    return n in {"image_gen", "image_generation", "image_edit", "img_gen"} or "image_gen" in n or "img_gen" in n


def _first_int_from_mapping(m: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[int]:
    for k in keys:
        if k in m and m.get(k) is not None:
            return _int(m.get(k), 0)
    return None


def _parse_reset_after(raw: Any) -> int:
    s = _one_str(raw)
    if not s:
        return 0
    if s.isdigit():
        return _int(s, 0)
    dt = _gpt_parse_dt(s)
    if dt is None:
        return 0
    return int(dt.timestamp())


def _gpt_membership_from_raw(raw: Any, fallback: str = "") -> str:
    if isinstance(raw, dict):
        for key in ("plan_type", "membership", "account_plan", "subscription", "account_plan_type"):
            s = _one_str(raw.get(key))
            if s:
                return s
        for v in raw.values():
            s = _gpt_membership_from_raw(v, "")
            if s:
                return s
    elif isinstance(raw, list):
        for item in raw:
            s = _gpt_membership_from_raw(item, "")
            if s:
                return s
    return fallback


def _gpt_normalize_balance_payload(data: Any, *, access_token: str = "") -> Dict[str, Any]:
    d = data if isinstance(data, dict) else {}
    remaining = -1
    total = -1
    reset_at = 0
    limits = d.get("limits_progress") if isinstance(d.get("limits_progress"), list) else []
    for item in limits:
        if not isinstance(item, dict) or not _gpt_is_image_quota_feature(item.get("feature_name")):
            continue
        rem = _first_int_from_mapping(item, ("remaining",))
        if rem is not None and (remaining < 0 or rem < remaining):
            remaining = rem
        max_v = _first_int_from_mapping(item, ("max_value", "cap", "total", "limit"))
        if max_v is not None and max_v > total:
            total = max_v
        if total < 0 and rem is not None:
            used = _first_int_from_mapping(item, ("used", "used_value", "consumed"))
            if used is not None:
                total = rem + used
        ts = _parse_reset_after(item.get("reset_after"))
        if ts > 0 and (reset_at <= 0 or ts < reset_at):
            reset_at = ts
    if remaining < 0:
        for key in ("image_quota_remaining", "remaining", "remaining_quota", "credits"):
            if key in d:
                remaining = _int(d.get(key), 0)
                break
    if total < 0:
        for key in ("image_quota_total", "quota_total", "total", "limit"):
            if key in d:
                total = _int(d.get(key), 0)
                break
    plan = _one_str(d.get("plan_type") or d.get("account_plan") or d.get("subscription") or d.get("account_plan_type") or _gpt_plan_from_jwt(access_token))
    return {
        "remaining": max(0, remaining if remaining >= 0 else 0),
        "image_quota_remaining": max(0, remaining if remaining >= 0 else 0),
        "image_quota_total": max(0, total if total >= 0 else 0),
        "image_quota_reset_at": reset_at,
        "plan_type": plan or None,
        "membership": plan or None,
        "default_model_slug": _one_str(d.get("default_model_slug") or d.get("default_model")) or None,
        "blocked_features": d.get("blocked_features") if isinstance(d.get("blocked_features"), list) else [],
        "raw": d,
    }


def _gpt_extension_ids_from_session(sess: Any, *, space_id: Optional[str] = None, window_key: Optional[str] = None) -> Tuple[str, str]:
    sid = _one_str(space_id or getattr(getattr(sess, "pw_ctx", None), "space_id", ""))
    wkey = _one_str(window_key or getattr(getattr(sess, "pw_ctx", None), "window_key", ""))
    if not sid or not wkey:
        raise RuntimeError("缺少插件连接标识：space_id/window_key")
    return sid, wkey


async def gpt_fetch_access_token_via_extension(
    *,
    sess: Any,
    target_url: str = DEFAULT_GPT_TARGET,
    space_id: Optional[str] = None,
    window_key: Optional[str] = None,
    connect_wait_seconds: float = 8.0,
    token_timeout_seconds: float = 45.0,
    log_file: Optional[Path] = None,
    auto_triger_connection: Optional[bool] = True,
) -> Dict[str, Any]:
    """通过浏览器插件读取 ChatGPT ``/api/auth/session`` access_token。"""
    sid, wkey = _gpt_extension_ids_from_session(sess, space_id=space_id, window_key=window_key)
    log_file = log_file or (Path(sess.monitor_log_path) if getattr(sess, "monitor_log_path", None) else MONITOR_LOG_FILE)
    target = _gpt_target_url(target_url)
    client = await ensure_extension_connected_via_window(
        sess=sess,
        target_url=target,
        space_id=sid,
        window_key=wkey,
        wait_seconds=connect_wait_seconds,
        log_file=log_file,
        force_open=getattr(sess, "browser_force_open", None),
        headless=getattr(sess, "browser_headless", None),
        pure_mode=getattr(sess, "browser_pure_mode", None),
        auto_triger_connection=auto_triger_connection,
    )
    if client is None:
        raise NonPenalizedTaskError(f"浏览器插件未连接：space_id={sid!r} window_key={wkey!r}", status_code=503)
    append_log(log_file, "[gpt][token] fetch access_token via extension")
    info = await submit_extension_task(
        space_id=sid,
        window_key=wkey,
        provider="gpt",
        payload={"action": "get_access_token", "workflow_kind": "fetch_access_token", "target_url": target},
        progress_cb=_noop_progress_cb,
        timeout_seconds=max(5.0, float(token_timeout_seconds or 45.0)),
    )
    at = _one_str((info or {}).get("access_token") or (info or {}).get("accessToken"))
    if not at:
        raise NonPenalizedTaskError("插件未返回 GPT access_token", status_code=401)
    out = dict(info)
    out["access_token"] = at
    out["expires"] = _one_str(out.get("expires")) or None
    out["source"] = _one_str(out.get("source")) or "extension.auth_session"
    append_log(log_file, f"[gpt][token] extension returned access_token len={len(at)}")
    return out


async def gpt_fetch_access_token_in_window(
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    target_url: str = DEFAULT_GPT_TARGET,
    headless: bool = False,
    pure_mode: bool = True,
    timeout_seconds: float = 60.0,
) -> Dict[str, Any]:
    """兼容旧入口：通过浏览器插件读取 ChatGPT 长 access_token。"""
    sess = get_or_create_veo_session(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = headless
    sess.browser_pure_mode = pure_mode
    return await gpt_fetch_access_token_via_extension(
        sess=sess,
        target_url=target_url or DEFAULT_GPT_TARGET,
        space_id=space_id,
        window_key=window_key,
        connect_wait_seconds=10.0,
        token_timeout_seconds=timeout_seconds,
        log_file=sess._log_file,
    )


async def gpt_fetch_balance_via_extension(
    *,
    sess: Any,
    target_url: str = DEFAULT_GPT_TARGET,
    access_token: Optional[str] = None,
    access_expires: Optional[str] = None,
    space_id: Optional[str] = None,
    window_key: Optional[str] = None,
    connect_wait_seconds: float = 8.0,
    balance_timeout_seconds: float = 45.0,
    log_file: Optional[Path] = None,
    auto_triger_connection: Optional[bool] = True,
) -> Dict[str, Any]:
    """通过浏览器插件在 ChatGPT 页面内读取额度/会员信息。"""
    sid, wkey = _gpt_extension_ids_from_session(sess, space_id=space_id, window_key=window_key)
    log_file = log_file or (Path(sess.monitor_log_path) if getattr(sess, "monitor_log_path", None) else MONITOR_LOG_FILE)
    target = _gpt_target_url(target_url)
    client = await ensure_extension_connected_via_window(
        sess=sess,
        target_url=target,
        space_id=sid,
        window_key=wkey,
        wait_seconds=connect_wait_seconds,
        log_file=log_file,
        headless=getattr(sess, "browser_headless", None),
        pure_mode=getattr(sess, "browser_pure_mode", None),
        auto_triger_connection=auto_triger_connection,
    )
    if client is None:
        raise NonPenalizedTaskError(f"浏览器插件未连接：space_id={sid!r} window_key={wkey!r}", status_code=503)
    at = _one_str(access_token)
    exp = _one_str(access_expires) or None
    if not at:
        tok = await gpt_fetch_access_token_via_extension(
            sess=sess,
            target_url=target,
            space_id=sid,
            window_key=wkey,
            connect_wait_seconds=0.5,
            token_timeout_seconds=min(45.0, max(10.0, balance_timeout_seconds)),
            log_file=log_file,
            auto_triger_connection=False,
        )
        at = _one_str(tok.get("access_token"))
        exp = _one_str(tok.get("expires")) or exp
    if not at:
        raise NonPenalizedTaskError("缺少 GPT access_token，无法读取余额", status_code=401)
    return await submit_extension_task(
        space_id=sid,
        window_key=wkey,
        provider="gpt",
        payload={"action": "refresh_balance", "workflow_kind": "balance_refresh", "target_url": target, "access_token": at, "access_expires": exp},
        progress_cb=_noop_progress_cb,
        timeout_seconds=max(5.0, float(balance_timeout_seconds or 45.0)),
    )


async def gpt_fetch_membership_via_extension(
    *,
    sess: Any,
    target_url: str = DEFAULT_GPT_TARGET,
    access_token: Optional[str] = None,
    access_expires: Optional[str] = None,
    space_id: Optional[str] = None,
    window_key: Optional[str] = None,
    connect_wait_seconds: float = 8.0,
    membership_timeout_seconds: float = 45.0,
    log_file: Optional[Path] = None,
    auto_triger_connection: Optional[bool] = True,
) -> Dict[str, Any]:
    """通过浏览器插件读取 ChatGPT 会员/套餐信息。

    该入口与 ``gpt_workflow`` 共用同一套插件连接、access token 获取和
    ChatGPT Web ``conversation/init`` 流程；不会走两层代理。
    """
    sid, wkey = _gpt_extension_ids_from_session(sess, space_id=space_id, window_key=window_key)
    log_file = log_file or (Path(sess.monitor_log_path) if getattr(sess, "monitor_log_path", None) else MONITOR_LOG_FILE)
    target = _gpt_target_url(target_url)
    client = await ensure_extension_connected_via_window(
        sess=sess,
        target_url=target,
        space_id=sid,
        window_key=wkey,
        wait_seconds=connect_wait_seconds,
        log_file=log_file,
        headless=getattr(sess, "browser_headless", None),
        pure_mode=getattr(sess, "browser_pure_mode", None),
        auto_triger_connection=auto_triger_connection,
    )
    if client is None:
        raise NonPenalizedTaskError(f"浏览器插件未连接：space_id={sid!r} window_key={wkey!r}", status_code=503)

    at = _one_str(access_token)
    exp = _one_str(access_expires) or None
    if not _gpt_access_token_still_valid(at, exp, margin_seconds=60.0):
        tok = await gpt_fetch_access_token_via_extension(
            sess=sess,
            target_url=target,
            space_id=sid,
            window_key=wkey,
            connect_wait_seconds=0.5,
            token_timeout_seconds=min(45.0, max(10.0, membership_timeout_seconds)),
            log_file=log_file,
            auto_triger_connection=False,
        )
        at = _one_str(tok.get("access_token"))
        exp = _one_str(tok.get("expires")) or exp
    if not at:
        raise NonPenalizedTaskError("缺少 GPT access_token，无法读取会员信息", status_code=401)

    append_log(log_file, "[gpt][membership] fetch membership via extension")
    result = await submit_extension_task(
        space_id=sid,
        window_key=wkey,
        provider="gpt",
        payload={"action": "refresh_membership", "workflow_kind": "membership_refresh", "target_url": target, "access_token": at, "access_expires": exp},
        progress_cb=_noop_progress_cb,
        timeout_seconds=max(5.0, float(membership_timeout_seconds or 45.0)),
    )
    out = dict(result or {})
    membership = _one_str(out.get("membership") or out.get("plan_title") or out.get("plan_type") or _gpt_plan_from_jwt(at))
    if not membership:
        membership = _gpt_membership_from_raw(out.get("raw"), "")
    if membership:
        out["membership"] = membership
        out["plan_title"] = _one_str(out.get("plan_title") or membership)
        out["plan_type"] = _one_str(out.get("plan_type") or membership)
    out["access_token"] = at
    out["expires"] = exp
    out["source"] = _one_str(out.get("source")) or "extension.membership"
    return out


async def gpt_fetch_membership_in_window(
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    target_url: str = DEFAULT_GPT_TARGET,
    access_token: Optional[str] = None,
    access_expires: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
    timeout_seconds: float = 60.0,
) -> Dict[str, Any]:
    """兼容管理端入口：打开/连接指纹窗口插件并读取 GPT 会员信息。"""
    sess = get_or_create_veo_session(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = headless
    sess.browser_pure_mode = pure_mode
    return await gpt_fetch_membership_via_extension(
        sess=sess,
        target_url=target_url or DEFAULT_GPT_TARGET,
        access_token=access_token,
        access_expires=access_expires,
        space_id=space_id,
        window_key=window_key,
        connect_wait_seconds=10.0,
        membership_timeout_seconds=timeout_seconds,
        log_file=sess._log_file,
        auto_triger_connection=True,
    )


async def refresh_gpt_balance_via_extension(
    *,
    db: Any,
    picked: Any,
    refresh_timeout_seconds: float,
    signal_window_pool_replenish: Optional[Any] = None,
    auto_triger_connection: Optional[bool] = True,
    force_refresh_token: Optional[bool] = False,
) -> Optional[Dict[str, Any]]:
    """通过浏览器插件刷新 GPT 账号额度并写回 task_type_window。"""
    try:
        target = _gpt_target_url(_one_str(getattr(picked, "default_target_url", "")) or DEFAULT_GPT_TARGET)
        sess = get_or_create_veo_session(
            vendor=getattr(picked, "browser_vendor", "roxy"),
            base_url=getattr(picked, "browser_base_url", ""),
            access_key=getattr(picked, "browser_access_key", None),
            space_id=getattr(picked, "space_id", ""),
            window_key=getattr(picked, "window_key", ""),
        )
        sess.browser_headless = bool(getattr(picked, "headless", False))
        sess.browser_pure_mode = bool(getattr(picked, "pure_mode", True))
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
        print(f"access_token:{access_token} access_expires:{access_expires}");
        result = await gpt_fetch_balance_via_extension(
            sess=sess,
            target_url=target,
            access_token=access_token,
            access_expires=access_expires,
            space_id=getattr(picked, "space_id", ""),
            window_key=getattr(picked, "window_key", ""),
            connect_wait_seconds=0.5,
            balance_timeout_seconds=max(1.0, float(refresh_timeout_seconds or 45.0)),
            log_file=MONITOR_LOG_FILE,
            auto_triger_connection=auto_triger_connection,
        )
        remaining = _int(result.get("remaining", result.get("image_quota_remaining")), 0)
        kwargs: Dict[str, Any] = {
            "mapping_id": int(getattr(picked, "mapping_id", 0) or 0),
            "remaining_quota": remaining,
            "sora_remaining_count": remaining,
            "sora_access_token": access_token,
            "sora_access_expires": access_expires,
        }
        if result.get("cooldown_until"):
            kwargs["cooldown_until"] = str(result.get("cooldown_until"))
        if result.get("membership") or result.get("plan_type"):
            kwargs["sora_plan_title"] = _one_str(result.get("membership") or result.get("plan_type")) or None
        await db.update_task_type_window(**kwargs)
        try:
            if remaining < 30 and signal_window_pool_replenish is not None:
                hi = await db.task_type_has_mapping_remaining_quota_above(getattr(picked, "task_code", ""), 30)
                if hi:
                    signal_window_pool_replenish()
        except Exception:
            pass
        return result
    except Exception as e:
        logger.warning("refresh_gpt_balance_via_extension skipped: mapping=%s err=%s", getattr(picked, "mapping_id", None), e)
        return None


async def gpt_fetch_balance_by_proxy(
    *,
    access_token: str,
    target_url: str = DEFAULT_GPT_TARGET,
    db: Any = None,
    picked: Any = None,
    log_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """经本机/窗口代理读取 ChatGPT 额度。

    对齐 gpt2api-main ``probeChatGPTAccount``：优先 POST
    ``/backend-api/conversation/init``，解析 ``limits_progress`` 中的图片额度。
    """
    at = _one_str(access_token)
    if not at:
        raise NonPenalizedTaskError("缺少 GPT access_token", status_code=401)
    target = _gpt_target_url(target_url)
    base = _gpt_origin(target)
    log_file = log_file or MONITOR_LOG_FILE
    raw: Dict[str, Any] = {}
    errors: List[str] = []
    success = False

    init_url = base + GPT_BALANCE_INIT_PATH
    init_body = {"gizmo_id": None, "requested_default_model": None, "conversation_id": None, "timezone_offset_min": -480, "system_hints": ["picture_v2"]}
    st, text, data = await _gpt_proxy_request(
        method="POST",
        url=init_url,
        headers=_gpt_auth_headers(at, target_url=target, path=GPT_BALANCE_INIT_PATH),
        json_data=init_body,
        db=db,
        picked=picked,
        log_file=log_file,
        timeout=45.0,
    )
    raw[init_url] = data if data is not None else {"status": st, "body": safe_trim(text, 1000)}
    if int(st or 0) < 400 and isinstance(data, dict):
        success = True
        out = _gpt_normalize_balance_payload(data, access_token=at)
    else:
        errors.append(f"conversation/init HTTP {st}: {safe_trim(text, 300)}")
        out = _gpt_normalize_balance_payload({}, access_token=at)

    for path in ("/backend-api/me", "/backend-api/accounts/check/v4-2023-04-27"):
        url = base + path
        try:
            st, tx, obj = await _gpt_proxy_request(
                method="GET",
                url=url,
                headers=_gpt_auth_headers(at, target_url=target, path=path),
                db=db,
                picked=picked,
                log_file=log_file,
                timeout=30.0,
            )
            raw[url] = obj if obj is not None else {"status": st, "body": safe_trim(tx, 1000)}
            if int(st or 0) < 400 and isinstance(obj, dict):
                success = True
                if not out.get("membership"):
                    m = _gpt_membership_from_raw(obj, "")
                    if m:
                        out["membership"] = m
                        out["plan_type"] = out.get("plan_type") or m
                for key in ("remaining", "remaining_quota", "credits", "image_quota_remaining"):
                    if out.get("remaining", 0) <= 0 and key in obj:
                        out["remaining"] = _int(obj.get(key), 0)
                        out["image_quota_remaining"] = out["remaining"]
            elif int(st or 0) >= 400:
                errors.append(f"{path} HTTP {st}: {safe_trim(tx, 200)}")
        except Exception as e:
            raw[url] = {"error": str(e)}
            errors.append(f"{path}: {e}")
    if not success and errors:
        raise RuntimeError("GPT 余额查询失败：" + safe_trim(errors[0], 500))
    out["raw"] = raw
    return out


async def gpt_fetch_membership_by_proxy(*, access_token: str, target_url: str = DEFAULT_GPT_TARGET, db: Any = None, picked: Any = None) -> Dict[str, Any]:
    if not access_token:
        raise NonPenalizedTaskError("缺少 GPT access_token", status_code=401)
    info = await gpt_fetch_balance_by_proxy(access_token=access_token, target_url=target_url, db=db, picked=picked)
    membership = _one_str(info.get("membership") or info.get("plan_type") or _gpt_plan_from_jwt(access_token))
    if not membership:
        membership = _gpt_membership_from_raw(info.get("raw"), "")
    return {"membership": membership or None, "raw": info.get("raw"), **{k: v for k, v in info.items() if k != "raw"}}


async def refresh_gpt_balance(ctx: Any) -> int:
    row = ctx.mapping_row or {}
    target_url = _gpt_target_url(_one_str(row.get("default_target_url")) or DEFAULT_GPT_TARGET)
    mapping_id = int(row.get("id") or row.get("mapping_id") or 0)
    picked = SimpleNamespace(
        window_pk=int(row.get("window_pk") or 0),
        mapping_id=mapping_id,
        task_code=_one_str(row.get("task_code") or getattr(getattr(ctx, "task_type", None), "code", "")),
        default_target_url=target_url,
        browser_vendor=_one_str(row.get("vendor") or "roxy"),
        browser_base_url=_one_str(row.get("lan_addr")),
        browser_access_key=row.get("access_key"),
        space_id=_one_str(row.get("space_id")),
        window_key=_one_str(row.get("window_key")),
        headless=_bool(row.get("_headless", row.get("headless")), False),
        pure_mode=_bool(row.get("pure_mode"), True),
    )
    if app_config.extension_executor_enabled and picked.space_id and picked.window_key and picked.browser_base_url:
        print(f"-----------------");
        ext_info = await refresh_gpt_balance_via_extension(db=ctx.db, picked=picked, refresh_timeout_seconds=30.0, auto_triger_connection=True)
        print(f"ext_info:{ext_info}");
        if isinstance(ext_info, dict) and (ext_info.get("remaining") is not None or ext_info.get("image_quota_remaining") is not None):
            return _int(ext_info.get("remaining", ext_info.get("image_quota_remaining")), 0)

    at = _one_str(row.get("sora_access_token"))
    if not at:
        raise RuntimeError("缺少 GPT access_token（插件未读取到余额，且未保存 access_token，无法回退两层代理）")
    info = await gpt_fetch_balance_by_proxy(access_token=at, target_url=target_url, db=ctx.db, picked=picked)
    remaining = _int(info.get("remaining", info.get("image_quota_remaining")), 0)
    kwargs: Dict[str, Any] = {"mapping_id": mapping_id, "remaining_quota": remaining, "sora_remaining_count": remaining}
    if info.get("membership") or info.get("plan_type"):
        kwargs["sora_plan_title"] = _one_str(info.get("membership") or info.get("plan_type")) or None
    if info.get("cooldown_until"):
        kwargs["cooldown_until"] = str(info.get("cooldown_until"))
    await ctx.db.update_task_type_window(**kwargs)
    return remaining


async def gpt_submit_task_via_extension(
    *,
    payload: Dict[str, Any],
    progress_cb: ProgressCB,
    space_id: str,
    window_key: str,
    target_url: str,
    access_token: str,
    access_expires: Optional[str],
    timeout_seconds: float,
) -> Dict[str, Any]:
    p = _gpt_apply_image2_payload_defaults(dict(payload or {}))
    kind = "video" if _is_video_payload(p) else "image"
    max_wait = float(p.get("max_wait_seconds") or p.get("gpt_pending_max_wait_seconds") or timeout_seconds or 600.0)
    ext_payload = dict(p)
    ext_payload.update(
        {
            "action": "submit_task",
            "workflow_kind": kind,
            "target_url": _gpt_target_url(target_url),
            "access_token": _one_str(access_token),
            "access_expires": _one_str(access_expires) or None,
            "reference_urls": _gpt_collect_reference_urls(p),
            "count": _gpt_count(p),
            "timeout_seconds": timeout_seconds,
            "max_wait_seconds": max_wait,
            "poll_interval_seconds": float(p.get("poll_interval_seconds") or p.get("gpt_pending_poll_interval_seconds") or 5.0),
        }
    )
    return await submit_extension_task(
        space_id=space_id,
        window_key=window_key,
        provider="gpt",
        payload=ext_payload,
        progress_cb=progress_cb,
        timeout_seconds=max(1.0, max_wait + 60.0),
    )


async def gpt_query_task_progress_via_extension(
    *,
    sess: Any,
    target_url: str,
    access_token: str,
    conversation_id: str,
    progress_cb: ProgressCB = _noop_progress_cb,
    file_ids: Optional[List[str]] = None,
    sediment_ids: Optional[List[str]] = None,
    workflow_kind: str = "image",
    space_id: Optional[str] = None,
    window_key: Optional[str] = None,
    timeout_seconds: float = 45.0,
) -> Dict[str, Any]:
    """通过插件查询/补轮询 ChatGPT conversation 生成结果。"""
    sid, wkey = _gpt_extension_ids_from_session(sess, space_id=space_id, window_key=window_key)
    if await wait_extension_client(sid, wkey, timeout_seconds=0.2) is None:
        await ensure_extension_connected_via_window(sess=sess, target_url=_gpt_target_url(target_url), space_id=sid, window_key=wkey, wait_seconds=8.0, log_file=getattr(sess, "_log_file", MONITOR_LOG_FILE))
    return await submit_extension_task(
        space_id=sid,
        window_key=wkey,
        provider="gpt",
        payload={
            "action": "query_progress",
            "workflow_kind": workflow_kind,
            "target_url": _gpt_target_url(target_url),
            "access_token": _one_str(access_token),
            "conversation_id": _one_str(conversation_id),
            "file_ids": file_ids or [],
            "sediment_ids": sediment_ids or [],
        },
        progress_cb=progress_cb,
        timeout_seconds=max(5.0, float(timeout_seconds or 45.0)),
    )

async def gpt_workflow(
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
    default_target_url: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
    db: Any = None,
    task_type_window_id: Optional[int] = None,
) -> Dict[str, Any]:
    payload = _gpt_apply_image2_payload_defaults(dict(payload or {}))
    prompt = _one_str(payload.get("prompt"))
    if not prompt:
        raise NonPenalizedTaskError("payload.prompt 不能为空", status_code=400)
    if not should_use_extension_executor({**payload, "executor": payload.get("executor") or "extension"}):
        raise NonPenalizedTaskError("GPT workflow only supports browser extension mode", status_code=400)

    target_url = _gpt_target_url(payload.get("gpt_url") or payload.get("target_url") or default_target_url or DEFAULT_GPT_TARGET)
    sess = get_or_create_veo_session(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    sess.browser_headless = headless
    sess.browser_pure_mode = pure_mode
    log_file = getattr(sess, "_log_file", MONITOR_LOG_FILE)
    kind = "video" if _is_video_payload(payload) else "image"
    await progress_cb(1, {"stage": "init", "workflow_kind": kind, "prompt": safe_trim(prompt, 200)})

    try:
        await ensure_extension_connected_via_window(
            sess=sess,
            target_url=target_url,
            space_id=space_id,
            window_key=window_key,
            wait_seconds=float(payload.get("extension_connect_wait_seconds") or 10.0),
            headless=headless,
            pure_mode=pure_mode,
            log_file=log_file,
        )
    except Exception as e:
        append_log(log_file, f"[gpt] trigger/wait extension failed: {e}")

    at = _one_str(access_token)
    exp = _one_str(access_expires) or None
    if db is not None and task_type_window_id:
        try:
            row = await db.get_task_type_window_context(int(task_type_window_id))
            if row:
                at = _one_str(row.get("sora_access_token") or at)
                exp = _one_str(row.get("sora_access_expires") or exp) or None
        except Exception as e:
            append_log(log_file, f"[gpt] reload access_token from DB failed (use call args): {e}")

    margin = float(payload.get("gpt_access_token_renew_margin_seconds") or 300.0)
    if not _gpt_access_token_still_valid(at, exp, margin_seconds=margin):
        tok = await gpt_fetch_access_token_via_extension(
            sess=sess,
            target_url=target_url,
            space_id=space_id,
            window_key=window_key,
            connect_wait_seconds=float(payload.get("extension_connect_wait_seconds") or 10.0),
            token_timeout_seconds=float(payload.get("extension_token_timeout_seconds") or 45.0),
            log_file=log_file,
            auto_triger_connection=True,
        )
        at = _one_str(tok.get("access_token"))
        exp = _one_str(tok.get("expires")) or None
        if at and db is not None and task_type_window_id:
            try:
                await db.update_task_type_window(mapping_id=int(task_type_window_id), sora_access_token=at, sora_access_expires=exp)
                append_log(log_file, f"[gpt] persisted access_token to task_type_window id={int(task_type_window_id)}")
            except Exception as e:
                append_log(log_file, f"[gpt] persist access_token to DB failed (non-fatal): {e}")
    if not at:
        raise NonPenalizedTaskError("缺少 GPT access_token：请确认 ChatGPT 已登录", status_code=401)

    try:
        return await gpt_submit_task_via_extension(
            payload=payload,
            progress_cb=progress_cb,
            space_id=space_id,
            window_key=window_key,
            target_url=target_url,
            access_token=at,
            access_expires=exp,
            timeout_seconds=timeout_seconds,
        )
    except Exception as e:
        raise
