"""Adobe Firefly GPT Image 2 workflow through a logged-in fingerprint browser."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import httpx

from ..core.paths import DATA_DIR
from .playwright_broswer_context import get_or_create_ctx as get_or_create_playwright_ctx
from .task_executor_types import NonPenalizedTaskError, ProgressCB


DEFAULT_ADOBE_IMAGE2_TARGET = "https://firefly.adobe.com/studio"
ADOBE_IMAGE2_PROVIDER_MODEL = "gpt-image@2"
ADOBE_IMAGE2_FEATURE_ID = "firefly_3p:external:gpt_image_2"
ADOBE_IMAGE2_PUBLIC_ASSET_DIR = DATA_DIR / "adobe_image2_assets"
ADOBE_IMAGE2_SUBMIT_URL = "https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async"
ADOBE_IMAGE2_UPLOAD_URL = "https://firefly-3p.ff.adobe.io/v2/storage/image"
ADOBE_IMAGE2_API_KEY = "clio-playground-web"
ADOBE_IMAGE2_MAX_REFERENCE_IMAGES = 4
ADOBE_IMAGE2_MAX_REFERENCE_BYTES = 30 * 1024 * 1024

ADOBE_IMAGE2_PUBLIC_MODEL_ALIASES: Dict[str, str] = {
    "adobe-gpt-image2": "medium",
    "adobe-gpt-image2-low": "low",
    "adobe-gpt-image2-medium": "medium",
    "adobe-gpt-image2-high": "high",
}

_ALLOWED_QUALITIES = {"low", "medium", "high"}
_ADOBE_IMAGE2_DETAIL_LEVELS = {"low": 1, "medium": 3, "high": 5}
_ADOBE_IMAGE2_SIZES_BY_ASPECT_RATIO: Dict[str, Tuple[int, int]] = {
    "auto": (2048, 2048),
    "8:1": (6144, 768),
    "4:1": (4096, 1024),
    "21:9": (3024, 1296),
    "16:9": (2560, 1440),
    "5:4": (2240, 1792),
    "4:3": (2304, 1728),
    "3:2": (2496, 1664),
    "1:1": (2048, 2048),
    "4:5": (1792, 2240),
    "3:4": (1728, 2304),
    "2:3": (1664, 2496),
    "9:16": (1440, 2560),
    "1:4": (1024, 4096),
    "1:8": (768, 6144),
}
_ALLOWED_ASPECT_RATIOS = frozenset(_ADOBE_IMAGE2_SIZES_BY_ASPECT_RATIO)
_ADOBE_IMAGE2_ASPECT_RATIO_ERROR = (
    "Adobe GPT Image 2 aspect_ratio must be auto, 8:1, 4:1, 21:9, 16:9, "
    "5:4, 4:3, 3:2, 1:1, 4:5, 3:4, 2:3, 9:16, 1:4, or 1:8"
)
_SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_IMAGE_EXTENSIONS = {".avif", ".gif", ".jpg", ".jpeg", ".png", ".webp"}
_MIN_GENERATED_IMAGE_EDGE = 256
_MAX_GENERATED_IMAGE_BYTES = 50 * 1024 * 1024
_MIN_REFERENCE_IMAGE_EDGE = 16
_MAX_REFERENCE_IMAGE_EDGE = 16_384
_MAX_REFERENCE_IMAGE_PIXELS = 100_000_000
_REFERENCE_IMAGE_SINGLE_KEYS = (
    "image",
    "image_url",
    "imageUrl",
    "input_image",
    "inputImage",
    "input_image_url",
    "inputImageUrl",
    "reference_image",
    "referenceImage",
    "reference_image_url",
    "referenceImageUrl",
    "first_image_url",
    "firstImageUrl",
)
_REFERENCE_IMAGE_LIST_KEYS = (
    "images",
    "image_urls",
    "imageUrls",
    "input_images",
    "inputImages",
    "reference_images",
    "referenceImages",
    "reference_image_urls",
    "referenceImageUrls",
)
_REFERENCE_MASK_KEYS = (
    "mask",
    "mask_url",
    "maskUrl",
    "mask_image",
    "maskImage",
    "mask_image_url",
    "maskImageUrl",
)
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


def _reference_input_error(message: str, *, stage: str = "reference_input") -> NonPenalizedTaskError:
    return NonPenalizedTaskError(
        message,
        status_code=400,
        retryable=False,
        stage=stage,
    )


def _one_reference_source(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "image_url", "imageUrl", "src", "data"):
            candidate = _one_reference_source(value.get(key))
            if candidate:
                return candidate
    return ""


def resolve_adobe_image2_reference_sources(payload: Dict[str, Any]) -> List[str]:
    """Collect common JSON image aliases without changing their order."""
    body = payload or {}
    for key in _REFERENCE_MASK_KEYS:
        if key in body and body.get(key) not in (None, "", []):
            raise _reference_input_error("Adobe GPT Image 2 masks are not supported")

    sources: List[str] = []

    def add(value: Any, field: str) -> None:
        if value in (None, ""):
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item, field)
            return
        source = _one_reference_source(value)
        if not source:
            raise _reference_input_error(f"Adobe GPT Image 2 field {field} contains an invalid image reference")
        if source not in sources:
            sources.append(source)

    for key in _REFERENCE_IMAGE_SINGLE_KEYS:
        if key in body:
            add(body.get(key), key)
    for key in _REFERENCE_IMAGE_LIST_KEYS:
        if key in body:
            add(body.get(key), key)

    if len(sources) > ADOBE_IMAGE2_MAX_REFERENCE_IMAGES:
        raise _reference_input_error(
            f"Adobe GPT Image 2 accepts at most {ADOBE_IMAGE2_MAX_REFERENCE_IMAGES} reference images"
        )
    return sources


def validate_adobe_reference_image_bytes(
    data: bytes,
    declared_content_type: str = "",
) -> Tuple[int, int, str]:
    if not data:
        raise _reference_input_error("Adobe GPT Image 2 reference image is empty")
    if len(data) > ADOBE_IMAGE2_MAX_REFERENCE_BYTES:
        raise _reference_input_error(
            f"Adobe GPT Image 2 reference image exceeds {ADOBE_IMAGE2_MAX_REFERENCE_BYTES // (1024 * 1024)} MB"
        )

    declared = _one_str(declared_content_type).split(";", 1)[0].lower()
    if declared and not declared.startswith("image/") and declared != "application/octet-stream":
        raise _reference_input_error("Adobe GPT Image 2 reference URL did not return an image MIME type")
    try:
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            image_format = _one_str(image.format).upper()
            image.verify()
    except NonPenalizedTaskError:
        raise
    except Exception as exc:
        raise _reference_input_error(f"Adobe GPT Image 2 reference image is invalid: {exc}") from exc

    format_to_mime = {
        "AVIF": "image/avif",
        "GIF": "image/gif",
        "JPEG": "image/jpeg",
        "JPG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }
    actual_mime = format_to_mime.get(image_format, "")
    if not actual_mime:
        raise _reference_input_error(
            "Adobe GPT Image 2 reference image must be AVIF, GIF, JPEG, PNG, or WebP"
        )
    normalized_declared = {
        "image/jpg": "image/jpeg",
        "image/pjpeg": "image/jpeg",
        "image/x-png": "image/png",
    }.get(declared, declared)
    if normalized_declared.startswith("image/") and normalized_declared != actual_mime:
        raise _reference_input_error("Adobe GPT Image 2 reference image MIME type does not match its content")
    if (
        width < _MIN_REFERENCE_IMAGE_EDGE
        or height < _MIN_REFERENCE_IMAGE_EDGE
        or width > _MAX_REFERENCE_IMAGE_EDGE
        or height > _MAX_REFERENCE_IMAGE_EDGE
        or width * height > _MAX_REFERENCE_IMAGE_PIXELS
    ):
        raise _reference_input_error(
            f"Adobe GPT Image 2 reference image dimensions are unsupported ({width}x{height})"
        )
    return int(width), int(height), actual_mime


def decode_adobe_reference_data_url(source: str) -> Tuple[bytes, str]:
    value = _one_str(source)
    if not value.lower().startswith("data:") or "," not in value:
        raise _reference_input_error("Adobe GPT Image 2 reference data URL is invalid")
    metadata, encoded = value.split(",", 1)
    parts = metadata[5:].split(";")
    declared_mime = _one_str(parts[0]).lower()
    if not declared_mime.startswith("image/"):
        raise _reference_input_error("Adobe GPT Image 2 data URL must use an image MIME type")
    if "base64" not in {part.strip().lower() for part in parts[1:]}:
        raise _reference_input_error("Adobe GPT Image 2 data URL must be base64 encoded")
    compact = re.sub(r"\s+", "", unquote(encoded))
    max_encoded_length = ((ADOBE_IMAGE2_MAX_REFERENCE_BYTES + 2) // 3) * 4 + 4
    if len(compact) > max_encoded_length:
        raise _reference_input_error(
            f"Adobe GPT Image 2 reference image exceeds {ADOBE_IMAGE2_MAX_REFERENCE_BYTES // (1024 * 1024)} MB"
        )
    try:
        data = base64.b64decode(compact, validate=True)
    except Exception as exc:
        raise _reference_input_error("Adobe GPT Image 2 reference data URL contains invalid base64") from exc
    _, _, actual_mime = validate_adobe_reference_image_bytes(data, declared_mime)
    return data, actual_mime


def _local_reference_path(source: str) -> Optional[Path]:
    parsed = urlparse(_one_str(source))
    host = (parsed.hostname or "").lower()
    if parsed.scheme and (parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost", "::1"}):
        return None
    path = unquote(parsed.path or "")
    roots = {
        "/public/adobe-image2-assets/": ADOBE_IMAGE2_PUBLIC_ASSET_DIR,
        "/public/gpt-assets/": DATA_DIR / "gpt_assets",
    }
    for prefix, root in roots.items():
        if not path.startswith(prefix):
            continue
        filename = path[len(prefix) :]
        if not filename or Path(filename).name != filename:
            raise _reference_input_error("Adobe GPT Image 2 local reference path is invalid")
        candidate = (root / filename).resolve()
        resolved_root = root.resolve()
        if candidate.parent != resolved_root:
            raise _reference_input_error("Adobe GPT Image 2 local reference path is invalid")
        return candidate
    return None


def _is_public_reference_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(address.is_global)


async def _validate_adobe_reference_remote_url(url: str) -> None:
    try:
        parsed = urlparse(_one_str(url))
    except Exception as exc:
        raise _reference_input_error("Adobe GPT Image 2 reference URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise _reference_input_error("Adobe GPT Image 2 reference URL must be an HTTP(S) URL without credentials")
    host = parsed.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise _reference_input_error("Adobe GPT Image 2 reference URL cannot target a private network")
        return
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        resolved = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except (OSError, ValueError) as exc:
        raise _reference_input_error("Adobe GPT Image 2 reference URL host could not be resolved") from exc
    addresses = {item[4][0].split("%", 1)[0] for item in resolved if item and item[4]}
    if not addresses or any(not _is_public_reference_ip(address) for address in addresses):
        raise _reference_input_error("Adobe GPT Image 2 reference URL cannot target a private network")


async def _download_adobe_reference_image(source: str) -> Tuple[bytes, str]:
    current = _one_str(source)
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=True) as client:
        for _ in range(4):
            await _validate_adobe_reference_remote_url(current)
            try:
                async with client.stream(
                    "GET",
                    current,
                    headers={"Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.1"},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = _one_str(response.headers.get("location"))
                        if not location:
                            raise _reference_input_error("Adobe GPT Image 2 reference URL redirect is invalid")
                        current = urljoin(current, location)
                        continue
                    if response.status_code < 200 or response.status_code >= 300:
                        raise _reference_input_error(
                            f"Adobe GPT Image 2 reference URL returned HTTP {response.status_code}",
                            stage="reference_download",
                        )
                    content_length = _one_str(response.headers.get("content-length"))
                    if content_length.isdigit() and int(content_length) > ADOBE_IMAGE2_MAX_REFERENCE_BYTES:
                        raise _reference_input_error(
                            f"Adobe GPT Image 2 reference image exceeds {ADOBE_IMAGE2_MAX_REFERENCE_BYTES // (1024 * 1024)} MB"
                        )
                    content_type = _one_str(response.headers.get("content-type"))
                    chunks: List[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > ADOBE_IMAGE2_MAX_REFERENCE_BYTES:
                            raise _reference_input_error(
                                f"Adobe GPT Image 2 reference image exceeds {ADOBE_IMAGE2_MAX_REFERENCE_BYTES // (1024 * 1024)} MB"
                            )
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    _, _, actual_mime = validate_adobe_reference_image_bytes(data, content_type)
                    return data, actual_mime
            except NonPenalizedTaskError:
                raise
            except httpx.HTTPError as exc:
                raise _reference_input_error(
                    f"Adobe GPT Image 2 reference image download failed: {exc}",
                    stage="reference_download",
                ) from exc
    raise _reference_input_error("Adobe GPT Image 2 reference URL redirected too many times")


async def _load_adobe_reference_image(source: str) -> Tuple[bytes, str]:
    value = _one_str(source)
    if value.lower().startswith("data:"):
        return decode_adobe_reference_data_url(value)
    local_path = _local_reference_path(value)
    if local_path is not None:
        try:
            if not local_path.is_file():
                raise FileNotFoundError(str(local_path))
            if local_path.stat().st_size > ADOBE_IMAGE2_MAX_REFERENCE_BYTES:
                raise _reference_input_error(
                    f"Adobe GPT Image 2 reference image exceeds {ADOBE_IMAGE2_MAX_REFERENCE_BYTES // (1024 * 1024)} MB"
                )
            data = local_path.read_bytes()
        except NonPenalizedTaskError:
            raise
        except OSError as exc:
            raise _reference_input_error("Adobe GPT Image 2 local reference image was not found") from exc
        _, _, actual_mime = validate_adobe_reference_image_bytes(data)
        return data, actual_mime
    return await _download_adobe_reference_image(value)


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
            _ADOBE_IMAGE2_ASPECT_RATIO_ERROR,
            status_code=400,
            retryable=False,
        )
    return ratio


def build_adobe_image2_payload(
    *,
    prompt: str,
    quality: str,
    aspect_ratio: str,
    seed: Optional[int] = None,
    reference_blob_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build the GPT Image 2 request used by Adobe Firefly Studio."""
    prompt_value = _one_str(prompt)
    if not prompt_value:
        raise NonPenalizedTaskError("payload.prompt cannot be empty", status_code=400, retryable=False)
    if quality not in _ADOBE_IMAGE2_DETAIL_LEVELS:
        raise NonPenalizedTaskError(
            "Adobe GPT Image 2 quality must be low, medium, or high",
            status_code=400,
            retryable=False,
        )
    if aspect_ratio not in _ADOBE_IMAGE2_SIZES_BY_ASPECT_RATIO:
        raise NonPenalizedTaskError(
            _ADOBE_IMAGE2_ASPECT_RATIO_ERROR,
            status_code=400,
            retryable=False,
        )
    width, height = _ADOBE_IMAGE2_SIZES_BY_ASPECT_RATIO[aspect_ratio]
    resolved_seed = int(seed) if seed is not None else secrets.randbelow(1_000_001)
    blob_ids: List[str] = []
    for value in reference_blob_ids or []:
        blob_id = _one_str(value)
        if blob_id and blob_id not in blob_ids:
            blob_ids.append(blob_id)
    return {
        "modelId": "gpt-image",
        "modelVersion": "2",
        "n": 1,
        "prompt": prompt_value,
        "seeds": [resolved_seed],
        "output": {"storeInputs": True},
        "referenceBlobs": [{"id": blob_id, "usage": "subject"} for blob_id in blob_ids],
        "generationMetadata": {
            "module": "image2image" if blob_ids else "text2image",
            "submodule": "ff-image-generate",
        },
        "modelSpecificPayload": {"size": f"{width}x{height}"},
        "outputResolution": "2K",
        "generationSettings": {"detailLevel": _ADOBE_IMAGE2_DETAIL_LEVELS[quality]},
        "size": {"width": width, "height": height},
    }


