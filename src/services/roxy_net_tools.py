"""
从 `sora2api/sora_net_tools.py` 移植而来。

用途：提供“旧 Selenium(performance log/CDP) 方案”的抓包工具方法。
本次“同步窗口”功能本身不依赖该文件，但按你的要求一并迁移到 `fpbrowser2api/src/services/`，
方便后续把“窗口打开后的自动化请求抓取/调试”能力接入任务执行器。
"""

from __future__ import annotations

import json
import re
import time
from base64 import b64decode
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.paths import MONITOR_LOG_FILE


def append_log(log_file: Path, s: str) -> None:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8", newline="\n") as f:
            f.write(s)
            if not s.endswith("\n"):
                f.write("\n")
    except Exception:
        pass


def sniff_http_transaction(
    driver,
    *,
    url_regex: str,
    method: Optional[str] = None,
    timeout_seconds: float = 15.0,
    log_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    被动抓取某个请求的完整信息（request/response），并把完整内容写入 logs.txt。

    参数：
    - url_regex: 用于匹配完整 URL（建议传完整域名+路径）
    - method: 可选，限制 GET/POST 等（None 表示不限制）
    - timeout_seconds: 监听时长
    - log_path: 可选日志路径；不传默认写到 fpbrowser2api/logs.txt

    返回 dict：
    {
      "seen": bool,
      "request_id": str|None,
      "url": str|None,
      "method": str|None,
      "status": int|None,
      "response_body": str|None,
      "headers": dict|None,
      "log_file": str
    }
    """
    url_pat = re.compile(url_regex, flags=re.IGNORECASE)
    method_norm = method.upper().strip() if method else None
    deadline = time.time() + max(1.0, float(timeout_seconds))

    # 默认写到 fpbrowser2api 根目录，避免在 src/ 下产生提交噪音
    log_file = Path(log_path) if log_path else (MONITOR_LOG_FILE)

    result: Dict[str, Any] = {
        "seen": False,
        "request_id": None,
        "url": None,
        "method": None,
        "status": None,
        "response_body": None,
        "headers": None,
        "log_file": str(log_file),
    }

    # 开启 CDP Network（不强依赖，但有助于 getResponseBody）
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass

    append_log(log_file, "\n" + "=" * 100)
    append_log(log_file, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] sniff_start url_regex={url_regex!r} method={method_norm!r}")

    matched_request_ids: set[str] = set()

    while time.time() < deadline:
        try:
            logs = driver.get_log("performance") or []
        except Exception:
            logs = []

        for entry in logs:
            try:
                msg = json.loads(entry.get("message", "{}")).get("message", {})
                ev = msg.get("method")
                params = msg.get("params") or {}
            except Exception:
                continue

            if ev == "Network.requestWillBeSent":
                req = params.get("request") or {}
                url = (req.get("url") or "").strip()
                m = (req.get("method") or "").strip().upper()
                if not url or not url_pat.search(url):
                    continue
                if method_norm and m != method_norm:
                    continue
                request_id = str(params.get("requestId") or "")
                if not request_id:
                    continue

                result["seen"] = True
                matched_request_ids.add(request_id)
                if not result["request_id"]:
                    result["request_id"] = request_id
                    result["url"] = url
                    result["method"] = m

                append_log(log_file, "-" * 100)
                append_log(log_file, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] requestWillBeSent requestId={request_id}")
                append_log(log_file, f"url: {url}")
                append_log(log_file, f"method: {m}")

                # request postData（可选）
                try:
                    pd = driver.execute_cdp_cmd("Network.getRequestPostData", {"requestId": request_id}) or {}
                    post_data = pd.get("postData")
                    if post_data:
                        append_log(log_file, "postData:")
                        append_log(log_file, str(post_data))
                except Exception:
                    pass

            elif ev == "Network.responseReceived":
                request_id = str(params.get("requestId") or "")
                resp = params.get("response") or {}
                url = (resp.get("url") or "").strip()
                status = resp.get("status")
                if not request_id or not url or not url_pat.search(url):
                    continue

                result["seen"] = True
                matched_request_ids.add(request_id)
                if not result["request_id"]:
                    result["request_id"] = request_id
                    result["url"] = url
                if result["status"] is None:
                    result["status"] = status
                headers = resp.get("headers")
                if headers and result["headers"] is None:
                    result["headers"] = headers

                append_log(log_file, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] responseReceived requestId={request_id}")
                append_log(log_file, f"status: {status}")
                append_log(log_file, f"url: {url}")
                if headers:
                    append_log(log_file, "headers:")
                    try:
                        append_log(log_file, json.dumps(headers, ensure_ascii=False, indent=2))
                    except Exception:
                        append_log(log_file, str(headers))

            elif ev == "Network.loadingFinished":
                request_id = str(params.get("requestId") or "")
                if not request_id or request_id not in matched_request_ids:
                    continue
                if result["response_body"] is not None:
                    continue
                try:
                    body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id}) or {}
                    body_text = body.get("body")
                    is_base64 = body.get("base64Encoded")
                    if body_text is None:
                        continue
                    if is_base64:
                        try:
                            body_text = b64decode(body_text).decode("utf-8", errors="replace")
                        except Exception:
                            body_text = str(body_text)

                    result["response_body"] = str(body_text)
                    append_log(log_file, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] loadingFinished requestId={request_id}")
                    append_log(log_file, "responseBody:")
                    append_log(log_file, str(body_text))
                    deadline = time.time()
                except Exception as e:
                    append_log(log_file, f"getResponseBody failed: {e}")

        time.sleep(0.2)

    append_log(log_file, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] sniff_end")
    return result


def sniff_post(
    driver,
    *,
    url_regex: str,
    timeout_seconds: float = 15.0,
    log_path: Optional[str] = None,
) -> Dict[str, Any]:
    return sniff_http_transaction(
        driver,
        url_regex=url_regex,
        method="POST",
        timeout_seconds=timeout_seconds,
        log_path=log_path,
    )


def sniff_get(
    driver,
    *,
    url_regex: str,
    timeout_seconds: float = 15.0,
    log_path: Optional[str] = None,
) -> Dict[str, Any]:
    return sniff_http_transaction(
        driver,
        url_regex=url_regex,
        method="GET",
        timeout_seconds=timeout_seconds,
        log_path=log_path,
    )
