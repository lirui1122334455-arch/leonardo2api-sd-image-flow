"""Leonardo.ai video generation executor.

This executor reuses an already logged-in fingerprint browser window. It
captures the app's own GraphQL auth headers from that page, then submits the
same `Generate` mutation used by the Seedance 2.0 Fast page.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, unquote_to_bytes, urlencode, urlparse, urlunparse

import httpx

from ..core.paths import MONITOR_LOG_FILE
from .playwright_broswer_context import (
    append_log,
    get_or_create_ctx as get_or_create_playwright_ctx,
    page_fetch_json,
    page_fetch_tx,
    safe_trim,
)
from .task_executor_types import NonPenalizedTaskError, ProgressCB


DEFAULT_LEONARDO_TARGET = "https://app.leonardo.ai/generate?model=seedance-2.0-fast"
LEONARDO_GRAPHQL_URL = "https://api.leonardo.ai/v1/graphql"
LEONARDO_SESSION_PING_PATH = "/api/auth/cross-origin-cookie"
LEONARDO_SCHEMA_VERSION = "1.209.3"
LEONARDO_DEFAULT_AUTH_CACHE_SECONDS = 600.0
LEONARDO_SEEDANCE_MODEL = "seedance-2.0"
LEONARDO_SEEDANCE_FAST_MODEL = "seedance-2.0-fast"
LEONARDO_SEEDANCE_MINI_MODEL = "seedance-2.0-mini"
LEONARDO_MODEL_ALIASES: Dict[str, str] = {
    "seedance-2": LEONARDO_SEEDANCE_MODEL,
    "seedance-2.0": LEONARDO_SEEDANCE_MODEL,
    "leonardo-seedance-2": LEONARDO_SEEDANCE_MODEL,
    "leonardo-seedance-2.0": LEONARDO_SEEDANCE_MODEL,
    "seedance-2-leonardo": LEONARDO_SEEDANCE_MODEL,
    "seedance-2.0-leonardo": LEONARDO_SEEDANCE_MODEL,
    "seedance-2-fast": LEONARDO_SEEDANCE_FAST_MODEL,
    "seedance-2.0-fast": LEONARDO_SEEDANCE_FAST_MODEL,
    "leonardo-seedance-2-fast": LEONARDO_SEEDANCE_FAST_MODEL,
    "leonardo-seedance-2.0-fast": LEONARDO_SEEDANCE_FAST_MODEL,
    "seedance-2-fast-leonardo": LEONARDO_SEEDANCE_FAST_MODEL,
    "seedance-2.0-fast-leonardo": LEONARDO_SEEDANCE_FAST_MODEL,
    "seedance-2-mini": LEONARDO_SEEDANCE_MINI_MODEL,
    "seedance-2.0-mini": LEONARDO_SEEDANCE_MINI_MODEL,
    "leonardo-seedance-2-mini": LEONARDO_SEEDANCE_MINI_MODEL,
    "leonardo-seedance-2.0-mini": LEONARDO_SEEDANCE_MINI_MODEL,
    "seedance-2-mini-leonardo": LEONARDO_SEEDANCE_MINI_MODEL,
    "seedance-2.0-mini-leonardo": LEONARDO_SEEDANCE_MINI_MODEL,
}
LEONARDO_PUBLIC_MODEL_ALIASES: Dict[str, str] = {
    k: v
    for k, v in LEONARDO_MODEL_ALIASES.items()
    if k.startswith("leonardo-") or k.endswith("-leonardo")
}

_LEONARDO_AUTH_CACHE: Dict[str, Tuple[float, Dict[str, str]]] = {}
_VIDEO_URL_KEY_HINTS = {
    "motionmp4url",
    "motionmp4",
    "motion_mp4_url",
    "videourl",
    "video_url",
}
_LEONARDO_UPLOAD_TYPE_INIT = "INIT"
_LEONARDO_INIT_IMAGE_TYPE_UPLOADED = "UPLOADED"
_LEONARDO_INIT_IMAGE_TYPE_GENERATED = "GENERATED"
_LEONARDO_IMAGE_REFERENCE_MAX_COUNT = 6
_LEONARDO_VIDEO_REFERENCE_MAX_COUNT = 1
_LEONARDO_AUDIO_REFERENCE_MAX_COUNT = 1
_LEONARDO_MAX_IMAGE_DOWNLOAD_BYTES = 30 * 1024 * 1024
_LEONARDO_MAX_VIDEO_DOWNLOAD_BYTES = 200 * 1024 * 1024
_LEONARDO_MAX_AUDIO_DOWNLOAD_BYTES = 15 * 1024 * 1024
_LEONARDO_DEFAULT_REFERENCE_STRENGTH = "MID"
_LEONARDO_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
_LEONARDO_VIDEO_EXTENSIONS = {"mp4", "mov"}
_LEONARDO_AUDIO_EXTENSIONS = {"mp3", "wav"}
_LEONARDO_RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

LEONARDO_USER_TOKENS_QUERY = """query GetUserTokensFromSub {
  user_details {
    plan
    tokenRenewalDate
    paidTokens
    subscriptionTokens
    rolloverTokens
    subscriptionGptTokens
    subscriptionModelTokens
  }
}"""


def _one_str(value: Any) -> str:
    return str(value or "").strip()


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _bool_from_payload(value: Any, *, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(value)


def _int_from_payload(value: Any, *, default: int, minimum: Optional[int] = None) -> int:
    try:
        out = int(float(str(value).strip()))
    except Exception:
        out = int(default)
    if minimum is not None:
        out = max(int(minimum), out)
    return out


def _int_from_leonardo_value(value: Any, *, default: int = 0) -> int:
    if value is None:
        return int(default)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value or "").strip().replace(",", "")
    if not s:
        return int(default)
    try:
        return int(float(s))
    except Exception:
        return int(default)


def _leonardo_token_balance_from_graphql(obj: Dict[str, Any]) -> Dict[str, Any]:
    data = obj.get("data") if isinstance(obj, dict) else {}
    details = (data or {}).get("user_details") if isinstance(data, dict) else None
    detail = details[0] if isinstance(details, list) and details and isinstance(details[0], dict) else {}
    subscription_tokens = _int_from_leonardo_value(detail.get("subscriptionTokens"))
    rollover_tokens = _int_from_leonardo_value(detail.get("rolloverTokens"))
    paid_tokens = _int_from_leonardo_value(detail.get("paidTokens"))
    total_tokens = max(0, subscription_tokens + rollover_tokens + paid_tokens)
    return {
        "remaining_quota": total_tokens,
        "subscription_tokens": subscription_tokens,
        "rollover_tokens": rollover_tokens,
        "paid_tokens": paid_tokens,
        "plan": _one_str(detail.get("plan")),
        "token_renewal_date": _one_str(detail.get("tokenRenewalDate")),
        "subscription_gpt_tokens": _int_from_leonardo_value(detail.get("subscriptionGptTokens")),
        "subscription_model_tokens": _int_from_leonardo_value(detail.get("subscriptionModelTokens")),
        "raw": detail,
    }


def _normalize_aspect_ratio(payload: Dict[str, Any]) -> str:
    raw = _one_str(
        payload.get("aspect_ratio")
        or payload.get("aspectRatio")
        or payload.get("ratio")
        or payload.get("size_ratio")
    )
    if not raw:
        return "1:1"
    s = raw.lower().replace(" ", "")
    aliases = {
        "landscape": "16:9",
        "horizontal": "16:9",
        "portrait": "9:16",
        "vertical": "9:16",
        "square": "1:1",
    }
    return aliases.get(s, raw)


def _normalize_resolution(payload: Dict[str, Any]) -> str:
    raw = _one_str(
        payload.get("resolution")
        or payload.get("size")
        or payload.get("mode")
        or payload.get("quality")
    )
    if not raw:
        return "RESOLUTION_720"
    s = raw.upper().replace(" ", "").replace("-", "_")
    if s in {"720", "720P", "HD", "RESOLUTION720"}:
        return "RESOLUTION_720"
    if s in {"480", "480P", "SD", "STANDARD", "RESOLUTION480"}:
        return "RESOLUTION_480"
    if s in {"1080", "1080P", "FULLHD", "FHD", "RESOLUTION1080"}:
        return "RESOLUTION_1080"
    if s.startswith("RESOLUTION_"):
        return s
    return "RESOLUTION_720"


def _parse_size_dimensions(value: Any) -> Optional[Tuple[int, int]]:
    s = _one_str(value)
    if not s:
        return None
    match = re.search(r"(\d{2,5})\s*x\s*(\d{2,5})", s, re.IGNORECASE)
    if not match:
        return None
    width = _int_from_payload(match.group(1), default=0, minimum=0)
    height = _int_from_payload(match.group(2), default=0, minimum=0)
    if width <= 0 or height <= 0:
        return None
    return width, height


def _aspect_ratio_from_dimensions(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "1:1"
    known = (
        ("1:1", 1, 1),
        ("16:9", 16, 9),
        ("9:16", 9, 16),
        ("4:3", 4, 3),
        ("3:4", 3, 4),
    )
    ratio = float(width) / float(height)
    return min(known, key=lambda item: abs(ratio - (item[1] / item[2])))[0]


def _dimensions_from_payload(payload: Dict[str, Any]) -> Tuple[int, int, str, str]:
    width = _int_from_payload(payload.get("width"), default=0, minimum=0)
    height = _int_from_payload(payload.get("height"), default=0, minimum=0)
    aspect = _normalize_aspect_ratio(payload)
    resolution = _normalize_resolution(payload)
    if width > 0 and height > 0:
        return width, height, aspect, resolution
    size_dims = _parse_size_dimensions(payload.get("size") or payload.get("dimensions"))
    if size_dims is not None:
        width, height = size_dims
        if not _one_str(payload.get("aspect_ratio") or payload.get("aspectRatio") or payload.get("ratio") or payload.get("size_ratio")):
            aspect = _aspect_ratio_from_dimensions(width, height)
        return width, height, aspect, resolution

    by_resolution: Dict[str, Dict[str, Tuple[int, int]]] = {
        "RESOLUTION_480": {
            "16:9": (864, 496),
            "9:16": (496, 864),
            "1:1": (496, 496),
            "4:3": (640, 480),
            "3:4": (480, 640),
        },
        "RESOLUTION_720": {
            "16:9": (1280, 720),
            "9:16": (720, 1280),
            "1:1": (720, 720),
            "4:3": (960, 720),
            "3:4": (720, 960),
        },
        "RESOLUTION_1080": {
            "16:9": (1920, 1080),
            "9:16": (1080, 1920),
            "1:1": (1080, 1080),
            "4:3": (1440, 1080),
            "3:4": (1080, 1440),
        },
    }
    table = by_resolution.get(resolution) or by_resolution["RESOLUTION_720"]
    dims = table.get(aspect)
    if dims is None:
        dims = table["16:9"]
    return dims[0], dims[1], aspect, resolution


def _resolve_model(payload: Dict[str, Any]) -> str:
    raw = _one_str(payload.get("leonardo_model") or payload.get("model") or LEONARDO_SEEDANCE_FAST_MODEL)
    key = raw.lower()
    return LEONARDO_MODEL_ALIASES.get(key, raw or LEONARDO_SEEDANCE_FAST_MODEL)


def _prompt_from_payload(payload: Dict[str, Any]) -> str:
    prompt = _one_str(payload.get("prompt"))
    if prompt:
        return prompt
    for key in ("parameters", "leonardo_parameters"):
        extra = payload.get(key)
        if isinstance(extra, dict):
            prompt = _one_str(extra.get("prompt"))
            if prompt:
                return prompt
    return ""


def leonardo_target_url_for_model(model: str, base_url: Optional[str] = None) -> str:
    target = _one_str(base_url) or DEFAULT_LEONARDO_TARGET
    model_id = _resolve_model({"model": model})
    try:
        parsed = urlparse(target)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["model"] = model_id
        return urlunparse(parsed._replace(query=urlencode(query)))
    except Exception:
        return f"https://app.leonardo.ai/generate?model={model_id}"


def _build_generate_parameters(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    width, height, aspect_ratio, resolution = _dimensions_from_payload(payload)
    duration = _int_from_payload(payload.get("duration") or payload.get("seconds"), default=8, minimum=1)
    quantity = _int_from_payload(payload.get("quantity") or payload.get("n") or payload.get("num_videos"), default=1, minimum=1)
    quantity = max(1, min(quantity, 4))

    seed = payload.get("seed")
    seed_value = _int_from_payload(seed, default=-1) if seed is not None else -1
    prompt = _prompt_from_payload(payload)
    params: Dict[str, Any] = {
        "height": height,
        "width": width,
        "duration": duration,
        "motion_has_audio": _bool_from_payload(
            payload.get("motion_has_audio")
            if "motion_has_audio" in payload
            else payload.get("audio", payload.get("with_audio")),
            default=True,
        ),
        "quantity": quantity,
        "prompt": prompt,
        "seed": seed_value,
    }
    negative = _one_str(payload.get("negative_prompt") or payload.get("negativePrompt"))
    if negative:
        params["negative_prompt"] = negative

    for key in ("parameters", "leonardo_parameters"):
        extra = payload.get(key)
        if isinstance(extra, dict):
            params.update(extra)

    meta = {
        "width": int(params.get("width") or width),
        "height": int(params.get("height") or height),
        "duration": int(params.get("duration") or duration),
        "quantity": int(params.get("quantity") or quantity),
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "motion_has_audio": bool(params.get("motion_has_audio")),
    }
    return params, meta


def _graphql_error_message(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    errors = obj.get("errors")
    if not isinstance(errors, list) or not errors:
        return ""
    parts: List[str] = []
    for item in errors[:3]:
        if isinstance(item, dict):
            parts.append(_one_str(item.get("message")) or _compact_json(item))
        else:
            parts.append(_one_str(item))
    return "; ".join([p for p in parts if p])


def _leonardo_url_block_reason(url: str) -> str:
    s = _one_str(url)
    if not s:
        return ""
    lower = s.lower()
    try:
        parsed = urlparse(s)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
    except Exception:
        host = ""
        path = ""
    if "challenges.cloudflare.com" in host or "/cdn-cgi/" in lower:
        return "cloudflare_challenge"
    if host == "app.leonardo.ai" and (
        path.startswith("/auth/login")
        or path.startswith("/auth/signin")
        or path.startswith("/auth/error")
    ):
        return "login_required"
    return ""


def _leonardo_block_status(reason: str) -> int:
    return 503 if reason == "cloudflare_challenge" else 401


def _leonardo_block_message(reason: str, *, stage: str, page_url: str = "") -> str:
    if reason == "cloudflare_challenge":
        return (
            "Leonardo page is showing Cloudflare verification; "
            f"manual verification is required before continuing. stage={stage} url={safe_trim(page_url, 180)!r}"
        )
    if reason == "login_required":
        return (
            "Leonardo account is logged out; please log in in the fingerprint browser first. "
            f"stage={stage} url={safe_trim(page_url, 180)!r}"
        )
    return f"Leonardo page is not usable. stage={stage} reason={reason or 'unknown'} url={safe_trim(page_url, 180)!r}"


async def _leonardo_page_block_reason(page: Any, *, deep: bool = False) -> str:
    if page is None:
        return ""
    try:
        if page.is_closed():
            return "page_closed"
    except Exception:
        pass
    try:
        url = _one_str(getattr(page, "url", ""))
    except Exception:
        url = ""
    reason = _leonardo_url_block_reason(url)
    if reason:
        return reason
    try:
        title = _one_str(await page.title()).lower()
    except Exception:
        title = ""
    if title:
        if (
            "just a moment" in title
            or "attention required" in title
            or "verify you are human" in title
            or "cloudflare" in title
        ):
            return "cloudflare_challenge"
        if ("sign in" in title or "log in" in title or "login" in title) and "leonardo" in title:
            return "login_required"
    if not deep:
        return ""
    try:
        html = (await page.content() or "").lower()
    except Exception:
        html = ""
    if not html:
        return ""
    if (
        "verify you are human" in html
        or "cf-challenge" in html
        or "cf-ray" in html
        or "challenges.cloudflare.com" in html
        or ("cloudflare" in html and ("just a moment" in html or "/cdn-cgi/" in html))
        or ("turnstile" in html and ("cloudflare" in html or "/cdn-cgi/" in html))
    ):
        return "cloudflare_challenge"
    if (
        "auth/login" in html
        or "auth/signin" in html
        or ("sign in" in html and "leonardo" in html)
        or ("log in" in html and "leonardo" in html)
    ):
        return "login_required"
    return ""


async def _raise_if_leonardo_page_blocked(page: Any, *, stage: str) -> None:
    reason = await _leonardo_page_block_reason(page, deep=True)
    if not reason:
        return
    try:
        page_url = _one_str(getattr(page, "url", ""))
    except Exception:
        page_url = ""
    raise NonPenalizedTaskError(
        _leonardo_block_message(reason, stage=stage, page_url=page_url),
        status_code=_leonardo_block_status(reason),
    )


async def _find_or_open_leonardo_page(
    pw_ctx: Any,
    target_url: str,
    *,
    log_file: Path,
    allow_new_page: bool = True,
    navigate_if_needed: bool = True,
    bring_to_front: bool = True,
) -> Optional[Any]:
    context = getattr(pw_ctx, "context", None)
    if context is None:
        raise NonPenalizedTaskError("Leonardo browser context is not initialized", status_code=502)
    pages = list(getattr(context, "pages", []) or [])
    target_page = None
    blocked_page = None
    for page in pages:
        try:
            if page.is_closed():
                continue
            url = _one_str(getattr(page, "url", ""))
        except Exception:
            continue
        if "app.leonardo.ai" in url:
            target_page = page
            break
        if blocked_page is None and _leonardo_url_block_reason(url):
            blocked_page = page
    if target_page is None and blocked_page is not None:
        target_page = blocked_page
    if target_page is None:
        if not allow_new_page:
            return None
        target_page = await context.new_page()
    if bring_to_front:
        try:
            await target_page.bring_to_front()
        except Exception:
            pass
    cur = _one_str(getattr(target_page, "url", ""))
    blocked_reason = _leonardo_url_block_reason(cur)
    if blocked_reason:
        append_log(
            log_file,
            f"[leonardo] reuse blocked page without navigation: reason={blocked_reason} url={safe_trim(cur, 180)!r}",
        )
        pw_ctx.page = target_page
        return target_page
    if "app.leonardo.ai" not in cur:
        if not navigate_if_needed:
            return None
        append_log(log_file, f"[leonardo] goto target={safe_trim(target_url, 120)!r}")
        await target_page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
    pw_ctx.page = target_page
    return target_page


async def _capture_graphql_headers(
    page: Any,
    *,
    target_url: str,
    cache_key: str,
    log_file: Path,
    timeout_seconds: float = 25.0,
    cache_seconds: float = 0.0,
    navigate: bool = True,
) -> Dict[str, str]:
    now = time.time()
    cached = _LEONARDO_AUTH_CACHE.get(cache_key)
    await _raise_if_leonardo_page_blocked(page, stage="before_auth_capture")
    if cache_seconds > 0 and cached and now < cached[0]:
        return dict(cached[1])

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Dict[str, str]] = loop.create_future()

    def on_request(request: Any) -> None:
        try:
            if request.url != LEONARDO_GRAPHQL_URL:
                return
            headers = {str(k).lower(): str(v) for k, v in (request.headers or {}).items()}
            auth = _one_str(headers.get("authorization"))
            if not auth:
                return
            out = {
                "Authorization": auth,
                "X-Leo-Schema-Version": _one_str(headers.get("x-leo-schema-version")) or LEONARDO_SCHEMA_VERSION,
            }
            if not fut.done():
                fut.set_result(out)
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)

    page.on("request", on_request)
    try:
        if navigate:
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                append_log(log_file, f"[leonardo] auth capture goto failed, continuing: {safe_trim(str(exc), 240)}")
            await _raise_if_leonardo_page_blocked(page, stage="after_auth_capture_goto")
        headers = await asyncio.wait_for(fut, timeout=max(3.0, float(timeout_seconds)))
        if cache_seconds > 0:
            _LEONARDO_AUTH_CACHE[cache_key] = (time.time() + float(cache_seconds), dict(headers))
        append_log(log_file, "[leonardo] captured GraphQL authorization header")
        return headers
    except asyncio.TimeoutError as exc:
        raise NonPenalizedTaskError(
            "Leonardo auth header capture timed out. Make sure the fingerprint window is logged in to app.leonardo.ai.",
            status_code=401,
        ) from exc
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass


async def _leonardo_session_ping(page: Any, *, log_file: Path) -> Dict[str, Any]:
    await _raise_if_leonardo_page_blocked(page, stage="before_session_ping")
    tx = await page_fetch_tx(
        page,
        url=LEONARDO_SESSION_PING_PATH,
        method="GET",
        headers={"Accept": "*/*"},
        json_data=None,
        log_file=log_file,
    )
    status = int(tx.get("status") or 0)
    if status in {200, 204} or (200 <= status < 300):
        return {"ok": True, "status": status}
    if status in {401, 403}:
        raise NonPenalizedTaskError(
            f"Leonardo session ping unauthorized: status={status}",
            status_code=401,
        )
    raise NonPenalizedTaskError(
        f"Leonardo session ping failed: status={status} body={safe_trim(str(tx.get('response_body') or ''), 240)}",
        status_code=502,
    )


async def leonardo_keepalive(
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    target_url: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
    auth_cache_seconds: float = LEONARDO_DEFAULT_AUTH_CACHE_SECONDS,
    auth_capture_timeout_seconds: float = 20.0,
    probe_graphql: bool = True,
    log_file: Path = MONITOR_LOG_FILE,
) -> Dict[str, Any]:
    """Warm the Leonardo page/session without submitting a generation."""
    sess = get_or_create_playwright_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    target = _one_str(target_url) or DEFAULT_LEONARDO_TARGET
    async with sess.driver_lock:
        try:
            conn = await sess.fp_client.get_open_window_connection_info(
                vendor=browser_vendor,
                base_url=browser_base_url,
                access_key=browser_access_key,
                window_key=window_key,
            )
        except Exception as exc:
            append_log(log_file, f"[leonardo] keepalive connection check failed: {safe_trim(str(exc), 200)}")
            conn = None
        if not conn:
            append_log(log_file, "[leonardo] keepalive skipped: fingerprint window is not open")
            return {
                "ok": False,
                "reason": "window_not_open",
                "target_url": target,
                "auth_cached": False,
                "auth_cache_seconds": float(auth_cache_seconds),
            }
        await sess.ensure_open(
            args=[],
            force_open=False,
            headless=headless,
            require_page=False,
            pure_mode=pure_mode,
        )
        page = await _find_or_open_leonardo_page(
            sess,
            target,
            log_file=log_file,
            allow_new_page=False,
            navigate_if_needed=False,
            bring_to_front=False,
        )
        if page is None:
            append_log(log_file, "[leonardo] keepalive skipped: no existing Leonardo app page")
            return {
                "ok": False,
                "reason": "no_leonardo_page",
                "target_url": target,
                "auth_cached": False,
                "auth_cache_seconds": float(auth_cache_seconds),
            }
        blocked_reason = await _leonardo_page_block_reason(page, deep=True)
        if blocked_reason:
            page_url = _one_str(getattr(page, "url", ""))
            append_log(
                log_file,
                f"[leonardo] keepalive skipped: reason={blocked_reason} url={safe_trim(page_url, 180)!r}",
            )
            return {
                "ok": False,
                "reason": blocked_reason,
                "target_url": target,
                "page_url": page_url,
                "auth_cached": False,
                "auth_cache_seconds": float(auth_cache_seconds),
            }
        try:
            ping_info = await _leonardo_session_ping(page, log_file=log_file)
        except NonPenalizedTaskError as exc:
            status = getattr(exc, "status_code", None)
            reason = "login_required" if status == 401 else "session_ping_failed"
            append_log(
                log_file,
                f"[leonardo] keepalive session ping skipped: reason={reason} err={safe_trim(str(exc), 240)}",
            )
            return {
                "ok": False,
                "reason": reason,
                "target_url": target,
                "page_url": _one_str(getattr(page, "url", "")),
                "auth_cached": False,
                "auth_cache_seconds": float(auth_cache_seconds),
            }
        except Exception as exc:
            append_log(log_file, f"[leonardo] keepalive session ping skipped: {safe_trim(str(exc), 240)}")
            return {
                "ok": False,
                "reason": "session_ping_failed",
                "target_url": target,
                "page_url": _one_str(getattr(page, "url", "")),
                "auth_cached": False,
                "auth_cache_seconds": float(auth_cache_seconds),
            }
        headers: Dict[str, str] = {}
        try:
            headers = await _capture_graphql_headers(
                page,
                target_url=target,
                cache_key=sess.cache_key,
                log_file=log_file,
                timeout_seconds=auth_capture_timeout_seconds,
                cache_seconds=auth_cache_seconds,
                navigate=False,
            )
        except NonPenalizedTaskError as exc:
            append_log(log_file, f"[leonardo] keepalive auth capture skipped: {safe_trim(str(exc), 240)}")
        balance_info: Dict[str, Any] = {}
        if probe_graphql and headers.get("Authorization"):
            try:
                obj = await _leonardo_graphql(
                    page,
                    auth_headers=headers,
                    operation_name="GetUserTokensFromSub",
                    query=LEONARDO_USER_TOKENS_QUERY,
                    variables={},
                    log_file=log_file,
                )
                balance_info = _leonardo_token_balance_from_graphql(obj)
            except NonPenalizedTaskError as exc:
                status = getattr(exc, "status_code", None)
                reason = "login_required" if status == 401 else "graphql_probe_failed"
                append_log(
                    log_file,
                    f"[leonardo] keepalive graphql probe skipped: reason={reason} err={safe_trim(str(exc), 240)}",
                )
                return {
                    "ok": False,
                    "reason": reason,
                    "target_url": target,
                    "page_url": _one_str(getattr(page, "url", "")),
                    "auth_cached": bool(headers.get("Authorization")),
                    "auth_cache_seconds": float(auth_cache_seconds),
                }
            except Exception as exc:
                append_log(log_file, f"[leonardo] keepalive graphql probe skipped: {safe_trim(str(exc), 240)}")
                return {
                    "ok": False,
                    "reason": "graphql_probe_failed",
                    "target_url": target,
                    "page_url": _one_str(getattr(page, "url", "")),
                    "auth_cached": bool(headers.get("Authorization")),
                    "auth_cache_seconds": float(auth_cache_seconds),
                }
    return {
        "ok": True,
        "target_url": target,
        "page_url": _one_str(getattr(page, "url", "")),
        "session_ping": ping_info,
        "auth_cached": bool(headers.get("Authorization")),
        "auth_cache_seconds": float(auth_cache_seconds),
        **({"balance": balance_info} if balance_info else {}),
    }


async def leonardo_fetch_token_balance(
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    target_url: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
    auth_cache_seconds: float = LEONARDO_DEFAULT_AUTH_CACHE_SECONDS,
    auth_capture_timeout_seconds: float = 25.0,
    log_file: Path = MONITOR_LOG_FILE,
) -> Dict[str, Any]:
    """Read Leonardo token balance from the logged-in app session."""
    sess = get_or_create_playwright_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    target = _one_str(target_url) or DEFAULT_LEONARDO_TARGET
    async with sess.driver_lock:
        await sess.ensure_open(
            args=[target],
            force_open=False,
            headless=headless,
            require_page=False,
            pure_mode=pure_mode,
        )
        page = await _find_or_open_leonardo_page(sess, target, log_file=log_file)
        if page is None:
            raise NonPenalizedTaskError(
                "Leonardo page is not open; please open app.leonardo.ai in the fingerprint browser first.",
                status_code=401,
            )
        auth_headers = await _capture_graphql_headers(
            page,
            target_url=target,
            cache_key=sess.cache_key,
            log_file=log_file,
            timeout_seconds=auth_capture_timeout_seconds,
            cache_seconds=auth_cache_seconds,
        )
        obj = await _leonardo_graphql(
            page,
            auth_headers=auth_headers,
            operation_name="GetUserTokensFromSub",
            query=LEONARDO_USER_TOKENS_QUERY,
            variables={},
            log_file=log_file,
        )

    return _leonardo_token_balance_from_graphql(obj)


def _graphql_headers(auth_headers: Dict[str, str]) -> Dict[str, str]:
    out = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "X-Leo-Schema-Version": _one_str(auth_headers.get("X-Leo-Schema-Version")) or LEONARDO_SCHEMA_VERSION,
    }
    auth = _one_str(auth_headers.get("Authorization"))
    if auth:
        out["Authorization"] = auth
    return out


async def _leonardo_graphql(
    page: Any,
    *,
    auth_headers: Dict[str, str],
    operation_name: str,
    query: str,
    variables: Dict[str, Any],
    log_file: Path,
) -> Dict[str, Any]:
    tx = await page_fetch_json(
        page,
        url=LEONARDO_GRAPHQL_URL,
        method="POST",
        headers=_graphql_headers(auth_headers),
        json_data={
            "operationName": operation_name,
            "variables": variables,
            "query": query,
        },
        log_file=log_file,
    )
    obj = tx.get("_json")
    if not isinstance(obj, dict):
        raise NonPenalizedTaskError(f"Leonardo {operation_name} returned non-object JSON", status_code=502)
    msg = _graphql_error_message(obj)
    if msg:
        status = 401 if "unauthorized" in msg.lower() or "jwt" in msg.lower() else 502
        raise NonPenalizedTaskError(f"Leonardo {operation_name} failed: {safe_trim(msg, 700)}", status_code=status)
    return obj


UPLOAD_IMAGE_MUTATION = """mutation UploadImage($uploadImageInput: UploadImageInput!) {
  uploadImage(arg1: $uploadImageInput) {
    uploadId
    url
    fields
    __typename
  }
}"""


UPLOADED_IMAGE_QUERY = """query GetUploadedImageById($uploadId: uuid!) {
  init_images_by_pk(id: $uploadId) {
    id
    url
    imageWidth: image_width
    imageHeight: image_height
    createdAt
  }
}"""


S3_UPLOAD_MODERATION_MUTATION = """mutation S3UploadModeration($datasetImageIds: [String]) {
  s3UploadModeration(datasetImageIds: $datasetImageIds) {
    id
    moderation_result
  }
}"""


INIT_IMAGE_MODERATION_QUERY = """query GetInitImageModeration($akUUID: uuid!) {
  init_image_moderation(where: {akUUID: {_eq: $akUUID}}) {
    akUUID
    initImageId
    checkStatus
    init_image {
      id
      url
      imageWidth: image_width
      imageHeight: image_height
    }
  }
}"""


INIT_IMAGE_SOURCE_QUERY = """query GetInitImageSource($initImageId: uuid!) {
  init_images_by_pk(id: $initImageId) {
    id
    url
    imageWidth: image_width
    imageHeight: image_height
    createdAt
  }
}"""


UPLOADED_MEDIA_QUERY = """query GetUploadedMediaById($uploadId: uuid!) {
  uploaded_media(where: {id: {_eq: $uploadId}}, limit: 1) {
    id
    url
    thumbnailUrl
    width
    height
    duration
    fileSize
    status
    statusReason
    video_fps
    videoCodec
    createdAt
  }
}"""


GENERATE_MUTATION = """mutation Generate($request: CreateGenerationRequest!) {
  generate(request: $request) {
    apiCreditCost
    generationId
    __typename
  }
}"""


GENERATION_QUERY = """query LeonardoGenerationById($id: uuid!) {
  generations_by_pk(id: $id) {
    id
    status
    prompt
    imageWidth
    imageHeight
    motionModel
    motionDurationSeconds
    motionGenerationResolution
    motionFrameInterpolation
    motionHasAudio
    modelId
    generated_images {
      id
      url
      assetURL
      motionMP4URL
      motionGIFURL
      image_width
      image_height
      generated_image_variation_motion {
        akUUID
        id
        status
        generatedImageId
        mediaHeight
        mediaWidth
        motionTransformType
        resolution
        url
      }
    }
  }
}"""


def _split_string_list(value: str) -> List[str]:
    s = _one_str(value)
    if not s:
        return []
    if s.lower().startswith("data:"):
        return [s]
    parts = re.split(r"[\r\n,]+", s)
    out = [_one_str(part) for part in parts]
    return [part for part in out if part]


def _list_strs(value: Any, *, dict_keys: Tuple[str, ...] = ("url", "image_url", "imageUrl", "src")) -> List[str]:
    out: List[str] = []

    def add(item: Any) -> None:
        if isinstance(item, str):
            for part in _split_string_list(item):
                if part and part not in out:
                    out.append(part)
        elif isinstance(item, (list, tuple, set)):
            for sub in item:
                add(sub)
        elif isinstance(item, dict):
            for key in dict_keys:
                if key in item:
                    add(item.get(key))
                    break

    add(value)
    return out


def _first_present_str(payload: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = _one_str(payload.get(key))
        if value:
            return value
    return ""


def _list_field_strs(
    payload: Dict[str, Any],
    keys: Tuple[str, ...],
    *,
    dict_keys: Tuple[str, ...] = ("url", "image_url", "imageUrl", "src"),
) -> List[str]:
    out: List[str] = []
    for key in keys:
        for item in _list_strs(payload.get(key), dict_keys=dict_keys):
            if item not in out:
                out.append(item)
    return out


def _normalize_uploaded_ref_type(value: Any, *, default: str = _LEONARDO_INIT_IMAGE_TYPE_UPLOADED) -> str:
    s = _one_str(value).upper().replace("-", "_").replace(" ", "_")
    if s in {_LEONARDO_INIT_IMAGE_TYPE_UPLOADED, _LEONARDO_INIT_IMAGE_TYPE_GENERATED}:
        return s
    return default


def _content_type_base(headers: Dict[str, str]) -> str:
    for key, value in (headers or {}).items():
        if str(key or "").lower() == "content-type":
            return _one_str(value).split(";", 1)[0].strip().lower()
    return ""


def _extension_from_mime(content_type: str) -> str:
    ct = _one_str(content_type).split(";", 1)[0].strip().lower()
    if not ct:
        return ""
    aliases = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "video/mp4": "mp4",
        "video/webm": "webm",
        "video/quicktime": "mov",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/wave": "wav",
        "audio/x-pn-wav": "wav",
    }
    if ct in aliases:
        return aliases[ct]
    guessed = mimetypes.guess_extension(ct) or ""
    guessed = guessed.lstrip(".").lower()
    if guessed == "jpe":
        return "jpg"
    return guessed


def _extension_from_source(source: str) -> str:
    s = _one_str(source)
    if not s or s.lower().startswith("data:"):
        return ""
    try:
        path = unquote(urlparse(s).path or "")
    except Exception:
        path = s
    name = Path(path).name
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def _extension_list_label(extensions: set[str]) -> str:
    return ", ".join(f".{ext}" for ext in sorted(extensions))


def _guess_filename_and_extension(
    source: str,
    *,
    content_type: str,
    media_kind: str,
) -> Tuple[str, str, str]:
    if media_kind == "image":
        allowed = _LEONARDO_IMAGE_EXTENSIONS
        fallback_ext = "jpg"
        fallback_mime = "image/jpeg"
    elif media_kind == "audio":
        allowed = _LEONARDO_AUDIO_EXTENSIONS
        fallback_ext = "mp3"
        fallback_mime = "audio/mpeg"
    else:
        allowed = _LEONARDO_VIDEO_EXTENSIONS
        fallback_ext = "mp4"
        fallback_mime = "video/mp4"

    source_ext = _extension_from_source(source)
    mime = _one_str(content_type).split(";", 1)[0].strip().lower()
    mime_ext = _extension_from_mime(mime)
    if source_ext in allowed:
        ext = source_ext
    elif mime_ext in allowed:
        ext = mime_ext
    else:
        unsupported_ext = source_ext or (mime_ext if mime and mime != "application/octet-stream" else "")
        if unsupported_ext:
            raise NonPenalizedTaskError(
                f"Leonardo {media_kind} reference supports only {_extension_list_label(allowed)} files",
                status_code=400,
            )
        ext = fallback_ext

    try:
        raw_name = Path(unquote(urlparse(source).path or "")).name
    except Exception:
        raw_name = ""
    if not raw_name or "." not in raw_name:
        raw_name = f"leonardo-reference-{int(time.time())}.{ext}"
    raw_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._") or f"leonardo-reference.{ext}"
    if "." not in raw_name:
        raw_name = f"{raw_name}.{ext}"
    elif raw_name.rsplit(".", 1)[-1].lower() not in allowed:
        raw_name = f"{raw_name.rsplit('.', 1)[0]}.{ext}"

    if not mime or mime == "application/octet-stream":
        mime = mimetypes.guess_type(raw_name)[0] or fallback_mime
    return raw_name, ext, mime


def _decode_data_url(value: str, *, max_bytes: int) -> Optional[Tuple[bytes, Dict[str, str]]]:
    s = _one_str(value)
    if not s.lower().startswith("data:"):
        return None
    header, sep, raw_data = s.partition(",")
    if not sep:
        raise NonPenalizedTaskError("Leonardo reference data URL is invalid", status_code=400)
    content_type = "application/octet-stream"
    meta = header[5:]
    if meta:
        first = meta.split(";", 1)[0].strip()
        if first:
            content_type = first
    try:
        if ";base64" in header.lower():
            data = base64.b64decode(raw_data, validate=True)
        else:
            data = unquote_to_bytes(raw_data)
    except Exception as exc:
        raise NonPenalizedTaskError("Leonardo reference data URL could not be decoded", status_code=400) from exc
    if len(data) > max_bytes:
        limit_mb = max(1, int(max_bytes) // (1024 * 1024))
        raise NonPenalizedTaskError(f"Leonardo reference file exceeds {limit_mb}MB limit", status_code=400)
    return data, {"content-type": content_type}


async def _download_remote_asset(
    source: str,
    *,
    media_kind: str,
    max_bytes: int,
    log_file: Path,
) -> Tuple[bytes, Dict[str, str]]:
    data_url = _decode_data_url(source, max_bytes=max_bytes)
    if data_url is not None:
        append_log(log_file, f"[leonardo-upload] decoded data URL kind={media_kind} bytes={len(data_url[0])}")
        return data_url

    url = _one_str(source)
    if not url.startswith(("http://", "https://")):
        raise NonPenalizedTaskError("Leonardo reference asset must be an http(s) URL or data URL", status_code=400)
    if media_kind == "image":
        accept = "image/*,*/*"
    elif media_kind == "audio":
        accept = "audio/*,*/*"
    else:
        accept = "video/*,*/*"
    timeout = httpx.Timeout(connect=20.0, read=180.0, write=30.0, pool=20.0)
    headers = {"Accept": accept, "User-Agent": "Mozilla/5.0"}
    out = b""
    hdrs: Dict[str, str] = {}
    last_exc: Optional[Exception] = None
    for attempt in range(1, 5):
        chunks: List[bytes] = []
        total = 0
        try:
            async with httpx.AsyncClient(follow_redirects=True, trust_env=True, timeout=timeout) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code >= 400:
                        body = safe_trim((await resp.aread()).decode("utf-8", errors="replace"), 500)
                        append_log(
                            log_file,
                            f"[leonardo-upload] download status={resp.status_code} attempt={attempt}/4 kind={media_kind} url={safe_trim(url, 180)!r} body={body!r}",
                        )
                        if resp.status_code in _LEONARDO_RETRYABLE_HTTP_STATUS_CODES and attempt < 4:
                            await asyncio.sleep(min(12.0, 2.0 * attempt))
                            continue
                        raise NonPenalizedTaskError(
                            f"Leonardo reference asset download failed: HTTP {resp.status_code} url={safe_trim(url, 300)!r}",
                            status_code=502 if resp.status_code in _LEONARDO_RETRYABLE_HTTP_STATUS_CODES else 400,
                        )
                    cl = resp.headers.get("content-length")
                    if cl:
                        try:
                            if int(cl) > int(max_bytes):
                                limit_mb = max(1, int(max_bytes) // (1024 * 1024))
                                raise NonPenalizedTaskError(
                                    f"Leonardo reference asset exceeds {limit_mb}MB limit: Content-Length={cl}",
                                    status_code=400,
                                )
                        except NonPenalizedTaskError:
                            raise
                        except Exception:
                            pass
                    async for chunk in resp.aiter_bytes(1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > int(max_bytes):
                            limit_mb = max(1, int(max_bytes) // (1024 * 1024))
                            raise NonPenalizedTaskError(f"Leonardo reference asset exceeds {limit_mb}MB limit", status_code=400)
                        chunks.append(chunk)
                    out = b"".join(chunks)
                    hdrs = {str(k).lower(): str(v) for k, v in resp.headers.items()}
            if out:
                break
            append_log(
                log_file,
                f"[leonardo-upload] download empty attempt={attempt}/4 kind={media_kind} url={safe_trim(url, 180)!r}",
            )
            if attempt < 4:
                await asyncio.sleep(min(12.0, 2.0 * attempt))
                continue
            raise NonPenalizedTaskError("Leonardo reference asset download returned empty content", status_code=502)
        except NonPenalizedTaskError:
            raise
        except Exception as exc:
            last_exc = exc
            detail = safe_trim(str(exc) or exc.__class__.__name__, 300)
            append_log(
                log_file,
                f"[leonardo-upload] download exception attempt={attempt}/4 type={exc.__class__.__name__} kind={media_kind} url={safe_trim(url, 180)!r} detail={detail!r}",
            )
            if attempt < 4:
                await asyncio.sleep(min(12.0, 2.0 * attempt))
                continue
            raise NonPenalizedTaskError(
                f"Leonardo reference asset download failed: {exc.__class__.__name__}: {detail}",
                status_code=502,
            ) from exc
    if not out:
        if last_exc is not None:
            detail = safe_trim(str(last_exc) or last_exc.__class__.__name__, 300)
            raise NonPenalizedTaskError(
                f"Leonardo reference asset download failed: {last_exc.__class__.__name__}: {detail}",
                status_code=502,
            ) from last_exc
        raise NonPenalizedTaskError("Leonardo reference asset download returned empty content", status_code=502)
    append_log(log_file, f"[leonardo-upload] downloaded kind={media_kind} bytes={len(out)} url={safe_trim(url, 180)!r}")
    return out, hdrs


async def _post_s3_upload_form(
    *,
    upload_url: str,
    fields: Dict[str, Any],
    filename: str,
    data: bytes,
    content_type: str,
    log_file: Path,
) -> None:
    form_fields = {str(k): str(v) for k, v in (fields or {}).items()}
    timeout = httpx.Timeout(connect=20.0, read=180.0, write=180.0, pool=20.0)
    last_exc: Optional[Exception] = None
    for attempt in range(1, 5):
        try:
            async with httpx.AsyncClient(follow_redirects=True, trust_env=True, timeout=timeout) as client:
                resp = await client.post(
                    upload_url,
                    data=form_fields,
                    files={"file": (filename, data, content_type)},
                )
        except Exception as exc:
            last_exc = exc
            detail = safe_trim(str(exc) or exc.__class__.__name__, 300)
            append_log(
                log_file,
                f"[leonardo-upload] s3 exception attempt={attempt}/4 type={exc.__class__.__name__} filename={safe_trim(filename, 120)!r} detail={detail!r}",
            )
            if attempt < 4:
                await asyncio.sleep(min(12.0, 2.0 * attempt))
                continue
            raise NonPenalizedTaskError(
                f"Leonardo S3 upload failed: {exc.__class__.__name__}: {detail}",
                status_code=502,
            ) from exc
        append_log(log_file, f"[leonardo-upload] s3 status={resp.status_code} attempt={attempt}/4 filename={safe_trim(filename, 120)!r}")
        if 200 <= resp.status_code < 300:
            return
        body = safe_trim(resp.text, 500)
        if resp.status_code in {408, 425, 429, 500, 502, 503, 504} and attempt < 4:
            await asyncio.sleep(min(12.0, 2.0 * attempt))
            continue
        raise NonPenalizedTaskError(
            f"Leonardo S3 upload failed: HTTP {resp.status_code} body={body!r}",
            status_code=502,
        )
    if last_exc is not None:
        detail = safe_trim(str(last_exc) or last_exc.__class__.__name__, 300)
        raise NonPenalizedTaskError(
            f"Leonardo S3 upload failed: {last_exc.__class__.__name__}: {detail}",
            status_code=502,
        ) from last_exc


async def _poll_uploaded_image(
    page: Any,
    *,
    auth_headers: Dict[str, str],
    upload_id: str,
    log_file: Path,
) -> Dict[str, Any]:
    deadline = time.time() + 90.0
    last_obj: Dict[str, Any] = {}
    interval = 2.0
    while time.time() < deadline:
        obj = await _leonardo_graphql(
            page,
            auth_headers=auth_headers,
            operation_name="GetUploadedImageById",
            query=UPLOADED_IMAGE_QUERY,
            variables={"uploadId": upload_id},
            log_file=log_file,
        )
        image = ((obj.get("data") or {}) if isinstance(obj.get("data"), dict) else {}).get("init_images_by_pk")
        if isinstance(image, dict):
            last_obj = image
            if _one_str(image.get("id")):
                return image
        await asyncio.sleep(interval)
        interval = min(8.0, interval + 1.0)
    raise NonPenalizedTaskError(
        f"Leonardo uploaded image was not ready: {safe_trim(_compact_json(last_obj), 600)}",
        status_code=504,
    )


async def _leonardo_run_s3_upload_moderation(
    page: Any,
    *,
    auth_headers: Dict[str, str],
    upload_id: str,
    log_file: Path,
) -> Dict[str, Any]:
    obj = await _leonardo_graphql(
        page,
        auth_headers=auth_headers,
        operation_name="S3UploadModeration",
        query=S3_UPLOAD_MODERATION_MUTATION,
        variables={"datasetImageIds": [upload_id]},
        log_file=log_file,
    )
    result = ((obj.get("data") or {}) if isinstance(obj.get("data"), dict) else {}).get("s3UploadModeration")
    if isinstance(result, list) and result and isinstance(result[0], dict):
        append_log(log_file, f"[leonardo-upload] moderation upload_id={upload_id} result={safe_trim(_compact_json(result[0]), 500)}")
        return result[0]
    if isinstance(result, dict):
        append_log(log_file, f"[leonardo-upload] moderation upload_id={upload_id} result={safe_trim(_compact_json(result), 500)}")
        return result
    append_log(log_file, f"[leonardo-upload] moderation upload_id={upload_id} payload={safe_trim(_compact_json(obj), 500)}")
    return {}


async def _poll_init_image_moderation(
    page: Any,
    *,
    auth_headers: Dict[str, str],
    upload_id: str,
    log_file: Path,
) -> Dict[str, Any]:
    deadline = time.time() + 90.0
    last_record: Dict[str, Any] = {}
    interval = 2.0
    while time.time() < deadline:
        obj = await _leonardo_graphql(
            page,
            auth_headers=auth_headers,
            operation_name="GetInitImageModeration",
            query=INIT_IMAGE_MODERATION_QUERY,
            variables={"akUUID": upload_id},
            log_file=log_file,
        )
        records = ((obj.get("data") or {}) if isinstance(obj.get("data"), dict) else {}).get("init_image_moderation")
        record = records[0] if isinstance(records, list) and records and isinstance(records[0], dict) else None
        if isinstance(record, dict):
            last_record = record
            status = _one_str(record.get("checkStatus"))
            image = record.get("init_image") if isinstance(record.get("init_image"), dict) else None
            if image and _one_str(image.get("id")):
                return image
            init_image_id = _one_str(record.get("initImageId"))
            if init_image_id:
                source_obj = await _leonardo_graphql(
                    page,
                    auth_headers=auth_headers,
                    operation_name="GetInitImageSource",
                    query=INIT_IMAGE_SOURCE_QUERY,
                    variables={"initImageId": init_image_id},
                    log_file=log_file,
                )
                source = ((source_obj.get("data") or {}) if isinstance(source_obj.get("data"), dict) else {}).get("init_images_by_pk")
                if isinstance(source, dict) and _one_str(source.get("id")):
                    return source
            if status.lower() in {"blocked", "failed", "timeout", "time_out", "rejected"}:
                raise NonPenalizedTaskError(
                    f"Leonardo uploaded image moderation failed: {safe_trim(_compact_json(record), 700)}",
                    status_code=400,
                )
        await asyncio.sleep(interval)
        interval = min(8.0, interval + 1.0)
    raise NonPenalizedTaskError(
        f"Leonardo uploaded image moderation was not ready: {safe_trim(_compact_json(last_record), 600)}",
        status_code=504,
    )


async def _finalize_uploaded_image(
    page: Any,
    *,
    auth_headers: Dict[str, str],
    upload_id: str,
    log_file: Path,
) -> Dict[str, Any]:
    try:
        return await _poll_init_image_moderation(page, auth_headers=auth_headers, upload_id=upload_id, log_file=log_file)
    except NonPenalizedTaskError as moderation_exc:
        if getattr(moderation_exc, "status_code", None) == 400:
            raise
        append_log(log_file, f"[leonardo-upload] moderation poll fallback upload_id={upload_id} error={safe_trim(str(moderation_exc), 500)!r}")
        return await _poll_uploaded_image(page, auth_headers=auth_headers, upload_id=upload_id, log_file=log_file)


async def _poll_uploaded_media(
    page: Any,
    *,
    auth_headers: Dict[str, str],
    upload_id: str,
    log_file: Path,
) -> Dict[str, Any]:
    deadline = time.time() + 180.0
    last_media: Dict[str, Any] = {}
    interval = 1.0
    while time.time() < deadline:
        obj = await _leonardo_graphql(
            page,
            auth_headers=auth_headers,
            operation_name="GetUploadedMediaById",
            query=UPLOADED_MEDIA_QUERY,
            variables={"uploadId": upload_id},
            log_file=log_file,
        )
        media_list = ((obj.get("data") or {}) if isinstance(obj.get("data"), dict) else {}).get("uploaded_media")
        media = media_list[0] if isinstance(media_list, list) and media_list and isinstance(media_list[0], dict) else None
        if isinstance(media, dict):
            last_media = media
            status = _one_str(media.get("status")).upper()
            if status == "COMPLETE":
                return media
            if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
                reason = _one_str(media.get("statusReason"))
                raise NonPenalizedTaskError(
                    f"Leonardo uploaded media processing failed: status={status} reason={safe_trim(reason, 300)}",
                    status_code=502,
                )
        await asyncio.sleep(interval)
        interval = min(5.0, interval + 0.5)
    raise NonPenalizedTaskError(
        f"Leonardo uploaded media was not ready: {safe_trim(_compact_json(last_media), 600)}",
        status_code=504,
    )


async def _leonardo_upload_asset(
    page: Any,
    *,
    auth_headers: Dict[str, str],
    source: str,
    media_kind: str,
    log_file: Path,
    progress_cb: ProgressCB,
    index: int,
    team_id: str = "",
) -> Dict[str, Any]:
    if media_kind not in {"image", "video", "audio"}:
        raise ValueError(f"unsupported media_kind={media_kind!r}")
    await progress_cb(4, {"stage": "upload_reference_download", "media_kind": media_kind, "index": index})
    if media_kind == "image":
        max_bytes = _LEONARDO_MAX_IMAGE_DOWNLOAD_BYTES
    elif media_kind == "audio":
        max_bytes = _LEONARDO_MAX_AUDIO_DOWNLOAD_BYTES
    else:
        max_bytes = _LEONARDO_MAX_VIDEO_DOWNLOAD_BYTES
    data, headers = await _download_remote_asset(source, media_kind=media_kind, max_bytes=max_bytes, log_file=log_file)
    filename, extension, content_type = _guess_filename_and_extension(
        source,
        content_type=_content_type_base(headers),
        media_kind=media_kind,
    )
    upload_input: Dict[str, Any] = {
        "uploadType": _LEONARDO_UPLOAD_TYPE_INIT,
        "extension": extension,
    }
    if media_kind == "audio":
        upload_input["originalFilename"] = filename
    if team_id:
        upload_input["teamId"] = team_id

    await progress_cb(5, {"stage": "upload_reference_init", "media_kind": media_kind, "index": index, "extension": extension})
    obj = await _leonardo_graphql(
        page,
        auth_headers=auth_headers,
        operation_name="UploadImage",
        query=UPLOAD_IMAGE_MUTATION,
        variables={"uploadImageInput": upload_input},
        log_file=log_file,
    )
    upload = ((obj.get("data") or {}) if isinstance(obj.get("data"), dict) else {}).get("uploadImage")
    if not isinstance(upload, dict):
        raise NonPenalizedTaskError(f"Leonardo UploadImage returned invalid payload: {safe_trim(_compact_json(obj), 700)}", status_code=502)
    upload_id = _one_str(upload.get("uploadId"))
    upload_url = _one_str(upload.get("url"))
    raw_fields = upload.get("fields")
    if isinstance(raw_fields, str):
        try:
            fields = json.loads(raw_fields)
        except Exception as exc:
            raise NonPenalizedTaskError("Leonardo UploadImage returned invalid S3 fields JSON", status_code=502) from exc
    elif isinstance(raw_fields, dict):
        fields = raw_fields
    else:
        fields = {}
    if not upload_id or not upload_url or not fields:
        raise NonPenalizedTaskError(
            f"Leonardo UploadImage missing upload data: {safe_trim(_compact_json(upload), 700)}",
            status_code=502,
        )

    await progress_cb(6, {"stage": "upload_reference_s3", "media_kind": media_kind, "index": index, "upload_id": upload_id})
    await _post_s3_upload_form(
        upload_url=upload_url,
        fields=fields,
        filename=filename,
        data=data,
        content_type=content_type,
        log_file=log_file,
    )

    await progress_cb(7, {"stage": "upload_reference_poll", "media_kind": media_kind, "index": index, "upload_id": upload_id})
    if media_kind == "image":
        image = await _finalize_uploaded_image(page, auth_headers=auth_headers, upload_id=upload_id, log_file=log_file)
        return {
            "id": _one_str(image.get("id") or upload_id),
            "type": _LEONARDO_INIT_IMAGE_TYPE_UPLOADED,
            "url": _one_str(image.get("url")),
            "width": _int_from_payload(image.get("imageWidth"), default=0, minimum=0),
            "height": _int_from_payload(image.get("imageHeight"), default=0, minimum=0),
        }

    media = await _poll_uploaded_media(page, auth_headers=auth_headers, upload_id=upload_id, log_file=log_file)
    return {
        "id": _one_str(media.get("id") or upload_id),
        "type": _LEONARDO_INIT_IMAGE_TYPE_UPLOADED,
        "url": _one_str(media.get("url")),
        "file_url": _one_str(media.get("url")),
        "thumbnail_url": _one_str(media.get("thumbnailUrl")),
        "width": _int_from_payload(media.get("width"), default=0, minimum=0),
        "height": _int_from_payload(media.get("height"), default=0, minimum=0),
        "duration": _int_from_payload(media.get("duration"), default=0, minimum=0),
        "status": _one_str(media.get("status")),
    }


def _image_guidance_entry(ref: Dict[str, Any], *, ref_type_key: str = "") -> Dict[str, Any]:
    image = {
        "id": _one_str(ref.get("id")),
        "type": _normalize_uploaded_ref_type(ref.get(ref_type_key) if ref_type_key else ref.get("type")),
    }
    entry: Dict[str, Any] = {
        "image": image,
        "strength": _one_str(ref.get("strength")) or _LEONARDO_DEFAULT_REFERENCE_STRENGTH,
    }
    return entry


def _video_guidance_entry(ref: Dict[str, Any]) -> Dict[str, Any]:
    video: Dict[str, Any] = {
        "id": _one_str(ref.get("id")),
        "type": _normalize_uploaded_ref_type(ref.get("type")),
    }
    for key in ("duration", "width", "height"):
        value = _int_from_payload(ref.get(key), default=0, minimum=0)
        if value > 0:
            video[key] = value
    return {"video": video}


def _audio_guidance_entry(ref: Dict[str, Any]) -> Dict[str, Any]:
    audio: Dict[str, Any] = {
        "id": _one_str(ref.get("id")),
        "type": _normalize_uploaded_ref_type(ref.get("type")),
    }
    duration = _int_from_payload(ref.get("duration"), default=0, minimum=0)
    if duration > 0:
        audio["duration"] = duration
    return {"audio": audio}


def _append_guidance(guidances: Dict[str, Any], key: str, entries: List[Dict[str, Any]]) -> None:
    clean = [entry for entry in entries if isinstance(entry, dict)]
    if not clean:
        return
    current = guidances.get(key)
    if current is None:
        guidances[key] = clean
    elif isinstance(current, list):
        current.extend(clean)
    else:
        guidances[key] = [current, *clean]


async def _build_reference_guidances(
    page: Any,
    *,
    auth_headers: Dict[str, str],
    payload: Dict[str, Any],
    log_file: Path,
    progress_cb: ProgressCB,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    team_id = _one_str(payload.get("leonardo_team_id") or payload.get("teamId") or payload.get("team_id"))
    image_upload_count = 0
    video_upload_count = 0
    audio_upload_count = 0

    async def image_ref_from_source(source: str) -> Dict[str, Any]:
        nonlocal image_upload_count
        image_upload_count += 1
        return await _leonardo_upload_asset(
            page,
            auth_headers=auth_headers,
            source=source,
            media_kind="image",
            log_file=log_file,
            progress_cb=progress_cb,
            index=image_upload_count,
            team_id=team_id,
        )

    async def video_ref_from_source(source: str) -> Dict[str, Any]:
        nonlocal video_upload_count
        video_upload_count += 1
        return await _leonardo_upload_asset(
            page,
            auth_headers=auth_headers,
            source=source,
            media_kind="video",
            log_file=log_file,
            progress_cb=progress_cb,
            index=video_upload_count,
            team_id=team_id,
        )

    async def audio_ref_from_source(source: str) -> Dict[str, Any]:
        nonlocal audio_upload_count
        audio_upload_count += 1
        return await _leonardo_upload_asset(
            page,
            auth_headers=auth_headers,
            source=source,
            media_kind="audio",
            log_file=log_file,
            progress_cb=progress_cb,
            index=audio_upload_count,
            team_id=team_id,
        )

    start_refs: List[Dict[str, Any]] = []
    end_refs: List[Dict[str, Any]] = []
    image_refs: List[Dict[str, Any]] = []
    video_refs: List[Dict[str, Any]] = []
    audio_refs: List[Dict[str, Any]] = []

    start_id = _first_present_str(payload, ("start_frame_id", "startFrameId", "first_image_id", "firstImageId", "image_id", "imageId"))
    if start_id:
        start_refs.append({"id": start_id, "type": payload.get("start_frame_type") or payload.get("first_image_type")})
    else:
        start_url = _first_present_str(
            payload,
            (
                "start_frame_url",
                "startFrameUrl",
                "start_frame_image_url",
                "first_image_url",
                "firstImageUrl",
                "image_url",
                "imageUrl",
            ),
        )
        if start_url:
            start_refs.append(await image_ref_from_source(start_url))

    end_id = _first_present_str(
        payload,
        ("end_frame_id", "endFrameId", "last_image_id", "lastImageId", "end_image_id", "endImageId", "last_frame_image_id"),
    )
    if end_id:
        end_refs.append({"id": end_id, "type": payload.get("end_frame_type") or payload.get("last_image_type")})
    else:
        end_url = _first_present_str(
            payload,
            (
                "end_frame_url",
                "endFrameUrl",
                "end_frame_image_url",
                "last_image_url",
                "lastImageUrl",
                "last_frame_image_url",
                "end_image_url",
                "endImageUrl",
            ),
        )
        if end_url:
            end_refs.append(await image_ref_from_source(end_url))

    for ref_id in _list_field_strs(
        payload,
        ("reference_image_ids", "referenceImageIds", "image_reference_ids", "imageReferenceIds", "ref_image_ids"),
        dict_keys=("id", "upload_id", "uploadId", "initImageId"),
    ):
        image_refs.append({"id": ref_id, "type": payload.get("reference_image_type")})
    image_ref_urls = _list_field_strs(
        payload,
        (
            "reference_image_urls",
            "referenceImageUrls",
            "image_reference_urls",
            "imageReferenceUrls",
            "reference_images",
            "referenceImages",
            "images",
        ),
    )
    if len(image_ref_urls) + len(image_refs) > _LEONARDO_IMAGE_REFERENCE_MAX_COUNT:
        raise NonPenalizedTaskError(
            f"Leonardo image_reference supports at most {_LEONARDO_IMAGE_REFERENCE_MAX_COUNT} images",
            status_code=400,
        )
    for url in image_ref_urls:
        image_refs.append(await image_ref_from_source(url))

    video_id = _first_present_str(payload, ("reference_video_id", "referenceVideoId", "video_reference_id", "videoReferenceId"))
    if video_id:
        video_refs.append(
            {
                "id": video_id,
                "type": payload.get("reference_video_type") or payload.get("video_reference_type"),
                "duration": payload.get("reference_video_duration") or payload.get("video_reference_duration"),
                "width": payload.get("reference_video_width") or payload.get("video_reference_width"),
                "height": payload.get("reference_video_height") or payload.get("video_reference_height"),
            }
        )
    else:
        video_urls = _list_field_strs(payload, ("reference_video_url", "referenceVideoUrl", "video_reference_url", "videoReferenceUrl"))
        if len(video_urls) > _LEONARDO_VIDEO_REFERENCE_MAX_COUNT:
            raise NonPenalizedTaskError(
                f"Leonardo video_reference_base supports at most {_LEONARDO_VIDEO_REFERENCE_MAX_COUNT} video",
                status_code=400,
            )
        for url in video_urls:
            video_refs.append(await video_ref_from_source(url))

    audio_id = _first_present_str(payload, ("reference_audio_id", "referenceAudioId", "audio_reference_id", "audioReferenceId", "audio_id", "audioId"))
    if audio_id:
        audio_refs.append(
            {
                "id": audio_id,
                "type": payload.get("reference_audio_type") or payload.get("audio_reference_type"),
                "duration": payload.get("reference_audio_duration") or payload.get("audio_reference_duration"),
            }
        )
    for ref_id in _list_field_strs(
        payload,
        ("reference_audio_ids", "referenceAudioIds", "audio_reference_ids", "audioReferenceIds"),
        dict_keys=("id", "upload_id", "uploadId", "mediaId", "audioId"),
    ):
        audio_refs.append(
            {
                "id": ref_id,
                "type": payload.get("reference_audio_type") or payload.get("audio_reference_type"),
                "duration": payload.get("reference_audio_duration") or payload.get("audio_reference_duration"),
            }
        )
    audio_ref_urls = _list_field_strs(
        payload,
        (
            "reference_audio_url",
            "referenceAudioUrl",
            "reference_audio_urls",
            "referenceAudioUrls",
            "audio_reference_url",
            "audioReferenceUrl",
            "reference_audios",
            "referenceAudios",
            "audio_references",
            "audioReferences",
            "audio_url",
            "audioUrl",
        ),
        dict_keys=("url", "audio_url", "audioUrl", "file_url", "fileUrl", "src"),
    )
    if len(audio_ref_urls) + len(audio_refs) > _LEONARDO_AUDIO_REFERENCE_MAX_COUNT:
        raise NonPenalizedTaskError(
            f"Leonardo audio_reference supports at most {_LEONARDO_AUDIO_REFERENCE_MAX_COUNT} audio",
            status_code=400,
        )
    for url in audio_ref_urls:
        audio_refs.append(await audio_ref_from_source(url))

    if audio_refs and not (start_refs or end_refs or image_refs or video_refs):
        raise NonPenalizedTaskError(
            "Leonardo audio_reference requires at least one image or video reference",
            status_code=400,
        )

    guidances: Dict[str, Any] = {}
    _append_guidance(guidances, "start_frame", [_image_guidance_entry(ref) for ref in start_refs])
    _append_guidance(guidances, "end_frame", [_image_guidance_entry(ref) for ref in end_refs])
    _append_guidance(guidances, "image_reference", [_image_guidance_entry(ref) for ref in image_refs])
    _append_guidance(guidances, "video_reference_base", [_video_guidance_entry(ref) for ref in video_refs])
    _append_guidance(guidances, "audio_reference", [_audio_guidance_entry(ref) for ref in audio_refs])

    meta = {
        "start_frame_count": len(start_refs),
        "end_frame_count": len(end_refs),
        "image_reference_count": len(image_refs),
        "video_reference_count": len(video_refs),
        "audio_reference_count": len(audio_refs),
        "reference_upload_image_count": image_upload_count,
        "reference_upload_video_count": video_upload_count,
        "reference_upload_audio_count": audio_upload_count,
    }
    return guidances, meta


def _looks_like_video_url(value: str) -> bool:
    s = _one_str(value)
    if not s.startswith(("http://", "https://")):
        return False
    path = unquote(urlparse(s).path or "").lower()
    return path.endswith((".mp4", ".mov", ".webm"))


def _looks_like_non_video_media_url(value: str) -> bool:
    s = _one_str(value)
    if not s.startswith(("http://", "https://")):
        return False
    path = unquote(urlparse(s).path or "").lower()
    return path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp3", ".wav"))


def _collect_video_urls(value: Any, *, key_hint: str = "") -> List[str]:
    out: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            k = str(key or "").strip()
            lk = k.lower()
            if isinstance(item, str):
                if lk in _VIDEO_URL_KEY_HINTS and item.startswith(("http://", "https://")) and not _looks_like_non_video_media_url(item):
                    out.append(item.strip())
                elif _looks_like_video_url(item):
                    out.append(item.strip())
            else:
                out.extend(_collect_video_urls(item, key_hint=k))
    elif isinstance(value, list):
        for item in value:
            out.extend(_collect_video_urls(item, key_hint=key_hint))
    elif isinstance(value, str):
        if key_hint.lower() in _VIDEO_URL_KEY_HINTS or _looks_like_video_url(value):
            s = value.strip()
            if s.startswith(("http://", "https://")):
                out.append(s)
    deduped: List[str] = []
    seen = set()
    for url in out:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _generation_status(generation: Any) -> str:
    if not isinstance(generation, dict):
        return ""
    return _one_str(generation.get("status")).upper()


async def _poll_generation_until_video(
    page: Any,
    *,
    auth_headers: Dict[str, str],
    generation_id: str,
    timeout_seconds: float,
    log_file: Path,
    progress_cb: ProgressCB,
) -> Dict[str, Any]:
    started = time.time()
    deadline = started + max(60.0, float(timeout_seconds or 900.0))
    last_snapshot: Dict[str, Any] = {}
    interval = 8.0
    while time.time() < deadline:
        obj = await _leonardo_graphql(
            page,
            auth_headers=auth_headers,
            operation_name="LeonardoGenerationById",
            query=GENERATION_QUERY,
            variables={"id": generation_id},
            log_file=log_file,
        )
        generation = ((obj.get("data") or {}) if isinstance(obj.get("data"), dict) else {}).get("generations_by_pk")
        if isinstance(generation, dict):
            last_snapshot = generation
        status = _generation_status(generation)
        urls = _collect_video_urls(generation)
        elapsed = max(0.0, time.time() - started)
        pct = 10 + min(85, int(elapsed / max(1.0, (deadline - started)) * 85))
        await progress_cb(
            pct,
            {
                "stage": "poll_generation",
                "generation_id": generation_id,
                "status": status,
                "video_url_found": bool(urls),
            },
        )
        append_log(
            log_file,
            f"[leonardo] poll generation={generation_id} status={status!r} urls={len(urls)}",
        )
        if urls:
            return {
                "generation": generation,
                "video_url": urls[0],
                "urls": urls,
            }
        if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
            raise NonPenalizedTaskError(
                f"Leonardo generation failed: status={status} generation={safe_trim(_compact_json(generation), 700)}",
                status_code=502,
            )
        await asyncio.sleep(interval)
        interval = min(15.0, interval + 1.0)

    raise NonPenalizedTaskError(
        f"Leonardo generation timed out without video URL: {safe_trim(_compact_json(last_snapshot), 900)}",
        status_code=504,
    )


async def leonardo_workflow(
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
    pure_mode: bool = True,
    db: Any = None,
    task_type_window_id: Optional[int] = None,
) -> Dict[str, Any]:
    del access_expires, db, task_type_window_id

    p = dict(payload or {})
    prompt = _prompt_from_payload(p)
    if not prompt:
        raise NonPenalizedTaskError("payload.prompt cannot be empty", status_code=400)

    model = _resolve_model(p)
    params, meta = _build_generate_parameters(p)
    reference_meta: Dict[str, Any] = {}
    public = _bool_from_payload(p.get("public"), default=not _bool_from_payload(p.get("private"), default=False))
    target_page_hint = (
        _one_str(p.get("leonardo_url") or p.get("target_url"))
        or _one_str(default_target_url)
        or DEFAULT_LEONARDO_TARGET
    )
    target_page = leonardo_target_url_for_model(model, target_page_hint)
    log_file = Path(_one_str(p.get("monitor_log_path"))) if _one_str(p.get("monitor_log_path")) else MONITOR_LOG_FILE

    sess = get_or_create_playwright_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )

    await progress_cb(
        1,
        {
            "stage": "init",
            "workflow_kind": "video",
            "provider": "leonardo",
            "model": model,
            **meta,
        },
    )

    started = time.time()
    async with sess.driver_lock:
        try:
            await sess.ensure_open(
                args=[target_page],
                force_open=False,
                headless=headless,
                require_page=False,
                pure_mode=pure_mode,
            )
            page = await _find_or_open_leonardo_page(sess, target_page, log_file=log_file)
            if page is None:
                raise NonPenalizedTaskError(
                    "Leonardo page is not open; please open app.leonardo.ai in the fingerprint browser first.",
                    status_code=401,
                )
            auth_headers: Dict[str, str] = {}
            access_raw = _one_str(p.get("leonardo_access_token") or p.get("access_token") or access_token)
            if access_raw:
                auth_headers["Authorization"] = access_raw if access_raw.lower().startswith("bearer ") else f"Bearer {access_raw}"
                auth_headers["X-Leo-Schema-Version"] = _one_str(p.get("x_leo_schema_version")) or LEONARDO_SCHEMA_VERSION
            else:
                await progress_cb(3, {"stage": "capture_auth_header"})
                auth_headers = await _capture_graphql_headers(
                    page,
                    target_url=target_page,
                    cache_key=sess.cache_key,
                    log_file=log_file,
                    timeout_seconds=float(p.get("leonardo_auth_capture_timeout_seconds") or 25.0),
                    cache_seconds=float(p.get("leonardo_auth_cache_seconds") or LEONARDO_DEFAULT_AUTH_CACHE_SECONDS),
                )

            reference_guidances, reference_meta = await _build_reference_guidances(
                page,
                auth_headers=auth_headers,
                payload=p,
                log_file=log_file,
                progress_cb=progress_cb,
            )
            if reference_guidances:
                existing_guidances = params.get("guidances")
                if isinstance(existing_guidances, dict):
                    merged_guidances = dict(existing_guidances)
                else:
                    merged_guidances = {}
                for guidance_key, guidance_entries in reference_guidances.items():
                    _append_guidance(merged_guidances, guidance_key, list(guidance_entries if isinstance(guidance_entries, list) else [guidance_entries]))
                params["guidances"] = merged_guidances
                await progress_cb(8, {"stage": "reference_guidances_ready", **reference_meta})

            request_body = {
                "model": model,
                "public": public,
                "parameters": params,
            }
            append_log(log_file, f"[leonardo] submit request={safe_trim(_compact_json(request_body), 1000)}")
            await progress_cb(9, {"stage": "submit_api", "model": model, **meta, **reference_meta})
            obj = await _leonardo_graphql(
                page,
                auth_headers=auth_headers,
                operation_name="Generate",
                query=GENERATE_MUTATION,
                variables={"request": request_body},
                log_file=log_file,
            )
            generate = ((obj.get("data") or {}) if isinstance(obj.get("data"), dict) else {}).get("generate")
            generation_id = _one_str((generate or {}).get("generationId") if isinstance(generate, dict) else "")
            api_credit_cost = (generate or {}).get("apiCreditCost") if isinstance(generate, dict) else None
            if not generation_id:
                raise NonPenalizedTaskError(
                    f"Leonardo Generate succeeded but generationId is missing: {safe_trim(_compact_json(obj), 700)}",
                    status_code=502,
                )

            await progress_cb(
                10,
                {
                    "stage": "submitted",
                    "generation_id": generation_id,
                    "api_credit_cost": api_credit_cost,
                },
            )
            poll = await _poll_generation_until_video(
                page,
                auth_headers=auth_headers,
                generation_id=generation_id,
                timeout_seconds=timeout_seconds,
                log_file=log_file,
                progress_cb=progress_cb,
            )
            elapsed_ms = int(max(0.0, (time.time() - started) * 1000.0))
            video_url = _one_str(poll.get("video_url"))
            await progress_cb(
                100,
                {
                    "stage": "done",
                    "generation_id": generation_id,
                    "video_url": video_url,
                    "elapsed_ms": elapsed_ms,
                },
            )
            return {
                "type": "leonardo_workflow_video",
                "message": "Leonardo Seedance video generation completed",
                "share_url": video_url,
                "video_url": video_url,
                "urls": poll.get("urls") or [video_url],
                "workflow_kind": "video",
                "video_mode": "v2v" if reference_meta.get("video_reference_count") else ("i2v" if (reference_meta.get("start_frame_count") or reference_meta.get("end_frame_count") or reference_meta.get("image_reference_count") or reference_meta.get("audio_reference_count")) else "t2v"),
                "provider": "leonardo",
                "model": model,
                "model_key": model,
                "generation_id": generation_id,
                "api_credit_cost": api_credit_cost,
                "public": public,
                "elapsed_ms": elapsed_ms,
                **meta,
                **reference_meta,
            }
        finally:
            try:
                await sess.disconnect_playwright_only()
            except Exception:
                pass