def _decode_adobe_jwt_claims(token: str) -> Dict[str, Any]:
    parts = _one_str(token).split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    try:
        encoded = parts[1] + ("=" * (-len(parts[1]) % 4))
        value = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def build_adobe_image2_nonce(token: str, prompt: str) -> str:
    claims = _decode_adobe_jwt_claims(token)
    user_id = _one_str(claims.get("user_id") or claims.get("aa_id") or claims.get("sub"))
    prompt_value = _one_str(prompt)
    if not user_id or not prompt_value:
        return ""
    value = f"{user_id}-{prompt_value[:256]}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def extract_adobe_poll_url(headers: Any, payload: Any) -> str:
    normalized_headers = {
        _one_str(key).lower(): _one_str(value)
        for key, value in (headers.items() if isinstance(headers, dict) else [])
    }
    override = normalized_headers.get("x-override-status-link", "")
    if override:
        return override
    body = payload if isinstance(payload, dict) else {}
    links = body.get("links") if isinstance(body.get("links"), dict) else {}
    result = links.get("result")
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        return _one_str(result.get("href"))
    return ""


def _adobe_progress_value(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.rstrip("%"))
        except ValueError:
            return None
    elif isinstance(value, dict):
        for key in ("progress", "percentage", "percent", "task_progress", "taskProgress", "value"):
            nested = _adobe_progress_value(value.get(key))
            if nested is not None:
                return nested
        return None
    else:
        return None
    if parsed <= 1:
        parsed *= 100
    return max(0.0, min(100.0, parsed))


