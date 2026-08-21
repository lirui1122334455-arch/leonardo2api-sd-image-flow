"""Fish Audio web-app TTS executor.

The executor reuses the logged-in Fish Audio session in a fingerprint browser.
It intentionally does not persist the web token or require a Fish API key.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .playwright_broswer_context import append_log, get_or_create_ctx as get_or_create_playwright_ctx, safe_trim
from .task_executor_types import NonPenalizedTaskError, ProgressCB


DEFAULT_FISH_AUDIO_TARGET = "https://fish.audio/zh-CN/app/playground/"
FISH_API_BASE = "https://api.fish.audio"
FISH_RECAPTCHA_SITE_KEY = "6LfR4RwqAAAAAEvptRw9zohw7HeDU6NCqtAnJk1i"
MONITOR_LOG_FILE = Path("logs/fish_audio_monitor.log")

_AUTH_CACHE_SECONDS = 300.0
_AUTH_CACHE: Dict[str, tuple[float, Dict[str, str]]] = {}
_SUPPORTED_FORMATS = {"mp3", "wav", "pcm", "opus"}


def _one_str(value: Any) -> str:
    return str(value or "").strip()


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise NonPenalizedTaskError(f"invalid boolean value: {value}", status_code=400)


def _optional_float(value: Any, *, name: str) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise NonPenalizedTaskError(f"payload.{name} must be a number", status_code=400) from exc


def _prompt_from_payload(payload: Dict[str, Any]) -> str:
    return _one_str(payload.get("text") or payload.get("prompt"))


def _reference_id_from_payload(payload: Dict[str, Any]) -> str:
    return _one_str(
        payload.get("reference_id")
        or payload.get("voice_id")
        or payload.get("model_id")
        or payload.get("model")
    )


def _build_fish_task_body(
    payload: Dict[str, Any],
    *,
    reference_id: str,
    recaptcha: str,
) -> Dict[str, Any]:
    text = _prompt_from_payload(payload)
    if not text:
        raise NonPenalizedTaskError("payload.text or payload.prompt cannot be empty", status_code=400)
    if not reference_id:
        raise NonPenalizedTaskError(
            "Fish Audio voice is missing; pass payload.reference_id, voice_id, model_id, or model",
            status_code=400,
        )

    audio_format = _one_str(payload.get("format") or payload.get("response_format") or "mp3").lower()
    if audio_format not in _SUPPORTED_FORMATS:
        raise NonPenalizedTaskError(
            f"unsupported Fish Audio format {audio_format!r}; use one of {sorted(_SUPPORTED_FORMATS)}",
            status_code=400,
        )

    backend = _one_str(payload.get("backend") or payload.get("version") or "s2.1-pro")
    latency = _one_str(payload.get("latency") or "balanced")
    normalize = _optional_bool(payload.get("normalize"))
    if normalize is None:
        normalize = False

    prosody_raw = payload.get("prosody")
    prosody: Dict[str, Any] = dict(prosody_raw) if isinstance(prosody_raw, dict) else {}
    speed = _optional_float(payload.get("speed"), name="speed")
    volume = _optional_float(payload.get("volume"), name="volume")
    normalize_loudness = _optional_bool(payload.get("normalize_loudness"))
    if speed is not None:
        prosody["speed"] = speed
    if volume is not None:
        prosody["volume"] = volume
    if normalize_loudness is not None:
        prosody["normalize_loudness"] = normalize_loudness

    sampler_raw = payload.get("sampler")
    sampler: Dict[str, Any] = dict(sampler_raw) if isinstance(sampler_raw, dict) else {}
    temperature = _optional_float(payload.get("temperature"), name="temperature")
    top_p = _optional_float(payload.get("top_p"), name="top_p")
    if temperature is not None:
        sampler["temperature"] = temperature
    if top_p is not None:
        sampler["top_p"] = top_p

    parameters: Dict[str, Any] = {
        "text": text,
        "model_id": reference_id,
        "format": audio_format,
        "latency": latency,
        "backend": backend,
        "normalize": normalize,
    }
    body: Dict[str, Any] = {
        "type": "tts",
        "stream": True,
        "model": reference_id,
        "parameters": parameters,
        "recaptcha": recaptcha,
        "format": audio_format,
        "backend": backend,
        "latency": latency,
        "normalize": normalize,
    }
    if prosody:
        parameters["prosody"] = prosody
        body["prosody"] = prosody
    if sampler:
        parameters["sampler"] = sampler
        body["sampler"] = sampler
    group_id = _one_str(payload.get("group_id"))
    if group_id:
        body["group_id"] = group_id
    return body


def _is_fish_page_url(url: str) -> bool:
    try:
        host = (urlparse(_one_str(url)).hostname or "").lower()
    except Exception:
        return False
    return host == "fish.audio" or host.endswith(".fish.audio")


async def _find_or_open_fish_page(pw_ctx: Any, target_url: str) -> Any:
    context = getattr(pw_ctx, "context", None)
    if context is None:
        raise NonPenalizedTaskError("Fish Audio browser context is not initialized", status_code=502)
    page = None
    fallback_page = None
    for candidate in list(getattr(context, "pages", []) or []):
        try:
            if candidate.is_closed() or not _is_fish_page_url(candidate.url):
                continue
            if "/app/playground" in candidate.url or "/app/text-to-speech" in candidate.url:
                page = candidate
                break
            if fallback_page is None:
                fallback_page = candidate
        except Exception:
            continue
    if page is None:
        page = fallback_page
    if page is None:
        page = await context.new_page()
    try:
        await page.bring_to_front()
    except Exception:
        pass
    current_url = _one_str(getattr(page, "url", ""))
    if "/app/playground" not in current_url and "/app/text-to-speech" not in current_url:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
    pw_ctx.page = page
    return page


def _captured_fish_headers(request: Any) -> Dict[str, str]:
    try:
        headers = {str(k).lower(): str(v) for k, v in (request.headers or {}).items()}
    except Exception:
        return {}
    auth = _one_str(headers.get("authorization"))
    if not auth:
        return {}
    result = {"Authorization": auth, "Accept": "application/json, text/plain, */*"}
    if _one_str(headers.get("x-team-id")):
        result["X-Team-Id"] = headers["x-team-id"]
    if _one_str(headers.get("x-workspace-id")):
        result["X-Workspace-Id"] = headers["x-workspace-id"]
    for key in ("x-fish-amp-device-id", "x-fish-amp-session-id"):
        if _one_str(headers.get(key)):
            result[key] = headers[key]
    return result


async def _capture_fish_auth_headers(
    page: Any,
    *,
    target_url: str,
    cache_key: str,
    timeout_seconds: float = 20.0,
) -> Dict[str, str]:
    cached = _AUTH_CACHE.get(cache_key)
    if cached and cached[0] > time.time():
        return dict(cached[1])

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Dict[str, str]] = loop.create_future()

    def on_request(request: Any) -> None:
        try:
            parsed = urlparse(_one_str(request.url))
            if parsed.hostname != "api.fish.audio":
                return
            headers = _captured_fish_headers(request)
            if headers and not future.done():
                future.set_result(headers)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)

    page.on("request", on_request)
    try:
        current_url = _one_str(getattr(page, "url", ""))
        if not _is_fish_page_url(current_url):
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        else:
            await page.reload(wait_until="domcontentloaded", timeout=60_000)
        headers = await asyncio.wait_for(future, timeout=max(3.0, float(timeout_seconds)))
        _AUTH_CACHE[cache_key] = (time.time() + _AUTH_CACHE_SECONDS, dict(headers))
        return headers
    except asyncio.TimeoutError as exc:
        raise NonPenalizedTaskError(
            "Fish Audio authorization capture timed out; make sure the fingerprint window is logged in",
            status_code=401,
        ) from exc
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass


async def _fish_fetch_json(
    page: Any,
    *,
    path: str,
    headers: Dict[str, str],
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
) -> tuple[int, Any]:
    response = await page.evaluate(
        """async (args) => {
          try {
            const init = {method: args.method, headers: args.headers};
            if (args.body !== null) init.body = JSON.stringify(args.body);
            const response = await fetch(args.url, init);
            const text = await response.text();
            let data = null;
            try { data = text ? JSON.parse(text) : null; } catch (_) { data = {raw: text.slice(0, 1000)}; }
            return {status: response.status, data};
          } catch (error) {
            return {status: 0, data: {message: String(error)}};
          }
        }""",
        {"url": f"{FISH_API_BASE}{path}", "method": method.upper(), "headers": headers, "body": body},
    )
    return int((response or {}).get("status") or 0), (response or {}).get("data")


async def _resolve_reference_id(page: Any, headers: Dict[str, str], payload: Dict[str, Any]) -> str:
    reference_id = _reference_id_from_payload(payload)
    if reference_id:
        return reference_id

    status, latest = await _fish_fetch_json(page, path="/model/latest-used", headers=headers)
    if status == 200:
        values = latest if isinstance(latest, list) else (latest or {}).get("data") if isinstance(latest, dict) else None
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict) and _one_str(item.get("_id") or item.get("id")):
                    return _one_str(item.get("_id") or item.get("id"))

    status, tasks = await _fish_fetch_json(
        page,
        path="/task?page_size=10&page_number=1&type=tts&state=finished",
        headers=headers,
    )
    if status == 200 and isinstance(tasks, dict):
        for item in tasks.get("items") or []:
            if not isinstance(item, dict):
                continue
            parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
            candidate = _one_str(parameters.get("model_id"))
            if candidate:
                return candidate
    return ""


async def _submit_fish_tts(
    page: Any,
    *,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    reference_id: str,
) -> Dict[str, Any]:
    request_template = _build_fish_task_body(payload, reference_id=reference_id, recaptcha="__runtime__")
    response = await page.evaluate(
        """async (args) => {
          const loadRecaptcha = async () => {
            if (!window.grecaptcha?.enterprise) {
              await new Promise((resolve, reject) => {
                const callbackName = `fishCodexRecaptcha${Math.random().toString(36).slice(2)}`;
                const timeout = setTimeout(() => reject(new Error('reCAPTCHA load timed out')), 20000);
                window[callbackName] = () => { clearTimeout(timeout); resolve(); };
                const script = document.createElement('script');
                script.src = `https://www.recaptcha.net/recaptcha/enterprise.js?render=${args.siteKey}&onload=${callbackName}`;
                script.async = true;
                script.defer = true;
                script.onerror = () => { clearTimeout(timeout); reject(new Error('reCAPTCHA load failed')); };
                document.head.appendChild(script);
              });
            }
            if (!window.grecaptcha?.enterprise) throw new Error('reCAPTCHA is unavailable');
            return await window.grecaptcha.enterprise.execute(args.siteKey, {action: 'playground'});
          };
          try {
            const recaptcha = await loadRecaptcha();
            const body = {...args.body, recaptcha};
            const response = await fetch(args.url, {
              method: 'POST',
              headers: {...args.headers, 'content-type': 'application/json'},
              body: JSON.stringify(body),
            });
            if (!response.ok) {
              const text = await response.text();
              let error = null;
              try { error = JSON.parse(text); } catch (_) { error = {message: text.slice(0, 1000)}; }
              return {status: response.status, error};
            }
            const taskId = response.headers.get('task-id') || '';
            let bytes = 0;
            if (response.body) {
              const reader = response.body.getReader();
              for (;;) {
                const {value, done} = await reader.read();
                if (done) break;
                bytes += value?.byteLength || 0;
              }
            }
            return {status: response.status, taskId, bytes};
          } catch (error) {
            return {status: 0, error: {message: String(error)}};
          }
        }""",
        {"url": f"{FISH_API_BASE}/task", "headers": headers, "body": request_template, "siteKey": FISH_RECAPTCHA_SITE_KEY},
    )
    return dict(response or {})


async def _poll_fish_task(
    page: Any,
    *,
    headers: Dict[str, str],
    fish_task_id: str,
    timeout_seconds: float,
    progress_cb: ProgressCB,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(10.0, float(timeout_seconds))
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, data = await _fish_fetch_json(page, path=f"/task/{fish_task_id}", headers=headers)
        if status == 401:
            raise NonPenalizedTaskError("Fish Audio login expired", status_code=401)
        if status == 200 and isinstance(data, dict):
            last = data
            state = _one_str(data.get("state")).lower()
            if state == "finished" and _one_str(data.get("result")):
                return data
            if state == "failed":
                message = _one_str(data.get("error") or data.get("message")) or "Fish Audio generation failed"
                raise NonPenalizedTaskError(message, status_code=422)
            await progress_cb(70 if state in {"running", "processing"} else 40, {"stage": "poll", "state": state})
        elif status in {400, 403, 404}:
            raise NonPenalizedTaskError(f"Fish Audio task polling failed with HTTP {status}", status_code=status)
        await asyncio.sleep(1.0)
    raise NonPenalizedTaskError(
        f"Fish Audio generation timed out: {safe_trim(json.dumps(last, ensure_ascii=False, default=str), 500)}",
        status_code=504,
    )


async def fish_audio_workflow(
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
    **_: Any,
) -> Dict[str, Any]:
    p = dict(payload or {})
    if not _prompt_from_payload(p):
        raise NonPenalizedTaskError("payload.text or payload.prompt cannot be empty", status_code=400)

    target_url = _one_str(p.get("fish_audio_url") or p.get("target_url") or default_target_url) or DEFAULT_FISH_AUDIO_TARGET
    log_file = Path(_one_str(p.get("monitor_log_path"))) if _one_str(p.get("monitor_log_path")) else MONITOR_LOG_FILE
    sess = get_or_create_playwright_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )

    await progress_cb(1, {"stage": "init", "provider": "fish_audio", "workflow_kind": "audio"})
    started = time.time()
    async with sess.driver_lock:
        await sess.ensure_open(args=[target_url], force_open=False, headless=headless, require_page=False, pure_mode=pure_mode)
        page = await _find_or_open_fish_page(sess, target_url)
        await progress_cb(5, {"stage": "capture_auth"})
        auth_headers = await _capture_fish_auth_headers(
            page,
            target_url=target_url,
            cache_key=sess.cache_key,
            timeout_seconds=min(30.0, max(8.0, float(timeout_seconds) / 4.0)),
        )

        reference_id = await _resolve_reference_id(page, auth_headers, p)
        if not reference_id:
            raise NonPenalizedTaskError(
                "Fish Audio voice could not be inferred; pass payload.reference_id from My Voices or Discovery",
                status_code=400,
            )
        backend = _one_str(p.get("backend") or p.get("version") or "s2.1-pro")
        audio_format = _one_str(p.get("format") or p.get("response_format") or "mp3").lower()
        await progress_cb(10, {"stage": "submit", "backend": backend, "reference_id": reference_id})
        submitted = await _submit_fish_tts(page, headers=auth_headers, payload=p, reference_id=reference_id)
        status = int(submitted.get("status") or 0)
        fish_task_id = _one_str(submitted.get("taskId"))
        if status == 401:
            _AUTH_CACHE.pop(sess.cache_key, None)
            raise NonPenalizedTaskError("Fish Audio login expired", status_code=401)
        if not (200 <= status < 300) or not fish_task_id:
            error = submitted.get("error")
            message = _one_str((error or {}).get("message") if isinstance(error, dict) else error)
            raise NonPenalizedTaskError(
                message or f"Fish Audio generation request failed with HTTP {status}", status_code=status or 502
            )
        append_log(log_file, f"[fish_audio] submitted task={fish_task_id} backend={backend} format={audio_format}")
        await progress_cb(30, {"stage": "submitted", "fish_task_id": fish_task_id})
        task = await _poll_fish_task(
            page,
            headers=auth_headers,
            fish_task_id=fish_task_id,
            timeout_seconds=max(10.0, float(timeout_seconds) - (time.time() - started)),
            progress_cb=progress_cb,
        )

    audio_url = _one_str(task.get("result"))
    elapsed_ms = int(max(0.0, (time.time() - started) * 1000.0))
    await progress_cb(100, {"stage": "done", "fish_task_id": fish_task_id, "audio_url": audio_url})
    return {
        "type": "fish_audio_tts",
        "provider": "fish_audio",
        "workflow_kind": "audio",
        "message": "Fish Audio speech generation completed",
        "audio_url": audio_url,
        "share_url": audio_url,
        "url": audio_url,
        "urls": [audio_url],
        "fish_task_id": fish_task_id,
        "reference_id": reference_id,
        "model": backend,
        "backend": backend,
        "format": audio_format,
        "elapsed_ms": elapsed_ms,
    }
