"""ElevenLabs web-app audio executor using a logged-in fingerprint window."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode, urlparse

from ..core.paths import DATA_DIR
from .playwright_broswer_context import append_log, get_or_create_ctx as get_or_create_playwright_ctx, safe_trim
from .task_executor_types import NonPenalizedTaskError, ProgressCB


DEFAULT_ELEVENLABS_TARGET = "https://elevenlabs.io/app/sound-effects"
ELEVENLABS_PUBLIC_ASSET_DIR = DATA_DIR / "elevenlabs_assets"
MONITOR_LOG_FILE = Path("logs/elevenlabs_monitor.log")

_AUTH_CACHE_SECONDS = 300.0
_AUTH_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}
_MAX_AUDIO_BYTES = 64 * 1024 * 1024
_SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_SFX_MODELS = {"eleven_text_to_sound_v2", "eleven_text_to_sound_v3"}
_TTS_OUTPUT_FORMATS = {
    "mp3_22050_32",
    "mp3_44100_32",
    "mp3_44100_64",
    "mp3_44100_96",
    "mp3_44100_128",
    "mp3_44100_192",
    "pcm_16000",
    "pcm_22050",
    "pcm_24000",
    "pcm_44100",
    "opus_48000_32",
    "opus_48000_64",
    "opus_48000_96",
    "opus_48000_128",
    "opus_48000_192",
    "ulaw_8000",
    "alaw_8000",
}


def _generation_headers(surface: str) -> Dict[str, str]:
    return {
        "X-Generation-Surface": surface,
        "X-Generation-Actor": "User",
    }


def _one_str(value: Any) -> str:
    return str(value or "").strip()


def _optional_bool(value: Any, *, default: Optional[bool] = None) -> Optional[bool]:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise NonPenalizedTaskError(f"invalid boolean value: {value}", status_code=400)


def _float_field(payload: Dict[str, Any], *keys: str, default: Optional[float] = None) -> Optional[float]:
    raw: Any = None
    found = False
    for key in keys:
        if key in payload and payload.get(key) not in (None, ""):
            raw = payload.get(key)
            found = True
            break
    if not found:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise NonPenalizedTaskError(f"payload.{keys[0]} must be a number", status_code=400) from exc


def _prompt_from_payload(payload: Dict[str, Any]) -> str:
    return _one_str(payload.get("text") or payload.get("prompt") or payload.get("input"))


def _workflow_mode(payload: Dict[str, Any]) -> str:
    raw = _one_str(
        payload.get("mode")
        or payload.get("action")
        or payload.get("workflow")
        or payload.get("audio_type")
        or "sound_effects"
    ).lower().replace("-", "_")
    if raw in {"sound", "sfx", "sound_effect", "sound_effects", "text_to_sound"}:
        return "sound_effects"
    if raw in {"tts", "speech", "text_to_speech"}:
        return "tts"
    raise NonPenalizedTaskError("payload.mode must be sound_effects or tts", status_code=400)


def _build_sfx_request_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = _prompt_from_payload(payload)
    if not text:
        raise NonPenalizedTaskError("payload.text or payload.prompt cannot be empty", status_code=400)
    if len(text) > 450:
        raise NonPenalizedTaskError("ElevenLabs sound effect prompt must be at most 450 characters", status_code=400)

    duration = _float_field(payload, "duration_seconds", "duration", default=None)
    if duration is not None and not 0.5 <= duration <= 30.0:
        raise NonPenalizedTaskError("payload.duration_seconds must be between 0.5 and 30", status_code=400)
    influence = _float_field(payload, "prompt_influence", default=0.3)
    if influence is None or not 0.0 <= influence <= 1.0:
        raise NonPenalizedTaskError("payload.prompt_influence must be between 0 and 1", status_code=400)

    model_id = _one_str(payload.get("model_id") or payload.get("model") or "eleven_text_to_sound_v2")
    if model_id not in _SFX_MODELS:
        raise NonPenalizedTaskError(
            f"unsupported ElevenLabs sound effect model {model_id!r}; use one of {sorted(_SFX_MODELS)}",
            status_code=400,
        )

    body: Dict[str, Any] = {
        "text": text,
        "prompt_influence": influence,
        "duration_seconds": duration,
        "loop": bool(_optional_bool(payload.get("loop"), default=False)),
        "model_id": model_id,
        "output_format": _one_str(payload.get("output_format") or "opus_48000_128"),
    }
    generations_raw = payload.get("number_of_generations")
    if generations_raw in (None, ""):
        generations = 4
    else:
        try:
            generations = int(generations_raw)
        except (TypeError, ValueError) as exc:
            raise NonPenalizedTaskError("payload.number_of_generations must be an integer", status_code=400) from exc
        if not 1 <= generations <= 4:
            raise NonPenalizedTaskError("payload.number_of_generations must be between 1 and 4", status_code=400)
    body["number_of_generations"] = generations
    return body


def _build_tts_request_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = _prompt_from_payload(payload)
    if not text:
        raise NonPenalizedTaskError("payload.text or payload.prompt cannot be empty", status_code=400)
    body: Dict[str, Any] = {
        "text": text,
        "model_id": _one_str(payload.get("model_id") or payload.get("model") or "eleven_multilingual_v2"),
    }
    language_code = _one_str(payload.get("language_code"))
    if language_code:
        body["language_code"] = language_code
    seed = payload.get("seed")
    if seed not in (None, ""):
        try:
            body["seed"] = int(seed)
        except (TypeError, ValueError) as exc:
            raise NonPenalizedTaskError("payload.seed must be an integer", status_code=400) from exc

    settings_raw = payload.get("voice_settings")
    settings: Dict[str, Any] = dict(settings_raw) if isinstance(settings_raw, dict) else {}
    for key in ("stability", "similarity_boost", "style", "speed"):
        value = _float_field(payload, key, default=None)
        if value is not None:
            settings[key] = value
    boost = _optional_bool(payload.get("use_speaker_boost"), default=None)
    if boost is not None:
        settings["use_speaker_boost"] = boost
    if settings:
        body["voice_settings"] = settings
    return body


def _is_elevenlabs_page_url(url: str) -> bool:
    try:
        host = (urlparse(_one_str(url)).hostname or "").lower()
    except Exception:
        return False
    return host == "elevenlabs.io" or host.endswith(".elevenlabs.io")


def _is_elevenlabs_api_url(url: str) -> bool:
    try:
        host = (urlparse(_one_str(url)).hostname or "").lower()
    except Exception:
        return False
    return host == "api.elevenlabs.io" or (host.startswith("api.") and host.endswith(".elevenlabs.io"))


async def _find_or_open_elevenlabs_page(pw_ctx: Any, target_url: str) -> Any:
    context = getattr(pw_ctx, "context", None)
    if context is None:
        raise NonPenalizedTaskError("ElevenLabs browser context is not initialized", status_code=502)
    page = None
    for candidate in list(getattr(context, "pages", []) or []):
        try:
            if not candidate.is_closed() and _is_elevenlabs_page_url(candidate.url):
                page = candidate
                if "/app/" in candidate.url:
                    break
        except Exception:
            continue
    if page is None:
        page = await context.new_page()
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.bring_to_front()
    except Exception:
        pass
    pw_ctx.page = page
    return page


def _captured_auth(request: Any) -> Optional[Dict[str, Any]]:
    try:
        if not _is_elevenlabs_api_url(request.url):
            return None
        parsed = urlparse(request.url)
        headers = {str(k).lower(): str(v) for k, v in (request.headers or {}).items()}
    except Exception:
        return None
    authorization = _one_str(headers.get("authorization"))
    if not authorization:
        return None
    captured_headers = {
        "Authorization": authorization,
        "Accept": "application/json, text/plain, */*",
    }
    posthog_session_id = _one_str(headers.get("x-posthog-session-id"))
    if posthog_session_id:
        captured_headers["X-Posthog-Session-Id"] = posthog_session_id
    return {
        "api_base": f"{parsed.scheme or 'https'}://{parsed.netloc}",
        "headers": captured_headers,
    }


async def _capture_elevenlabs_auth(
    page: Any,
    *,
    target_url: str,
    cache_key: str,
    timeout_seconds: float = 25.0,
) -> Dict[str, Any]:
    cached = _AUTH_CACHE.get(cache_key)
    if cached and cached[0] > time.time():
        return {"api_base": cached[1]["api_base"], "headers": dict(cached[1]["headers"])}

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Dict[str, Any]] = loop.create_future()
    latest: Dict[str, Any] = {}

    def on_request(request: Any) -> None:
        captured = _captured_auth(request)
        if captured:
            latest["value"] = captured
            if not future.done():
                future.set_result(captured)

    page.on("request", on_request)
    try:
        if _is_elevenlabs_page_url(getattr(page, "url", "")):
            await page.reload(wait_until="domcontentloaded", timeout=60_000)
        else:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        captured = await asyncio.wait_for(future, timeout=max(5.0, float(timeout_seconds)))
        # Firebase may refresh its bearer token during page startup. Prefer the
        # most recent API request after the initial request burst settles.
        await asyncio.sleep(1.0)
        captured = latest.get("value") or captured
        _AUTH_CACHE[cache_key] = (time.time() + _AUTH_CACHE_SECONDS, captured)
        return {"api_base": captured["api_base"], "headers": dict(captured["headers"])}
    except asyncio.TimeoutError as exc:
        raise NonPenalizedTaskError(
            "ElevenLabs authorization capture timed out; make sure the fingerprint window is logged in",
            status_code=401,
        ) from exc
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass


async def _page_fetch_json(
    page: Any,
    *,
    url: str,
    headers: Dict[str, str],
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
) -> tuple[int, Any]:
    result = await page.evaluate(
        """async (args) => {
          try {
            const init = {method: args.method, headers: args.headers};
            if (args.body !== null) init.body = JSON.stringify(args.body);
            const response = await fetch(args.url, init);
            const text = await response.text();
            let data = null;
            try { data = text ? JSON.parse(text) : null; }
            catch (_) { data = {raw: text.slice(0, 2000)}; }
            return {status: response.status, data};
          } catch (error) {
            return {status: 0, data: {message: String(error)}};
          }
        }""",
        {"url": url, "method": method.upper(), "headers": headers, "body": body},
    )
    return int((result or {}).get("status") or 0), (result or {}).get("data")


async def _page_fetch_audio(
    page: Any,
    *,
    url: str,
    headers: Dict[str, str],
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
) -> tuple[int, str, bytes, Any]:
    result = await page.evaluate(
        """async (args) => {
          try {
            const init = {method: args.method, headers: args.headers};
            if (args.body !== null) init.body = JSON.stringify(args.body);
            const response = await fetch(args.url, init);
            const contentType = response.headers.get('content-type') || '';
            if (!response.ok) {
              const text = await response.text();
              let error = null;
              try { error = text ? JSON.parse(text) : null; }
              catch (_) { error = {message: text.slice(0, 2000)}; }
              return {status: response.status, contentType, error};
            }
            const bytes = new Uint8Array(await response.arrayBuffer());
            if (bytes.byteLength > args.maxBytes) {
              return {status: 413, contentType, error: {message: 'audio response is too large'}};
            }
            let binary = '';
            const chunk = 32768;
            for (let i = 0; i < bytes.length; i += chunk) {
              binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
            }
            return {status: response.status, contentType, base64: btoa(binary)};
          } catch (error) {
            return {status: 0, contentType: '', error: {message: String(error)}};
          }
        }""",
        {
            "url": url,
            "method": method.upper(),
            "headers": headers,
            "body": body,
            "maxBytes": _MAX_AUDIO_BYTES,
        },
    )
    encoded = _one_str((result or {}).get("base64"))
    try:
        data = base64.b64decode(encoded, validate=True) if encoded else b""
    except Exception as exc:
        raise NonPenalizedTaskError("ElevenLabs returned invalid audio data", status_code=502) from exc
    return (
        int((result or {}).get("status") or 0),
        _one_str((result or {}).get("contentType")),
        data,
        (result or {}).get("error"),
    )


def _error_message(data: Any, default: str) -> str:
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, dict):
            return _one_str(detail.get("message") or detail.get("status")) or default
        if isinstance(detail, list):
            parts = [_one_str(item.get("msg")) for item in detail if isinstance(item, dict)]
            if any(parts):
                return "; ".join(x for x in parts if x)
        return _one_str(data.get("message") or data.get("error") or detail) or default
    return _one_str(data) or default


def _audio_extension(content_type: str, output_format: str = "") -> str:
    media_type = _one_str(content_type).split(";", 1)[0].lower()
    if media_type in {"audio/mpeg", "audio/mp3"}:
        return ".mp3"
    if media_type in {"audio/ogg", "audio/opus"}:
        return ".opus"
    if media_type in {"audio/wav", "audio/wave", "audio/x-wav"}:
        return ".wav"
    if media_type in {"audio/ulaw", "audio/basic"} or output_format.startswith("ulaw_"):
        return ".ulaw"
    if media_type == "audio/alaw" or output_format.startswith("alaw_"):
        return ".alaw"
    if media_type in {"audio/l16", "audio/pcm", "application/octet-stream"} or output_format.startswith("pcm_"):
        return ".pcm"
    if output_format.startswith("opus_"):
        return ".opus"
    return ".mp3"


def _save_audio_asset(task_id: str, index: int, data: bytes, extension: str) -> str:
    tid = _one_str(task_id)
    if not _SAFE_TASK_ID_RE.fullmatch(tid):
        raise NonPenalizedTaskError("invalid task_id for ElevenLabs audio asset", status_code=500)
    if not data:
        raise NonPenalizedTaskError("ElevenLabs returned an empty audio file", status_code=502)
    ext = extension if extension in {".mp3", ".opus", ".wav", ".pcm", ".ulaw", ".alaw"} else ".mp3"
    ELEVENLABS_PUBLIC_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    path = ELEVENLABS_PUBLIC_ASSET_DIR / f"{tid}-{int(index)}{ext}"
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
    return f"/public/elevenlabs-assets/{path.name}"


def _subscription_summary(data: Any) -> Dict[str, Any]:
    obj = data if isinstance(data, dict) else {}
    used = int(obj.get("character_count") or 0)
    limit = int(obj.get("character_limit") or 0)
    return {
        "tier": _one_str(obj.get("tier")),
        "character_count": used,
        "character_limit": limit,
        "remaining_quota": max(0, limit - used),
        "next_character_count_reset_unix": int(obj.get("next_character_count_reset_unix") or 0),
    }


async def _fetch_subscription(page: Any, auth: Dict[str, Any]) -> Dict[str, Any]:
    status, data = await _page_fetch_json(
        page,
        url=f"{auth['api_base']}/v1/user/subscription",
        headers=auth["headers"],
    )
    if status == 401:
        raise NonPenalizedTaskError(
            _error_message(data, "ElevenLabs login expired"),
            status_code=401,
        )
    if status != 200:
        raise NonPenalizedTaskError(
            _error_message(data, f"ElevenLabs subscription request failed with HTTP {status}"),
            status_code=status or 502,
        )
    return _subscription_summary(data)


async def _resolve_voice_id(page: Any, auth: Dict[str, Any], payload: Dict[str, Any]) -> str:
    requested = _one_str(payload.get("voice_id") or payload.get("voice"))
    if requested:
        return requested
    status, data = await _page_fetch_json(
        page,
        url=f"{auth['api_base']}/v1/voices?show_legacy=true",
        headers=auth["headers"],
    )
    if status == 200 and isinstance(data, dict):
        for voice in data.get("voices") or []:
            if isinstance(voice, dict) and _one_str(voice.get("voice_id")):
                return _one_str(voice.get("voice_id"))
    return ""


async def _run_sound_effects(
    page: Any,
    *,
    auth: Dict[str, Any],
    payload: Dict[str, Any],
    task_id: str,
    progress_cb: ProgressCB,
) -> Dict[str, Any]:
    body = _build_sfx_request_body(payload)
    status, data = await _page_fetch_json(
        page,
        url=f"{auth['api_base']}/sound-generation",
        headers={
            **auth["headers"],
            **_generation_headers("Sound Effects"),
            "Content-Type": "application/json",
        },
        method="POST",
        body=body,
    )
    if status == 401:
        raise NonPenalizedTaskError(
            _error_message(data, "ElevenLabs login expired"),
            status_code=401,
        )
    if not 200 <= status < 300 or not isinstance(data, dict):
        raise NonPenalizedTaskError(
            _error_message(data, f"ElevenLabs sound generation failed with HTTP {status}"),
            status_code=status or 502,
        )

    history_ids: list[str] = []
    for row in data.get("sound_generations_with_waveforms") or []:
        if not isinstance(row, dict):
            continue
        item = row.get("sound_generation_history_item")
        if not isinstance(item, dict):
            continue
        history_id = _one_str(item.get("sound_generation_history_item_id"))
        if history_id and history_id not in history_ids:
            history_ids.append(history_id)
    if not history_ids:
        raise NonPenalizedTaskError(
            f"ElevenLabs response did not include sound history IDs: {safe_trim(json.dumps(data, default=str), 500)}",
            status_code=502,
        )

    urls: list[str] = []
    for index, history_id in enumerate(history_ids):
        await progress_cb(40 + int(45 * index / max(1, len(history_ids))), {"stage": "download", "index": index})
        audio_status, content_type, audio, error = await _page_fetch_audio(
            page,
            url=(
                f"{auth['api_base']}/v1/sound-generation/history/"
                f"{quote(history_id, safe='')}/audio?convert_to_mpeg=true"
            ),
            headers=auth["headers"],
        )
        if audio_status != 200:
            raise NonPenalizedTaskError(
                _error_message(error, f"ElevenLabs sound download failed with HTTP {audio_status}"),
                status_code=audio_status or 502,
            )
        urls.append(_save_audio_asset(task_id, index, audio, _audio_extension(content_type)))

    return {
        "type": "elevenlabs_sound_effects",
        "provider": "elevenlabs",
        "workflow_kind": "audio",
        "message": "ElevenLabs sound effect generation completed",
        "audio_url": urls[0],
        "share_url": urls[0],
        "url": urls[0],
        "urls": urls,
        "public_urls": urls,
        "elevenlabs_history_ids": history_ids,
        "model": body["model_id"],
        "duration_seconds": body["duration_seconds"],
        "loop": body["loop"],
        "format": "mp3",
    }


async def _run_tts(
    page: Any,
    *,
    auth: Dict[str, Any],
    payload: Dict[str, Any],
    task_id: str,
) -> Dict[str, Any]:
    body = _build_tts_request_body(payload)
    voice_id = await _resolve_voice_id(page, auth, payload)
    if not voice_id:
        raise NonPenalizedTaskError("ElevenLabs voice_id is missing and no account voice was found", status_code=400)
    output_format = _one_str(payload.get("output_format") or payload.get("format") or "mp3_44100_128")
    if output_format not in _TTS_OUTPUT_FORMATS:
        raise NonPenalizedTaskError(
            f"unsupported ElevenLabs TTS output_format {output_format!r}",
            status_code=400,
        )
    query = urlencode({"output_format": output_format})
    status, content_type, audio, error = await _page_fetch_audio(
        page,
        url=f"{auth['api_base']}/v1/text-to-speech/{quote(voice_id, safe='')}?{query}",
        headers={
            **auth["headers"],
            **_generation_headers("Speech Synthesis"),
            "Content-Type": "application/json",
        },
        method="POST",
        body=body,
    )
    if status == 401:
        raise NonPenalizedTaskError(
            _error_message(error, "ElevenLabs login expired"),
            status_code=401,
        )
    if not 200 <= status < 300:
        raise NonPenalizedTaskError(
            _error_message(error, f"ElevenLabs text-to-speech failed with HTTP {status}"),
            status_code=status or 502,
        )
    url = _save_audio_asset(task_id, 0, audio, _audio_extension(content_type, output_format))
    return {
        "type": "elevenlabs_tts",
        "provider": "elevenlabs",
        "workflow_kind": "audio",
        "message": "ElevenLabs speech generation completed",
        "audio_url": url,
        "share_url": url,
        "url": url,
        "urls": [url],
        "public_urls": [url],
        "voice_id": voice_id,
        "model": body["model_id"],
        "format": output_format,
    }


async def elevenlabs_fetch_subscription(
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    target_url: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
    timeout_seconds: float = 30.0,
) -> Dict[str, Any]:
    target = _one_str(target_url) or DEFAULT_ELEVENLABS_TARGET
    sess = get_or_create_playwright_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    async with sess.driver_lock:
        await sess.ensure_open(args=[target], force_open=False, headless=headless, require_page=False, pure_mode=pure_mode)
        page = await _find_or_open_elevenlabs_page(sess, target)
        auth = await _capture_elevenlabs_auth(
            page,
            target_url=target,
            cache_key=sess.cache_key,
            timeout_seconds=timeout_seconds,
        )
        return await _fetch_subscription(page, auth)


async def elevenlabs_workflow(
    payload: Dict[str, Any],
    progress_cb: ProgressCB,
    *,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    timeout_seconds: float,
    task_id: str,
    default_target_url: Optional[str] = None,
    headless: bool = False,
    pure_mode: bool = True,
    db: Any = None,
    task_type_window_id: Optional[int] = None,
    **_: Any,
) -> Dict[str, Any]:
    p = dict(payload or {})
    mode = _workflow_mode(p)
    if mode == "sound_effects":
        _build_sfx_request_body(p)
    else:
        _build_tts_request_body(p)

    target = _one_str(p.get("elevenlabs_url") or p.get("target_url") or default_target_url) or DEFAULT_ELEVENLABS_TARGET
    log_file = Path(_one_str(p.get("monitor_log_path"))) if _one_str(p.get("monitor_log_path")) else MONITOR_LOG_FILE
    sess = get_or_create_playwright_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )

    await progress_cb(1, {"stage": "init", "provider": "elevenlabs", "workflow_kind": "audio", "mode": mode})
    started = time.time()
    subscription: Dict[str, Any] = {}
    async with sess.driver_lock:
        await sess.ensure_open(args=[target], force_open=False, headless=headless, require_page=False, pure_mode=pure_mode)
        page = await _find_or_open_elevenlabs_page(sess, target)
        await progress_cb(5, {"stage": "capture_auth"})
        auth = await _capture_elevenlabs_auth(
            page,
            target_url=target,
            cache_key=sess.cache_key,
            timeout_seconds=min(30.0, max(8.0, float(timeout_seconds) / 4.0)),
        )
        await progress_cb(15, {"stage": "submit", "mode": mode})
        try:
            if mode == "sound_effects":
                result = await _run_sound_effects(
                    page,
                    auth=auth,
                    payload=p,
                    task_id=task_id,
                    progress_cb=progress_cb,
                )
            else:
                result = await _run_tts(page, auth=auth, payload=p, task_id=task_id)
        except NonPenalizedTaskError as exc:
            if int(getattr(exc, "status_code", 0) or 0) == 401:
                _AUTH_CACHE.pop(sess.cache_key, None)
            raise
        try:
            subscription = await _fetch_subscription(page, auth)
        except Exception as exc:
            append_log(log_file, f"[elevenlabs] subscription refresh skipped: {safe_trim(str(exc), 300)}")

    if subscription and db is not None and int(task_type_window_id or 0) > 0:
        try:
            await db.update_task_type_window(
                mapping_id=int(task_type_window_id),
                remaining_quota=int(subscription.get("remaining_quota") or 0),
                sora_remaining_count=int(subscription.get("remaining_quota") or 0),
                sora_plan_title=_one_str(subscription.get("tier")) or None,
            )
        except Exception as exc:
            append_log(log_file, f"[elevenlabs] quota persistence skipped: {safe_trim(str(exc), 300)}")

    elapsed_ms = int(max(0.0, (time.time() - started) * 1000.0))
    result["mode"] = mode
    result["elapsed_ms"] = elapsed_ms
    if subscription:
        result["remaining_quota"] = int(subscription.get("remaining_quota") or 0)
        result["tier"] = _one_str(subscription.get("tier"))
    append_log(log_file, f"[elevenlabs] completed mode={mode} task={task_id} elapsed_ms={elapsed_ms}")
    await progress_cb(100, {"stage": "done", "mode": mode, "audio_url": result.get("audio_url")})
    return result
