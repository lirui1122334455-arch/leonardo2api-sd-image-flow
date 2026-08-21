"""Fish Audio voice catalog normalization and caching.

The admin test UI uses a small curated CSV by default and may opt into Fish's
public model directory. Public directory failures fall back to the curated
catalog so TTS testing remains usable when Fish changes or is unavailable.
"""

from __future__ import annotations

import asyncio
import csv
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from ..core.logger import logger
from ..core.paths import APP_ROOT


FISH_PUBLIC_MODEL_URL = "https://api.fish.audio/model"
CURATED_CATALOG_PATH = APP_ROOT / "FISH_AUDIO_VOICE_CATALOG.csv"

PUBLIC_CACHE_SECONDS = 300.0
_PUBLIC_CACHE: Dict[Tuple[str, str, str, str, int], Tuple[float, Dict[str, Any]]] = {}
_PUBLIC_CACHE_LOCK = asyncio.Lock()

LANGUAGE_NAMES = {
    "ar": "阿拉伯语",
    "de": "德语",
    "el": "希腊语",
    "en": "英语",
    "es": "西班牙语",
    "fr": "法语",
    "it": "意大利语",
    "ja": "日语",
    "ko": "韩语",
    "pt": "葡萄牙语",
    "ro": "罗马尼亚语",
    "ru": "俄语",
    "sv": "瑞典语",
    "sw": "斯瓦希里语",
    "tl": "菲律宾语",
    "zh": "中文",
}
LANGUAGE_CODES_BY_NAME = {name: code for code, name in LANGUAGE_NAMES.items()}
AGE_LABELS = {"young": "青年", "middle-aged": "中年", "old": "年长"}
AGE_TAGS_BY_LABEL = {label: tag for tag, label in AGE_LABELS.items()}
GENDER_LABELS = {"male": "男", "female": "女"}
GENDER_TAGS_BY_LABEL = {label: tag for tag, label in GENDER_LABELS.items()}

_NON_TRAIT_TAGS = {
    "male",
    "female",
    "young",
    "middle-aged",
    "old",
    "narration",
    "social-media",
    "advertisement",
    "character-voice",
    "entertainment",
}


def _one_str(value: Any) -> str:
    return str(value or "").strip()


def _split_pipe(value: Any) -> List[str]:
    return [part.strip() for part in _one_str(value).split("|") if part.strip()]


def _first_tag(tags: Iterable[str], choices: Iterable[str]) -> str:
    choice_set = set(choices)
    return next((tag for tag in tags if tag in choice_set), "")


