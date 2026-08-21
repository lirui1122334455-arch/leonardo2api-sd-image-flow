"""Zark Lab video executor backed by a logged-in fingerprint window."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote, urlparse

import httpx

from .playwright_broswer_context import (
    append_log,
    get_or_create_ctx as get_or_create_playwright_ctx,
    safe_trim,
)
from .task_executor_types import NonPenalizedTaskError, ProgressCB


DEFAULT_ZARKLAB_TARGET = "https://www.zarklab.ai/"
ZARKLAB_CHAT_BASE = "https://chatbot.zarklab.ai"
ZARKLAB_SEARCH_BASE = "https://search.zarklab.ai"
MONITOR_LOG_FILE = Path("logs/zarklab_monitor.log")

ZARKLAB_PUBLIC_MODEL_ALIASES: Dict[str, str] = {
    "zark-seedance-2.5": "fal-seedance-2-5",
    "zark-seedance-2.0-lite": "fal-seedance-2-fast",
    "zark-seedance-2.0-mini": "fal-seedance-2-mini",
    "zark-seedance-2.0": "fal-seedance-2-pro",
    "zark-minimax-h3": "fal-minimax-h3",
}

_INTERNAL_MODEL_ALIASES: Dict[str, str] = {
    **ZARKLAB_PUBLIC_MODEL_ALIASES,
    "fal-seedance-2-5": "fal-seedance-2-5",
    "fal-seedance-2-fast": "fal-seedance-2-fast",
    "fal-seedance-2-mini": "fal-seedance-2-mini",
    "fal-seedance-2-pro": "fal-seedance-2-pro",
    "fal-minimax-h3": "fal-minimax-h3",
    "seedance 2.5": "fal-seedance-2-5",
    "seedance 2 lite": "fal-seedance-2-fast",
    "seedance 2 mini": "fal-seedance-2-mini",
    "seedance 2": "fal-seedance-2-pro",
    "minimax h3": "fal-minimax-h3",
}

_MODEL_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "fal-seedance-2-5": {
        "durations": set(range(4, 31)),
        "default_duration": 4,
        "resolutions": {"480p": "480p", "720p": "720p"},
        "default_resolution": "480p",
        "aspect_ratios": {"auto", "16:9", "21:9", "4:3", "1:1", "3:4", "9:16"},
        "default_aspect_ratio": "auto",
        "sound": {"on", "off"},
        "max_images": 30,
        "max_videos": 10,
        "max_audios": 10,
        "max_references": 50,
    },
    "fal-seedance-2-fast": {
        "durations": set(range(4, 16)),
        "default_duration": 4,
        "resolutions": {"480p": "480p", "720p": "720p"},
        "default_resolution": "480p",
        "aspect_ratios": {"auto", "16:9", "21:9", "4:3", "1:1", "3:4", "9:16"},
        "default_aspect_ratio": "auto",
        "sound": {"on", "off"},
        "max_images": 9,
        "max_videos": 3,
        "max_audios": 3,
        "max_references": 12,
    },
    "fal-seedance-2-mini": {
        "durations": set(range(4, 16)),
        "default_duration": 4,
        "resolutions": {"480p": "480p", "720p": "720p"},
        "default_resolution": "480p",
        "aspect_ratios": {"auto", "16:9", "21:9", "4:3", "1:1", "3:4", "9:16"},
        "default_aspect_ratio": "auto",
        "sound": {"on", "off"},
        "max_images": 9,
        "max_videos": 3,
        "max_audios": 3,
        "max_references": 12,
    },
    "fal-seedance-2-pro": {
        "durations": set(range(4, 16)),
        "default_duration": 4,
        "resolutions": {"480p": "480p", "720p": "720p", "1080p": "1080p", "4k": "4k"},
        "default_resolution": "480p",
        "aspect_ratios": {"auto", "16:9", "21:9", "4:3", "1:1", "3:4", "9:16"},
        "default_aspect_ratio": "auto",
        "sound": {"on", "off"},
        "max_images": 9,
        "max_videos": 3,
        "max_audios": 3,
        "max_references": 12,
    },
    "fal-minimax-h3": {
        "durations": set(range(5, 16)),
        "default_duration": 5,
        "resolutions": {"768p": "768P", "2k": "2K", "4k": "4K"},
        "default_resolution": "768P",
        "aspect_ratios": {"16:9", "21:9", "4:3", "1:1", "3:4", "9:16"},
        "default_aspect_ratio": "16:9",
        "sound": {"on"},
        "max_images": 9,
        "max_videos": 3,
        "max_audios": 3,
        "max_references": 12,
    },
}

_AUTH_AND_WORKSPACE_JS = r"""
async ({ workspaceOverride }) => {
  const one = (value) => String(value || '').trim();
  let webpackRequire = window.__fpbrowserZarkWebpackRequire;
  if (!webpackRequire) {
    const chunks = window.webpackChunk_N_E;
    if (!Array.isArray(chunks)) throw new Error('Zark webpack runtime is not available');
    chunks.push([[`fpbrowser_zark_${Date.now()}`], {}, (req) => {
      webpackRequire = req;
      window.__fpbrowserZarkWebpackRequire = req;
    }]);
  }
  if (!webpackRequire) throw new Error('Zark webpack runtime could not be captured');

  const moduleIds = new Set();
  if (webpackRequire.m) {
    for (const id of Object.keys(webpackRequire.m)) {
      try {
        const source = Function.prototype.toString.call(webpackRequire.m[id]);
        if (source.includes('auth.zarklab.ai')) moduleIds.add(id);
      } catch (_) {}
    }
  }
  for (const id of ['46926']) moduleIds.add(id);
  if (webpackRequire.c) {
    for (const id of Object.keys(webpackRequire.c)) moduleIds.add(id);
  }

  const roots = [];
  for (const id of moduleIds) {
    try {
      const loaded = webpackRequire(id);
      roots.push(loaded);
      if (loaded && typeof loaded === 'object') {
        for (const exported of Object.values(loaded)) {
          roots.push(exported);
          try {
            if (typeof exported === 'function' && exported.length === 0) {
              const source = Function.prototype.toString.call(exported);
              if (source.includes('popupRedirectResolver') || source.includes('persistence')) {
                roots.push(exported());
              }
            }
          } catch (_) {}
        }
      }
    } catch (_) {}
  }

  const seen = new Set();
  const queue = roots.map((value) => ({ value, depth: 0 }));
  let currentUser = null;
  while (queue.length && !currentUser) {
    const { value, depth } = queue.shift();
    if ((typeof value !== 'object' && typeof value !== 'function') || value === null || seen.has(value)) continue;
    seen.add(value);
    try {
      const user = value.currentUser;
      if (user && typeof user.getIdToken === 'function') {
        currentUser = user;
        break;
      }
    } catch (_) {}
    if (depth >= 4) continue;
    let keys = [];
    try { keys = Object.getOwnPropertyNames(value).slice(0, 100); } catch (_) {}
    for (const key of keys) {
      if (['prototype', 'caller', 'callee', 'arguments'].includes(key)) continue;
      try { queue.push({ value: value[key], depth: depth + 1 }); } catch (_) {}
    }
  }
  if (!currentUser) throw new Error('Zark login session was not found');
  const token = await currentUser.getIdToken();
  if (!one(token)) throw new Error('Zark login token is empty');

  const isWorkspaceId = (value) => /^ps-[A-Za-z0-9_-]{8,}$/.test(one(value));
  let workspaceId = isWorkspaceId(workspaceOverride) ? one(workspaceOverride) : '';
  const urls = [location.href];
  try { urls.push(...performance.getEntriesByType('resource').map((entry) => entry.name)); } catch (_) {}
  if (!workspaceId) {
    for (let i = urls.length - 1; i >= 0 && !workspaceId; i -= 1) {
      try {
        const parsed = new URL(urls[i], location.href);
        for (const key of ['spaceId', 'space_id', 'workspaceId', 'workspace_id']) {
          const candidate = parsed.searchParams.get(key);
          if (isWorkspaceId(candidate)) { workspaceId = one(candidate); break; }
        }
      } catch (_) {}
    }
  }

  const findWorkspace = (root) => {
    const localSeen = new Set();
    const localQueue = [{ value: root, depth: 0 }];
    while (localQueue.length) {
      const { value, depth } = localQueue.shift();
      if (isWorkspaceId(value)) return one(value);
      if ((typeof value !== 'object' && typeof value !== 'function') || value === null || localSeen.has(value)) continue;
      localSeen.add(value);
      if (depth >= 5) continue;
      let next = value;
      try {
        if (typeof value.getState === 'function') next = value.getState();
      } catch (_) {}
      let keys = [];
      try { keys = Object.getOwnPropertyNames(next).slice(0, 120); } catch (_) {}
      for (const key of keys) {
        try {
          const child = next[key];
          if (isWorkspaceId(child)) return one(child);
          localQueue.push({ value: child, depth: depth + 1 });
        } catch (_) {}
      }
    }
    return '';
  };
  if (!workspaceId) {
    const workspaceIds = new Set(['58991']);
    if (webpackRequire.m) {
      for (const id of Object.keys(webpackRequire.m)) {
        try {
          const source = Function.prototype.toString.call(webpackRequire.m[id]);
          if (source.includes('spaceId') && (source.includes('getState') || source.includes('workspace'))) {
            workspaceIds.add(id);
          }
        } catch (_) {}
      }
    }
    for (const id of workspaceIds) {
      try {
        workspaceId = findWorkspace(webpackRequire(id));
        if (workspaceId) break;
      } catch (_) {}
    }
  }
  if (!workspaceId) throw new Error('Zark workspace id was not found');
  return { token, workspaceId };
}
"""

_FETCH_TEXT_JS = r"""
async ({ url, method, headers, body }) => {
  try {
    const init = { method, headers: headers || {}, credentials: 'include' };
    if (body !== null && body !== undefined) init.body = JSON.stringify(body);
    const response = await fetch(url, init);
    return { status: response.status, text: await response.text(), error: '' };
  } catch (error) {
    return { status: 0, text: '', error: String(error) };
  }
}
"""

_FETCH_SSE_JS = r"""
async ({ url, headers, body }) => {
  const events = [];
  let buffer = '';
  let status = 0;
  const consumeLine = (line) => {
    if (!line.startsWith('data:')) return;
    const raw = line.slice(5).trim();
    if (!raw || raw === '[DONE]') return;
    try { events.push(JSON.parse(raw)); } catch (_) { events.push({ type: 'unparsed', raw: raw.slice(0, 2000) }); }
    if (events.length > 1000) events.splice(0, events.length - 1000);
  };
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: headers || {},
      credentials: 'include',
      body: JSON.stringify(body),
    });
    status = response.status;
    if (!response.ok) return { status, events, error: await response.text(), complete: false };
    if (!response.body) return { status, events, error: 'response body is empty', complete: false };
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const part = await reader.read();
      buffer += decoder.decode(part.value || new Uint8Array(), { stream: !part.done });
      let newline = buffer.indexOf('\n');
      while (newline >= 0) {
        consumeLine(buffer.slice(0, newline).replace(/\r$/, ''));
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf('\n');
      }
      if (part.done) break;
    }
    if (buffer.trim()) consumeLine(buffer.trim());
    return { status, events, error: '', complete: true };
  } catch (error) {
    return { status, events, error: String(error), complete: false };
  }
}
"""

_UPLOAD_FILE_JS = r"""
async ({ url, token, fileName, mimeType, base64Data }) => {
  try {
    const binary = atob(base64Data);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    const form = new FormData();
    form.append('files', new Blob([bytes], { type: mimeType || 'application/octet-stream' }), fileName);
    form.append('folderId', 'root');
    const response = await fetch(url, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, Accept: 'application/json, text/plain, */*' },
      credentials: 'include',
      body: form,
    });
    return { status: response.status, text: await response.text(), error: '' };
  } catch (error) {
    return { status: 0, text: '', error: String(error) };
  }
}
"""


def _one_str(value: Any) -> str:
    return str(value or "").strip()


def _optional_bool(value: Any, *, default: Optional[bool] = None) -> Optional[bool]:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = _one_str(value).lower()
    if normalized in {"1", "true", "yes", "on", "y"}:
        return True
    if normalized in {"0", "false", "no", "off", "n"}:
        return False
    raise NonPenalizedTaskError(f"invalid boolean value: {value}", status_code=400)


def resolve_zarklab_model(payload: Dict[str, Any]) -> Tuple[str, str]:
    requested = _one_str(
        payload.get("zarklab_model")
        or payload.get("provider_model")
        or payload.get("model")
        or "zark-seedance-2.5"
    )
    internal = _INTERNAL_MODEL_ALIASES.get(requested.lower())
    if not internal:
        raise NonPenalizedTaskError(
            f"unsupported Zark model {requested!r}; use one of {sorted(ZARKLAB_PUBLIC_MODEL_ALIASES)}",
            status_code=400,
        )
    public = next((key for key, value in ZARKLAB_PUBLIC_MODEL_ALIASES.items() if value == internal), requested)
    return public, internal


def _integer_field(payload: Dict[str, Any], name: str, default: int) -> int:
    raw = payload.get(name)
    if raw is None or raw == "":
        return int(default)
    try:
        value = float(str(raw).strip())
        if not value.is_integer():
            raise ValueError
        return int(value)
    except (TypeError, ValueError) as exc:
        raise NonPenalizedTaskError(f"payload.{name} must be an integer", status_code=400) from exc


def _string_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    values: Iterable[Any]
    if isinstance(value, str):
        values = [part for part in value.split(",")]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        values = [value]
    out: List[str] = []
    for item in values:
        candidate = _one_str(item)
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _reference_values(value: Any) -> List[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return [value]


def _normalize_reference_role(value: Any, default: str = "inspiration") -> str:
    role = _one_str(value).lower()
    return role if role in {"start_frame", "end_frame", "inspiration"} else default


def _normalize_media_type(value: Any, default: str = "file") -> str:
    media_type = _one_str(value).lower()
    return media_type if media_type in {"image", "video", "audio"} else default


def _reference_file_entries(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    seen: set[str] = set()

    def add(value: Any, *, role: str = "inspiration", media_type: str = "file") -> None:
        for item in _reference_values(value):
            if isinstance(item, dict):
                file_id = _one_str(item.get("file_id") or item.get("fileId") or item.get("id"))
                entry_role = _normalize_reference_role(item.get("role") or item.get("studioRole"), role)
                entry_media_type = _normalize_media_type(
                    item.get("media_type") or item.get("mediaType") or item.get("type"), media_type
                )
            else:
                file_id = _one_str(item)
                entry_role = role
                entry_media_type = media_type
            if not file_id or urlparse(file_id).scheme in {"http", "https", "data"} or file_id in seen:
                continue
            seen.add(file_id)
            entries.append({"file_id": file_id, "role": entry_role, "media_type": entry_media_type})

    for key in ("start_frame_file_id", "first_frame_file_id", "first_image_file_id"):
        add(payload.get(key), role="start_frame", media_type="image")
    for key in ("end_frame_file_id", "last_frame_file_id", "last_image_file_id"):
        add(payload.get(key), role="end_frame", media_type="image")
    add(payload.get("references"))
    for key in ("reference_image_file_ids", "image_file_ids"):
        add(payload.get(key), media_type="image")
    for key in ("reference_video_file_ids", "video_file_ids"):
        add(payload.get(key), media_type="video")
    for key in ("reference_audio_file_ids", "audio_file_ids"):
        add(payload.get(key), media_type="audio")
    for key in (
        "current_attachment_file_ids",
        "reference_file_ids",
        "file_ids",
        "attachment_file_ids",
        "zark_file_ids",
    ):
        add(payload.get(key))
    add(payload.get("first_image_url"), role="start_frame", media_type="image")
    add(payload.get("last_image_url"), role="end_frame", media_type="image")
    add(payload.get("image") or payload.get("image_url"), media_type="image")
    return entries


def _reference_file_ids(payload: Dict[str, Any]) -> List[str]:
    return [entry["file_id"] for entry in _reference_file_entries(payload)]


def _reference_url_entries(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    seen: set[str] = set()

    def add(value: Any, *, role: str = "inspiration", media_type: str = "file") -> None:
        for item in _reference_values(value):
            if isinstance(item, dict):
                candidate = _one_str(
                    item.get("url") or item.get("image_url") or item.get("file_url") or item.get("src")
                )
                entry_role = _normalize_reference_role(item.get("role") or item.get("studioRole"), role)
                entry_media_type = _normalize_media_type(
                    item.get("media_type") or item.get("mediaType") or item.get("type"), media_type
                )
            else:
                candidate = _one_str(item)
                entry_role = role
                entry_media_type = media_type
            if urlparse(candidate).scheme not in {"http", "https", "data"} or candidate in seen:
                continue
            seen.add(candidate)
            entries.append({"url": candidate, "role": entry_role, "media_type": entry_media_type})

    for key in ("first_image_url", "start_frame_url", "first_frame_image_url", "start_image_url"):
        add(payload.get(key), role="start_frame", media_type="image")
    for key in ("last_image_url", "end_frame_url", "last_frame_image_url", "end_image_url"):
        add(payload.get(key), role="end_frame", media_type="image")
    add(payload.get("references"))
    for key in ("image", "image_url", "images", "image_urls", "reference_images", "reference_image_urls"):
        add(payload.get(key), media_type="image")
    for key in ("video", "video_url", "videos", "video_urls", "reference_videos", "reference_video_urls"):
        add(payload.get(key), media_type="video")
    for key in ("audio", "audio_url", "audios", "audio_urls", "reference_audios", "reference_audio_urls"):
        add(payload.get(key), media_type="audio")
    return entries


def _reference_urls(payload: Dict[str, Any]) -> List[str]:
    return [entry["url"] for entry in _reference_url_entries(payload)]


def _safe_upload_name(source_url: str, mime_type: str, index: int) -> str:
    try:
        name = unquote(Path(urlparse(source_url).path).name)
    except Exception:
        name = ""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        extension = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip()) or ".bin"
        name = f"zark-reference-{index}{extension}"
    return name[:180]


async def _download_reference(source_url: str, index: int) -> Tuple[str, str, bytes]:
    if source_url.startswith("data:"):
        match = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", source_url, re.DOTALL)
        if not match:
            raise NonPenalizedTaskError("invalid data URL reference", status_code=400)
        mime_type = _one_str(match.group(1)) or "application/octet-stream"
        try:
            data = (
                base64.b64decode(match.group(3), validate=True)
                if match.group(2)
                else unquote(match.group(3)).encode("utf-8")
            )
        except Exception as exc:
            raise NonPenalizedTaskError("invalid data URL reference payload", status_code=400) from exc
    else:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=45.0) as client:
                response = await client.get(source_url)
                response.raise_for_status()
                data = response.content
                mime_type = _one_str(response.headers.get("content-type")).split(";", 1)[0]
        except Exception as exc:
            raise NonPenalizedTaskError(
                f"could not download Zark reference URL: {safe_trim(str(exc), 400)}", status_code=400
            ) from exc
    if not data:
        raise NonPenalizedTaskError("Zark reference file is empty", status_code=400)
    if len(data) > 32 * 1024 * 1024:
        raise NonPenalizedTaskError("Zark reference file must be at most 32 MB", status_code=400)
    if not mime_type or mime_type == "application/octet-stream":
        mime_type = mimetypes.guess_type(urlparse(source_url).path)[0] or "application/octet-stream"
    if not mime_type.startswith(("image/", "video/", "audio/")):
        raise NonPenalizedTaskError(
            f"unsupported Zark reference content type {mime_type!r}", status_code=400
        )
    return _safe_upload_name(source_url, mime_type, index), mime_type, data


def _extract_uploaded_file_ids(data: Any) -> List[str]:
    ids: List[str] = []
    for node in _walk_dicts(data):
        for key in ("fileId", "file_id"):
            file_id = _one_str(node.get(key))
            if file_id and file_id not in ids:
                ids.append(file_id)
        for key in ("fileIds", "file_ids"):
            for file_id in _string_list(node.get(key)):
                if file_id not in ids:
                    ids.append(file_id)
    return ids


async def _upload_reference_urls(
    page: Any,
    *,
    token: str,
    workspace_id: str,
    source_urls: Sequence[str],
) -> List[str]:
    inputs = [{"url": source_url, "role": "inspiration", "media_type": "file"} for source_url in source_urls]
    entries = await _upload_reference_entries(
        page,
        token=token,
        workspace_id=workspace_id,
        source_entries=inputs,
    )
    return [entry["file_id"] for entry in entries]


async def _upload_reference_entries(
    page: Any,
    *,
    token: str,
    workspace_id: str,
    source_entries: Sequence[Dict[str, str]],
) -> List[Dict[str, str]]:
    uploaded: List[Dict[str, str]] = []
    seen: set[str] = set()
    upload_url = f"{ZARKLAB_CHAT_BASE}/v1/files?spaceId={quote(workspace_id, safe='')}"
    for index, source_entry in enumerate(source_entries, start=1):
        source_url = source_entry["url"]
        file_name, mime_type, data = await _download_reference(source_url, index)
        response = await page.evaluate(
            _UPLOAD_FILE_JS,
            {
                "url": upload_url,
                "token": token,
                "fileName": file_name,
                "mimeType": mime_type,
                "base64Data": base64.b64encode(data).decode("ascii"),
            },
        )
        parsed = _json_from_response(response, operation=f"reference upload {index}")
        file_ids = _extract_uploaded_file_ids(parsed)
        if not file_ids:
            raise NonPenalizedTaskError(
                f"Zark Lab reference upload {index} returned no file id", status_code=502, retryable=False
            )
        for file_id in file_ids:
            if file_id in seen:
                continue
            seen.add(file_id)
            uploaded.append(
                {
                    "file_id": file_id,
                    "role": _normalize_reference_role(source_entry.get("role")),
                    "media_type": _normalize_media_type(mime_type.split("/", 1)[0]),
                }
            )
    return uploaded


def _validate_reference_entries(caps: Dict[str, Any], entries: Sequence[Dict[str, str]]) -> None:
    roles = [entry.get("role") for entry in entries]
    start_count = roles.count("start_frame")
    end_count = roles.count("end_frame")
    if start_count > 1 or end_count > 1:
        raise NonPenalizedTaskError("Zark Lab supports at most one start frame and one end frame", status_code=400)
    if end_count and not start_count:
        raise NonPenalizedTaskError("Zark Lab end frame requires a start frame", status_code=400)
    if (start_count or end_count) and any(role == "inspiration" for role in roles):
        raise NonPenalizedTaskError(
            "Zark Lab frame mode cannot be combined with inspiration references", status_code=400
        )

    counts = {
        media_type: sum(1 for entry in entries if entry.get("media_type") == media_type)
        for media_type in ("image", "video", "audio")
    }
    limits = {
        "image": int(caps["max_images"]),
        "video": int(caps["max_videos"]),
        "audio": int(caps["max_audios"]),
    }
    for media_type, count in counts.items():
        if count > limits[media_type]:
            raise NonPenalizedTaskError(
                f"Zark Lab {media_type} references must be at most {limits[media_type]}", status_code=400
            )
    if len(entries) > int(caps["max_references"]):
        raise NonPenalizedTaskError(
            f"Zark Lab references must be at most {caps['max_references']} in total", status_code=400
        )


def build_zarklab_tool_params(payload: Dict[str, Any]) -> Dict[str, Any]:
    _, internal_model = resolve_zarklab_model(payload)
    caps = _MODEL_CAPABILITIES[internal_model]
    duration_payload = dict(payload)
    if duration_payload.get("duration") in (None, "") and duration_payload.get("seconds") not in (None, ""):
        duration_payload["duration"] = duration_payload.get("seconds")
    duration = _integer_field(duration_payload, "duration", int(caps["default_duration"]))
    if duration not in caps["durations"]:
        allowed = sorted(caps["durations"])
        raise NonPenalizedTaskError(
            f"{internal_model} duration must be between {allowed[0]} and {allowed[-1]} seconds",
            status_code=400,
        )

    raw_resolution = _one_str(payload.get("resolution") or caps["default_resolution"])
    resolution = caps["resolutions"].get(raw_resolution.lower())
    if not resolution:
        raise NonPenalizedTaskError(
            f"{internal_model} resolution must be one of {sorted(caps['resolutions'].values())}",
            status_code=400,
        )

    aspect_ratio = _one_str(payload.get("aspect_ratio") or payload.get("ratio") or caps["default_aspect_ratio"])
    if aspect_ratio not in caps["aspect_ratios"]:
        raise NonPenalizedTaskError(
            f"{internal_model} aspect_ratio must be one of {sorted(caps['aspect_ratios'])}",
            status_code=400,
        )

    sound_bool = _optional_bool(payload.get("sound"), default=None)
    if sound_bool is None:
        sound_raw = _one_str(payload.get("sound") or payload.get("audio") or "on").lower()
        sound = "off" if sound_raw in {"off", "false", "0", "no"} else "on"
    else:
        sound = "on" if sound_bool else "off"
    if sound not in caps["sound"]:
        raise NonPenalizedTaskError(f"{internal_model} always generates sound; sound must be on", status_code=400)

    file_entries = _reference_file_entries(payload)
    url_entries = _reference_url_entries(payload)
    reference_entries = [*file_entries, *url_entries]
    _validate_reference_entries(caps, reference_entries)
    interpolate = any(entry.get("role") == "start_frame" for entry in reference_entries) and any(
        entry.get("role") == "end_frame" for entry in reference_entries
    )
    selected_action = "interpolate" if interpolate else "generate"

    tool_params: Dict[str, Any] = {
        "model": internal_model,
        "selected_tool": "video",
        "force_executor": "video_gen",
        "director_mode": "creator",
        "selected_action": selected_action,
        "selected_model": internal_model,
        "action": selected_action,
        "aspect_ratio": aspect_ratio,
        "duration": str(duration),
        "resolution": resolution,
        "sound": sound,
    }
    references = [entry["file_id"] for entry in file_entries]
    if references:
        tool_params["current_attachment_file_ids"] = references
        tool_params["reference_file_ids"] = references
    return tool_params


def build_zarklab_quote_body(tool_params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "target_media": "video",
        "action": _one_str(tool_params.get("action")) or "generate",
        "selected_model": tool_params["selected_model"],
        "duration": int(tool_params["duration"]),
        "resolution": tool_params["resolution"],
        "aspect_ratio": tool_params["aspect_ratio"],
    }


def parse_zarklab_sse(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    events: List[Dict[str, Any]] = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except Exception:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def extract_zarklab_file_ids(events: Sequence[Dict[str, Any]]) -> List[str]:
    file_ids: List[str] = []
    for event in events:
        for node in _walk_dicts(event):
            for key in ("generated_file_ids", "file_ids"):
                for file_id in _string_list(node.get(key)):
                    if file_id not in file_ids:
                        file_ids.append(file_id)
            file_id = _one_str(node.get("file_id"))
            if file_id and file_id not in file_ids:
                file_ids.append(file_id)
    return file_ids


def _sse_terminal_state(events: Sequence[Dict[str, Any]]) -> Tuple[str, str]:
    terminal = ""
    error = ""
    for event in events:
        for node in _walk_dicts(event):
            event_type = _one_str(node.get("type") or node.get("event") or node.get("event_type")).lower()
            status = _one_str(node.get("status") or node.get("state")).lower()
            if status in {"failed", "cancelled", "canceled"}:
                terminal = status
                error = _one_str(node.get("error") or node.get("message") or node.get("detail")) or error
            elif status in {"complete", "completed", "saved", "success", "succeeded"}:
                terminal = status
            if event_type == "error":
                terminal = "failed"
                error = _one_str(node.get("error") or node.get("message") or node.get("detail")) or error
    return terminal, error


def extract_zarklab_video_urls(file_detail: Any) -> List[str]:
    scored: List[Tuple[int, str]] = []

    def visit(value: Any, key_path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{key_path}.{key}" if key_path else str(key))
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key_path)
            return
        candidate = _one_str(value)
        if not candidate.startswith(("https://", "http://")):
            return
        path = key_path.lower()
        url_lower = candidate.lower().split("?", 1)[0]
        score = 0
        if any(part in path for part in ("video_url", "download_url", "signed_url", "presignedurl", "file_url", "media_url", "source_url", "cdn_url")):
            score += 100
        if url_lower.endswith((".mp4", ".mov", ".webm", ".m3u8")):
            score += 80
        if "video" in path:
            score += 30
        if "playback" in path:
            score += 60
        if any(part in path for part in ("thumbnail", "poster", "preview", "image", "avatar", "icon")):
            score -= 100
        if score > 0:
            scored.append((score, candidate))

    visit(file_detail)
    out: List[str] = []
    for _, url in sorted(scored, key=lambda item: item[0], reverse=True):
        if url not in out:
            out.append(url)
    return out


def _find_json_value(value: Any, key: str) -> Any:
    for node in _walk_dicts(value):
        if key in node and node.get(key) is not None:
            return node.get(key)
    return None


def _json_from_response(response: Dict[str, Any], *, operation: str) -> Dict[str, Any]:
    status = int(response.get("status") or 0)
    text = _one_str(response.get("text"))
    if status == 401:
        raise NonPenalizedTaskError("Zark Lab login expired", status_code=401, retryable=False)
    if not 200 <= status < 300:
        detail = safe_trim(text or _one_str(response.get("error")), 800)
        raise NonPenalizedTaskError(
            f"Zark Lab {operation} failed with HTTP {status}: {detail}",
            status_code=status or 502,
            retryable=False,
        )
    try:
        data = json.loads(text) if text else {}
    except Exception as exc:
        raise NonPenalizedTaskError(
            f"Zark Lab {operation} returned invalid JSON", status_code=502, retryable=False
        ) from exc
    if not isinstance(data, dict):
        raise NonPenalizedTaskError(
            f"Zark Lab {operation} returned an unexpected response", status_code=502, retryable=False
        )
    return data


async def _find_or_open_zarklab_page(session: Any, target_url: str) -> Any:
    context = getattr(session, "context", None)
    if context is None:
        raise NonPenalizedTaskError("Zark Lab browser context is not initialized", status_code=502)
    page = None
    for candidate in list(getattr(context, "pages", []) or []):
        try:
            if candidate.is_closed():
                continue
            host = (urlparse(_one_str(candidate.url)).hostname or "").lower()
            if host == "zarklab.ai" or host.endswith(".zarklab.ai"):
                page = candidate
                if host == "www.zarklab.ai":
                    break
        except Exception:
            continue
    if page is None:
        page = await context.new_page()
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
    session.page = page
    return page


async def _resolve_auth_and_workspace(page: Any, workspace_override: str = "") -> Dict[str, str]:
    try:
        info = await page.evaluate(_AUTH_AND_WORKSPACE_JS, {"workspaceOverride": workspace_override})
    except Exception as exc:
        raise NonPenalizedTaskError(
            f"Zark Lab login/workspace discovery failed: {safe_trim(str(exc), 500)}",
            status_code=401,
            retryable=False,
        ) from exc
    token = _one_str((info or {}).get("token"))
    workspace_id = _one_str((info or {}).get("workspaceId"))
    if not token or not workspace_id:
        raise NonPenalizedTaskError("Zark Lab login or workspace is unavailable", status_code=401, retryable=False)
    return {"token": token, "workspace_id": workspace_id}


async def _browser_fetch(
    page: Any,
    *,
    url: str,
    method: str,
    token: str,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return await page.evaluate(
        _FETCH_TEXT_JS,
        {
            "url": url,
            "method": method,
            "headers": {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            },
            "body": body,
        },
    )


async def _fetch_quote(
    page: Any,
    *,
    token: str,
    workspace_id: str,
    tool_params: Dict[str, Any],
) -> Dict[str, Any]:
    url = f"{ZARKLAB_SEARCH_BASE}/v1/quotes/media/decision?spaceId={quote(workspace_id, safe='')}"
    response = await _browser_fetch(
        page,
        url=url,
        method="POST",
        token=token,
        body=build_zarklab_quote_body(tool_params),
    )
    decision = _json_from_response(response, operation="credit quote")
    allowed = _find_json_value(decision, "allowed")
    if allowed is False:
        available = _find_json_value(decision, "available_credits")
        estimated = _find_json_value(decision, "estimated_credits")
        raise NonPenalizedTaskError(
            f"Zark Lab rejected the request: estimated_credits={estimated}, available_credits={available}",
            status_code=402,
            retryable=False,
        )
    return decision


async def _post_sse(
    page: Any,
    *,
    url: str,
    token: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    return await page.evaluate(
        _FETCH_SSE_JS,
        {
            "url": url,
            "headers": {
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
            },
            "body": body,
        },
    )


async def _fetch_file_detail(
    page: Any, *, token: str, workspace_id: str, file_id: str
) -> Dict[str, Any]:
    response = await _browser_fetch(
        page,
        url=(
            f"{ZARKLAB_CHAT_BASE}/v1/files/{quote(file_id, safe='')}"
            f"?spaceId={quote(workspace_id, safe='')}"
        ),
        method="GET",
        token=token,
    )
    return _json_from_response(response, operation=f"file lookup {file_id}")


async def _persist_quote(db: Any, mapping_id: Optional[int], decision: Dict[str, Any]) -> None:
    if db is None or int(mapping_id or 0) <= 0:
        return
    available = _find_json_value(decision, "available_credits")
    if available is None:
        return
    try:
        remaining = int(float(str(available)))
        await db.update_task_type_window(
            mapping_id=int(mapping_id),
            remaining_quota=remaining,
            sora_remaining_count=remaining,
        )
    except Exception:
        return


async def zarklab_fetch_credits(
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    target_url: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
    workspace_id: str = "",
) -> Dict[str, Any]:
    target = _one_str(target_url) or DEFAULT_ZARKLAB_TARGET
    session = get_or_create_playwright_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    tool_params = build_zarklab_tool_params({"model": "zark-seedance-2.0-mini"})
    async with session.driver_lock:
        await session.ensure_open(
            args=[target], force_open=False, headless=headless, require_page=False, pure_mode=pure_mode
        )
        page = await _find_or_open_zarklab_page(session, target)
        auth = await _resolve_auth_and_workspace(page, workspace_id)
        decision = await _fetch_quote(
            page,
            token=auth["token"],
            workspace_id=auth["workspace_id"],
            tool_params=tool_params,
        )
    available = _find_json_value(decision, "available_credits")
    return {
        "remaining_quota": int(float(str(available or 0))),
        "estimated_credits": _find_json_value(decision, "estimated_credits"),
        "workspace_id": auth["workspace_id"],
    }


async def zarklab_workflow(
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
    db: Any = None,
    task_type_window_id: Optional[int] = None,
    **_: Any,
) -> Dict[str, Any]:
    p = dict(payload or {})
    prompt = _one_str(p.get("prompt") or p.get("text") or p.get("input"))
    if not prompt:
        raise NonPenalizedTaskError("payload.prompt cannot be empty", status_code=400)
    public_model, internal_model = resolve_zarklab_model(p)
    tool_params = build_zarklab_tool_params(p)
    target = _one_str(p.get("zarklab_url") or p.get("target_url") or default_target_url) or DEFAULT_ZARKLAB_TARGET
    workspace_override = _one_str(p.get("zark_workspace_id") or p.get("workspace_id"))
    log_file = Path(_one_str(p.get("monitor_log_path"))) if _one_str(p.get("monitor_log_path")) else MONITOR_LOG_FILE
    dry_run = bool(
        _optional_bool(
            p.get("dry_run") if p.get("dry_run") is not None else p.get("skip_submit"),
            default=False,
        )
    )
    run_id = _one_str(p.get("_zark_run_id") or p.get("run_id")) or str(uuid.uuid4())
    session = get_or_create_playwright_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )

    started = time.time()
    await progress_cb(1, {"stage": "init", "provider": "zarklab", "model": public_model})
    async with session.driver_lock:
        await session.ensure_open(
            args=[target], force_open=False, headless=headless, require_page=False, pure_mode=pure_mode
        )
        page = await _find_or_open_zarklab_page(session, target)
        await progress_cb(5, {"stage": "auth", "provider": "zarklab"})
        auth = await _resolve_auth_and_workspace(page, workspace_override)
        decision = await _fetch_quote(
            page,
            token=auth["token"],
            workspace_id=auth["workspace_id"],
            tool_params=tool_params,
        )
        await _persist_quote(db, task_type_window_id, decision)
        estimated = _find_json_value(decision, "estimated_credits")
        available = _find_json_value(decision, "available_credits")
        await progress_cb(
            10,
            {
                "stage": "quote",
                "estimated_credits": estimated,
                "available_credits": available,
                "model": public_model,
            },
        )
        if dry_run:
            return {
                "type": "zarklab_video_quote",
                "provider": "zarklab",
                "workflow_kind": "video",
                "dry_run": True,
                "allowed": _find_json_value(decision, "allowed") is not False,
                "estimated_credits": estimated,
                "available_credits": available,
                "model": public_model,
                "provider_model": internal_model,
                "duration": int(tool_params["duration"]),
                "resolution": tool_params["resolution"],
                "aspect_ratio": tool_params["aspect_ratio"],
            }

        reference_entries = _reference_file_entries(p)
        source_entries = _reference_url_entries(p)
        if source_entries:
            await progress_cb(12, {"stage": "upload_references", "count": len(source_entries)})
            uploaded_entries = await _upload_reference_entries(
                page,
                token=auth["token"],
                workspace_id=auth["workspace_id"],
                source_entries=source_entries,
            )
            known_ids = {entry["file_id"] for entry in reference_entries}
            reference_entries.extend(
                entry for entry in uploaded_entries if entry["file_id"] not in known_ids
            )

        if reference_entries:
            _validate_reference_entries(_MODEL_CAPABILITIES[internal_model], reference_entries)
            reference_ids = [entry["file_id"] for entry in reference_entries]
            tool_params["current_attachment_file_ids"] = reference_ids
            tool_params["reference_file_ids"] = reference_ids

        request_body = {"question": prompt, "tool_params": tool_params, "run_id": run_id}
        if reference_entries:
            reference_ids = [entry["file_id"] for entry in reference_entries]
            request_body["current_attachment_file_ids"] = reference_ids
            request_body["reference_file_ids"] = reference_ids
            request_body["files"] = reference_entries
            request_body["references"] = reference_entries
        await progress_cb(15, {"stage": "submit", "run_id": run_id, "model": public_model})
        submit_url = (
            f"{ZARKLAB_CHAT_BASE}/v1/spaces/{quote(auth['workspace_id'], safe='')}/chat"
        )
        submitted = False
        try:
            stream = await _post_sse(
                page,
                url=submit_url,
                token=auth["token"],
                body=request_body,
            )
            status = int((stream or {}).get("status") or 0)
            submitted = 200 <= status < 300
        except Exception as exc:
            stream = {"status": 0, "events": [], "error": str(exc), "complete": False}

        events = parse_zarklab_sse((stream or {}).get("events"))
        file_ids = extract_zarklab_file_ids(events)
        terminal, terminal_error = _sse_terminal_state(events)
        stream_complete = bool((stream or {}).get("complete"))
        stream_error = _one_str((stream or {}).get("error"))
        if int((stream or {}).get("status") or 0) == 401:
            raise NonPenalizedTaskError("Zark Lab login expired", status_code=401, retryable=False)
        stream_status = int((stream or {}).get("status") or 0)
        if stream_status > 0 and stream_status not in range(200, 300) and not submitted:
            raise NonPenalizedTaskError(
                f"Zark Lab generation request failed: {safe_trim(stream_error, 800)}",
                status_code=stream_status,
                retryable=False,
            )

        if not file_ids and terminal not in {"failed", "cancelled", "canceled"}:
            await progress_cb(65, {"stage": "resume", "run_id": run_id, "model": public_model})
            try:
                resume = await _post_sse(
                    page,
                    url=f"{ZARKLAB_CHAT_BASE}/v1/chat/resume",
                    token=auth["token"],
                    body={"runId": run_id, "workspaceId": auth["workspace_id"]},
                )
            except Exception as exc:
                raise NonPenalizedTaskError(
                    f"Zark Lab stream interrupted; resume run_id={run_id} failed: {safe_trim(str(exc), 500)}",
                    status_code=504,
                    submitted=True,
                    retryable=False,
                    stage="resume",
                ) from exc
            resume_events = parse_zarklab_sse((resume or {}).get("events"))
            events.extend(resume_events)
            file_ids = extract_zarklab_file_ids(events)
            terminal, terminal_error = _sse_terminal_state(events)
            stream_complete = stream_complete or bool((resume or {}).get("complete"))
            stream_error = stream_error or _one_str((resume or {}).get("error"))

        if terminal in {"failed", "cancelled", "canceled"}:
            raise NonPenalizedTaskError(
                terminal_error or f"Zark Lab generation {terminal}",
                status_code=422,
                submitted=True,
                retryable=False,
                stage="generation",
            )
        if not file_ids:
            append_log(
                log_file,
                f"[zarklab] no file ids run={run_id} complete={stream_complete} error={safe_trim(stream_error, 300)}",
            )
            raise NonPenalizedTaskError(
                "Zark Lab generation stream ended without a generated file id",
                status_code=504,
                submitted=submitted,
                retryable=False,
                stage="poll",
            )

        await progress_cb(90, {"stage": "files", "run_id": run_id, "file_ids": file_ids})
        file_details: List[Dict[str, Any]] = []
        video_urls: List[str] = []
        for file_id in file_ids:
            detail = await _fetch_file_detail(
                page,
                token=auth["token"],
                workspace_id=auth["workspace_id"],
                file_id=file_id,
            )
            file_details.append(detail)
            for url in extract_zarklab_video_urls(detail):
                if url not in video_urls:
                    video_urls.append(url)
        if not video_urls:
            raise NonPenalizedTaskError(
                "Zark Lab generated files did not contain a video URL",
                status_code=502,
                submitted=True,
                retryable=False,
                stage="files",
            )

        try:
            updated_quote = await _fetch_quote(
                page,
                token=auth["token"],
                workspace_id=auth["workspace_id"],
                tool_params=tool_params,
            )
            await _persist_quote(db, task_type_window_id, updated_quote)
            available = _find_json_value(updated_quote, "available_credits")
        except Exception:
            pass

    elapsed_ms = int(max(0.0, time.time() - started) * 1000)
    append_log(
        log_file,
        f"[zarklab] completed run={run_id} model={internal_model} files={len(file_ids)} elapsed_ms={elapsed_ms}",
    )
    await progress_cb(100, {"stage": "done", "run_id": run_id, "video_url": video_urls[0]})
    return {
        "type": "zarklab_video",
        "provider": "zarklab",
        "workflow_kind": "video",
        "message": "Zark Lab video generation completed",
        "generation_id": run_id,
        "run_id": run_id,
        "file_ids": file_ids,
        "video_url": video_urls[0],
        "share_url": video_urls[0],
        "url": video_urls[0],
        "urls": video_urls,
        "model": public_model,
        "provider_model": internal_model,
        "duration": int(tool_params["duration"]),
        "resolution": tool_params["resolution"],
        "aspect_ratio": tool_params["aspect_ratio"],
        "sound": tool_params["sound"],
        "estimated_credits": estimated,
        "remaining_quota": available,
        "elapsed_ms": elapsed_ms,
    }
