"""Isolated Leonardo full-cookie/JWT probe used by the admin canary flow.

The probe deliberately does not feed the production scheduler. Cookie values
and bearer tokens stay in process memory and are never returned or logged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote, urlparse

from .fp_browser_client import FPBrowserClient
from .playwright_broswer_context import normalize_cdp_endpoint, safe_trim


LEONARDO_APP_ORIGINS = (
    "https://app.leonardo.ai",
    "https://api.leonardo.ai",
    "https://leonardo.ai",
)
LEONARDO_SESSION_URL = "https://app.leonardo.ai/api/auth/get-session"
LEONARDO_GRAPHQL_URL = "https://api.leonardo.ai/v1/graphql"
LEONARDO_BALANCE_QUERY = """query GetTokenBalance {
  user_details {
    subscriptionTokens
    rolloverTokens
    paidTokens
    tokenRenewalDate
    subscriptionGptTokens
    subscriptionModelTokens
    plan
    __typename
  }
}"""
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_SNAPSHOT_MAX_AGE_SECONDS = 24 * 3600


@dataclass
class LeonardoCookieSnapshot:
    mapping_id: int
    captured_at: float
    cookie_header: str = field(repr=False)
    cookie_names: tuple[str, ...]
    page_url: str
    proxy_url: str = field(default="", repr=False)
    proxy_protocol: str = ""
    browser_last_ip: str = ""


_SNAPSHOTS: Dict[int, LeonardoCookieSnapshot] = {}
_SNAPSHOT_LOCK = asyncio.Lock()


def _iso_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_error(value: Any, limit: int = 240) -> str:
    text = str(value or "")
    text = re.sub(r"(://)[^/@\s]+@", r"\1***@", text)
    text = re.sub(r"(?i)(authorization|cookie)\s*[:=]\s*[^\s,;]+", r"\1=***", text)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._-]+", "Bearer ***", text)
    return safe_trim(text, limit)


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    value = str(token or "").strip()
    if not _JWT_RE.fullmatch(value):
        return {}
    try:
        payload = value.split(".", 2)[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        obj = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _is_likely_leonardo_jwt(token: str) -> bool:
    payload = _decode_jwt_payload(token)
    if not payload:
        return False
    issuer = str(payload.get("iss") or "").lower()
    token_use = str(payload.get("token_use") or "").lower()
    return (
        "cognito-idp" in issuer
        or token_use in {"access", "id"}
        or "cognito:username" in payload
    )


def _jwt_rank(token: str) -> tuple[int, int]:
    payload = _decode_jwt_payload(token)
    token_use = str(payload.get("token_use") or "").lower()
    use_score = 3 if token_use == "access" else 2 if token_use == "id" else 1
    try:
        exp = int(payload.get("exp") or 0)
    except Exception:
        exp = 0
    return use_score, exp


def _find_best_jwt(value: Any) -> str:
    candidates: set[str] = set()

    def walk(node: Any, key_hint: str = "") -> None:
        if isinstance(node, str):
            token = node.strip()
            if _JWT_RE.fullmatch(token) and "cf_access" not in key_hint.lower():
                candidates.add(token)
            return
        if isinstance(node, list):
            for item in node:
                walk(item, key_hint)
            return
        if isinstance(node, dict):
            for key, item in node.items():
                key_text = str(key or "")
                if "cf_access" in key_text.lower():
                    continue
                if isinstance(item, (dict, list)) or "token" in key_text.lower():
                    walk(item, key_text)

    walk(value)
    if not candidates:
        return ""
    likely = [token for token in candidates if _is_likely_leonardo_jwt(token)]
    pool = likely or list(candidates)
    pool.sort(key=_jwt_rank, reverse=True)
    return pool[0]


def _jwt_metadata(token: str) -> Dict[str, Any]:
    payload = _decode_jwt_payload(token)
    if not payload:
        return {"found": False}
    now = int(time.time())
    try:
        exp = int(payload.get("exp") or 0)
    except Exception:
        exp = 0
    issuer_host = ""
    try:
        issuer_host = str(urlparse(str(payload.get("iss") or "")).hostname or "")
    except Exception:
        pass
    return {
        "found": True,
        "token_use": safe_trim(str(payload.get("token_use") or ""), 32),
        "issuer_host": safe_trim(issuer_host, 120),
        "expires_at": _iso_timestamp(exp) if exp > 0 else "",
        "ttl_seconds": max(0, exp - now) if exp > 0 else None,
        "expired": bool(exp > 0 and exp <= now),
    }


def _cookie_header(cookies: Iterable[Dict[str, Any]]) -> tuple[str, tuple[str, ...]]:
    now = time.time()
    usable: list[Dict[str, Any]] = []
    for raw in cookies:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        value = str(raw.get("value") or "")
        domain = str(raw.get("domain") or "").strip().lower().lstrip(".")
        if not name or not value or not domain.endswith("leonardo.ai"):
            continue
        try:
            expires = float(raw.get("expires") or 0)
        except Exception:
            expires = 0
        if expires > 0 and expires <= now:
            continue
        usable.append(raw)
    usable.sort(
        key=lambda item: (
            -len(str(item.get("path") or "/")),
            str(item.get("name") or ""),
        )
    )
    header = "; ".join(f"{item.get('name')}={item.get('value')}" for item in usable)
    names = tuple(sorted({str(item.get("name") or "") for item in usable}))
    return header, names


def _build_proxy_url(detail: Dict[str, Any]) -> tuple[str, str, str]:
    proxy = detail.get("proxyInfo") if isinstance(detail, dict) else None
    proxy = proxy if isinstance(proxy, dict) else {}
    protocol = str(proxy.get("protocol") or proxy.get("proxyMethod") or "").strip().lower()
    host = str(proxy.get("host") or "").strip()
    port = str(proxy.get("port") or "").strip()
    username = str(proxy.get("proxyUserName") or "").strip()
    password = str(proxy.get("proxyPassword") or "").strip()
    last_ip = str(proxy.get("lastIp") or "").strip()
    if not host or not port:
        return "", protocol, last_ip
    if protocol not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}:
        protocol = "http"
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    return f"{protocol}://{auth}{host}:{port}", protocol, last_ip


def _set_cookie_names(response: Any) -> list[str]:
    values: list[str] = []
    try:
        values = list(response.headers.get_list("set-cookie"))
    except Exception:
        try:
            raw = response.headers.get("set-cookie")
            values = [str(raw)] if raw else []
        except Exception:
            values = []
    names: set[str] = set()
    for value in values:
        first = str(value or "").split(";", 1)[0]
        if "=" in first:
            names.add(first.split("=", 1)[0].strip())
    return sorted(name for name in names if name)


async def _capture_live_snapshot(mapping_id: int, ctx_row: Dict[str, Any]) -> LeonardoCookieSnapshot:
    vendor = str(ctx_row.get("vendor") or "roxy").strip() or "roxy"
    base_url = str(ctx_row.get("lan_addr") or "").strip()
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "").strip()
    window_key = str(ctx_row.get("window_key") or "").strip()
    if not base_url or not space_id or not window_key:
        raise RuntimeError("mapping missing browser context")

    client = FPBrowserClient()
    conn = await client.get_open_window_connection_info(
        vendor=vendor,
        base_url=base_url,
        access_key=access_key,
        window_key=window_key,
    )
    endpoint_raw = str((conn or {}).get("http") or (conn or {}).get("ws") or "").strip()
    if not endpoint_raw:
        raise RuntimeError("fingerprint window is not open")

    proxy_url = ""
    proxy_protocol = ""
    browser_last_ip = ""
    try:
        detail = await client.get_browser_detail(
            vendor=vendor,
            base_url=base_url,
            access_key=access_key,
            space_id=space_id,
            window_key=window_key,
        )
        proxy_url, proxy_protocol, browser_last_ip = _build_proxy_url(detail or {})
    except Exception:
        pass

    from playwright.async_api import async_playwright  # type: ignore

    page_url = ""
    cookies: list[Dict[str, Any]] = []
    endpoint = normalize_cdp_endpoint(endpoint_raw, base_url=base_url)
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(endpoint)
        try:
            context = browser.contexts[0] if browser.contexts else None
            if context is None:
                raise RuntimeError("fingerprint browser context is unavailable")
            cookies = list(await context.cookies(list(LEONARDO_APP_ORIGINS)) or [])
            pages = list(context.pages or [])
            page = next((p for p in pages if "app.leonardo.ai" in str(getattr(p, "url", "") or "")), None)
            if page is not None:
                page_url = str(getattr(page, "url", "") or "")
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    header, names = _cookie_header(cookies)
    if not header:
        raise RuntimeError("no usable Leonardo cookies found in the window")
    return LeonardoCookieSnapshot(
        mapping_id=int(mapping_id),
        captured_at=time.time(),
        cookie_header=header,
        cookie_names=names,
        page_url=safe_trim(page_url, 300),
        proxy_url=proxy_url,
        proxy_protocol=proxy_protocol,
        browser_last_ip=browser_last_ip,
    )


def _snapshot_metadata(snapshot: LeonardoCookieSnapshot) -> Dict[str, Any]:
    names = set(snapshot.cookie_names)
    session_tokens = sorted(name for name in names if "better-auth.session_token" in name)
    session_data = sorted(name for name in names if "better-auth.session_data" in name)
    return {
        "mapping_id": snapshot.mapping_id,
        "captured_at": _iso_timestamp(snapshot.captured_at),
        "age_seconds": max(0, int(time.time() - snapshot.captured_at)),
        "cookie_count": len(snapshot.cookie_names),
        "better_auth_session_token_count": len(session_tokens),
        "better_auth_session_data_count": len(session_data),
        "cf_access_cookie_present": "CF_Access_Token" in names,
        "page_url": snapshot.page_url,
        "proxy_configured": bool(snapshot.proxy_url),
        "proxy_protocol": snapshot.proxy_protocol,
        "browser_last_ip": snapshot.browser_last_ip,
    }


async def _probe_snapshot(snapshot: LeonardoCookieSnapshot) -> Dict[str, Any]:
    try:
        from curl_cffi.requests import AsyncSession  # type: ignore
    except Exception as exc:
        raise RuntimeError("curl_cffi is required for the Leonardo TLS probe") from exc

    common_headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://app.leonardo.ai",
        "referer": "https://app.leonardo.ai/",
        "cookie": snapshot.cookie_header,
    }
    request_kwargs: Dict[str, Any] = {
        "timeout": 30,
        "impersonate": "chrome",
        "allow_redirects": False,
    }
    if snapshot.proxy_url:
        request_kwargs["proxy"] = snapshot.proxy_url

    auth_status = 0
    auth_content_type = ""
    auth_error = ""
    auth_obj: Dict[str, Any] = {}
    set_cookie_names: list[str] = []
    egress_ip = ""
    graphql_status = 0
    graphql_error = ""
    graphql_obj: Dict[str, Any] = {}
    token = ""

    async with AsyncSession() as session:
        try:
            ip_response = await session.get(
                "https://api.ipify.org?format=json",
                headers={"accept": "application/json"},
                **request_kwargs,
            )
            if int(ip_response.status_code or 0) == 200:
                ip_obj = ip_response.json()
                if isinstance(ip_obj, dict):
                    egress_ip = safe_trim(str(ip_obj.get("ip") or ""), 80)
        except Exception:
            pass

        try:
            auth_response = await session.get(
                LEONARDO_SESSION_URL,
                headers=common_headers,
                **request_kwargs,
            )
            auth_status = int(auth_response.status_code or 0)
            auth_content_type = safe_trim(str(auth_response.headers.get("content-type") or ""), 120)
            set_cookie_names = _set_cookie_names(auth_response)
            if "json" in auth_content_type.lower() or auth_status == 200:
                try:
                    parsed = auth_response.json()
                    if isinstance(parsed, dict):
                        auth_obj = parsed
                except Exception as exc:
                    auth_error = f"session response was not JSON: {_safe_error(exc, 120)}"
        except Exception as exc:
            auth_error = _safe_error(exc)

        token = _find_best_jwt(auth_obj)
        if token:
            graphql_headers = {
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
                "origin": "https://app.leonardo.ai",
                "referer": "https://app.leonardo.ai/",
                "x-leo-schema-version": "latest",
            }
            try:
                gql_response = await session.post(
                    LEONARDO_GRAPHQL_URL,
                    headers=graphql_headers,
                    json={
                        "operationName": "GetTokenBalance",
                        "variables": {},
                        "query": LEONARDO_BALANCE_QUERY,
                    },
                    **request_kwargs,
                )
                graphql_status = int(gql_response.status_code or 0)
                try:
                    parsed = gql_response.json()
                    if isinstance(parsed, dict):
                        graphql_obj = parsed
                except Exception as exc:
                    graphql_error = f"GraphQL response was not JSON: {_safe_error(exc, 120)}"
            except Exception as exc:
                graphql_error = _safe_error(exc)

    details = ((graphql_obj.get("data") or {}).get("user_details") or []) if isinstance(graphql_obj, dict) else []
    detail = details[0] if isinstance(details, list) and details and isinstance(details[0], dict) else {}
    errors = graphql_obj.get("errors") if isinstance(graphql_obj, dict) else None
    gql_messages: list[str] = []
    if isinstance(errors, list):
        for item in errors[:3]:
            if isinstance(item, dict):
                gql_messages.append(safe_trim(str(item.get("message") or ""), 180))
            elif item:
                gql_messages.append(safe_trim(str(item), 180))
    if gql_messages and not graphql_error:
        graphql_error = "; ".join(message for message in gql_messages if message)

    def as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    subscription = as_int(detail.get("subscriptionTokens"))
    rollover = as_int(detail.get("rolloverTokens"))
    paid = as_int(detail.get("paidTokens"))
    session_has_identity = bool(auth_obj.get("session") and auth_obj.get("user"))
    jwt_info = _jwt_metadata(token)
    graphql_ok = bool(graphql_status == 200 and detail and not graphql_error)
    return {
        "success": bool(session_has_identity and jwt_info.get("found") and graphql_ok),
        "snapshot": _snapshot_metadata(snapshot),
        "transport": {
            "tls_impersonation": "chrome",
            "proxy_used": bool(snapshot.proxy_url),
            "egress_ip": egress_ip,
            "matches_browser_last_ip": bool(
                egress_ip and snapshot.browser_last_ip and egress_ip == snapshot.browser_last_ip
            ),
        },
        "auth": {
            "http_status": auth_status,
            "content_type": auth_content_type,
            "session_present": session_has_identity,
            "set_cookie_names": set_cookie_names,
            "error": auth_error,
        },
        "jwt": jwt_info,
        "graphql": {
            "http_status": graphql_status,
            "ok": graphql_ok,
            "error": graphql_error,
            "remaining_quota": subscription + rollover + paid if detail else None,
            "subscription_tokens": subscription if detail else None,
            "rollover_tokens": rollover if detail else None,
            "paid_tokens": paid if detail else None,
            "token_renewal_date": safe_trim(str(detail.get("tokenRenewalDate") or ""), 100),
            "plan": safe_trim(str(detail.get("plan") or ""), 100),
        },
    }


async def capture_and_probe_leonardo_cookie(mapping_id: int, ctx_row: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = await _capture_live_snapshot(mapping_id, ctx_row)
    async with _SNAPSHOT_LOCK:
        _SNAPSHOTS[int(mapping_id)] = snapshot
    result = await _probe_snapshot(snapshot)
    result["mode"] = "captured_live_snapshot"
    return result


async def probe_saved_leonardo_cookie(mapping_id: int) -> Dict[str, Any]:
    async with _SNAPSHOT_LOCK:
        snapshot = _SNAPSHOTS.get(int(mapping_id))
    if snapshot is None:
        raise RuntimeError("no in-memory cookie snapshot for this mapping; capture it first")
    if time.time() - snapshot.captured_at > _SNAPSHOT_MAX_AGE_SECONDS:
        async with _SNAPSHOT_LOCK:
            _SNAPSHOTS.pop(int(mapping_id), None)
        raise RuntimeError("the in-memory cookie snapshot expired; capture it again")
    result = await _probe_snapshot(snapshot)
    result["mode"] = "retested_saved_snapshot"
    return result


async def get_leonardo_cookie_snapshot_status(mapping_id: int) -> Dict[str, Any]:
    async with _SNAPSHOT_LOCK:
        snapshot = _SNAPSHOTS.get(int(mapping_id))
    if snapshot is None:
        return {"exists": False, "mapping_id": int(mapping_id)}
    return {"exists": True, **_snapshot_metadata(snapshot)}