def normalize_public_voice(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    voice_id = _one_str(item.get("_id") or item.get("id"))
    if not voice_id:
        return None

    tags = [_one_str(tag) for tag in item.get("tags") or [] if _one_str(tag)]
    gender_tag = _first_tag(tags, GENDER_LABELS)
    age_tag = _first_tag(tags, AGE_LABELS)
    language_codes = [_one_str(code).lower() for code in item.get("languages") or [] if _one_str(code)]
    language_names = [LANGUAGE_NAMES.get(code, code) for code in language_codes]
    traits = [tag for tag in tags if tag not in _NON_TRAIT_TAGS][:8]
    samples = item.get("samples") if isinstance(item.get("samples"), list) else []
    preview_url = ""
    for sample in samples:
        if isinstance(sample, dict) and _one_str(sample.get("audio")):
            preview_url = _one_str(sample.get("audio"))
            break

    return {
        "voice_id": voice_id,
        "name": _one_str(item.get("title")) or voice_id,
        "gender": GENDER_LABELS.get(gender_tag, "未知"),
        "gender_tag": gender_tag,
        "age_group": AGE_LABELS.get(age_tag, "未知"),
        "age_tag": age_tag,
        "languages": language_names,
        "language_codes": language_codes,
        "voice_traits": traits,
        "preview_url": preview_url,
        "description": _one_str(item.get("description")),
        "source": "public",
    }


def load_curated_voices(path: Path = CURATED_CATALOG_PATH) -> List[Dict[str, Any]]:
    voices: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            voice_id = _one_str(row.get("voice_id"))
            if not voice_id:
                continue
            languages = _split_pipe(row.get("languages"))
            voices.append(
                {
                    "voice_id": voice_id,
                    "name": _one_str(row.get("name")) or voice_id,
                    "gender": _one_str(row.get("gender")) or "未知",
                    "gender_tag": GENDER_TAGS_BY_LABEL.get(_one_str(row.get("gender")), ""),
                    "age_group": _one_str(row.get("age_group")) or "未知",
                    "age_tag": AGE_TAGS_BY_LABEL.get(_one_str(row.get("age_group")), ""),
                    "languages": languages,
                    "language_codes": [LANGUAGE_CODES_BY_NAME.get(name, name) for name in languages],
                    "voice_traits": _split_pipe(row.get("voice_traits")),
                    "preview_url": _one_str(row.get("preview_url")),
                    "description": "",
                    "source": "curated",
                }
            )
    return voices


def filter_voices(
    voices: Iterable[Dict[str, Any]],
    *,
    query: str = "",
    language: str = "",
    gender: str = "",
    age_group: str = "",
) -> List[Dict[str, Any]]:
    query_key = _one_str(query).casefold()
    language_key = _one_str(language).casefold()
    gender_key = _one_str(gender).casefold()
    age_key = _one_str(age_group).casefold()
    result: List[Dict[str, Any]] = []

    for voice in voices:
        languages = [_one_str(x) for x in voice.get("languages") or []]
        language_codes = [_one_str(x) for x in voice.get("language_codes") or []]
        traits = [_one_str(x) for x in voice.get("voice_traits") or []]
        if language_key and language_key not in {x.casefold() for x in languages + language_codes}:
            continue
        if gender_key and gender_key not in {
            _one_str(voice.get("gender")).casefold(),
            _one_str(voice.get("gender_tag")).casefold(),
        }:
            continue
        if age_key and age_key not in {
            _one_str(voice.get("age_group")).casefold(),
            _one_str(voice.get("age_tag")).casefold(),
        }:
            continue
        if query_key:
            haystack = " ".join(
                [
                    _one_str(voice.get("voice_id")),
                    _one_str(voice.get("name")),
                    _one_str(voice.get("description")),
                    *languages,
                    *language_codes,
                    *traits,
                ]
            ).casefold()
            if query_key not in haystack:
                continue
        result.append(dict(voice))
    return result


async def _fetch_public_voices(
    *,
    query: str,
    language: str,
    gender: str,
    age_group: str,
    page: int,
) -> Dict[str, Any]:
    remote_tag = _one_str(gender) or _one_str(age_group)
    params: Dict[str, Any] = {"page_size": 100, "page_number": page, "type": "tts"}
    if _one_str(query):
        params["title"] = _one_str(query)
    if _one_str(language):
        params["language"] = _one_str(language)
    if remote_tag:
        params["tag"] = remote_tag

    timeout = httpx.Timeout(connect=8.0, read=20.0, write=10.0, pool=8.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(FISH_PUBLIC_MODEL_URL, params=params)
        response.raise_for_status()
        payload = response.json()

    normalized = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        voice = normalize_public_voice(item)
        if voice is not None:
            normalized.append(voice)
    return {
        "voices": normalized,
        "remote_total": int(payload.get("total") or len(normalized)),
        "has_more": bool(payload.get("has_more")),
    }


async def list_fish_audio_voices(
    *,
    source: str = "curated",
    query: str = "",
    language: str = "",
    gender: str = "",
    age_group: str = "",
    page: int = 1,
    page_size: int = 100,
) -> Dict[str, Any]:
    normalized_source = "public" if _one_str(source).lower() == "public" else "curated"
    safe_page = max(1, int(page or 1))
    safe_page_size = max(1, min(100, int(page_size or 100)))
    fallback = False
    error_message = ""
    remote_total: Optional[int] = None
    has_more = False

    if normalized_source == "public":
        cache_key = (
            _one_str(query).casefold(),
            _one_str(language).casefold(),
            _one_str(gender).casefold(),
            _one_str(age_group).casefold(),
            safe_page,
        )
        cached = _PUBLIC_CACHE.get(cache_key)
        data: Optional[Dict[str, Any]] = None
        if cached and cached[0] > time.monotonic():
            data = dict(cached[1])
        if data is None:
            try:
                async with _PUBLIC_CACHE_LOCK:
                    cached = _PUBLIC_CACHE.get(cache_key)
                    if cached and cached[0] > time.monotonic():
                        data = dict(cached[1])
                    else:
                        data = await _fetch_public_voices(
                            query=query,
                            language=language,
                            gender=gender,
                            age_group=age_group,
                            page=safe_page,
                        )
                        _PUBLIC_CACHE[cache_key] = (time.monotonic() + PUBLIC_CACHE_SECONDS, dict(data))
            except Exception as exc:
                fallback = True
                error_message = str(exc)
                logger.warning("Fish Audio public voice catalog unavailable; using curated fallback: %s", exc)
        if data is not None:
            voices = data.get("voices") or []
            remote_total = int(data.get("remote_total") or len(voices))
            has_more = bool(data.get("has_more"))
        else:
            voices = load_curated_voices()
    else:
        voices = load_curated_voices()

    filtered = filter_voices(
        voices,
        query=query,
        language=language,
        gender=gender,
        age_group=age_group,
    )
    items = filtered[:safe_page_size]
    effective_source = "curated" if fallback else normalized_source
    return {
        "source": effective_source,
        "requested_source": normalized_source,
        "fallback": fallback,
        "warning": error_message[:300] if fallback else "",
        "page": safe_page,
        "page_size": safe_page_size,
        "count": len(items),
        "filtered_total": len(filtered),
        "remote_total": remote_total,
        "has_more": has_more,
        "items": items,
    }