def parse_adobe_poll_result(payload: Any, headers: Any = None) -> Dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    normalized_headers = {
        _one_str(key).lower(): _one_str(value)
        for key, value in (headers.items() if isinstance(headers, dict) else [])
    }
    status = _one_str(body.get("status") or normalized_headers.get("x-task-status")).upper()
    progress = _adobe_progress_value(
        body.get("progress")
        or body.get("percentage")
        or body.get("task_progress")
        or body.get("taskProgress")
        or body.get("task")
        or body.get("result")
        or body.get("meta")
        or normalized_headers.get("x-task-progress")
    )
    urls: List[str] = []
    outputs = body.get("outputs") if isinstance(body.get("outputs"), list) else []
    for output in outputs:
        item = output if isinstance(output, dict) else {}
        image = item.get("image") if isinstance(item.get("image"), dict) else {}
        url = _one_str(image.get("presignedUrl") or image.get("url") or item.get("presignedUrl"))
        if url and url not in urls:
            urls.append(url)
    if urls:
        return {"status": "SUCCEEDED", "progress": 100.0, "urls": urls}
    if status in {"FAILED", "CANCELLED", "ERROR"}:
        return {"status": "FAILED", "progress": progress, "urls": []}
    return {"status": "IN_PROGRESS", "progress": progress, "urls": []}


