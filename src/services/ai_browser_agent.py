"""LLM 驱动的通用浏览器智能体。

本模块把“模型调用”和“Playwright 页面实时操作”封装成可复用能力：

- 大模型中转站地址固定为 https://www.newtoken.club/；
- 根据模型自动选择 `/v1/responses` 或 `/v1/chat/completions`；
- 对浏览器页面做轻量 DOM 扫描，给可交互元素分配临时 ref；
- LLM 只返回结构化 JSON 动作，执行器再通过 Playwright 落地；
- 表单资料支持 `value_key` 引用，尽量避免把密码/CVV/卡号等敏感明文交给模型。

设计上不绑定 PayPal，未来 Google 登录、页面分析、其它智能化操作都可以直接复用
`AIBrowserAgent.run(...)`。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from ..core.logger import logger
from ..core.paths import LOGS_DIR
from .playwright_broswer_context import page_fetch_json, page_fetch_tx, safe_trim


AI_AGENT_BASE_URL = "https://www.newtoken.club/"
AI_AGENT_MODELS: Tuple[str, ...] = (
    "gpt-5.5",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
)
AI_AGENT_DEFAULT_MODEL = "gpt-5.5"

_SECRET_KEY_HINTS = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "key",
    "cvv",
    "cvc",
    "card_number",
    "cardnumber",
    "sms_api_url",
    "2fa",
    "twofa",
    "otp",
    "efa",
)


def get_ai_agent_default_api_key() -> str:
    """读取默认 AI API Key。

    不写数据库；优先环境变量，随后读取 `config/setting.toml` 的 `[ai_agent].api_key`。
    前端页面也支持把 key 保存在用户本机 localStorage，并随请求临时传入后端。
    """

    for name in ("AI_AGENT_API_KEY", "NEWTOKEN_API_KEY", "OPENAI_API_KEY"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    try:
        from ..core.config import config

        raw = config.get_raw_config()
        value = str((raw.get("ai_agent") or {}).get("api_key") or "").strip()
        if value:
            return value
    except Exception:
        pass
    return ""


def normalize_ai_agent_model(model: Optional[str]) -> str:
    m = str(model or "").strip()
    return m if m in AI_AGENT_MODELS else AI_AGENT_DEFAULT_MODEL


def get_ai_agent_default_model() -> str:
    for name in ("AI_AGENT_MODEL", "NEWTOKEN_MODEL"):
        value = (os.getenv(name) or "").strip()
        if value:
            return normalize_ai_agent_model(value)
    try:
        from ..core.config import config

        raw = config.get_raw_config()
        value = str((raw.get("ai_agent") or {}).get("default_model") or "").strip()
        if value:
            return normalize_ai_agent_model(value)
    except Exception:
        pass
    return AI_AGENT_DEFAULT_MODEL


def endpoint_path_for_model(model: Optional[str]) -> str:
    """根据模型选择接口路径。

    - gpt-*：优先走 Responses API；
    - claude-*：走 OpenAI 兼容 chat/completions。
    """

    m = normalize_ai_agent_model(model)
    if m.startswith("gpt-"):
        return "/v1/responses"
    return "/v1/chat/completions"


def model_api_type(model: Optional[str]) -> str:
    return "responses" if endpoint_path_for_model(model).endswith("/responses") else "chat_completions"


def fixed_ai_agent_base_url() -> str:
    return AI_AGENT_BASE_URL


def _mask_value(value: Any, *, keep_left: int = 2, keep_right: int = 4) -> str:
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= keep_left + keep_right + 2:
        return "*" * min(len(s), 8)
    return f"{s[:keep_left]}{'*' * min(12, max(3, len(s) - keep_left - keep_right))}{s[-keep_right:]}"


def is_secret_key(key: Any) -> bool:
    k = str(key or "").strip().lower()
    if not k:
        return False
    return any(h in k for h in _SECRET_KEY_HINTS)


def _safe_data_for_prompt(data: Dict[str, Any]) -> Dict[str, Any]:
    """把执行资料转换成可交给模型的“字段清单”。

    模型只需要知道有哪些字段、字段大致是什么；真正填值由执行器根据 value_key
    在本地取值完成。
    """

    out: Dict[str, Any] = {}
    for k, v in (data or {}).items():
        if v is None:
            continue
        if isinstance(v, str):
            vv = v.strip()
            if not vv:
                continue
        else:
            vv = v
        if is_secret_key(k):
            out[str(k)] = {
                "available": True,
                "masked": _mask_value(vv),
                "use": f'value_key="{k}"',
            }
        else:
            text = str(vv)
            out[str(k)] = text if len(text) <= 120 else text[:117] + "..."
    return out


def _flatten_text_parts(value: Any) -> List[str]:
    parts: List[str] = []
    if value is None:
        return parts
    if isinstance(value, str):
        if value:
            parts.append(value)
        return parts
    if isinstance(value, list):
        for item in value:
            parts.extend(_flatten_text_parts(item))
        return parts
    if isinstance(value, dict):
        # Responses API 常见：{"type":"output_text","text":"..."}
        for key in ("text", "content", "output_text"):
            if isinstance(value.get(key), str):
                parts.append(str(value.get(key) or ""))
        # Chat Completions 常见：{"message":{"content":"..."}}
        if isinstance(value.get("message"), dict):
            parts.extend(_flatten_text_parts(value.get("message")))
        if isinstance(value.get("content"), list):
            parts.extend(_flatten_text_parts(value.get("content")))
        return parts
    return parts


def _parse_llm_text(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    if isinstance(payload.get("output_text"), str):
        return str(payload.get("output_text") or "")
    if isinstance(payload.get("choices"), list) and payload["choices"]:
        parts = _flatten_text_parts(payload["choices"][0])
        if parts:
            return "\n".join([p for p in parts if p])
    if isinstance(payload.get("output"), list):
        parts = _flatten_text_parts(payload.get("output"))
        if parts:
            return "\n".join([p for p in parts if p])
    # 兼容一些中转站直接返回 text/content 的格式。
    parts = _flatten_text_parts(payload)
    return "\n".join([p for p in parts if p])


def extract_json_object(text: str) -> Dict[str, Any]:
    """从模型输出中提取第一个 JSON object。"""

    raw = str(text or "").strip()
    if not raw:
        raise ValueError("模型没有返回内容")
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I).strip()
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # 扫描第一个平衡的大括号 JSON，避免模型包裹少量说明文字。
    start = raw.find("{")
    if start < 0:
        raise ValueError(f"未找到 JSON object：{safe_trim(raw, 300)}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                obj = json.loads(raw[start : i + 1])
                if isinstance(obj, dict):
                    return obj
                break
    raise ValueError(f"无法解析 JSON object：{safe_trim(raw, 300)}")


class AIModelClient:
    """固定中转站的模型客户端。"""

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 75.0,
    ) -> None:
        self.base_url = AI_AGENT_BASE_URL.rstrip("/") + "/"
        self.model = normalize_ai_agent_model(model)
        self.api_key = str(api_key or get_ai_agent_default_api_key() or "").strip()
        self.timeout = float(timeout or 75.0)

    @property
    def endpoint_path(self) -> str:
        return endpoint_path_for_model(self.model)

    @property
    def api_type(self) -> str:
        return model_api_type(self.model)

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise RuntimeError("缺少 AI API Key：请在智能体页面/PayPal页面填写，或设置环境变量 AI_AGENT_API_KEY / NEWTOKEN_API_KEY")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def chat_text(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> str:
        """发送多轮消息并返回文本。"""

        url = urljoin(self.base_url, self.endpoint_path.lstrip("/"))
        timeout = httpx.Timeout(self.timeout, connect=20.0)

        if self.api_type == "responses":
            instructions = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system").strip()
            input_items = [
                {"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")}
                for m in messages
                if m.get("role") != "system"
            ]
            if not input_items:
                input_items = [{"role": "user", "content": ""}]
            payload: Dict[str, Any] = {
                "model": self.model,
                "input": input_items,
                "temperature": float(temperature),
                "max_output_tokens": int(max_tokens),
            }
            if instructions:
                payload["instructions"] = instructions
        else:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")}
                    for m in messages
                ],
                "temperature": float(temperature),
                "max_tokens": int(max_tokens),
            }

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            try:
                resp = await client.post(url, headers=self._headers(), json=payload)
            except httpx.HTTPError as e:
                raise RuntimeError(f"AI 接口请求失败：{e}") from e
        if resp.status_code >= 400:
            body = safe_trim(resp.text, 1000)
            raise RuntimeError(f"AI 接口错误：HTTP {resp.status_code} {body}")
        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"AI 接口返回非 JSON：{safe_trim(resp.text, 500)}") from e
        text = _parse_llm_text(data)
        if not text:
            raise RuntimeError(f"AI 接口未返回文本：{safe_trim(json.dumps(data, ensure_ascii=False), 600)}")
        return text


def _first_locator(locator: Any) -> Any:
    first_attr = getattr(locator, "first", None)
    return first_attr() if callable(first_attr) else (first_attr or locator)


def _css_attr_escape(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _safe_url(url: Any) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in {"http", "https"} or not p.netloc:
        return ""
    return raw


def _action_to_chinese(action: Dict[str, Any]) -> str:
    act = str((action or {}).get("action") or "").strip().lower()
    reason = str((action or {}).get("reason") or "").strip()
    ref = str((action or {}).get("ref") or "").strip()
    if act in {"goto", "navigate"}:
        text = f"正在通过 Playwright 打开页面：{safe_trim(str(action.get('url') or ''), 120)}"
    elif act == "click":
        text = f"正在通过 Playwright 点击页面元素 {ref}"
    elif act in {"fill", "type"}:
        source = str(action.get("value_key") or "输入内容")
        text = f"正在通过 Playwright 填写页面字段 {ref}，数据来源：{source}"
    elif act == "select":
        source = str(action.get("value_key") or action.get("value") or "选项")
        text = f"正在通过 Playwright 选择下拉项 {ref}，数据来源：{source}"
    elif act == "press":
        text = f"正在通过 Playwright 按键：{action.get('key') or 'Enter'}"
    elif act == "shortcut":
        text = f"正在通过 Playwright 发送组合快捷键：{action.get('keys') or action.get('key') or ''}"
    elif act == "open_devtools_network":
        text = "正在尝试打开浏览器 DevTools / Network 面板，并开始采集 network log"
    elif act == "wait":
        text = f"正在等待页面响应：{int(action.get('ms') or 1200)}ms"
    elif act in {"fetch_json", "fetch"}:
        text = f"正在通过页面上下文 page_fetch_{'json' if act == 'fetch_json' else 'tx'} 请求：{str(action.get('method') or 'GET').upper()} {safe_trim(str(action.get('url') or ''), 120)}"
    elif act == "evaluate":
        text = "正在通过 Playwright 在页面上下文执行 JavaScript 诊断脚本"
    elif act in {"done", "finish", "success"}:
        text = "AI 判断任务已完成"
    elif act in {"need_human", "human", "blocked", "captcha"}:
        text = "AI 判断需要人工接管"
    elif act in {"fail", "error"}:
        text = "AI 判断任务失败"
    else:
        text = f"正在执行动作：{act or '-'}"
    if reason:
        text += f"；原因：{reason}"
    return text


_OBSERVE_JS = r"""
(args) => {
  const prefix = String(args.prefix || 'ai');
  const frameIndex = Number(args.frameIndex || 0);
  const maxElements = Number(args.maxElements || 80);
  const maxText = Number(args.maxText || 4200);
  const trim = (s, n = 160) => {
    s = String(s || '').replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 3) + '...' : s;
  };
  const visible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const st = window.getComputedStyle(el);
    if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    if (r.bottom < 0 || r.right < 0 || r.top > (window.innerHeight || 0) + 200 || r.left > (window.innerWidth || 0) + 200) return false;
    return true;
  };
  const labelFor = (el) => {
    const out = [];
    try {
      if (el.labels) Array.from(el.labels).forEach(l => out.push(trim(l.innerText || l.textContent || '', 120)));
    } catch(e) {}
    const id = el.getAttribute('id');
    if (id) {
      try {
        const l = document.querySelector(`label[for="${CSS.escape(id)}"]`);
        if (l) out.push(trim(l.innerText || l.textContent || '', 120));
      } catch(e) {}
    }
    let p = el.parentElement;
    for (let i = 0; p && i < 2; i++, p = p.parentElement) {
      const txt = trim(p.innerText || p.textContent || '', 120);
      if (txt && txt.length <= 120) out.push(txt);
    }
    return Array.from(new Set(out.filter(Boolean))).slice(0, 3).join(' | ');
  };
  const maskVal = (el) => {
    const tag = (el.tagName || '').toLowerCase();
    if (!['input','textarea','select'].includes(tag)) return '';
    const type = String(el.getAttribute('type') || '').toLowerCase();
    const val = String(el.value || '');
    if (!val) return '';
    if (type === 'password') return '********';
    if (/card|cvv|cvc|pass|secret|token/i.test(`${el.name || ''} ${el.id || ''} ${el.autocomplete || ''}`)) {
      return val.length <= 4 ? '****' : `${val.slice(0, 2)}****${val.slice(-2)}`;
    }
    return trim(val, 80);
  };
  const selector = [
    'input:not([type="hidden"])',
    'textarea',
    'select',
    'button',
    'a[href]',
    '[role="button"]',
    '[role="link"]',
    '[role="textbox"]',
    '[contenteditable="true"]',
    '[tabindex]:not([tabindex="-1"])'
  ].join(',');
  const nodes = Array.from(document.querySelectorAll(selector));
  const elements = [];
  let idx = 0;
  for (const el of nodes) {
    if (elements.length >= maxElements) break;
    if (!visible(el)) continue;
    const tag = (el.tagName || '').toLowerCase();
    const type = String(el.getAttribute('type') || '').toLowerCase();
    const role = String(el.getAttribute('role') || '');
    const text = trim(el.innerText || el.textContent || el.getAttribute('value') || '', 180);
    const placeholder = trim(el.getAttribute('placeholder') || '', 120);
    const aria = trim(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || '', 120);
    const title = trim(el.getAttribute('title') || '', 120);
    const name = trim(el.getAttribute('name') || '', 100);
    const id = trim(el.getAttribute('id') || '', 100);
    const autocomplete = trim(el.getAttribute('autocomplete') || '', 100);
    const label = labelFor(el);
    if (!text && !placeholder && !aria && !title && !name && !id && !label && tag !== 'select') {
      continue;
    }
    const ref = `${prefix}-${frameIndex}-${idx++}`;
    try { el.setAttribute('data-ai-agent-ref', ref); } catch(e) {}
    let options = [];
    if (tag === 'select') {
      try {
        options = Array.from(el.options || []).slice(0, 30).map(o => ({
          value: trim(o.value, 80),
          label: trim(o.label || o.textContent || '', 100),
          selected: !!o.selected
        }));
      } catch(e) {}
    }
    elements.push({
      ref,
      tag,
      type,
      role,
      id,
      name,
      label,
      placeholder,
      aria,
      title,
      text,
      autocomplete,
      value: maskVal(el),
      checked: !!el.checked,
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      options
    });
  }
  const bodyText = trim((document.body && (document.body.innerText || document.body.textContent)) || '', maxText);
  return {
    url: String(location.href || ''),
    title: String(document.title || ''),
    text: bodyText,
    elements
  };
}
"""


@dataclass
class BrowserAgentStep:
    index: int
    action: Dict[str, Any]
    result: Dict[str, Any]
    observation_url: str = ""
    observation_title: str = ""
    raw_model_output: str = ""


@dataclass
class AIBrowserAgent:
    """可复用的 Playwright 页面智能体。"""

    page: Any
    model: Optional[str] = None
    api_key: Optional[str] = None
    max_steps: int = 12
    step_log: Optional[List[str]] = None
    llm_timeout: float = 75.0
    max_elements: int = 80
    max_text_chars: int = 4200
    client: AIModelClient = field(init=False)
    network_events: List[Dict[str, Any]] = field(default_factory=list)
    console_events: List[Dict[str, Any]] = field(default_factory=list)
    page_errors: List[Dict[str, Any]] = field(default_factory=list)
    _instrumented: bool = False

    def __post_init__(self) -> None:
        self.model = normalize_ai_agent_model(self.model)
        self.max_steps = max(1, min(int(self.max_steps or 12), 30))
        self.client = AIModelClient(model=self.model, api_key=self.api_key, timeout=self.llm_timeout)

    def log_step(self, message: str) -> None:
        msg = str(message or "").strip()
        if not msg:
            return
        if self.step_log is not None:
            self.step_log.append(msg)
        logger.info("[ai-browser-agent] %s", msg)

    @staticmethod
    def _append_limited(items: List[Dict[str, Any]], item: Dict[str, Any], *, limit: int = 160) -> None:
        try:
            items.append(item)
            if len(items) > limit:
                del items[: len(items) - limit]
        except Exception:
            pass

    def _ensure_instrumentation(self) -> None:
        """挂载页面级诊断监听：Network / Console / PageError。

        这些数据会进入 observe()，让模型可以分析 network log、接口错误、控制台错误和调用栈。
        """

        if self._instrumented:
            return
        self._instrumented = True

        def _ts() -> str:
            try:
                return time.strftime("%H:%M:%S", time.localtime())
            except Exception:
                return ""

        try:
            self.page.on(
                "request",
                lambda req: self._append_limited(
                    self.network_events,
                    {
                        "time": _ts(),
                        "event": "request",
                        "method": getattr(req, "method", ""),
                        "url": safe_trim(getattr(req, "url", "") or "", 500),
                        "resource_type": getattr(req, "resource_type", ""),
                    },
                ),
            )
        except Exception:
            pass
        try:
            self.page.on(
                "response",
                lambda resp: self._append_limited(
                    self.network_events,
                    {
                        "time": _ts(),
                        "event": "response",
                        "status": getattr(resp, "status", None),
                        "url": safe_trim(getattr(resp, "url", "") or "", 500),
                    },
                ),
            )
        except Exception:
            pass
        try:
            self.page.on(
                "requestfailed",
                lambda req: self._append_limited(
                    self.network_events,
                    {
                        "time": _ts(),
                        "event": "requestfailed",
                        "method": getattr(req, "method", ""),
                        "url": safe_trim(getattr(req, "url", "") or "", 500),
                        "failure": safe_trim(str(getattr(req, "failure", "") or ""), 500),
                    },
                ),
            )
        except Exception:
            pass
        try:
            self.page.on(
                "console",
                lambda msg: self._append_limited(
                    self.console_events,
                    {
                        "time": _ts(),
                        "type": getattr(msg, "type", ""),
                        "text": safe_trim(str(getattr(msg, "text", "") or ""), 1000),
                        "location": getattr(msg, "location", None),
                    },
                ),
            )
        except Exception:
            pass
        try:
            self.page.on(
                "pageerror",
                lambda exc: self._append_limited(
                    self.page_errors,
                    {
                        "time": _ts(),
                        "error": safe_trim(str(exc), 2000),
                    },
                ),
            )
        except Exception:
            pass

    async def observe(self) -> Dict[str, Any]:
        """扫描当前页面和 iframe 中的可交互元素。"""

        self._ensure_instrumentation()
        frames = []
        try:
            frames = list(getattr(self.page, "frames", []) or [])
        except Exception:
            frames = []
        if not frames:
            frames = [getattr(self.page, "main_frame", None) or self.page]

        observed_frames: List[Dict[str, Any]] = []
        all_elements: List[Dict[str, Any]] = []
        for frame_index, frame in enumerate(frames[:10]):
            try:
                data = await frame.evaluate(
                    _OBSERVE_JS,
                    {
                        "prefix": f"ai{int(time.time() * 1000) % 100000}",
                        "frameIndex": frame_index,
                        "maxElements": self.max_elements,
                        "maxText": self.max_text_chars,
                    },
                )
            except Exception as e:
                observed_frames.append(
                    {
                        "frame_index": frame_index,
                        "url": safe_trim(getattr(frame, "url", "") or "", 500),
                        "title": "",
                        "text": f"[frame scan failed: {safe_trim(str(e), 180)}]",
                        "elements": [],
                    }
                )
                continue

            if not isinstance(data, dict):
                continue
            elems = data.get("elements") if isinstance(data.get("elements"), list) else []
            for el in elems:
                if isinstance(el, dict):
                    el["frame_index"] = frame_index
                    all_elements.append(el)
            observed_frames.append(
                {
                    "frame_index": frame_index,
                    "url": data.get("url") or "",
                    "title": data.get("title") or "",
                    "text": data.get("text") or "",
                    "elements": elems,
                }
            )

        try:
            page_url = str(getattr(self.page, "url", "") or "")
        except Exception:
            page_url = ""
        try:
            page_title = await self.page.title()
        except Exception:
            page_title = ""

        return {
            "url": page_url,
            "title": page_title,
            "frames": observed_frames,
            "elements": all_elements[: self.max_elements * 10],
            "diagnostics": {
                "recent_network": self.network_events[-80:],
                "recent_console": self.console_events[-80:],
                "recent_page_errors": self.page_errors[-40:],
            },
        }

    async def _locator_for_ref(self, ref: str, frame_index: Optional[int] = None) -> Any:
        selector = f'[data-ai-agent-ref="{_css_attr_escape(ref)}"]'
        frames = list(getattr(self.page, "frames", []) or [])
        if frame_index is not None and 0 <= int(frame_index) < len(frames):
            return _first_locator(frames[int(frame_index)].locator(selector))
        # 兜底：全部 frame 找一次。
        last_err: Optional[Exception] = None
        for frame in frames or []:
            try:
                loc = _first_locator(frame.locator(selector))
                if await loc.count() > 0:
                    return loc
            except Exception as e:
                last_err = e
        try:
            return _first_locator(self.page.locator(selector))
        except Exception as e:
            raise RuntimeError(f"找不到元素 ref={ref}: {last_err or e}") from e

    @staticmethod
    def _resolve_value(action: Dict[str, Any], data: Dict[str, Any]) -> Tuple[str, str]:
        value_key = str(action.get("value_key") or "").strip()
        if value_key:
            if value_key not in data or data.get(value_key) in (None, ""):
                raise RuntimeError(f"value_key={value_key!r} 不存在或为空")
            return str(data.get(value_key) or ""), value_key
        return str(action.get("value") or ""), "value"

    @staticmethod
    def _normalize_shortcut(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        aliases = {
            "ctrl": "Control",
            "control": "Control",
            "cmd": "Meta",
            "command": "Meta",
            "meta": "Meta",
            "shift": "Shift",
            "alt": "Alt",
            "option": "Alt",
            "esc": "Escape",
            "escape": "Escape",
            "space": "Space",
            "del": "Delete",
            "delete": "Delete",
            "enter": "Enter",
            "return": "Enter",
        }
        parts = [p.strip() for p in re.split(r"\s*\+\s*", raw) if p.strip()]
        out: List[str] = []
        allowed_single = {
            "Enter",
            "Tab",
            "Escape",
            "Backspace",
            "ArrowDown",
            "ArrowUp",
            "ArrowLeft",
            "ArrowRight",
            "Space",
            "Delete",
            "Home",
            "End",
            "PageUp",
            "PageDown",
            "Insert",
        } | {f"F{i}" for i in range(1, 13)}
        for p in parts:
            lp = p.lower()
            norm = aliases.get(lp)
            if norm is None:
                if len(p) == 1 and p.isalnum():
                    norm = p.upper()
                else:
                    # 保留 Playwright 标准键名，比如 ArrowDown / PageDown / F12
                    norm = p[0].upper() + p[1:] if p else p
            if norm in {"Control", "Shift", "Alt", "Meta"} or norm in allowed_single or (len(norm) == 1 and norm.isalnum()):
                out.append(norm)
            else:
                raise RuntimeError(f"不允许的快捷键：{p}")
        return "+".join(out)

    async def execute_action(self, action: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        act = str(action.get("action") or "").strip().lower()
        if not act:
            raise RuntimeError("模型返回的 action 为空")

        if act in {"done", "finish", "success"}:
            return {"stop": True, "status": "done", "summary": str(action.get("summary") or action.get("reason") or "已完成")}
        if act in {"need_human", "human", "blocked", "captcha"}:
            return {
                "stop": True,
                "status": "need_human",
                "summary": str(action.get("summary") or action.get("reason") or "需要人工介入"),
            }
        if act in {"fail", "error"}:
            return {"stop": True, "status": "failed", "summary": str(action.get("summary") or action.get("reason") or "模型判断失败")}
        if act == "wait":
            ms = int(action.get("ms") or action.get("milliseconds") or 1200)
            ms = max(100, min(ms, 15000))
            await asyncio.sleep(ms / 1000.0)
            return {"stop": False, "status": "ok", "message": f"wait {ms}ms"}
        if act in {"goto", "navigate"}:
            url = _safe_url(action.get("url"))
            if not url:
                raise RuntimeError("goto 缺少有效 http/https url")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            await asyncio.sleep(1.0)
            return {"stop": False, "status": "ok", "message": f"已打开页面：{safe_trim(url, 160)}"}
        if act in {"fetch_json", "fetch"}:
            url = _safe_url(action.get("url"))
            if not url:
                raise RuntimeError(f"{act} 缺少有效 http/https url")
            method = str(action.get("method") or "GET").upper().strip()
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                raise RuntimeError(f"不允许的 fetch method：{method}")
            headers = action.get("headers") if isinstance(action.get("headers"), dict) else {}
            json_data = action.get("json") if isinstance(action.get("json"), dict) else None
            log_file = LOGS_DIR / "ai_browser_agent_fetch.log"
            if act == "fetch_json":
                tx = await page_fetch_json(
                    self.page,
                    url=url,
                    method=method,
                    headers={str(k): str(v) for k, v in headers.items()},
                    json_data=json_data,
                    log_file=log_file,
                )
                obj = tx.get("_json")
                return {
                    "stop": False,
                    "status": "ok",
                    "message": f"已通过页面上下文请求 JSON：{method} {safe_trim(url, 120)}",
                    "http_status": tx.get("status"),
                    "json": obj if isinstance(obj, (dict, list)) else safe_trim(str(obj), 1000),
                }
            tx = await page_fetch_tx(
                self.page,
                url=url,
                method=method,
                headers={str(k): str(v) for k, v in headers.items()},
                json_data=json_data,
                log_file=log_file,
            )
            return {
                "stop": False,
                "status": "ok",
                "message": f"已通过页面上下文请求：{method} {safe_trim(url, 120)}",
                "http_status": tx.get("status"),
                "body": safe_trim(str(tx.get("response_body") or ""), 1500),
            }
        if act == "press":
            key = str(action.get("key") or "Enter").strip()
            allowed = {
                "Enter",
                "Tab",
                "Escape",
                "Backspace",
                "ArrowDown",
                "ArrowUp",
                "ArrowLeft",
                "ArrowRight",
                "Space",
                "Delete",
                "Home",
                "End",
                "PageUp",
                "PageDown",
                "Insert",
            }
            allowed.update({f"F{i}" for i in range(1, 13)})
            if key not in allowed:
                raise RuntimeError(f"不允许的按键：{key}")
            await self.page.keyboard.press(key)
            await asyncio.sleep(float(action.get("after_ms") or 900) / 1000.0)
            return {"stop": False, "status": "ok", "message": f"已按键：{key}"}
        if act == "shortcut":
            shortcut = self._normalize_shortcut(action.get("keys") or action.get("key"))
            if not shortcut:
                raise RuntimeError("shortcut 缺少 keys")
            await self.page.keyboard.press(shortcut)
            await asyncio.sleep(float(action.get("after_ms") or 900) / 1000.0)
            return {"stop": False, "status": "ok", "message": f"已发送快捷键：{shortcut}"}
        if act == "open_devtools_network":
            # 对真实浏览器窗口，F12/快捷键可打开 DevTools；同时本智能体会继续采集 network/console/pageerror。
            # 不同内核/系统对 Network 面板快捷键支持不一致，因此采用“打开 DevTools + 尝试 Network 快捷键”的尽力策略。
            await self.page.keyboard.press("F12")
            await asyncio.sleep(1.0)
            try:
                await self.page.keyboard.press("Control+Shift+E")
                await asyncio.sleep(0.8)
            except Exception:
                pass
            return {
                "stop": False,
                "status": "ok",
                "message": "已尝试打开 DevTools/Network 面板；智能体已开始采集 network log、console log 和 page error。",
                "recent_network_count": len(self.network_events),
                "recent_console_count": len(self.console_events),
                "recent_page_error_count": len(self.page_errors),
            }
        if act == "evaluate":
            code = str(action.get("code") or "").strip()
            if not code:
                raise RuntimeError("evaluate 缺少 code")
            if len(code) > 12000:
                raise RuntimeError("evaluate code 过长")
            result = await self.page.evaluate(
                """async (code) => {
                  const fn = new Function('return (async () => {' + code + '\\n})()');
                  return await fn();
                }""",
                code,
            )
            return {
                "stop": False,
                "status": "ok",
                "message": "已执行页面诊断脚本",
                "result": result if isinstance(result, (dict, list, str, int, float, bool)) or result is None else safe_trim(str(result), 2000),
            }

        ref = str(action.get("ref") or "").strip()
        if not ref:
            raise RuntimeError(f"{act} 缺少 ref")
        frame_index_raw = action.get("frame_index")
        frame_index = int(frame_index_raw) if frame_index_raw is not None and str(frame_index_raw).isdigit() else None
        loc = await self._locator_for_ref(ref, frame_index=frame_index)

        if act == "click":
            try:
                await loc.click(timeout=8000)
            except Exception:
                await loc.click(timeout=3000, force=True)
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(float(action.get("after_ms") or 1200) / 1000.0)
            return {"stop": False, "status": "ok", "message": f"已点击页面元素：{ref}"}

        if act in {"fill", "type"}:
            value, source = self._resolve_value(action, data)
            if value == "":
                raise RuntimeError(f"填充值为空：source={source}")
            try:
                await loc.fill(value, timeout=8000)
            except Exception:
                await loc.click(timeout=3000, force=True)
                if bool(action.get("clear", True)):
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                await self.page.keyboard.insert_text(value)
            await asyncio.sleep(float(action.get("after_ms") or 500) / 1000.0)
            return {"stop": False, "status": "ok", "message": f"已填写页面字段：{ref}，数据来源：{source}"}

        if act == "select":
            value, source = self._resolve_value(action, data)
            if value == "":
                raise RuntimeError(f"选择值为空：source={source}")
            selected = False
            errors: List[str] = []
            for kwargs in ({"value": value}, {"label": value}, {"index": int(value)} if str(value).isdigit() else None):
                if not kwargs:
                    continue
                try:
                    await loc.select_option(**kwargs, timeout=6000)
                    selected = True
                    break
                except Exception as e:
                    errors.append(str(e))
            if not selected:
                raise RuntimeError(f"select 失败：{safe_trim('; '.join(errors), 300)}")
            await asyncio.sleep(float(action.get("after_ms") or 700) / 1000.0)
            return {"stop": False, "status": "ok", "message": f"已选择页面下拉项：{ref}，数据来源：{source}"}

        if act in {"check", "uncheck"}:
            checked = act == "check"
            try:
                await loc.set_checked(checked, timeout=6000)
            except Exception:
                await loc.click(timeout=5000, force=True)
            await asyncio.sleep(float(action.get("after_ms") or 500) / 1000.0)
            return {"stop": False, "status": "ok", "message": f"已{'勾选' if checked else '取消勾选'}页面元素：{ref}"}

        raise RuntimeError(f"不支持的 action：{act}")

    def _system_prompt(self) -> str:
        return (
            "你是一个浏览器自动化智能体的大脑，面向中文用户工作。你不会直接操作浏览器，必须只返回一个 JSON object，不能输出 Markdown。\n"
            "后端已经通过 Playwright 连接/打开指纹浏览器目标窗口；你会收到任务、可用资料字段、当前页面文本和可交互元素列表。每个元素都有 ref 和 frame_index。\n"
            "你必须用中文填写 reason、summary 等用户可见字段；不要返回英文交互文案。\n"
            "你每次只能选择一个下一步动作。允许动作：\n"
            '1) {"action":"fill","ref":"...","frame_index":0,"value_key":"字段名","reason":"..."}\n'
            '2) {"action":"select","ref":"...","frame_index":0,"value_key":"字段名","reason":"..."}\n'
            '3) {"action":"click","ref":"...","frame_index":0,"reason":"..."}\n'
            '4) {"action":"press","key":"Enter|Tab|Escape|Backspace|ArrowDown|ArrowUp|ArrowLeft|ArrowRight|Space","reason":"..."}\n'
            '5) {"action":"shortcut","keys":"Control+Shift+I|Control+Shift+J|F12|...","reason":"打开开发者工具/触发快捷键"}\n'
            '6) {"action":"open_devtools_network","reason":"打开窗口的 Network log 面板并开始采集网络日志"}\n'
            '7) {"action":"wait","ms":1200,"reason":"..."}\n'
            '8) {"action":"goto","url":"https://...","reason":"..."}\n'
            '9) {"action":"fetch_json","method":"GET|POST","url":"https://...","headers":{},"json":{},"reason":"通过页面上下文 page_fetch_json 调用接口"}\n'
            '10) {"action":"fetch","method":"GET|POST","url":"https://...","headers":{},"json":{},"reason":"通过页面上下文 page_fetch_tx 调用接口并读取文本"}\n'
            '11) {"action":"evaluate","code":"return {url: location.href, perf: performance.getEntriesByType(' + "'resource'" + ').slice(-20)};","reason":"在页面上下文分析性能/调用栈/资源日志"}\n'
            '12) {"action":"done","summary":"中文完成总结，必须说明已经完成了哪些页面操作"}\n'
            '13) {"action":"need_human","summary":"需要人工处理验证码/短信/风控/二次验证等"}\n'
            "填写账号、密码、卡号、CVV、手机号、地址等资料时，优先使用 value_key 引用可用字段，不要把敏感值写在 value 里。\n"
            "你会在 current_page.diagnostics 中看到 recent_network、recent_console、recent_page_errors，可用于分析 network log、控制台错误和调用堆栈。\n"
            "如果用户要求“打开 network log 面板/Network 面板/开发者工具网络日志”，优先使用 open_devtools_network；随后可 done，说明已开始采集日志。\n"
            "如果用户要求分析网络接口、请求失败、控制台报错、调用堆栈，可结合 diagnostics、fetch_json/fetch 和 evaluate 分析。\n"
            "只使用当前观察中真实存在、可见且未禁用的 ref。页面出现验证码、短信码、安全挑战、风控拦截、无法继续时返回 need_human。\n"
            "如果 auto_submit=false，不要点击最终完成登录/注册/支付/绑卡的提交按钮；可以完成必要的下一步以显示后续输入框。\n"
            "如果 auto_submit=true，可以点击明确的登录、继续、创建账号等按钮，但不要绕过验证码/风控。\n"
            "不要过早返回 done：只有当用户目标已经在页面上有明确完成证据，或已经完成必要填写/点击并停在需要人工接管的验证环节时，才可以 done/need_human。\n"
            "如果页面还可以继续操作，请继续选择 click/fill/select/wait/goto/fetch_json 等动作；登录/注册类任务通常需要多步扫描、填写、点击、等待。\n"
            "始终返回严格 JSON object。"
        )

    async def decide(
        self,
        *,
        task: str,
        observation: Dict[str, Any],
        data: Dict[str, Any],
        auto_submit: bool,
        previous_steps: Sequence[BrowserAgentStep],
    ) -> Tuple[Dict[str, Any], str]:
        simplified = {
            "url": observation.get("url"),
            "title": observation.get("title"),
            "frames": [
                {
                    "frame_index": f.get("frame_index"),
                    "url": safe_trim(f.get("url") or "", 300),
                    "title": safe_trim(f.get("title") or "", 160),
                    "text": safe_trim(f.get("text") or "", self.max_text_chars),
                }
                for f in observation.get("frames", [])
            ],
            "elements": observation.get("elements", [])[: self.max_elements],
            "diagnostics": observation.get("diagnostics") or {},
        }
        prior = [
            {
                "step": s.index,
                "action": {k: v for k, v in s.action.items() if k != "value"},
                "result": s.result,
            }
            for s in previous_steps[-8:]
        ]
        user_payload = {
            "task": task,
            "auto_submit": bool(auto_submit),
            "available_data": _safe_data_for_prompt(data),
            "current_page": simplified,
            "previous_steps": prior,
        }
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {
                "role": "user",
                "content": (
                    "请根据以下 JSON 判断下一步浏览器动作，只返回一个 JSON object：\n"
                    + json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
                ),
            },
        ]
        raw = await self.client.chat_text(messages, temperature=0.1, max_tokens=1200)
        action = extract_json_object(raw)
        return action, raw

    async def run(
        self,
        *,
        task: str,
        data: Optional[Dict[str, Any]] = None,
        auto_submit: bool = False,
        initial_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行一个高层浏览器任务。"""

        data = dict(data or {})
        if initial_url:
            url = _safe_url(initial_url)
            if url:
                self.log_step(f"正在通过 Playwright 打开目标页面：{safe_trim(url, 160)}")
                await self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                await asyncio.sleep(1.0)

        steps: List[BrowserAgentStep] = []
        status = "max_steps"
        summary = f"达到最大步数 {self.max_steps}，任务未明确完成"

        for idx in range(1, self.max_steps + 1):
            self.log_step(f"第 {idx} 步：正在通过 Playwright 扫描页面文本、按钮、输入框和 iframe...")
            observation = await self.observe()
            self.log_step(
                f"第 {idx} 步：页面扫描完成，当前标题「{safe_trim(str(observation.get('title') or ''), 80)}」，"
                f"发现 {len(observation.get('elements') or [])} 个可交互元素"
            )
            action, raw = await self.decide(
                task=task,
                observation=observation,
                data=data,
                auto_submit=auto_submit,
                previous_steps=steps,
            )
            safe_action_for_log = {k: v for k, v in action.items() if k != "value"}
            self.log_step(f"第 {idx} 步：AI 决策：{json.dumps(safe_action_for_log, ensure_ascii=False)}")
            self.log_step(f"第 {idx} 步：{_action_to_chinese(safe_action_for_log)}")
            result = await self.execute_action(action, data)
            self.log_step(f"第 {idx} 步：执行结果：{safe_trim(str(result.get('message') or result.get('summary') or result), 500)}")
            steps.append(
                BrowserAgentStep(
                    index=idx,
                    action=safe_action_for_log,
                    result=result,
                    observation_url=str(observation.get("url") or ""),
                    observation_title=str(observation.get("title") or ""),
                    raw_model_output=safe_trim(raw, 1200),
                )
            )
            if result.get("stop"):
                status = str(result.get("status") or "done")
                summary = str(result.get("summary") or "")
                break

        try:
            final_url = str(getattr(self.page, "url", "") or "")
        except Exception:
            final_url = ""
        try:
            title = await self.page.title()
        except Exception:
            title = ""

        return {
            "success": status in {"done", "need_human"},
            "status": status,
            "summary": summary,
            "model": self.model,
            "api_type": self.client.api_type,
            "endpoint_path": self.client.endpoint_path,
            "final_url": final_url,
            "title": title,
            "steps": [
                {
                    "index": s.index,
                    "action": s.action,
                    "result": s.result,
                    "observation_url": s.observation_url,
                    "observation_title": s.observation_title,
                    "raw_model_output": s.raw_model_output,
                }
                for s in steps
            ],
        }


def build_browser_task_prompt(*, action: str, target: str, extra_prompt: Optional[str] = None) -> str:
    action = str(action or "operate").strip()
    target = str(target or "当前页面").strip()
    base = f"请在浏览器中完成任务：{action}。目标站点/页面：{target}。"
    extra = str(extra_prompt or "").strip()
    if extra:
        base += "\n额外要求：" + extra
    return base
