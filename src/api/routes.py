"""Public API routes (task submit + task status)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Body, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from ..core.auth import AuthManager, verify_api_key_header
from ..core.config import config
from ..core.database import Database
from ..core.logger import logger
from ..core.models import TaskStatusResponse
from ..core.paths import DATA_DIR
from ..core.public_api_limits import (
    DEFAULT_PUBLIC_CREATE_TASK_MAX_INFLIGHT,
    DEFAULT_SERVER_COUNT,
    calc_public_browser_pool_limit,
    normalize_public_create_task_max_inflight,
    normalize_server_count,
)
from ..services.leonardo_task_executor import LEONARDO_PUBLIC_MODEL_ALIASES
from ..services.zarklab_task_executor import ZARKLAB_PUBLIC_MODEL_ALIASES
from ..services.task_service import TaskService
from ..services.task_handler_registry import CreateTaskContext, get_create_task_handler


router = APIRouter()

db: Database | None = None
task_service: TaskService | None = None

# ---- High-concurrency controls (public endpoints) ----
# 创建任务接口并发闸门，避免峰值时打爆 DB/线程资源。
_CREATE_TASK_MAX_INFLIGHT = DEFAULT_PUBLIC_CREATE_TASK_MAX_INFLIGHT
_SERVER_COUNT = DEFAULT_SERVER_COUNT
_CREATE_TASK_ACQUIRE_TIMEOUT_SEC = max(0.1, float(os.getenv("PUBLIC_CREATE_TASK_ACQUIRE_TIMEOUT_SEC", "3")))
_create_task_semaphore: asyncio.Semaphore | None = None
_create_task_gate_lock = asyncio.Lock()

# 高频查询缓存：轮询场景下减少重复读库。
_STATUS_CACHE_TTL_PENDING_SEC = max(0.05, float(os.getenv("TASK_STATUS_CACHE_TTL_PENDING_SEC", "2.0")))
_STATUS_CACHE_TTL_FINAL_SEC = max(1.0, float(os.getenv("TASK_STATUS_CACHE_TTL_FINAL_SEC", "20")))
_status_cache: dict[str, tuple[float, Dict[str, Any]]] = {}
_status_inflight: dict[str, asyncio.Future[Optional[Dict[str, Any]]]] = {}
_status_lock = asyncio.Lock()

# create_task 读前置配置的短缓存（降低热点读库）。
_SYSTEM_CONFIG_TTL_SEC = max(0.1, float(os.getenv("SYSTEM_CONFIG_CACHE_TTL_SEC", "5.0")))
_TASK_TYPE_TTL_SEC = max(0.1, float(os.getenv("TASK_TYPE_CACHE_TTL_SEC", "60.0")))
_system_config_cache: tuple[float, Optional[bool], Optional[int], Optional[int]] = (0.0, None, None, None)
_task_type_cache: dict[str, tuple[float, Any]] = {}


def set_dependencies(database: Database) -> None:
    global db, task_service, _create_task_semaphore
    db = database
    task_service = TaskService(database)
    _create_task_semaphore = asyncio.Semaphore(_CREATE_TASK_MAX_INFLIGHT)
    # 窗口池维护协程由 main lifespan 延迟启动，避免启动瞬间抢占事件循环导致管理页无法打开


class CreateTaskRequest(BaseModel):
    task_type_code: str = Field(min_length=2, max_length=64)
    json: Dict[str, Any] = Field(default_factory=dict)
    # 可选：指定执行窗口（仅用于调试/测试；不指定则走默认调度）
    mapping_id: Optional[int] = Field(default=None, ge=1)
    window_pk: Optional[int] = Field(default=None, ge=1)


class CreateVideoRequest(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    prompt: Optional[str] = None
    aspect_ratio: Optional[str] = None
    duration: Optional[int] = None
    image: Optional[str] = None
    negative_prompt: Optional[str] = None
    seed: Optional[int] = None
    # 允许透传未来新增的视频参数。
    model_config = {"extra": "allow"}


OPENAI_COMPAT_VIDEO_MODELS = (
    "seedance-2",
    "seedance-2-fast",
    "seedance-2-mini",
    "dreamina-seedance-2",
    "dreamina-seedance-2-fast",
    "dreamina-seedance-2-mini",
    "seedance-2-dreamina",
    "seedance-2-fast-dreamina",
    "seedance-2-mini-dreamina",
    *LEONARDO_PUBLIC_MODEL_ALIASES.keys(),
    *ZARKLAB_PUBLIC_MODEL_ALIASES.keys(),
    "nana-banana-2",
    "nana-banana-pro",
    "veo-3-1",
    "veo-3-1-lite",
    "veo-3-1-fast",
    "veo-3-1-quality",
    "gemini-omni",
    "gemini-omni-flash",
    "veo-omni",
    "veoomni",
    "VEOomni",
    "omni-flash",
    "gpt-image-2",
    "gpt-image2-1k",
    "gpt-image2-2k",
    "gpt-image2-4k",
)
OPENAI_COMPAT_VIDEO_MODEL_SET = set(OPENAI_COMPAT_VIDEO_MODELS)
OPENAI_COMPAT_VIDEO_MODEL_KEY_SET = {m.lower() for m in OPENAI_COMPAT_VIDEO_MODELS}
GEMINI_OMNI_VIDEO_MODEL_SET = {"gemini-omni", "gemini-omni-flash", "veo-omni", "veoomni", "omni-flash"}
VEO31_VIDEO_MODEL_KEYS: Dict[str, str] = {
    "veo-3-1": "veo_3_1_fast",
    "veo-3-1-fast": "veo_3_1_fast",
    "veo-3-1-lite": "veo_3_1_lite",
    "veo-3-1-quality": "veo_3_1_quality",
}
DREAMINA_PUBLIC_MODEL_ALIASES: Dict[str, str] = {
    "dreamina-seedance-2": "seedance-2",
    "dreamina-seedance-2-fast": "seedance-2-fast",
    "dreamina-seedance-2-mini": "seedance-2-mini",
    "seedance-2-dreamina": "seedance-2",
    "seedance-2-fast-dreamina": "seedance-2-fast",
    "seedance-2-mini-dreamina": "seedance-2-mini",
}
I2V_VIDEO_MODES = {"i2v", "image_to_video", "img2vid", "img2video", "first_frame", "first-frame"}
GPT_IMAGE2_VIDEO_MODELS: Dict[str, str] = {
    "gpt-image2-1k": "1k",
    "gpt-image2-2k": "2k",
    "gpt-image2-4k": "4k",
}
GPT_IMAGE2_ALIAS_MODELS = {"gpt-image-2", "gpt-image2"}
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
GPT_ASSET_MAX_BYTES = 80 * 1024 * 1024
GPT_ASSET_SIGN_TTL_SEC = max(60, int(os.getenv("GPT_ASSET_SIGN_TTL_SEC", "86400")))
LEONARDO_ASSET_MAX_BYTES = max(GPT_ASSET_MAX_BYTES, int(os.getenv("LEONARDO_ASSET_MAX_BYTES", str(80 * 1024 * 1024))))
GPT_PUBLIC_ASSET_DIR = DATA_DIR / "gpt_assets"
ELEVENLABS_PUBLIC_ASSET_DIR = DATA_DIR / "elevenlabs_assets"
GPT_PUBLIC_ASSET_MAX_BYTES = GPT_ASSET_MAX_BYTES
GPT_PUBLIC_ASSET_EXT_BY_TYPE = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}
STATUS_RESPONSE_LOG_DIR = Path(r"C:\manliu\logs")
STATUS_RESPONSE_LOG_FILE = STATUS_RESPONSE_LOG_DIR / "status_response_jsonl.log"


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            txt = _content_to_text(item)
            if txt:
                parts.append(txt)
        return "\n".join(parts).strip()
    if isinstance(value, dict):
        for key in ("text", "input_text", "content", "prompt"):
            txt = _content_to_text(value.get(key))
            if txt:
                return txt
    return str(value).strip()


def _messages_to_prompt(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    user_parts: list[str] = []
    all_parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        txt = _content_to_text(msg.get("content"))
        if not txt:
            continue
        all_parts.append(txt)
        if str(msg.get("role") or "").strip().lower() == "user":
            user_parts.append(txt)
    return "\n".join(user_parts or all_parts).strip()


def _public_one_url(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, dict):
        for key in ("url", "image_url", "imageUrl", "src"):
            s = _public_one_url(value.get(key))
            if s:
                return s
    return None


def _public_url_list(value: Any) -> Optional[list[Any]]:
    if not isinstance(value, list) or len(value) == 0:
        return None
    out: list[Any] = []
    for item in value:
        if isinstance(item, dict):
            u = _public_one_url(item)
            if u:
                out.append(item)
        else:
            u = _public_one_url(item)
            if u:
                out.append(u)
    return out or None


def _public_url_values(value: Any) -> list[str]:
    values = _public_url_list(value) or []
    out: list[str] = []
    for item in values:
        u = _public_one_url(item)
        if u and u not in out:
            out.append(u)
    return out


def _normalize_public_image_fields(body: Dict[str, Any]) -> None:
    """Normalize common frontend image field aliases before model routing."""

    if _public_url_list(body.get("images")) is None:
        for key in (
            "image_urls",
            "imageUrls",
            "input_images",
            "inputImages",
            "reference_images",
            "referenceImages",
            "reference_image_urls",
            "referenceImageUrls",
        ):
            urls = _public_url_list(body.get(key))
            if urls:
                body["images"] = urls
                break

    if not str(body.get("first_image_url") or body.get("firstImageUrl") or "").strip():
        for key in (
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
            "first_frame_image",
            "firstFrameImage",
            "first_frame_image_url",
            "firstFrameImageUrl",
        ):
            u = _public_one_url(body.get(key))
            if u:
                body["first_image_url"] = u
                break

    if not str(body.get("last_image_url") or body.get("lastImageUrl") or "").strip():
        for key in (
            "last_frame_image",
            "lastFrameImage",
            "last_frame_image_url",
            "lastFrameImageUrl",
            "end_frame_image",
            "endFrameImage",
            "end_frame_image_url",
            "endFrameImageUrl",
        ):
            u = _public_one_url(body.get(key))
            if u:
                body["last_image_url"] = u
                break


def _normalize_leonardo_prompt_aliases(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Keep Leonardo prompt_json readable after public image alias normalization."""

    body = dict(payload or {})
    ref_urls: list[str] = []
    for key in (
        "reference_image_urls",
        "referenceImageUrls",
        "image_reference_urls",
        "imageReferenceUrls",
        "reference_images",
        "referenceImages",
    ):
        for u in _public_url_values(body.get(key)):
            if u not in ref_urls:
                ref_urls.append(u)

    image_urls = _public_url_values(body.get("images"))
    if ref_urls and image_urls and len(ref_urls) == len(image_urls) and set(ref_urls) == set(image_urls):
        body.pop("images", None)
    return body