def _is_allowed_adobe_poll_url(url: str) -> bool:
    try:
        parsed = urlparse(_one_str(url))
    except Exception:
        return False
    host = (parsed.hostname or "").lower().strip(".")
    is_adobe_host = host in {"adobe.com", "adobe.io"} or host.endswith((".adobe.com", ".adobe.io"))
    return parsed.scheme == "https" and is_adobe_host


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
            if not candidate.is_closed() and _is_adobe_page_url(candidate.url) and "/studio" in candidate.url:
                page = candidate
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
            "Adobe Firefly account menu was not found",
            status_code=502,
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
    except Exception as menu_error:
        if isinstance(menu_error, NonPenalizedTaskError) and int(menu_error.status_code or 0) == 401:
            raise
        quota = await _capture_quota_from_reload(page)
        return {
            **quota,
            "platform_account": None,
            "plan_title": "Adobe Firefly",
            "next_reset": None,
        }


def _is_complete_sherlock_token(value: str) -> bool:
    candidate = unquote(_one_str(value))
    if not candidate:
        return False
    try:
        encoded = candidate + ("=" * (-len(candidate) % 4))
        parsed = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
    except Exception:
        return False
    return isinstance(parsed, dict) and all(_one_str(parsed.get(key)) for key in ("sid", "ark", "bfp", "ftr"))


