"""Adobe Firefly GPT Image 2 workflow through a logged-in fingerprint browser."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..core.paths import DATA_DIR
from .playwright_broswer_context import get_or_create_ctx as get_or_create_playwright_ctx
from .task_executor_types import NonPenalizedTaskError, ProgressCB


DEFAULT_ADOBE_IMAGE2_TARGET = "https://firefly.adobe.com/studio"
ADOBE_IMAGE2_PROVIDER_MODEL = "gpt-image@2"
ADOBE_IMAGE2_FEATURE_ID = "firefly_3p:external:gpt_image_2"
ADOBE_IMAGE2_PUBLIC_ASSET_DIR = DATA_DIR / "adobe_image2_assets"

ADOBE_IMAGE2_PUBLIC_MODEL_ALIASES: Dict[str, str] = {
    "adobe-gpt-image2": "medium",
    "adobe-gpt-image2-low": "low",
    "adobe-gpt-image2-medium": "medium",
    "adobe-gpt-image2-high": "high",
}

_ALLOWED_QUALITIES = {"low", "medium", "high"}
_ALLOWED_ASPECT_RATIOS = {"auto", "3:2", "1:1", "2:3"}
_SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_IMAGE_EXTENSIONS = {".avif", ".gif", ".jpg", ".jpeg", ".png", ".webp"}
_MIN_GENERATED_IMAGE_EDGE = 256
_MAX_GENERATED_IMAGE_BYTES = 50 * 1024 * 1024
_TRACKING_IMAGE_HOSTS = {
    "alb.reddit.com",
    "adobedc.demdex.net",
    "cm.everesttech.net",
    "dpm.demdex.net",
}
_ADOBE_NON_GENERATION_HOST_PREFIXES = ("sstats.", "metrics.", "analytics.")
_ADOBE_GENERATION_PATH_HINTS = (
    "/generate",
    "/generation",
    "/inference",
    "/action",
    "/create",
    "/job",
)
_ADOBE_GENERATION_BODY_HINTS = (
    ADOBE_IMAGE2_PROVIDER_MODEL,
    "gpt_image_2",
    ADOBE_IMAGE2_FEATURE_ID,
)
_ACCOUNT_TEXT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) visit(el.shadowRoot);
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      if (rect.width <= 0 || rect.height <= 0 || style.visibility === 'hidden' || style.display === 'none') continue;
      const text = Array.from(el.childNodes)
        .filter((node) => node.nodeType === Node.TEXT_NODE)
        .map((node) => node.textContent || '')
        .join(' ')
        .trim()
        .replace(/\s+/g, ' ');
      if (text && !seen.has(text)) {
        seen.add(text);
        out.push(text);
      }
    }
  };
  visit(document);
  return out;
}
"""
_LARGE_IMAGE_URLS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) visit(el.shadowRoot);
      if (el.tagName !== 'IMG') continue;
      const width = Number(el.naturalWidth || el.width || 0);
      const height = Number(el.naturalHeight || el.height || 0);
      const url = String(el.currentSrc || el.src || '').trim();
      if (width >= 256 && height >= 256 && url && !seen.has(url)) {
        seen.add(url);
        out.push(url);
      }
    }
  };
  visit(document);
  return out;
}
"""


def _one_str(value: Any) -> str:
    return str(value or "").strip()


def resolve_adobe_image2_quality(payload: Dict[str, Any]) -> Tuple[str, str]:
    model = _one_str(payload.get("model") or payload.get("adobe_model")).lower()
    quality = _one_str(payload.get("quality") or payload.get("image_quality")).lower()
    if not quality:
        quality = ADOBE_IMAGE2_PUBLIC_MODEL_ALIASES.get(model, "medium")
    if quality not in _ALLOWED_QUALITIES:
        raise NonPenalizedTaskError(
            "Adobe GPT Image 2 quality must be low, medium, or high",
            status_code=400,
            retryable=False,
        )
    public_model = model if model in ADOBE_IMAGE2_PUBLIC_MODEL_ALIASES else f"adobe-gpt-image2-{quality}"
    return public_model, quality


def resolve_adobe_image2_aspect_ratio(payload: Dict[str, Any]) -> str:
    ratio = _one_str(
        payload.get("aspect_ratio")
        or payload.get("ratio")
        or payload.get("size_ratio")
        or payload.get("aspectRatio")
        or "auto"
    ).lower()
    if ratio not in _ALLOWED_ASPECT_RATIOS:
        raise NonPenalizedTaskError(
            "Adobe GPT Image 2 aspect_ratio must be auto, 3:2, 1:1, or 2:3",
            status_code=400,
            retryable=False,
        )
    return ratio


def parse_adobe_account_text(lines: List[str]) -> Dict[str, Any]:
    normalized = [_one_str(line) for line in lines if _one_str(line)]
    joined = "\n".join(normalized)
    credit_match = re.search(
        r"([\d,.]+)\s*/\s*([\d,.]+)\s+credits?\s+left",
        joined,
        re.IGNORECASE,
    )
    remaining = 0
    provisioned = 0
    if credit_match:
        remaining = int(float(credit_match.group(1).replace(",", "")))
        provisioned = int(float(credit_match.group(2).replace(",", "")))

    email = ""
    for line in normalized:
        email_match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", line, re.IGNORECASE)
        if email_match:
            email = email_match.group(0)
            break

    reset = ""
    reset_match = re.search(r"Next reset:\s*([^\n]+)", joined, re.IGNORECASE)
    if reset_match:
        reset = reset_match.group(1).strip()

    premium = "premium features" in joined.lower()
    plan_title = "Adobe Firefly Premium" if premium else "Adobe Firefly"
    return {
        "platform_account": email or None,
        "plan_title": plan_title,
        "remaining_quota": max(0, remaining),
        "provisioned_quota": max(0, provisioned),
        "consumed_quota": max(0, provisioned - remaining),
        "next_reset": reset or None,
    }


def parse_adobe_effective_quota(payload: Any) -> Dict[str, Any]:
    obj = payload if isinstance(payload, dict) else {}
    quotas = obj.get("effectiveQuotas")
    if not isinstance(quotas, list):
        quotas = []
    quota: Dict[str, Any] = {}
    for item in quotas:
        if isinstance(item, dict) and _one_str(item.get("resourceType")) == "firefly_credits":
            quota = item
            break
    provisioned = int(float(quota.get("provisionedQuota") or 0))
    consumed = int(float(quota.get("consumedQuota") or 0))
    return {
        "remaining_quota": max(0, provisioned - consumed),
        "provisioned_quota": max(0, provisioned),
        "consumed_quota": max(0, consumed),
        "next_reset_timestamp": int(quota.get("nextQuotaRefreshTimestamp") or 0) or None,
    }


def _is_adobe_page_url(url: str) -> bool:
    try:
        host = (urlparse(_one_str(url)).hostname or "").lower()
    except Exception:
        return False
    return host == "firefly.adobe.com" or host.endswith(".firefly.adobe.com")


async def _find_or_open_adobe_page(pw_ctx: Any, target_url: str) -> Any:
    context = getattr(pw_ctx, "context", None)
    if context is None:
        raise NonPenalizedTaskError("Adobe browser context is not initialized", status_code=502)
    page = None
    for candidate in list(getattr(context, "pages", []) or []):
        try:
            if not candidate.is_closed() and _is_adobe_page_url(candidate.url):
                page = candidate
                if "/studio" in candidate.url:
                    break
        except Exception:
            continue
    if page is None:
        page = await context.new_page()
        await page.goto(target_url, wait_until="domcontentloaded", timeout=90_000)
    try:
        await page.bring_to_front()
    except Exception:
        pass
    pw_ctx.page = page
    return page


async def _wait_for_adobe_studio(page: Any, timeout_seconds: float) -> None:
    timeout_ms = int(max(10.0, min(float(timeout_seconds or 60.0), 120.0)) * 1000)
    try:
        await page.get_by_test_id("ugi-generate-button").wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        raise NonPenalizedTaskError(
            "Adobe Firefly Studio is not ready; confirm the account is signed in",
            status_code=401,
            retryable=False,
        ) from exc


async def _probe_account_menu(page: Any) -> Dict[str, Any]:
    account_button = page.locator('[aria-label$=" Account"]')
    if await account_button.count() <= 0:
        raise NonPenalizedTaskError(
            "Adobe Firefly login session was not found",
            status_code=401,
            retryable=False,
        )
    try:
        await account_button.last.click(timeout=10_000)
        await page.get_by_text(re.compile(r"credits?\s+left", re.IGNORECASE)).first.wait_for(
            state="visible", timeout=10_000
        )
        lines = await page.evaluate(_ACCOUNT_TEXT_JS)
        info = parse_adobe_account_text(lines if isinstance(lines, list) else [])
        if int(info.get("provisioned_quota") or 0) <= 0:
            raise NonPenalizedTaskError(
                "Adobe Firefly credit balance was not visible in the account menu",
                status_code=502,
                retryable=False,
            )
        return info
    finally:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass


async def _capture_quota_from_reload(page: Any, timeout_seconds: float = 30.0) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Dict[str, Any]] = loop.create_future()

    async def on_response(response: Any) -> None:
        if future.done():
            return
        url = _one_str(getattr(response, "url", ""))
        if "bks.adobe.io/v1/feature-auth-profile" not in url or "include-effective-quota-balance=true" not in url:
            return
        try:
            data = await response.json()
            parsed = parse_adobe_effective_quota(data)
            if int(parsed.get("provisioned_quota") or 0) > 0:
                future.set_result(parsed)
        except Exception:
            return

    page.on("response", on_response)
    try:
        await page.reload(wait_until="domcontentloaded", timeout=90_000)
        return await asyncio.wait_for(future, timeout=max(5.0, float(timeout_seconds or 30.0)))
    except asyncio.TimeoutError as exc:
        raise NonPenalizedTaskError(
            "Adobe Firefly credit API did not return a balance",
            status_code=504,
            retryable=False,
        ) from exc
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


async def _probe_adobe_account(page: Any) -> Dict[str, Any]:
    try:
        return await _probe_account_menu(page)
    except NonPenalizedTaskError as menu_error:
        if int(menu_error.status_code or 0) == 401:
            raise
        quota = await _capture_quota_from_reload(page)
        return {
            **quota,
            "platform_account": None,
            "plan_title": "Adobe Firefly",
            "next_reset": None,
        }


async def adobe_fetch_account_in_window(
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    target_url: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
    timeout_seconds: float = 45.0,
) -> Dict[str, Any]:
    target = _one_str(target_url) or DEFAULT_ADOBE_IMAGE2_TARGET
    session = get_or_create_playwright_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    async with session.driver_lock:
        await session.ensure_open(
            args=[target],
            force_open=False,
            headless=headless,
            require_page=False,
            pure_mode=pure_mode,
        )
        page = await _find_or_open_adobe_page(session, target)
        await _wait_for_adobe_studio(page, timeout_seconds)
        return await _probe_adobe_account(page)


async def _select_adobe_model(page: Any) -> None:
    picker = page.get_by_test_id("firefly-picker-")
    if await picker.count() <= 0:
        raise NonPenalizedTaskError("Adobe model picker was not found", status_code=502)
    current = _one_str(await picker.first.get_attribute("value"))
    if current == ADOBE_IMAGE2_PROVIDER_MODEL:
        return
    await picker.first.click(timeout=10_000)
    option = page.locator(f'sp-menu-item[value="{ADOBE_IMAGE2_PROVIDER_MODEL}"]')
    if await option.count() <= 0:
        option = page.get_by_text("GPT Image 2", exact=True)
    await option.last.click(timeout=10_000)


async def _select_adobe_aspect_ratio(page: Any, aspect_ratio: str) -> None:
    trigger = page.get_by_role("button", name=re.compile(r"^(Auto|Wide|Square|Tall)"))
    if await trigger.count() <= 0:
        raise NonPenalizedTaskError("Adobe aspect ratio control was not found", status_code=502)
    await trigger.first.click(timeout=10_000)
    value = "Auto" if aspect_ratio == "auto" else aspect_ratio
    option = page.get_by_test_id(f"ugi-aspect-ratio-item-{value}")
    await option.click(timeout=10_000)


async def _select_adobe_quality(page: Any, quality: str) -> None:
    trigger = page.get_by_role("button", name=re.compile(r"^(Low|Medium|High)"))
    if await trigger.count() <= 0:
        raise NonPenalizedTaskError("Adobe quality control was not found", status_code=502)
    await trigger.first.click(timeout=10_000)
    option = page.locator(f'sp-menu-item[value="{quality}"]')
    await option.click(timeout=10_000)


def _is_likely_image_url(url: str) -> bool:
    value = _one_str(url)
    if not value.startswith(("http://", "https://")):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if host in _TRACKING_IMAGE_HOSTS or host in {"bks.adobe.io", "p13n.adobe.io", "ims-na1.adobelogin.com"}:
        return False
    if Path(path).name.lower() in {"pixel.gif", "rp.gif", "sync.gif", "tracking.gif"}:
        return False
    suffix = Path(path).suffix.lower()
    return suffix in _IMAGE_EXTENSIONS or any(
        hint in path for hint in ("/image/", "/images/", "/asset/", "/assets/", "/rendition/")
    )


def _is_adobe_service_host(host: str) -> bool:
    value = _one_str(host).lower().strip(".")
    if not value or value in _TRACKING_IMAGE_HOSTS:
        return False
    if value.startswith(_ADOBE_NON_GENERATION_HOST_PREFIXES):
        return False
    return (
        value == "adobe.com"
        or value.endswith(".adobe.com")
        or value == "adobe.io"
        or value.endswith(".adobe.io")
        or "firefly" in value
    )


def is_adobe_generation_submission_request(
    *,
    url: str,
    method: str,
    post_data: Optional[str],
    prompt: str,
) -> bool:
    if _one_str(method).upper() not in {"POST", "PUT", "PATCH"}:
        return False
    parsed = urlparse(_one_str(url))
    if not _is_adobe_service_host(parsed.hostname or ""):
        return False
    path = (parsed.path or "").lower()
    body = _one_str(post_data).lower()
    prompt_value = _one_str(prompt).lower()
    if prompt_value and prompt_value in body:
        return True
    if any(hint in body for hint in _ADOBE_GENERATION_BODY_HINTS):
        return True
    return any(hint in path for hint in _ADOBE_GENERATION_PATH_HINTS)


def extract_adobe_image_urls(value: Any) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()

    def walk(current: Any, key_hint: str = "") -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                walk(child, _one_str(key).lower())
            return
        if isinstance(current, list):
            for child in current:
                walk(child, key_hint)
            return
        if not isinstance(current, str):
            return
        candidate = current.strip()
        if not candidate.startswith(("http://", "https://")):
            return
        key_is_media = any(part in key_hint for part in ("image", "asset", "download", "rendition", "url", "uri"))
        if (key_is_media or _is_likely_image_url(candidate)) and _is_likely_image_url(candidate) and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    walk(value)
    return out


async def _large_image_urls(page: Any) -> List[str]:
    try:
        values = await page.evaluate(_LARGE_IMAGE_URLS_JS)
    except Exception:
        return []
    return [value for value in values if isinstance(value, str) and _is_likely_image_url(value)]


def validate_adobe_image_bytes(data: bytes) -> Tuple[int, int, str]:
    if not data:
        raise NonPenalizedTaskError("Adobe returned an empty image file", status_code=502)
    if len(data) > _MAX_GENERATED_IMAGE_BYTES:
        raise NonPenalizedTaskError("Adobe image asset is too large", status_code=502)
    try:
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            image_format = _one_str(image.format).lower()
            image.verify()
    except NonPenalizedTaskError:
        raise
    except Exception as exc:
        raise NonPenalizedTaskError(
            f"Adobe returned an invalid image file: {exc}",
            status_code=502,
        ) from exc
    if width < _MIN_GENERATED_IMAGE_EDGE or height < _MIN_GENERATED_IMAGE_EDGE:
        raise NonPenalizedTaskError(
            f"Adobe returned a tracking/thumbnail image ({width}x{height})",
            status_code=502,
        )
    return int(width), int(height), image_format


def _image_extension(content_type: str, url: str) -> str:
    media_type = _one_str(content_type).split(";", 1)[0].lower()
    by_type = {
        "image/avif": ".avif",
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    if media_type in by_type:
        return by_type[media_type]
    suffix = Path(urlparse(_one_str(url)).path).suffix.lower()
    return suffix if suffix in _IMAGE_EXTENSIONS else ".png"


def _save_image_asset(task_id: str, index: int, data: bytes, extension: str) -> str:
    tid = _one_str(task_id)
    if not _SAFE_TASK_ID_RE.fullmatch(tid):
        raise NonPenalizedTaskError("invalid task_id for Adobe image asset", status_code=500)
    validate_adobe_image_bytes(data)
    ext = extension if extension in _IMAGE_EXTENSIONS else ".png"
    ADOBE_IMAGE2_PUBLIC_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    path = ADOBE_IMAGE2_PUBLIC_ASSET_DIR / f"{tid}-{int(index)}{ext}"
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return f"/public/adobe-image2-assets/{path.name}"


async def _persist_remote_images(
    page: Any,
    task_id: str,
    urls: List[str],
    *,
    preloaded: Optional[Dict[str, Tuple[bytes, str]]] = None,
) -> List[Tuple[str, str]]:
    persisted: List[Tuple[str, str]] = []
    for index, url in enumerate(urls[:4]):
        try:
            cached = (preloaded or {}).get(url)
            if cached is not None:
                data, extension = cached
            else:
                response = await page.context.request.get(url, timeout=60_000, fail_on_status_code=False)
                if not response.ok:
                    raise RuntimeError(f"HTTP {response.status}")
                content_type = _one_str(response.headers.get("content-type"))
                if not content_type.lower().startswith("image/"):
                    raise RuntimeError(f"unexpected content-type {content_type}")
                data = await response.body()
                extension = _image_extension(content_type, url)
            persisted.append((url, _save_image_asset(task_id, index, data, extension)))
        except Exception:
            continue
    return persisted


def _is_adobe_write_request(url: str, method: str) -> bool:
    if _one_str(method).upper() not in {"POST", "PUT", "PATCH"}:
        return False
    try:
        host = urlparse(_one_str(url)).hostname or ""
    except Exception:
        return False
    return _is_adobe_service_host(host)


async def _wait_for_adobe_submission(
    generate: Any,
    submission_event: asyncio.Event,
    *,
    initial_text: str,
    timeout_seconds: float,
) -> Optional[str]:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    normalized_initial = re.sub(r"\s+", " ", _one_str(initial_text)).lower()
    while time.monotonic() < deadline:
        if submission_event.is_set():
            return "request"
        try:
            if await generate.count() <= 0 or not await generate.is_visible():
                return "button_hidden"
            current_text = re.sub(r"\s+", " ", _one_str(await generate.inner_text())).lower()
            if current_text and current_text != normalized_initial:
                return "button_text"
            if not await generate.is_enabled():
                return "button_disabled"
        except Exception:
            return "button_replaced"
        await asyncio.sleep(0.2)
    return None


async def _run_adobe_generation(
    page: Any,
    *,
    prompt: str,
    public_model: str,
    quality: str,
    aspect_ratio: str,
    task_id: str,
    timeout_seconds: float,
    progress_cb: ProgressCB,
) -> Dict[str, Any]:
    await _select_adobe_model(page)
    await _select_adobe_aspect_ratio(page, aspect_ratio)
    await _select_adobe_quality(page, quality)

    prompt_box = page.get_by_role("textbox", name="Describe what you want to generate")
    if await prompt_box.count() <= 0:
        raise NonPenalizedTaskError("Adobe prompt input was not found", status_code=502)
    prompt_editor = prompt_box.first
    await prompt_editor.fill(prompt)
    try:
        await prompt_editor.press("Tab")
    except Exception:
        try:
            await prompt_editor.evaluate("el => el.blur()")
        except Exception:
            pass
    await asyncio.sleep(0.3)
    editor_text = re.sub(r"\s+", " ", _one_str(await prompt_editor.inner_text()))
    expected_text = re.sub(r"\s+", " ", prompt)
    if editor_text != expected_text:
        raise NonPenalizedTaskError(
            "Adobe prompt editor did not retain the requested prompt",
            status_code=502,
            retryable=True,
            stage="prompt",
        )

    before_images = set(await _large_image_urls(page))
    response_images: List[str] = []
    preloaded_images: Dict[str, Tuple[bytes, str]] = {}
    response_seen: set[str] = set()
    submission_event = asyncio.Event()
    adobe_write_event = asyncio.Event()
    activation_started = False

    def on_request(request: Any) -> None:
        try:
            url = _one_str(request.url)
            method = _one_str(request.method)
            if _is_adobe_write_request(url, method):
                adobe_write_event.set()
            post_data = getattr(request, "post_data", None)
            if callable(post_data):
                post_data = post_data()
            if is_adobe_generation_submission_request(
                url=url,
                method=method,
                post_data=post_data,
                prompt=prompt,
            ):
                submission_event.set()
        except Exception:
            return

    async def on_response(response: Any) -> None:
        if not activation_started:
            return
        try:
            content_type = _one_str(response.headers.get("content-type")).lower()
            if content_type.startswith("image/") and _is_likely_image_url(response.url):
                if response.url not in response_seen:
                    data = await response.body()
                    validate_adobe_image_bytes(data)
                    response_seen.add(response.url)
                    response_images.append(response.url)
                    preloaded_images[response.url] = (
                        data,
                        _image_extension(content_type, response.url),
                    )
                return
            if "json" not in content_type:
                return
            host = (urlparse(_one_str(response.url)).hostname or "").lower()
            if "adobe" not in host and "firefly" not in host:
                return
            for url in extract_adobe_image_urls(await response.json()):
                if url not in response_seen:
                    response_seen.add(url)
                    response_images.append(url)
        except Exception:
            return

    page.on("request", on_request)
    page.on("response", on_response)
    submitted = False
    started = time.monotonic()
    first_result_at: Optional[float] = None
    result_urls: List[str] = []
    persisted: List[str] = []
    rejected_urls: set[str] = set()
    try:
        generate = page.get_by_test_id("ugi-generate-button").first
        if await generate.count() <= 0 or not await generate.is_enabled():
            raise NonPenalizedTaskError("Adobe Generate button is not available", status_code=502)
        initial_button_text = _one_str(await generate.inner_text()) or "Generate"
        await progress_cb(12, {"stage": "submit", "provider": "adobe", "model": public_model})
        activation_started = True
        await generate.click(timeout=10_000)
        submit_signal = await _wait_for_adobe_submission(
            generate,
            submission_event,
            initial_text=initial_button_text,
            timeout_seconds=6.0,
        )
        if submit_signal is None and not adobe_write_event.is_set():
            await generate.focus(timeout=5_000)
            await generate.press("Enter", timeout=5_000)
            submit_signal = await _wait_for_adobe_submission(
                generate,
                submission_event,
                initial_text=initial_button_text,
                timeout_seconds=8.0,
            )
        if submit_signal is None:
            raise NonPenalizedTaskError(
                "Adobe Generate activation did not start a generation request",
                status_code=502,
                submitted=False,
                retryable=False,
                stage="submit",
            )
        submitted = True
        await progress_cb(
            18,
            {
                "stage": "submitted",
                "provider": "adobe",
                "model": public_model,
                "confirmation": submit_signal,
            },
        )

        deadline = started + max(30.0, float(timeout_seconds or 600.0) - 10.0)
        while time.monotonic() < deadline:
            new_dom = [
                url
                for url in await _large_image_urls(page)
                if url not in before_images and url not in rejected_urls
            ]
            combined = []
            for url in [*new_dom, *response_images]:
                if url not in rejected_urls and url not in combined:
                    combined.append(url)
            if combined:
                result_urls = combined
                if first_result_at is None:
                    first_result_at = time.monotonic()
                if time.monotonic() - first_result_at >= 3.0:
                    saved = await _persist_remote_images(
                        page,
                        task_id,
                        result_urls,
                        preloaded=preloaded_images,
                    )
                    if saved:
                        result_urls = [source for source, _ in saved]
                        persisted = [public_url for _, public_url in saved]
                        break
                    rejected_urls.update(result_urls)
                    result_urls = []
                    first_result_at = None
            elapsed = time.monotonic() - started
            pct = min(92, 15 + int(75 * elapsed / max(30.0, deadline - started)))
            await progress_cb(pct, {"stage": "generating", "provider": "adobe", "model": public_model})
            await asyncio.sleep(1.0)
    except NonPenalizedTaskError:
        raise
    except Exception as exc:
        raise NonPenalizedTaskError(
            f"Adobe GPT Image 2 generation failed: {exc}",
            status_code=502,
            submitted=submitted,
            retryable=not submitted,
            stage="generation" if submitted else "submit",
        ) from exc
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    if not result_urls or not persisted:
        raise NonPenalizedTaskError(
            "Adobe GPT Image 2 finished without a downloadable image URL",
            status_code=504,
            submitted=True,
            retryable=False,
            stage="result",
        )
    await progress_cb(98, {"stage": "result", "provider": "adobe", "count": len(persisted)})
    return {
        "type": "adobe_image2",
        "provider": "adobe",
        "workflow_kind": "image",
        "model": public_model,
        "provider_model": ADOBE_IMAGE2_PROVIDER_MODEL,
        "quality": quality,
        "aspect_ratio": aspect_ratio,
        "image_url": persisted[0],
        "url": persisted[0],
        "urls": persisted,
        "source_urls": result_urls,
    }


async def persist_adobe_account_info(db: Any, mapping_id: Optional[int], info: Dict[str, Any]) -> None:
    if db is None or int(mapping_id or 0) <= 0:
        return
    kwargs: Dict[str, Any] = {
        "mapping_id": int(mapping_id),
        "remaining_quota": int(info.get("remaining_quota") or 0),
        "sora_remaining_count": int(info.get("remaining_quota") or 0),
    }
    plan = _one_str(info.get("plan_title"))
    if plan:
        kwargs["sora_plan_title"] = plan
    reset = _one_str(info.get("next_reset"))
    if reset:
        kwargs["sora_subscription_end"] = reset
    await db.update_task_type_window(**kwargs)
    platform_account = _one_str(info.get("platform_account"))
    if not platform_account:
        return
    try:
        context = await db.get_task_type_window_context(int(mapping_id))
        window = await db.get_window(int((context or {}).get("window_pk") or 0))
        if window is None:
            return
        await db.update_window_platform_binding(
            space_pk=int(window.space_pk),
            window_key=_one_str(window.window_key),
            platform_account_id=None,
            platform_account=platform_account,
            platform_url=DEFAULT_ADOBE_IMAGE2_TARGET,
        )
    except Exception:
        return


async def adobe_image2_workflow(
    payload: Dict[str, Any],
    progress_cb: ProgressCB,
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    timeout_seconds: float,
    task_id: Optional[str] = None,
    default_target_url: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
    db: Any = None,
    task_type_window_id: Optional[int] = None,
    **_: Any,
) -> Dict[str, Any]:
    body = dict(payload or {})
    prompt = _one_str(body.get("prompt") or body.get("text") or body.get("input"))
    dry_run = bool(body.get("dry_run") or body.get("skip_submit"))
    if not prompt and not dry_run:
        raise NonPenalizedTaskError("payload.prompt cannot be empty", status_code=400, retryable=False)
    public_model, quality = resolve_adobe_image2_quality(body)
    aspect_ratio = resolve_adobe_image2_aspect_ratio(body)
    target = _one_str(body.get("adobe_url") or body.get("target_url") or default_target_url) or DEFAULT_ADOBE_IMAGE2_TARGET
    resolved_task_id = _one_str(task_id) or uuid.uuid4().hex

    session = get_or_create_playwright_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    await progress_cb(1, {"stage": "init", "provider": "adobe", "model": public_model})
    async with session.driver_lock:
        await session.ensure_open(
            args=[target],
            force_open=False,
            headless=headless,
            require_page=False,
            pure_mode=pure_mode,
        )
        page = await _find_or_open_adobe_page(session, target)
        await _wait_for_adobe_studio(page, min(timeout_seconds, 90.0))
        account = await _probe_adobe_account(page)
        await persist_adobe_account_info(db, task_type_window_id, account)
        await progress_cb(
            7,
            {
                "stage": "account",
                "provider": "adobe",
                "remaining_quota": int(account.get("remaining_quota") or 0),
            },
        )
        if dry_run:
            return {
                "type": "adobe_image2_probe",
                "provider": "adobe",
                "workflow_kind": "image",
                "dry_run": True,
                "model": public_model,
                "provider_model": ADOBE_IMAGE2_PROVIDER_MODEL,
                "quality": quality,
                "aspect_ratio": aspect_ratio,
                "remaining_quota": int(account.get("remaining_quota") or 0),
                "provisioned_quota": int(account.get("provisioned_quota") or 0),
                "plan_title": account.get("plan_title"),
            }

        result = await _run_adobe_generation(
            page,
            prompt=prompt,
            public_model=public_model,
            quality=quality,
            aspect_ratio=aspect_ratio,
            task_id=resolved_task_id,
            timeout_seconds=timeout_seconds,
            progress_cb=progress_cb,
        )
        try:
            refreshed = await _probe_adobe_account(page)
            await persist_adobe_account_info(db, task_type_window_id, refreshed)
            result["remaining_quota"] = int(refreshed.get("remaining_quota") or 0)
        except Exception:
            pass
        return result