def _normalize_video_request_body(raw_body: Dict[str, Any]) -> Dict[str, Any]:
    """Accept common video frontend payload variants without FastAPI 422s."""

    if not isinstance(raw_body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")

    body = dict(raw_body or {})
    nested = body.get("json")
    if isinstance(nested, dict):
        merged = dict(nested)
        merged.update({k: v for k, v in body.items() if k != "json"})
        body = merged

    model = (
        body.get("model")
        or body.get("video_model")
        or body.get("model_name")
        or body.get("videoModel")
    )
    model_s = str(model or "").strip()
    if not model_s:
        raise HTTPException(status_code=400, detail="缺少 model，例如 VEOomni、veo-3-1-fast")

    prompt = _content_to_text(body.get("prompt"))
    if not prompt:
        prompt = _content_to_text(body.get("input"))
    if not prompt:
        prompt = _content_to_text(body.get("text"))
    if not prompt:
        prompt = _content_to_text(body.get("content"))
    if not prompt:
        prompt = _messages_to_prompt(body.get("messages"))
    if not prompt:
        for key in ("parameters", "leonardo_parameters"):
            raw_params = body.get(key)
            if isinstance(raw_params, dict):
                prompt = _content_to_text(raw_params.get("prompt"))
                if prompt:
                    break
    if not prompt:
        raise HTTPException(status_code=400, detail="缺少 prompt/input：请填写视频提示词")

    body["model"] = model_s
    body["prompt"] = prompt
    _normalize_public_image_fields(body)
    return body


def _public_video_body_debug(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        return {"body_type": type(body).__name__}
    out: Dict[str, Any] = {
        "keys": sorted([str(k) for k in body.keys()])[:40],
        "model": str(
            body.get("model")
            or body.get("video_model")
            or body.get("model_name")
            or body.get("videoModel")
            or ""
        )[:128],
        "has_prompt": bool(str(body.get("prompt") or "").strip()),
        "has_input": bool(str(body.get("input") or "").strip()),
        "has_text": bool(str(body.get("text") or "").strip()),
        "has_content": bool(str(body.get("content") or "").strip()),
        "has_messages": isinstance(body.get("messages"), list) and bool(body.get("messages")),
    }
    nested = body.get("json")
    if isinstance(nested, dict):
        out["json_keys"] = sorted([str(k) for k in nested.keys()])[:40]
        out["json_model"] = str(nested.get("model") or nested.get("video_model") or "")[:128]
    return out


def _public_images_list(value: Any) -> Optional[list[Any]]:
    return value if isinstance(value, list) and len(value) > 0 else None


def _public_payload_requests_i2v(payload: Dict[str, Any]) -> bool:
    mode = str(
        payload.get("video_type")
        or payload.get("veo_video_type")
        or payload.get("video_mode")
        or payload.get("reference_mode")
        or ""
    ).strip().lower()
    if mode in I2V_VIDEO_MODES:
        return True
    if str(payload.get("first_image_url") or payload.get("firstImageUrl") or "").strip():
        return True
    if str(payload.get("image_url") or payload.get("imageUrl") or "").strip():
        return True
    if _public_images_list(payload.get("images")):
        return True
    if str(payload.get("last_image_url") or payload.get("lastImageUrl") or "").strip():
        return True
    if str(payload.get("end_image_url") or payload.get("endImageUrl") or "").strip():
        return True
    return False


def _gpt_image2_str(value: Any) -> str:
    return str(value or "").strip()


def _gpt_image2_parse_size(size: Any) -> Optional[tuple[int, int]]:
    s = _gpt_image2_str(size)
    m = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", s, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _gpt_image2_size_error(size: str) -> Optional[str]:
    dims = _gpt_image2_parse_size(size)
    if not dims:
        return "size must be 'auto' or a WIDTHxHEIGHT string, for example 1536x1024"
    w, h = dims
    if w <= 0 or h <= 0:
        return "size width and height must be positive"
    if w % 16 != 0 or h % 16 != 0:
        return "gpt-image-2 size width and height must both be multiples of 16"
    if max(w, h) > GPT_IMAGE2_MAX_EDGE:
        return f"gpt-image-2 size maximum edge must be <= {GPT_IMAGE2_MAX_EDGE}px"
    short_edge = min(w, h)
    if short_edge <= 0 or (max(w, h) / short_edge) > 3:
        return "gpt-image-2 size long edge to short edge ratio must not exceed 3:1"
    pixels = w * h
    if pixels < GPT_IMAGE2_MIN_PIXELS or pixels > GPT_IMAGE2_MAX_PIXELS:
        return (
            "gpt-image-2 size total pixels must be between "
            f"{GPT_IMAGE2_MIN_PIXELS} and {GPT_IMAGE2_MAX_PIXELS}"
        )
    return None


def _gpt_image2_normalize_size(size: Any) -> str:
    s = _gpt_image2_str(size)
    if not s:
        return ""
    if s.lower() == "auto":
        return "auto"
    dims = _gpt_image2_parse_size(s)
    if dims:
        s = f"{dims[0]}x{dims[1]}"
    err = _gpt_image2_size_error(s)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return s


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


def _gpt_image2_resolution_from_payload(payload: Dict[str, Any]) -> str:
    size = _gpt_image2_normalize_size(payload.get("size"))
    if size:
        tier = _gpt_image2_resolution_tier_for_size(size)
        if tier:
            return tier
    for key in ("resolution", "size_tier", "gpt_image2_resolution", "image_resolution"):
        s = _gpt_image2_str(payload.get(key)).lower().replace(" ", "")
        if s in {"1", "1k", "k1"}:
            return "1k"
        if s in {"2", "2k", "k2"}:
            return "2k"
        if s in {"4", "4k", "k4"}:
            return "4k"
    public = _gpt_image2_str(payload.get("gpt_image2_model") or payload.get("model")).lower()
    if public in GPT_IMAGE2_VIDEO_MODELS:
        return GPT_IMAGE2_VIDEO_MODELS[public]
    return "1k"


def _gpt_image2_ratio_from_size(size: Any) -> str:
    s = _gpt_image2_str(size)
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
    if w <= 0 or h <= 0:
        return ""
    import math

    g = math.gcd(w, h) or 1
    return f"{w // g}:{h // g}"


def _gpt_image2_ratio_from_payload(payload: Dict[str, Any]) -> str:
    ratio = _gpt_image2_str(
        payload.get("ratio")
        or payload.get("aspect_ratio")
        or payload.get("size_ratio")
        or payload.get("aspectRatio")
    )
    if ratio:
        return ratio
    return _gpt_image2_ratio_from_size(payload.get("size")) or "1:1"


def _gpt_image2_size_from_payload(payload: Dict[str, Any], resolution: str) -> str:
    size = _gpt_image2_normalize_size(payload.get("size"))
    if size:
        return size
    ratio = _gpt_image2_ratio_from_payload(payload)
    by_ratio = GPT_IMAGE2_SIZE_TABLE.get(resolution) or GPT_IMAGE2_SIZE_TABLE["1k"]
    return by_ratio.get(ratio) or by_ratio.get("1:1") or "1024x1024"


def _normalize_gpt_image2_video_payload(payload: Dict[str, Any], model_key: str) -> Dict[str, Any]:
    duration = payload.get("duration")
    if duration is not None and duration != 1:
        try:
            if int(float(str(duration).strip())) != 1:
                raise ValueError
        except Exception:
            raise HTTPException(status_code=400, detail=f"{payload.get('model') or model_key} only supports duration=1 (image generation mode)")

    resolution = _gpt_image2_resolution_from_payload(payload)
    public_model = model_key if GPT_IMAGE2_VIDEO_MODELS.get(model_key) == resolution else f"gpt-image2-{resolution}"
    size = _gpt_image2_size_from_payload(payload, resolution)
    ratio = _gpt_image2_ratio_from_size(size)
    if not ratio:
        ratio = _gpt_image2_str(
            payload.get("ratio")
            or payload.get("aspect_ratio")
            or payload.get("size_ratio")
            or payload.get("aspectRatio")
        )

    payload["duration"] = 1
    payload["workflow_kind"] = "image"
    payload["model"] = public_model
    payload["model_code"] = "gpt-image-2"
    payload["image_model_name"] = "gpt-image-2"
    payload["gpt_image2_model"] = public_model
    payload["resolution"] = resolution
    payload["size_tier"] = resolution.upper()
    payload["size"] = size
    if ratio:
        payload["aspect_ratio"] = ratio
        payload["ratio"] = ratio
    return payload


def _normalize_video_task_payload(payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Map OpenAI-compatible public video model names to internal task types."""

    payload = dict(payload or {})
    model = str(payload.get("model") or "").strip()
    model_key = model.lower()
    if model_key in ZARKLAB_PUBLIC_MODEL_ALIASES:
        task_type_code = "zarklab_video"
        payload["model"] = model_key
        payload["zarklab_model"] = ZARKLAB_PUBLIC_MODEL_ALIASES[model_key]
    elif model_key in DREAMINA_PUBLIC_MODEL_ALIASES:
        task_type_code = "dreamina_workflow"
        payload["model"] = DREAMINA_PUBLIC_MODEL_ALIASES[model_key]
        payload["model_name"] = DREAMINA_PUBLIC_MODEL_ALIASES[model_key]
    elif model_key in {"seedance-2", "seedance-2-fast", "seedance-2-mini"} or model_key in LEONARDO_PUBLIC_MODEL_ALIASES:
        task_type_code = "leonardo_workflow"
        leonardo_model = LEONARDO_PUBLIC_MODEL_ALIASES.get(model_key) or model_key
        payload["model"] = leonardo_model
        payload["leonardo_model"] = leonardo_model
    elif model_key in {"nana-banana-2"}:
        task_type_code = "veo_workflow"
        payload["n_frames"] = 1
        payload["image_model_name"] = "NARWHAL"
    elif model_key in {"nana-banana-pro"}:
        task_type_code = "veo_workflow"
        payload["n_frames"] = 1
        payload["image_model_name"] = "GEM_PIX_2"
    elif model_key in VEO31_VIDEO_MODEL_KEYS:
        task_type_code = "veo_workflow"
        # Flow currently exposes Veo 3.1 Lite/Fast/Quality as fixed 8s models.
        # Some frontends always send their own duration default; accept it and
        # normalize here so model selection does not fail with a public 400.
        payload["duration"] = 8
        payload["n_frames"] = 240
        payload["videoModelKey"] = VEO31_VIDEO_MODEL_KEYS[model_key]
    elif model_key in GEMINI_OMNI_VIDEO_MODEL_SET:
        task_type_code = "veo_workflow"
        duration = payload.get("duration")
        if duration is None:
            duration = 8
            payload["duration"] = duration
        try:
            duration_i = int(duration)
        except Exception:
            raise HTTPException(status_code=400, detail="gemini-omni duration must be one of 4, 6, 8, 10")
        if duration_i not in {4, 6, 8, 10}:
            raise HTTPException(status_code=400, detail="gemini-omni only supports duration=4, 6, 8, or 10")
        payload["duration"] = duration_i
        payload["model"] = "gemini-omni"
    elif model_key in GPT_IMAGE2_VIDEO_MODELS or model_key in GPT_IMAGE2_ALIAS_MODELS:
        task_type_code = "gpt_workflow"
        payload = _normalize_gpt_image2_video_payload(payload, model_key)
    else:
        task_type_code = model
    return task_type_code, payload


def _build_openai_chat_completion(model: str, content: str) -> Dict[str, Any]:
    now = int(time.time())
    return {
        "id": f"chatcmpl-fpbrowser2api-{now}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }


def _timestamp_ms(value: Any = None) -> int:
    """Return a NewAPI/OpenAI-compatible millisecond timestamp."""

    if value is None:
        return int(time.time() * 1000)
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return int(dt.timestamp() * 1000)
    if isinstance(value, (int, float)):
        # 13-digit values are already milliseconds; 10-digit values are seconds.
        fv = float(value)
        return int(fv if fv > 10_000_000_000 else fv * 1000)
    raw = str(value or "").strip()
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T", 1))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
    return int(time.time() * 1000)


def _normalize_newapi_task_status(status: Any) -> str:
    s = str(status or "").strip().lower()
    # NewAPI/中转站实现对异步任务状态的解析不完全一致：
    # - 有的认 OpenAI-ish 的 in_progress
    # - 有的认 Midjourney/通用异步任务的 processing
    # 对外优先使用更常见的 processing，避免上游只在 processing/completed/failed
    # 分支里更新任务状态。
    if s in {"running", "in_progress", "processing"}:
        return "processing"
    if s in {"queued", "completed", "failed"}:
        return s
    return s or "queued"


def _parse_task_prompt_payload(prompt_text: Any) -> Dict[str, Any]:
    raw = str(prompt_text or "").strip()
    if not raw or not raw.startswith("{"):
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _maybe_number(v: Any) -> Any:
    if v is None:
        return None
    try:
        fv = float(v)
        iv = int(fv)
        return iv if fv == iv else fv
    except Exception:
        return v


def _public_aspect_ratio(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("aspect_ratio", "ratio", "size_ratio", "image_aspect_ratio"):
        val = payload.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _public_duration(payload: Dict[str, Any]) -> Any:
    for key in ("duration", "seconds"):
        val = payload.get(key)
        if val is not None and str(val).strip():
            return _maybe_number(val)
    return None


def _build_newapi_video_create_response(task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build NewAPI-compatible response for POST /v1/videos."""

    p = dict(payload or {})
    model = str(p.get("model") or "").strip()
    duration = _public_duration(p)
    aspect_ratio = _public_aspect_ratio(p)
    resp: Dict[str, Any] = {
        "id": task_id,
        "task_id": task_id,
        "object": "video",
        "created_at": _timestamp_ms(),
        "status": "queued",
        "progress": 0,
        "model": model,
        "video_url": None,
        "metadata": {"result_urls": []},
    }
    if duration is not None:
        resp["seconds"] = str(duration)
        resp["duration"] = duration
    if aspect_ratio:
        resp["aspect_ratio"] = aspect_ratio
    prompt = str(p.get("prompt") or "").strip()
    if prompt:
        resp["prompt"] = prompt
    return resp


def _extract_public_result_urls(result: Any) -> tuple[Optional[str], Optional[str], list[str]]:
    if not isinstance(result, dict):
        return None, None, []
    share_url = str(
        result.get("share_url")
        or result.get("video_url")
        or result.get("image_url")
        or result.get("url")
        or ""
    ).strip()
    if not share_url:
        return None, None, []
    kind = str(result.get("workflow_kind") or result.get("type") or "").strip().lower()
    if "audio" in kind:
        audio_urls: list[str] = []
        for value in result.get("urls") or []:
            url = _public_one_url(value)
            if url and url not in audio_urls:
                audio_urls.append(url)
        if share_url not in audio_urls:
            audio_urls.insert(0, share_url)
        return None, None, audio_urls
    is_image = "image" in kind and "video" not in kind
    if is_image:
        return None, share_url, [share_url]
    return share_url, None, [share_url]


def _public_result_source_urls(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []

    values: list[Any] = []
    for key in ("urls", "result_urls", "images", "files"):
        val = result.get(key)
        if isinstance(val, list):
            values.extend(val)
    for key in ("share_url", "video_url", "image_url", "audio_url", "url"):
        values.append(result.get(key))

    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = _public_one_url(value)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _is_gpt_estuary_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    return parsed.scheme == "https" and host == "chatgpt.com" and path.startswith("/backend-api/estuary/content")


def _is_leonardo_cdn_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return parsed.scheme == "https" and host == "cdn.leonardo.ai" and path.startswith("/users/")


def _asset_source_kind(url: str) -> Optional[str]:
    if _is_gpt_estuary_url(url):
        return "gpt"
    if _is_leonardo_cdn_url(url):
        return "leonardo"
    return None


def _task_asset_sources(result: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url in _public_result_source_urls(result):
        kind = _asset_source_kind(url)
        if not kind or url in seen:
            continue
        seen.add(url)
        out.append((url, kind))
    return out


def _gpt_asset_sig(task_id: str, asset_index: int, exp: int) -> str:
    msg = f"{task_id}:{int(asset_index)}:{int(exp)}".encode("utf-8")
    key = str(config.api_key or "").encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _build_task_asset_proxy_urls(task_id: str, result: Any) -> list[str]:
    tid = str(task_id or "").strip()
    if not tid:
        return []
    sources = _task_asset_sources(result)
    if not sources:
        return []
    exp = int(time.time()) + GPT_ASSET_SIGN_TTL_SEC
    out: list[str] = []
    for idx, _ in enumerate(sources):
        sig = _gpt_asset_sig(tid, idx, exp)
        out.append(f"/v1/tasks/{tid}/assets/{idx}?exp={exp}&sig={sig}")
    return out


def _safe_gpt_public_asset_task_id(task_id: str) -> str:
    tid = str(task_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", tid):
        raise HTTPException(status_code=400, detail="invalid task_id")
    return tid


def _safe_gpt_public_asset_index(asset_index: int) -> int:
    try:
        idx = int(asset_index)
    except Exception:
        raise HTTPException(status_code=400, detail="asset index must be an integer")
    if idx < 0 or idx > 100:
        raise HTTPException(status_code=400, detail="asset index out of range")
    return idx


def _gpt_public_asset_rel_url(task_id: str, asset_index: int, ext: str = ".png") -> str:
    tid = _safe_gpt_public_asset_task_id(task_id)
    idx = _safe_gpt_public_asset_index(asset_index)
    suffix = str(ext or ".png").lower()
    if suffix not in {".avif", ".gif", ".jpg", ".jpeg", ".png", ".svg", ".webp"}:
        suffix = ".png"
    if suffix == ".jpeg":
        suffix = ".jpg"
    return f"/public/gpt-assets/{tid}-{idx}{suffix}"


def _gpt_public_asset_path(task_id: str, asset_index: int, ext: str = ".png") -> Path:
    rel = _gpt_public_asset_rel_url(task_id, asset_index, ext)
    filename = rel.rsplit("/", 1)[-1]
    return GPT_PUBLIC_ASSET_DIR / filename


def _find_existing_gpt_public_asset(task_id: str, asset_index: int) -> Optional[tuple[Path, str]]:
    tid = _safe_gpt_public_asset_task_id(task_id)
    idx = _safe_gpt_public_asset_index(asset_index)
    for ext in (".png", ".jpg", ".webp", ".avif", ".gif", ".svg"):
        path = GPT_PUBLIC_ASSET_DIR / f"{tid}-{idx}{ext}"
        if path.is_file():
            return path, _gpt_public_asset_rel_url(tid, idx, ext)
    return None


async def _materialize_gpt_public_assets(task_id: str, result: Any) -> list[str]:
    tid = _safe_gpt_public_asset_task_id(task_id)
    sources = [u for u in _public_result_source_urls(result) if _is_gpt_estuary_url(u)]
    if not sources:
        return []

    GPT_PUBLIC_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    access_token: Optional[str] = None
    urls: list[str] = []

    for idx, source_url in enumerate(sources):
        existing = _find_existing_gpt_public_asset(tid, idx)
        if existing is not None:
            urls.append(existing[1])
            continue

        if access_token is None:
            access_token = await _load_gpt_asset_access_token(tid)
        data, media_type = await _fetch_gpt_asset_bytes(source_url, access_token)
        if len(data) > GPT_PUBLIC_ASSET_MAX_BYTES:
            raise HTTPException(status_code=413, detail="GPT asset is too large")

        ext = GPT_PUBLIC_ASSET_EXT_BY_TYPE.get(str(media_type or "").split(";", 1)[0].strip().lower(), ".png")
        path = _gpt_public_asset_path(tid, idx, ext)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_bytes(data)
            tmp_path.replace(path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
        urls.append(_gpt_public_asset_rel_url(tid, idx, ext))

    return urls


async def _add_materialized_public_asset_urls(payload: Dict[str, Any], result: Any) -> Dict[str, Any]:
    task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
    if not task_id or not isinstance(result, dict):
        return payload
    if not any(_is_gpt_estuary_url(u) for u in _public_result_source_urls(result)):
        return payload

    try:
        public_urls = await _materialize_gpt_public_assets(task_id, result)
    except HTTPException as e:
        logger.warning("materialize GPT public asset skipped: task=%s status=%s detail=%s", task_id, e.status_code, e.detail)
        return payload
    except Exception as e:
        logger.warning("materialize GPT public asset failed: task=%s err=%s", task_id, e)
        return payload
    if not public_urls:
        return payload

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        payload["metadata"] = metadata
    metadata["public_urls"] = public_urls

    payload["public_urls"] = public_urls
    payload["public_image_url"] = public_urls[0]
    payload["public_url"] = public_urls[0]

    response_result = payload.get("result")
    if isinstance(response_result, dict):
        response_result["public_urls"] = public_urls
        response_result["public_image_url"] = public_urls[0]
        response_result["public_url"] = public_urls[0]
    return payload


def _add_absolute_public_asset_urls(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    out = dict(payload or {})
    public_urls = [str(u or "").strip() for u in (out.get("public_urls") or []) if str(u or "").strip()]
    if not public_urls:
        metadata = out.get("metadata")
        if isinstance(metadata, dict):
            public_urls = [str(u or "").strip() for u in (metadata.get("public_urls") or []) if str(u or "").strip()]
    if not public_urls:
        return out

    absolute = [_absolute_url(request, u) for u in public_urls]
    result_obj = out.get("result")
    result_kind = (
        str(result_obj.get("workflow_kind") or result_obj.get("type") or "").strip().lower()
        if isinstance(result_obj, dict)
        else ""
    )
    is_audio = "audio" in result_kind
    out["public_urls_absolute"] = absolute
    out["public_url_absolute"] = absolute[0]
    out["url"] = absolute[0]
    out["result_urls"] = absolute
    if is_audio:
        out["public_audio_url_absolute"] = absolute[0]
        out["audio_url"] = absolute[0]
    else:
        out["public_image_url_absolute"] = absolute[0]
        out["image_url"] = absolute[0]

    metadata = out.get("metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        metadata["result_urls"] = absolute
        metadata["public_urls_absolute"] = absolute
        out["metadata"] = metadata

    result = out.get("result")
    if isinstance(result, dict):
        result = dict(result)
        result["result_urls"] = absolute
        result["public_urls_absolute"] = absolute
        result["public_url_absolute"] = absolute[0]
        result["url"] = absolute[0]
        if is_audio:
            result["public_audio_url_absolute"] = absolute[0]
            result["audio_url"] = absolute[0]
            result["urls"] = absolute
        else:
            result["public_image_url_absolute"] = absolute[0]
            result["image_url"] = absolute[0]
        out["result"] = result
    return out


def _payload_contains_gpt_estuary_url(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False

    values: list[Any] = []
    for key in ("image_url", "url", "share_url", "video_url"):
        values.append(payload.get(key))
    for key in ("result_urls", "urls"):
        val = payload.get(key)
        if isinstance(val, list):
            values.extend(val)

    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("result_urls", "urls"):
            val = metadata.get(key)
            if isinstance(val, list):
                values.extend(val)

    result = payload.get("result")
    if isinstance(result, dict):
        values.extend(_public_result_source_urls(result))

    for value in values:
        url = _public_one_url(value)
        if url and _is_gpt_estuary_url(url):
            return True
    return False


def _payload_contains_leonardo_cdn_url(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False

    values: list[Any] = []
    for key in ("image_url", "url", "share_url", "video_url"):
        values.append(payload.get(key))
    for key in ("result_urls", "urls"):
        val = payload.get(key)
        if isinstance(val, list):
            values.extend(val)

    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("result_urls", "urls"):
            val = metadata.get(key)
            if isinstance(val, list):
                values.extend(val)

    result = payload.get("result")
    if isinstance(result, dict):
        values.extend(_public_result_source_urls(result))

    for value in values:
        url = _public_one_url(value)
        if url and _is_leonardo_cdn_url(url):
            return True
    return False


def _add_public_result_urls(payload: Dict[str, Any], result: Any) -> Dict[str, Any]:
    video_url, image_url, result_urls = _extract_public_result_urls(result)
    source_items = _task_asset_sources(result)
    proxy_urls = _build_task_asset_proxy_urls(str(payload.get("task_id") or payload.get("id") or ""), result)
    if not result_urls and not proxy_urls:
        return payload

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        payload["metadata"] = metadata
    if result_urls:
        metadata["result_urls"] = result_urls

    if video_url:
        payload["video_url"] = video_url
        payload.setdefault("url", video_url)
    if image_url:
        payload["image_url"] = image_url
        payload.setdefault("url", image_url)
    if isinstance(result, dict):
        kind = str(result.get("workflow_kind") or result.get("type") or "").strip().lower()
        audio_url = str(result.get("audio_url") or result.get("url") or "").strip() if "audio" in kind else ""
        if audio_url:
            payload["audio_url"] = audio_url
            payload.setdefault("url", audio_url)
        public_urls = [
            str(url or "").strip()
            for url in (result.get("public_urls") or [])
            if str(url or "").strip()
        ]
        if public_urls:
            payload["public_urls"] = public_urls
            metadata["public_urls"] = public_urls
            payload["public_url"] = public_urls[0]
            if "audio" in kind:
                payload["public_audio_url"] = public_urls[0]
            else:
                payload["public_image_url"] = public_urls[0]

    if proxy_urls:
        metadata["proxy_urls"] = proxy_urls
        payload["proxy_urls"] = proxy_urls
        first_kind = source_items[0][1] if source_items else ""
        if first_kind == "leonardo":
            payload["proxy_video_url"] = proxy_urls[0]
        else:
            payload["proxy_image_url"] = proxy_urls[0]
        response_result = payload.get("result")
        if isinstance(response_result, dict):
            response_result["proxy_urls"] = proxy_urls
            if first_kind == "leonardo":
                response_result["proxy_video_url"] = proxy_urls[0]
            else:
                response_result["proxy_image_url"] = proxy_urls[0]
    return payload


def _absolute_url(request: Request, url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return raw
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("data:"):
        return raw
    return str(request.base_url).rstrip("/") + raw


def _status_response_log_entry(endpoint: str, payload: Dict[str, Any], request: Optional[Request]) -> Dict[str, Any]:
    headers: Dict[str, Any] = {}
    if request is not None:
        headers = {
            "host": str(request.headers.get("host") or ""),
            "x_forwarded_for": str(request.headers.get("x-forwarded-for") or ""),
            "x_forwarded_host": str(request.headers.get("x-forwarded-host") or ""),
            "x_forwarded_proto": str(request.headers.get("x-forwarded-proto") or ""),
            "user_agent": str(request.headers.get("user-agent") or ""),
        }
    return {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "task_id": str(payload.get("task_id") or payload.get("id") or ""),
        "status": str(payload.get("status") or ""),
        "request": {
            "method": str(request.method if request is not None else ""),
            "url": str(request.url if request is not None else ""),
            "client": str(request.client.host if request is not None and request.client else ""),
            "headers": headers,
        },
        "response": payload,
    }


def _write_status_response_log_sync(entry: Dict[str, Any]) -> None:
    STATUS_RESPONSE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    with STATUS_RESPONSE_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def _log_status_response(endpoint: str, payload: Dict[str, Any], request: Optional[Request]) -> None:
    try:
        entry = _status_response_log_entry(endpoint, payload, request)
        await asyncio.to_thread(_write_status_response_log_sync, entry)
    except Exception as e:
        logger.warning("status response log failed: endpoint=%s task=%s err=%s", endpoint, payload.get("task_id") or payload.get("id"), e)


def _add_absolute_proxy_urls(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    out = dict(payload or {})
    proxy_urls = [str(u or "").strip() for u in (out.get("proxy_urls") or []) if str(u or "").strip()]
    if not proxy_urls:
        metadata = out.get("metadata")
        if isinstance(metadata, dict):
            proxy_urls = [str(u or "").strip() for u in (metadata.get("proxy_urls") or []) if str(u or "").strip()]
    if not proxy_urls:
        return out

    absolute = [_absolute_url(request, u) for u in proxy_urls]
    has_gpt_asset = _payload_contains_gpt_estuary_url(out)
    has_leonardo_asset = _payload_contains_leonardo_cdn_url(out)
    out["proxy_urls_absolute"] = absolute
    if has_leonardo_asset:
        out["proxy_video_url_absolute"] = absolute[0]
        out["video_url"] = absolute[0]
        out["url"] = absolute[0]
        out["result_urls"] = absolute
    else:
        out["proxy_image_url_absolute"] = absolute[0]
    if has_gpt_asset:
        out["image_url"] = absolute[0]
        out["url"] = absolute[0]
        out["result_urls"] = absolute

    metadata = out.get("metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        metadata["proxy_urls_absolute"] = absolute
        if has_leonardo_asset:
            metadata["result_urls"] = absolute
        if has_gpt_asset:
            metadata["result_urls"] = absolute
        out["metadata"] = metadata

    result = out.get("result")
    if isinstance(result, dict):
        result = dict(result)
        result["proxy_urls_absolute"] = absolute
        result_has_gpt_asset = _payload_contains_gpt_estuary_url(result)
        result_has_leonardo_asset = _payload_contains_leonardo_cdn_url(result)
        if result_has_leonardo_asset:
            result["proxy_video_url_absolute"] = absolute[0]
            result["share_url"] = absolute[0]
            result["video_url"] = absolute[0]
            result["url"] = absolute[0]
            result["urls"] = absolute
            result["result_urls"] = absolute
        else:
            result["proxy_image_url_absolute"] = absolute[0]
        if result_has_gpt_asset:
            result["image_url"] = absolute[0]
            result["url"] = absolute[0]
            result["result_urls"] = absolute
        out["result"] = result
    return out


def _request_has_valid_gpt_asset_auth(request: Request, task_id: str, asset_index: int) -> bool:
    authorization = str(request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if AuthManager.verify_api_key(token):
            return True

    exp_raw = str(request.query_params.get("exp") or "").strip()
    sig = str(request.query_params.get("sig") or "").strip().lower()
    if not exp_raw or not sig:
        return False
    try:
        exp = int(exp_raw)
    except Exception:
        return False
    if exp < int(time.time()):
        return False
    expected = _gpt_asset_sig(str(task_id or "").strip(), int(asset_index), exp)
    return hmac.compare_digest(sig, expected)


async def _get_task_asset_source(task_id: str, asset_index: int) -> tuple[str, str]:
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    tid = str(task_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="task_id cannot be empty")
    try:
        idx = int(asset_index)
    except Exception:
        raise HTTPException(status_code=400, detail="asset index must be an integer")
    if idx < 0:
        raise HTTPException(status_code=400, detail="asset index must be >= 0")

    task = await db.get_task(tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    sources = _task_asset_sources(task.result)
    if idx >= len(sources):
        raise HTTPException(status_code=404, detail="asset not found")
    return sources[idx]


def _parse_gpt_token_expires(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T", 1))
    except Exception:
        return None


async def _load_gpt_asset_access_token(task_id: str) -> str:
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")

    tid = str(task_id or "").strip()
    rows: list[tuple[Any, Any]] = []
    async with db._read_conn() as conn:  # type: ignore[attr-defined]
        cur = await conn.execute(
            """
            SELECT ttw.sora_access_token, ttw.sora_access_expires
            FROM tasks task
            JOIN task_type_windows ttw ON ttw.window_pk = task.window_pk
            JOIN task_types tt ON tt.id = ttw.task_type_id
            WHERE task.task_id = ?
              AND tt.code = 'gpt_workflow'
              AND ttw.deleted = 0
              AND TRIM(COALESCE(ttw.sora_access_token, '')) != ''
            ORDER BY ttw.id DESC
            LIMIT 1
            """,
            (tid,),
        )
        row = await cur.fetchone()
        if row:
            rows.append((row[0], row[1]))

        if not rows:
            cur = await conn.execute(
                """
                SELECT ttw.sora_access_token, ttw.sora_access_expires
                FROM task_type_windows ttw
                JOIN task_types tt ON tt.id = ttw.task_type_id
                WHERE tt.code = 'gpt_workflow'
                  AND ttw.deleted = 0
                  AND TRIM(COALESCE(ttw.sora_access_token, '')) != ''
                ORDER BY ttw.id DESC
                LIMIT 1
                """
            )
            row = await cur.fetchone()
            if row:
                rows.append((row[0], row[1]))

    if not rows:
        raise HTTPException(status_code=409, detail="GPT access token is missing; reconnect the GPT browser window")

    token = str(rows[0][0] or "").strip()
    expires_at = _parse_gpt_token_expires(rows[0][1])
    if expires_at is not None:
        now = datetime.now(timezone.utc) if expires_at.tzinfo is not None else datetime.now()
        if expires_at <= now:
            raise HTTPException(status_code=409, detail="GPT access token expired; reconnect the GPT browser window")
    if not token:
        raise HTTPException(status_code=409, detail="GPT access token is empty; reconnect the GPT browser window")
    return token


async def _fetch_gpt_asset_bytes(source_url: str, access_token: str) -> tuple[bytes, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(source_url, headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"failed to fetch GPT asset: {e}")

    if resp.status_code in {401, 403}:
        raise HTTPException(status_code=409, detail="GPT asset rejected the saved token; reconnect the GPT browser window")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"GPT asset fetch failed with HTTP {resp.status_code}")

    data = resp.content or b""
    if len(data) > GPT_ASSET_MAX_BYTES:
        raise HTTPException(status_code=413, detail="GPT asset is too large")
    content_type = str(resp.headers.get("content-type") or "application/octet-stream").split(";", 1)[0].strip().lower()
    if not (content_type.startswith("image/") or content_type == "application/octet-stream"):
        raise HTTPException(status_code=502, detail=f"GPT asset returned unexpected content-type: {content_type}")
    return data, content_type or "application/octet-stream"


def _asset_ext_for_media_type(media_type: str, source_url: str) -> str:
    mt = str(media_type or "").split(";", 1)[0].strip().lower()
    by_type = {
        "image/avif": ".avif",
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/svg+xml": ".svg",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
    }
    if mt in by_type:
        return by_type[mt]
    path = (urlparse(str(source_url or "")).path or "").lower()
    for ext in (".mp4", ".mov", ".webm", ".avif", ".gif", ".jpg", ".jpeg", ".png", ".svg", ".webp"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".bin"


def _media_type_for_asset_response(media_type: str, source_url: str) -> str:
    mt = str(media_type or "").split(";", 1)[0].strip().lower()
    if mt and mt != "application/octet-stream":
        return mt
    ext = _asset_ext_for_media_type(media_type, source_url)
    by_ext = {
        ".avif": "image/avif",
        ".gif": "image/gif",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
    }
    return by_ext.get(ext, mt or "application/octet-stream")


async def _fetch_leonardo_asset_bytes(source_url: str) -> tuple[bytes, str]:
    headers = {
        "Accept": "video/mp4,video/webm,video/*,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
    }
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(source_url, headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"failed to fetch Leonardo asset: {e}")

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Leonardo asset fetch failed with HTTP {resp.status_code}")

    data = resp.content or b""
    if len(data) > LEONARDO_ASSET_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Leonardo asset is too large")
    content_type = _media_type_for_asset_response(str(resp.headers.get("content-type") or ""), source_url)
    if not (content_type.startswith("video/") or content_type == "application/octet-stream"):
        raise HTTPException(status_code=502, detail=f"Leonardo asset returned unexpected content-type: {content_type}")
    return data, content_type


@router.get("/public/gpt-assets/{filename}")
async def get_public_gpt_asset(filename: str):
    name = str(filename or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}-[0-9]{1,3}\.(?:avif|gif|jpg|jpeg|png|svg|webp)", name):
        raise HTTPException(status_code=404, detail="asset not found")
    path = (GPT_PUBLIC_ASSET_DIR / name).resolve()
    root = GPT_PUBLIC_ASSET_DIR.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="asset not found")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(
        str(path),
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/public/elevenlabs-assets/{filename}")
async def get_public_elevenlabs_asset(filename: str):
    name = str(filename or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}-[0-9]{1,2}\.(?:alaw|mp3|opus|pcm|ulaw|wav)", name):
        raise HTTPException(status_code=404, detail="asset not found")
    path = (ELEVENLABS_PUBLIC_ASSET_DIR / name).resolve()
    root = ELEVENLABS_PUBLIC_ASSET_DIR.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="asset not found")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(
        str(path),
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _get_newapi_video_status_response(task_id: str, request: Optional[Request] = None) -> JSONResponse:
    """NewAPI-compatible status response for GET /v1/videos/{task_id}."""

    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    tid = (task_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="task_id 不能为空")

    task = await db.get_task(tid)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")


    payload = _parse_task_prompt_payload(getattr(task, "prompt", None))
    result = task.result if isinstance(task.result, dict) else None
    status = _normalize_newapi_task_status(task.status)
    video_url, image_url, result_urls = _extract_public_result_urls(result)
    model = str(payload.get("model") or (result or {}).get("model") or "").strip()
    duration = _public_duration(payload)
    aspect_ratio = _public_aspect_ratio(payload)

    resp: Dict[str, Any] = {
        "id": task.task_id,
        "task_id": task.task_id,
        "object": "video",
        "created_at": _timestamp_ms(task.created_at),
        "status": status,
        # 兼容部分中转站/面板只读取 state 或 task_status 的实现。
        "state": status,
        "task_status": status,
        "progress": int(task.progress or 0),
        "model": model,
        "video_url": video_url,
        "metadata": {"result_urls": result_urls},
        # 冗余标志，便于中转站判断是否终态。
        "success": status == "completed",
        "final": status in {"completed", "failed"},
    }

    if result and isinstance(result.get("runtime_progress"), dict):
        resp["runtime_progress"] = result["runtime_progress"]
        metadata = resp.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            resp["metadata"] = metadata
        metadata["runtime_progress"] = result["runtime_progress"]
        stage = str(result["runtime_progress"].get("stage") or "").strip()
        if stage:
            resp["stage"] = stage

    
    if image_url:
        resp["image_url"] = image_url
    if video_url or image_url:
        resp["url"] = video_url or image_url
    if duration is not None:
        resp["seconds"] = str(duration)
        resp["duration"] = duration
    if aspect_ratio:
        resp["aspect_ratio"] = aspect_ratio
    if task.completed_at:
        resp["completed_at"] = _timestamp_ms(task.completed_at)
    if status == "failed":
        resp["error"] = {
            "message": task.error_message or "task failed",
            # code 使用字符串，避免上游 JSON 结构体按 string 解析失败或忽略。
            "code": str((result or {}).get("status_code") or (result or {}).get("error_type") or "task_failed"),
        }

    resp = _add_public_result_urls(resp, result)
    resp = await _add_materialized_public_asset_urls(resp, result)
    if request is not None:
        resp = _add_absolute_proxy_urls(resp, request)
        resp = _add_absolute_public_asset_urls(resp, request)
    await _log_status_response("GET /v1/videos/{task_id}", resp, request)
    return JSONResponse(content=resp)


async def _get_public_runtime_limits_cached() -> tuple[bool, int, int]:
    if not db:
        return False, DEFAULT_PUBLIC_CREATE_TASK_MAX_INFLIGHT, DEFAULT_SERVER_COUNT
    now = time.monotonic()
    global _system_config_cache
    expire_at, cached_flag, cached_inflight, cached_sc = _system_config_cache
    if now < expire_at and cached_flag is not None and cached_inflight is not None and cached_sc is not None:
        return bool(cached_flag), int(cached_inflight), int(cached_sc)
    syscfg = await db.get_system_config()
    flag = bool(getattr(syscfg, "stop_accepting_tasks", False))
    inflight = normalize_public_create_task_max_inflight(getattr(syscfg, "public_create_task_max_inflight", None))
    sc = normalize_server_count(getattr(syscfg, "server_count", None))
    _system_config_cache = (now + _SYSTEM_CONFIG_TTL_SEC, flag, inflight, sc)
    return flag, inflight, sc


async def _ensure_create_task_gate_by_db_config() -> tuple[asyncio.Semaphore, bool]:
    """Apply cached DB limits to runtime gate and scheduler pool."""
    if not task_service:
        raise RuntimeError("task_service not initialized")
    stop_accepting, inflight, server_count = await _get_public_runtime_limits_cached()
    async with _create_task_gate_lock:
        global _CREATE_TASK_MAX_INFLIGHT, _SERVER_COUNT, _create_task_semaphore
        if _create_task_semaphore is None or inflight != _CREATE_TASK_MAX_INFLIGHT or server_count != _SERVER_COUNT:
            _CREATE_TASK_MAX_INFLIGHT = inflight
            _SERVER_COUNT = server_count
            _create_task_semaphore = asyncio.Semaphore(_CREATE_TASK_MAX_INFLIGHT)
            task_service.set_browser_pool_limit(calc_public_browser_pool_limit(_CREATE_TASK_MAX_INFLIGHT, _SERVER_COUNT))
    if _create_task_semaphore is None:
        raise RuntimeError("create task gate not initialized")
    return _create_task_semaphore, stop_accepting


async def _get_task_type_by_code_cached(task_type_code: str):
    if not db:
        return None
    tcode = (task_type_code or "").strip()
    if not tcode:
        return None
    now = time.monotonic()
    row = _task_type_cache.get(tcode)
    if row and now < row[0]:
        return row[1]
    task_type = await db.get_task_type_by_code(tcode)
    _task_type_cache[tcode] = (now + _TASK_TYPE_TTL_SEC, task_type)
    return task_type


async def _get_cached_task_status_payload(task_id: str) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    async with _status_lock:
        row = _status_cache.get(task_id)
        if not row:
            return None
        expire_at, payload = row
        if now >= expire_at:
            _status_cache.pop(task_id, None)
            return None
        return payload


async def _set_task_status_cache(task_id: str, payload: Dict[str, Any]) -> None:
    status = str(payload.get("status") or "").lower()
    ttl = _STATUS_CACHE_TTL_FINAL_SEC if status in {"completed", "failed"} else _STATUS_CACHE_TTL_PENDING_SEC
    async with _status_lock:
        _status_cache[task_id] = (time.monotonic() + ttl, payload)


async def _create_task_from_request(body: CreateTaskRequest) -> Dict[str, Any]:
    """Shared task creation implementation for /v1/tasks and compatible APIs.

    Authentication is intentionally kept on the route handlers to avoid
    duplicate Depends execution when one public endpoint adapts into this
    helper.
    """
    if not db or not task_service:
        raise HTTPException(status_code=500, detail="service not initialized")

    acquired = False
    gate: asyncio.Semaphore | None = None
    try:
        try:
            gate, stop_accepting = await _ensure_create_task_gate_by_db_config()
        except Exception:
            gate = _create_task_semaphore
            stop_accepting = False
        if gate is None:
            raise HTTPException(status_code=500, detail="service not initialized")

        try:
            await asyncio.wait_for(gate.acquire(), timeout=_CREATE_TASK_ACQUIRE_TIMEOUT_SEC)
            acquired = True
        except asyncio.TimeoutError:
            raise HTTPException(status_code=429, detail="请求过于繁忙，请稍后重试")

        # 系统维护：停止接收新任务
        if stop_accepting:
            raise HTTPException(status_code=503, detail="服务器稳定性&每日容量升级10分钟，请10分钟后再试...")

        tcode = (body.task_type_code or "").strip()
        payload = dict(body.json or {})
        task_type = await _get_task_type_by_code_cached(tcode)
        if not task_type or task_type.deleted or not task_type.enabled:
            raise ValueError("task_type_code 不存在或未启用")

        if tcode == "leonardo_workflow" or str(task_type.create_task_handler or "").strip() == "leonardo_workflow":
            payload = _normalize_leonardo_prompt_aliases(payload)

        try:
            handler = get_create_task_handler(task_type.create_task_handler)
        except KeyError as e:
            raise ValueError(str(e))
        tid = await handler(
            CreateTaskContext(
                task_type=task_type,
                payload=payload,
                mapping_id=body.mapping_id,
                window_pk=body.window_pk,
                db=db,
                task_service=task_service,
            )
        )
        await _set_task_status_cache(
            tid,
            TaskStatusResponse(
                task_id=tid,
                status="queued",
                progress=0,
                result=None,
                error_message=None,
            ).model_dump(),
        )
        return {"success": True, "task_id": tid}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if acquired and gate is not None:
            gate.release()


async def _get_task_status_response(task_id: str, request: Optional[Request] = None) -> JSONResponse:
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    tid = (task_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="task_id 不能为空")

    cached = await _get_cached_task_status_payload(tid)
    if cached is not None:
        cached = await _add_materialized_public_asset_urls(dict(cached), cached.get("result"))
        if request is not None:
            cached = _add_absolute_proxy_urls(cached, request)
            cached = _add_absolute_public_asset_urls(cached, request)
        await _log_status_response("GET /v1/tasks/{task_id}", cached, request)
        return JSONResponse(content=cached)

    # 单飞：同一 task_id 的并发查询仅触发一次 DB 读取。
    leader = False
    async with _status_lock:
        fut = _status_inflight.get(tid)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            _status_inflight[tid] = fut
            leader = True

    if not leader:
        payload = await fut
        if payload is None:
            raise HTTPException(status_code=404, detail="task not found")
        payload = await _add_materialized_public_asset_urls(dict(payload), payload.get("result"))
        if request is not None:
            payload = _add_absolute_proxy_urls(payload, request)
            payload = _add_absolute_public_asset_urls(payload, request)
        await _log_status_response("GET /v1/tasks/{task_id}", payload, request)
        return JSONResponse(content=payload)

    payload: Optional[Dict[str, Any]] = None
    try:
        task = await db.get_task(tid)
        if task:
            payload = TaskStatusResponse(
                task_id=task.task_id,
                status=task.status,
                progress=int(task.progress or 0),
                result=task.result,
                error_message=task.error_message,
                content_violation=int(task.content_violation or 0),
            ).model_dump()
            payload = _add_public_result_urls(payload, task.result)
            payload = await _add_materialized_public_asset_urls(payload, task.result)
            await _set_task_status_cache(tid, payload)
        fut.set_result(payload)
    except Exception as e:
        fut.set_exception(e)
        raise
    finally:
        async with _status_lock:
            if _status_inflight.get(tid) is fut:
                _status_inflight.pop(tid, None)

    if payload is None:
        raise HTTPException(status_code=404, detail="task not found")
    if request is not None:
        payload = _add_absolute_proxy_urls(payload, request)
        payload = _add_absolute_public_asset_urls(payload, request)
    await _log_status_response("GET /v1/tasks/{task_id}", payload, request)
    return JSONResponse(content=payload)


@router.get("/v1/task-types")
async def list_task_types(api_key: str = Depends(verify_api_key_header)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    items = await db.list_task_types()
    return {"success": True, "task_types": [t.model_dump() for t in items]}

@router.get("/v1/task-types-public")
async def list_task_types(api_key: str = Depends(verify_api_key_header)):
    if not db:
        raise HTTPException(status_code=500, detail="db not initialized")
    items = await db.list_task_types_public()
    return {"success": True, "task_types": [t.model_dump() for t in items]}


@router.post("/v1/tasks")
async def create_task(
    api_key: str = Depends(verify_api_key_header),
    body: CreateTaskRequest = Body(...),
):
    return await _create_task_from_request(body)


@router.get("/v1/models")
async def list_openai_compatible_models(api_key: str = Depends(verify_api_key_header)):
    """OpenAI-compatible model list, mainly for NewAPI channel discovery/test."""

    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "created": now,
                "owned_by": "fpbrowser2api",
            }
            for model in OPENAI_COMPAT_VIDEO_MODELS
        ],
    }


@router.get("/v1/models/{model_id}")
async def get_openai_compatible_model(model_id: str, api_key: str = Depends(verify_api_key_header)):
    """OpenAI-compatible single model lookup."""

    model = (model_id or "").strip()
    if model.lower() not in OPENAI_COMPAT_VIDEO_MODEL_KEY_SET:
        raise HTTPException(status_code=404, detail="model not found")
    return {
        "id": model,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "fpbrowser2api",
    }


@router.post("/v1/chat/completions")
async def create_chat_completion_for_newapi_test(
    api_key: str = Depends(verify_api_key_header),
    body: Dict[str, Any] = Body(...),
):
    """Minimal OpenAI chat-compatible endpoint for NewAPI channel tests.

    NewAPI's OpenAI-channel "test" button probes `/v1/chat/completions`.
    The real video creation endpoint is `/v1/videos`; this endpoint only
    returns a lightweight success response for the public video model names so
    channel health checks do not create real video tasks.
    """

    model = str((body or {}).get("model") or "").strip()
    if model.lower() not in OPENAI_COMPAT_VIDEO_MODEL_KEY_SET:
        raise HTTPException(
            status_code=400,
            detail=f"model {model or '<empty>'} is not supported by chat test endpoint",
        )
    return _build_openai_chat_completion(
        model,
        f"fpbrowser2api channel test ok for {model}; use POST /v1/videos to create video tasks.",
    )


@router.post("/v1/videos")
async def create_video(
    api_key: str = Depends(verify_api_key_header),
    body: Any = Body(...),
):
    try:
        payload = _normalize_video_request_body(body)
        task_type_code, payload = _normalize_video_task_payload(payload)
        created = await _create_task_from_request(
            CreateTaskRequest(
                task_type_code=task_type_code,
                json=payload,
            )
        )
    except HTTPException as e:
        if int(getattr(e, "status_code", 0) or 0) == 400:
            logger.warning(
                "public /v1/videos bad request: detail=%s body=%s",
                getattr(e, "detail", ""),
                _public_video_body_debug(body),
            )
        raise
    task_id = str(created.get("task_id") or "").strip()
    if not task_id:
        return created
    return _build_newapi_video_create_response(task_id, payload)


@router.get("/v1/tasks/{task_id}/assets/{asset_index}")
async def get_task_asset(task_id: str, asset_index: int, request: Request):
    if not _request_has_valid_gpt_asset_auth(request, task_id, asset_index):
        raise HTTPException(status_code=401, detail="Missing or invalid API key / asset signature")

    source_url, source_kind = await _get_task_asset_source(task_id, asset_index)
    if source_kind == "gpt":
        access_token = await _load_gpt_asset_access_token(task_id)
        data, media_type = await _fetch_gpt_asset_bytes(source_url, access_token)
    elif source_kind == "leonardo":
        data, media_type = await _fetch_leonardo_asset_bytes(source_url)
    else:
        raise HTTPException(status_code=404, detail="asset not found")

    ext = _asset_ext_for_media_type(media_type, source_url)
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f'inline; filename="{task_id}-{asset_index}{ext}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/v1/tasks/{task_id}")
async def get_task_status(task_id: str, request: Request, api_key: str = Depends(verify_api_key_header)):
    return await _get_task_status_response(task_id, request)


@router.get("/v1/videos/{task_id}")
async def get_video_status(task_id: str, request: Request, api_key: str = Depends(verify_api_key_header)):
    return await _get_newapi_video_status_response(task_id, request)