async def _read_sherlock_token(page: Any, timeout_seconds: float = 10.0) -> str:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds or 10.0))
    while time.monotonic() < deadline:
        try:
            cookies = list(await page.context.cookies() or [])
        except Exception:
            cookies = []
        for cookie in cookies:
            if not isinstance(cookie, dict) or _one_str(cookie.get("name")) != "sherlockToken":
                continue
            token = unquote(_one_str(cookie.get("value")))
            if _is_complete_sherlock_token(token):
                return token
        await asyncio.sleep(0.25)
    raise NonPenalizedTaskError(
        "Adobe Sherlock browser session is unavailable; reload the signed-in Firefly window",
        status_code=503,
        retryable=True,
        stage="auth",
    )


async def _capture_adobe_credentials(page: Any, timeout_seconds: float = 30.0) -> Dict[str, str]:
    """Capture ephemeral request credentials from the currently signed-in window."""
    loop = asyncio.get_running_loop()
    bearer_future: asyncio.Future[str] = loop.create_future()

    def on_request(request: Any) -> None:
        if bearer_future.done():
            return
        try:
            url = _one_str(getattr(request, "url", ""))
            if "bks.adobe.io/v1/feature-auth-profile" not in url:
                return
            headers = getattr(request, "headers", None) or {}
            authorization = _one_str(headers.get("authorization") or headers.get("Authorization"))
            if not authorization.lower().startswith("bearer "):
                return
            token = authorization.split(" ", 1)[1].strip()
            if token:
                bearer_future.set_result(token)
        except Exception:
            return

    page.on("request", on_request)
    try:
        await page.reload(wait_until="domcontentloaded", timeout=90_000)
        try:
            token = await asyncio.wait_for(
                bearer_future,
                timeout=max(5.0, float(timeout_seconds or 30.0)),
            )
        except asyncio.TimeoutError as exc:
            raise NonPenalizedTaskError(
                "Adobe authorization capture timed out; confirm the Firefly window is signed in",
                status_code=401,
                retryable=False,
                stage="auth",
            ) from exc
        sherlock_token = await _read_sherlock_token(page, timeout_seconds=min(10.0, timeout_seconds))
        return {"token": token, "sherlock_token": sherlock_token}
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass


async def _adobe_browser_fetch_json(
    page: Any,
    *,
    url: str,
    headers: Dict[str, str],
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
    timeout_ms: int = 60_000,
) -> Dict[str, Any]:
    result = await page.evaluate(
        """async (args) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), args.timeoutMs);
          try {
            const init = {
              method: args.method,
              headers: args.headers,
              credentials: 'include',
              signal: controller.signal,
            };
            if (args.body !== null) init.body = JSON.stringify(args.body);
            const response = await fetch(args.url, init);
            const text = await response.text();
            let data = null;
            try { data = text ? JSON.parse(text) : null; }
            catch (_) { data = {raw: text.slice(0, 2000)}; }
            return {
              status: response.status,
              data,
              headers: {
                'x-override-status-link': response.headers.get('x-override-status-link') || '',
                'x-task-status': response.headers.get('x-task-status') || '',
                'x-task-progress': response.headers.get('x-task-progress') || '',
                'retry-after': response.headers.get('retry-after') || '',
              },
            };
          } catch (error) {
            return {status: 0, data: null, headers: {}, error: String(error)};
          } finally {
            clearTimeout(timer);
          }
        }""",
        {
            "url": url,
            "method": _one_str(method).upper() or "GET",
            "headers": headers,
            "body": body,
            "timeoutMs": max(1_000, int(timeout_ms)),
        },
    )
    return result if isinstance(result, dict) else {"status": 0, "data": None, "headers": {}}


def extract_adobe_upload_blob_id(payload: Any) -> str:
    body = payload if isinstance(payload, dict) else {}
    images = body.get("images") if isinstance(body.get("images"), list) else []
    if images and isinstance(images[0], dict):
        blob_id = _one_str(images[0].get("id"))
        if blob_id:
            return blob_id
    return _one_str(body.get("id"))


async def _adobe_browser_upload_image(
    page: Any,
    *,
    token: str,
    data: bytes,
    content_type: str,
    timeout_ms: int = 60_000,
) -> str:
    result = await page.evaluate(
        """async (args) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), args.timeoutMs);
          try {
            const binary = atob(args.base64);
            const bytes = new Uint8Array(binary.length);
            for (let index = 0; index < binary.length; index += 1) {
              bytes[index] = binary.charCodeAt(index);
            }
            const response = await fetch(args.url, {
              method: 'POST',
              headers: args.headers,
              credentials: 'include',
              body: bytes,
              signal: controller.signal,
            });
            const text = await response.text();
            let payload = null;
            try { payload = text ? JSON.parse(text) : null; }
            catch (_) { payload = {raw: text.slice(0, 2000)}; }
            return {status: response.status, data: payload};
          } catch (error) {
            return {status: 0, data: null, error: String(error)};
          } finally {
            clearTimeout(timer);
          }
        }""",
        {
            "url": ADOBE_IMAGE2_UPLOAD_URL,
            "headers": {
                "Authorization": f"Bearer {_one_str(token)}",
                "x-api-key": ADOBE_IMAGE2_API_KEY,
                "Content-Type": _one_str(content_type) or "application/octet-stream",
                "Accept": "application/json",
            },
            "base64": base64.b64encode(data).decode("ascii"),
            "timeoutMs": max(1_000, int(timeout_ms)),
        },
    )
    response = result if isinstance(result, dict) else {"status": 0, "data": None}
    status = int(response.get("status") or 0)
    if status < 200 or status >= 300:
        raise _adobe_api_error(
            stage="reference_upload",
            status=status,
            payload=response.get("data"),
            submitted=False,
        )
    blob_id = extract_adobe_upload_blob_id(response.get("data"))
    if not blob_id:
        raise NonPenalizedTaskError(
            "Adobe reference image upload returned no blob ID",
            status_code=502,
            submitted=False,
            retryable=False,
            stage="reference_upload",
        )
    return blob_id


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
    trigger = page.get_by_role(
        "button",
        name=re.compile(
            r"^(Auto|Cinematic banner|Panorama|Ultra Wide|Widescreen|Classic|Landscape|"
            r"Wide|Square|Standard|Portrait|Tall|Vertical banner|Vertical strip|Vertical)"
        ),
    )
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
                if not content_type.lower().startswith("image/") and not content_type.lower().startswith(
                    "application/octet-stream"
                ):
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


async def _run_adobe_ui_generation(
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
        elif submit_signal is None:
            # A write request may precede the recognizable generation request
            # or UI transition. Wait once more without activating the button
            # again so a slow submission cannot be duplicated.
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


def _adobe_api_error(
    *,
    stage: str,
    status: int,
    payload: Any,
    submitted: bool,
) -> NonPenalizedTaskError:
    body = payload if isinstance(payload, dict) else {}
    body_text = " ".join(
        _one_str(body.get(key)) for key in ("error_code", "code", "message", "error") if _one_str(body.get(key))
    ).lower()
    if status in {401, 403}:
        return NonPenalizedTaskError(
            "Adobe account authorization failed; sign in to Firefly again",
            status_code=401,
            submitted=submitted,
            retryable=False,
            stage=stage,
        )
    if status == 402 or any(word in body_text for word in ("quota", "credit", "balance", "insufficient")):
        return NonPenalizedTaskError(
            "Adobe Firefly credits are exhausted",
            status_code=429,
            submitted=submitted,
            retryable=False,
            stage=stage,
        )
    if status == 429:
        return NonPenalizedTaskError(
            "Adobe generation rate limit reached",
            status_code=429,
            submitted=submitted,
            retryable=not submitted,
            stage=stage,
        )
    retryable = not submitted and (status == 0 or status in {408, 451} or status >= 500)
    return NonPenalizedTaskError(
        f"Adobe generation API returned HTTP {status or 'network error'}",
        status_code=503 if retryable else 502,
        submitted=submitted,
        retryable=retryable,
        stage=stage,
    )


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
    reference_sources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    references = list(reference_sources or [])
    await progress_cb(9, {"stage": "auth", "provider": "adobe", "model": public_model})
    credentials = await _capture_adobe_credentials(
        page,
        timeout_seconds=min(30.0, max(10.0, float(timeout_seconds or 600.0) / 4)),
    )
    token = credentials["token"]
    nonce = build_adobe_image2_nonce(token, prompt)
    if not nonce:
        raise NonPenalizedTaskError(
            "Adobe authorization token is invalid for GPT Image 2",
            status_code=401,
            retryable=False,
            stage="auth",
        )

    reference_blob_ids: List[str] = []
    for index, source in enumerate(references):
        await progress_cb(
            min(13, 10 + index),
            {
                "stage": "reference_download",
                "provider": "adobe",
                "model": public_model,
                "reference_index": index,
                "reference_count": len(references),
            },
        )
        image_data, image_mime = await _load_adobe_reference_image(source)
        await progress_cb(
            min(14, 11 + index),
            {
                "stage": "reference_upload",
                "provider": "adobe",
                "model": public_model,
                "reference_index": index,
                "reference_count": len(references),
            },
        )
        reference_blob_ids.append(
            await _adobe_browser_upload_image(
                page,
                token=token,
                data=image_data,
                content_type=image_mime,
            )
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": ADOBE_IMAGE2_API_KEY,
        "x-arp-session-id": credentials["sherlock_token"],
        "x-nonce": nonce,
        "Content-Type": "application/json",
        "Accept": "*/*",
    }
    request_payload = build_adobe_image2_payload(
        prompt=prompt,
        quality=quality,
        aspect_ratio=aspect_ratio,
        reference_blob_ids=reference_blob_ids,
    )
    submitted_progress = 22 if reference_blob_ids else 18
    await progress_cb(
        16 if reference_blob_ids else 12,
        {
            "stage": "submit",
            "provider": "adobe",
            "model": public_model,
            "generation_mode": "image2image" if reference_blob_ids else "text2image",
            "reference_count": len(reference_blob_ids),
        },
    )
    submit_response = await _adobe_browser_fetch_json(
        page,
        url=ADOBE_IMAGE2_SUBMIT_URL,
        headers=headers,
        method="POST",
        body=request_payload,
        timeout_ms=60_000,
    )
    submit_status = int(submit_response.get("status") or 0)
    if submit_status == 0:
        # The browser can lose the response after the POST has left the process.
        # Treat this as submitted so the task scheduler cannot charge twice.
        raise _adobe_api_error(
            stage="submit",
            status=0,
            payload=submit_response.get("data"),
            submitted=True,
        )
    if submit_status < 200 or submit_status >= 300:
        raise _adobe_api_error(
            stage="submit",
            status=submit_status,
            payload=submit_response.get("data"),
            submitted=False,
        )

    poll_url = extract_adobe_poll_url(
        submit_response.get("headers"),
        submit_response.get("data"),
    )
    if not poll_url:
        raise NonPenalizedTaskError(
            "Adobe accepted the generation but returned no polling URL",
            status_code=502,
            submitted=True,
            retryable=False,
            stage="submit",
        )
    if not _is_allowed_adobe_poll_url(poll_url):
        raise NonPenalizedTaskError(
            "Adobe returned an untrusted polling URL",
            status_code=502,
            submitted=True,
            retryable=False,
            stage="submit",
        )

    await progress_cb(
        submitted_progress,
        {"stage": "submitted", "provider": "adobe", "model": public_model},
    )
    poll_headers = {"Authorization": f"Bearer {token}", "Accept": "*/*"}
    started = time.monotonic()
    deadline = started + max(30.0, float(timeout_seconds or 600.0) - 10.0)
    result_urls: List[str] = []
    while time.monotonic() < deadline:
        poll_response = await _adobe_browser_fetch_json(
            page,
            url=poll_url,
            headers=poll_headers,
            timeout_ms=30_000,
        )
        poll_status = int(poll_response.get("status") or 0)
        if poll_status == 429 or poll_status >= 500:
            await asyncio.sleep(2.0)
            continue
        if poll_status < 200 or poll_status >= 300:
            raise _adobe_api_error(
                stage="poll",
                status=poll_status,
                payload=poll_response.get("data"),
                submitted=True,
            )

        parsed = parse_adobe_poll_result(
            poll_response.get("data"),
            poll_response.get("headers"),
        )
        if parsed["status"] == "FAILED":
            raise NonPenalizedTaskError(
                "Adobe GPT Image 2 generation failed upstream",
                status_code=502,
                submitted=True,
                retryable=False,
                stage="generation",
            )
        if parsed["status"] == "SUCCEEDED":
            result_urls = list(parsed["urls"])
            break

        upstream_progress = parsed.get("progress")
        if upstream_progress is None:
            elapsed = time.monotonic() - started
            pct = min(
                92,
                submitted_progress
                + int((92 - submitted_progress) * elapsed / max(30.0, deadline - started)),
            )
        else:
            pct = min(
                92,
                submitted_progress
                + int((92 - submitted_progress) * float(upstream_progress) / 100.0),
            )
        await progress_cb(pct, {"stage": "generating", "provider": "adobe", "model": public_model})
        retry_after = _one_str((poll_response.get("headers") or {}).get("retry-after"))
        try:
            delay = max(1.0, min(5.0, float(retry_after))) if retry_after else 2.0
        except ValueError:
            delay = 2.0
        await asyncio.sleep(delay)

    if not result_urls:
        raise NonPenalizedTaskError(
            "Adobe GPT Image 2 generation timed out while polling",
            status_code=504,
            submitted=True,
            retryable=False,
            stage="result",
        )

    saved = await _persist_remote_images(page, task_id, result_urls)
    if not saved:
        raise NonPenalizedTaskError(
            "Adobe GPT Image 2 finished without a valid downloadable image",
            status_code=502,
            submitted=True,
            retryable=False,
            stage="result",
        )
    source_urls = [source for source, _ in saved]
    public_urls = [public_url for _, public_url in saved]
    await progress_cb(98, {"stage": "result", "provider": "adobe", "count": len(public_urls)})
    result = {
        "type": "adobe_image2",
        "provider": "adobe",
        "workflow_kind": "image",
        "model": public_model,
        "provider_model": ADOBE_IMAGE2_PROVIDER_MODEL,
        "quality": quality,
        "aspect_ratio": aspect_ratio,
        "image_url": public_urls[0],
        "url": public_urls[0],
        "urls": public_urls,
        "source_urls": source_urls,
    }
    if reference_blob_ids:
        result["generation_mode"] = "image2image"
        result["reference_count"] = len(reference_blob_ids)
    return result


async def persist_adobe_account_info(db: Any, mapping_id: Optional[int], info: Dict[str, Any]) -> None:
    if db is None or int(mapping_id or 0) <= 0:
        return
    kwargs: Dict[str, Any] = {
        "mapping_id": int(mapping_id),
        "remaining_quota": int(info.get("remaining_quota") or 0),
        "sora_remaining_count": int(info.get("remaining_quota") or 0),
    }
    provisioned_quota = int(info.get("provisioned_quota") or 0)
    if provisioned_quota > 0:
        kwargs["daily_quota"] = provisioned_quota
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
        # A fingerprint window may hold independent logins for multiple sites.
        # Keep account identity mapping-local by leaving the shared window label
        # untouched whenever more than one task type uses this window.
        windows = await db.list_windows(int(window.space_pk))
        current = next(
            (item for item in (windows or []) if int(getattr(item, "id", 0) or 0) == int(window.id or 0)),
            None,
        )
        if current is None or int(getattr(current, "bound_task_type_count", 0) or 0) > 1:
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
    reference_sources = resolve_adobe_image2_reference_sources(body)
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
        # The quota API fallback reloads Studio. Wait for the controls again
        # before a paid task starts selecting model options and submitting.
        await _wait_for_adobe_studio(page, min(timeout_seconds, 90.0))
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
                "generation_mode": "image2image" if reference_sources else "text2image",
                "reference_count": len(reference_sources),
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
            reference_sources=reference_sources,
        )
        try:
            refreshed = await _probe_adobe_account(page)
            await persist_adobe_account_info(db, task_type_window_id, refreshed)
            result["remaining_quota"] = int(refreshed.get("remaining_quota") or 0)
        except Exception:
            pass
        return result
