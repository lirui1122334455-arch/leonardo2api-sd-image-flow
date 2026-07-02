"""Sora Plus 注册执行器（窗口账号自动填充流程）。

流程（当前实现）：
1) 根据 window_pk 查找窗口绑定的平台网址/账号/密码
2) 打开对应指纹浏览器窗口，关闭其他所有标签页，仅保留一个页面并 bring to front
3) 访问平台网址（Google 登录页），判断是否已登录
   - 是 → 跳过登录步骤
   - 否 → 填邮箱 → Next → 填密码 → Next → 2FA → Next
4) Google 登录成功后，跳转 https://chatgpt.com/
5) 点击页面中的 "Sign in" 按钮
6) 在弹出的登录选项中点击 "Continue with Google"
7) 若出现 2FA 验证页面，重新计算 TOTP code 并输入
8) 完成 ChatGPT 的 Google 账号登录
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import subprocess
import struct
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from ..core.database import Database
from .playwright_broswer_context import acquire_browser_open_slot, get_or_create_ctx, pick_working_page_from_context
from .task_executor_types import ProgressCB


def _s(v: Any) -> str:
    return str(v or "").strip()


def _safe_hostname(url: str) -> str:
    try:
        return str(urlparse(str(url or "").strip()).hostname or "").strip().lower()
    except Exception:
        return ""


def _main_domain(hostname: str) -> str:
    host = str(hostname or "").strip().lower().strip(".")
    if not host:
        return ""
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return host
    second_level_markers = {"ac", "co", "com", "edu", "gov", "net", "org"}
    country_tlds = {"au", "br", "cn", "hk", "jp", "kr", "mx", "nz", "sg", "tw", "uk", "za"}
    if len(parts[-1]) == 2 and parts[-1] in country_tlds and parts[-2] in second_level_markers and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _same_main_domain(url_a: str, url_b: str) -> bool:
    host_a = _safe_hostname(url_a)
    host_b = _safe_hostname(url_b)
    if not host_a or not host_b:
        return False
    return _main_domain(host_a) == _main_domain(host_b)


async def _pick_platform_domain_page(context: Any, *, platform_url: str) -> Any:
    """优先选与 platform_url 主域名一致的已开页面；找不到时回退可用页面。"""
    target_host = _safe_hostname(platform_url)
    target_url = str(platform_url or "").strip().lower()
    best = None
    best_score = -1
    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        pages = []
    for p in pages:
        try:
            if bool(getattr(p, "is_closed", lambda: False)()):
                continue
        except Exception:
            continue
        try:
            page_url = str(getattr(p, "url", "") or "").strip()
        except Exception:
            page_url = ""
        if not _same_main_domain(page_url, target_url):
            continue
        page_url_lc = page_url.lower()
        page_host = _safe_hostname(page_url)
        score = 0
        if page_url_lc.startswith(("http://", "https://")):
            score += 10
        if target_host and page_host == target_host:
            score += 5
        if target_url and page_url_lc.startswith(target_url):
            score += 3
        if score > best_score:
            best_score = score
            best = p
    if best is not None:
        return best
    return await pick_working_page_from_context(context)


GOOGLE_PASSWORD_PAGE_URL = "https://myaccount.google.com/signinoptions/password?continue=https%3A%2F%2Fmyaccount.google.com%2Fsecurity"
NEW_PASSWORD_VALUE = "Laky373155210"


async def _click_next_button(page: Any, *, timeout_ms: int) -> str:
    """点击 Next 按钮（兼容不同站点/页面结构），返回命中的 selector。"""
    selectors = [
        "#identifierNext",
        "button#identifierNext",
        "#passwordNext",
        "button#passwordNext",
        'button:has-text("Next")',
        'div[role="button"]:has-text("Next")',
        'input[type="submit"][value="Next"]',
    ]
    per_try_timeout = int(max(1000, min(5000, timeout_ms // 4)))
    last_err: Optional[Exception] = None
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            await loc.wait_for(state="visible", timeout=per_try_timeout)
            await loc.click(timeout=per_try_timeout)
            return sel
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"未找到可点击的 Next 按钮（已尝试 {selectors}），last_err={last_err}")


async def _click_google_signin_next(page: Any, *, timeout_ms: int) -> str:
    """Google accounts 登录链路上的 Next / Continue / 下一步（较 _click_next_button 更全）。"""
    selectors = [
        "#identifierNext",
        "button#identifierNext",
        "#passwordNext",
        "button#passwordNext",
        'button:has-text("Next")',
        'div[role="button"]:has-text("Next")',
        'button:has-text("Continue")',
        'div[role="button"]:has-text("Continue")',
        'button:has-text("下一步")',
        'div[role="button"]:has-text("下一步")',
        'button:has-text("继续")',
        'div[role="button"]:has-text("继续")',
        'input[type="submit"][value="Next"]',
    ]
    per_try_timeout = int(max(1000, min(5000, timeout_ms // 4)))
    last_err: Optional[Exception] = None
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            await loc.wait_for(state="visible", timeout=per_try_timeout)
            await loc.click(timeout=per_try_timeout)
            return sel
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"未找到 Google 登录 Next/Continue（已尝试 {len(selectors)} 种），last_err={last_err}")


async def _click_save_or_next_button(page: Any, *, timeout_ms: int) -> str:
    """在密码修改页点击 Change password / Save / Next。"""
    selectors = [
        'button:has-text("Change password")',
        'div[role="button"]:has-text("Change password")',
        'input[type="submit"][value="Change password"]',
        "#passwordNext",
        "button#passwordNext",
        'button:has-text("Save")',
        'div[role="button"]:has-text("Save")',
        'button:has-text("Next")',
        'div[role="button"]:has-text("Next")',
        'button:has-text("更改密码")',
        'div[role="button"]:has-text("更改密码")',
        'button:has-text("保存")',
        'div[role="button"]:has-text("保存")',
        'button:has-text("下一步")',
        'div[role="button"]:has-text("下一步")',
        'input[type="submit"][value="Save"]',
        'input[type="submit"][value="Next"]',
    ]
    per_try_timeout = int(max(1000, min(5000, timeout_ms // 4)))
    last_err: Optional[Exception] = None
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            await loc.wait_for(state="visible", timeout=per_try_timeout)
            await loc.click(timeout=per_try_timeout)
            return sel
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"未找到可点击的 Save/Next 按钮（已尝试 {selectors}），last_err={last_err}")


def _generate_totp_code(secret: str, *, digits: int = 6, period_seconds: int = 30) -> str:
    """基于 Google 2FA Secret 生成当前 TOTP code。"""
    s = str(secret or "").strip().replace(" ", "").replace("-", "").upper()
    if not s:
        raise RuntimeError("EFA(2FA Secret) 为空，无法生成验证码")
    pad_len = (-len(s)) % 8
    s_padded = s + ("=" * pad_len)
    try:
        key = base64.b32decode(s_padded, casefold=True)
    except Exception as e:
        raise RuntimeError(f"EFA(2FA Secret) 非法，无法 Base32 解码：{e}") from e

    counter = int(time.time() // max(1, int(period_seconds)))
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    code = code_int % (10**int(digits))
    return str(code).zfill(int(digits))


def _copy_to_system_clipboard(text: str) -> bool:
    """写入系统剪贴板；成功返回 True，失败返回 False。"""
    v = str(text or "")
    if not v:
        return False

    # Windows
    if os.name == "nt":
        try:
            subprocess.run("clip", input=v, text=True, shell=True, check=True)
            return True
        except Exception:
            return False

    # macOS
    try:
        subprocess.run(["pbcopy"], input=v, text=True, check=True)
        return True
    except Exception:
        pass

    # Linux Wayland / X11
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        try:
            subprocess.run(cmd, input=v, text=True, check=True)
            return True
        except Exception:
            continue
    return False


async def _generate_fresh_totp_code(secret: str, previous_code: str) -> str:
    """尽量生成与 previous_code 不同的新 code（最多等待约 35 秒）。"""
    old = str(previous_code or "").strip()
    cur = _generate_totp_code(secret)
    if cur != old:
        return cur
    for _ in range(35):
        await asyncio.sleep(1)
        cur = _generate_totp_code(secret)
        if cur != old:
            return cur
    return cur


async def _resolve_window_platform_credentials(db: Database, *, window_pk: int) -> Dict[str, Optional[str]]:
    """解析窗口绑定的平台凭据（url / username / password）。

    优先级：
    1) windows.platform_account_id 关联 platform_accounts.account_id
    2) 若未绑定 account_id，则按 windows.platform_account + platform_url 在本地账号库匹配
    """
    win = await db.get_window(int(window_pk))
    if not win:
        raise RuntimeError(f"窗口不存在：window_pk={window_pk}")

    platform_url = _s(getattr(win, "platform_url", None)) or None
    platform_username = _s(getattr(win, "platform_account", None)) or None
    platform_password: Optional[str] = None
    platform_efa: Optional[str] = None

    space_pk = int(getattr(win, "space_pk", 0) or 0)
    if space_pk <= 0:
        raise RuntimeError(f"窗口缺少 space_pk：window_pk={window_pk}")

    account_id = int(getattr(win, "platform_account_id", 0) or 0)
    if account_id > 0:
        acc = await db.get_platform_account(space_pk=space_pk, account_id=account_id)
        if acc:
            platform_url = _s(getattr(acc, "platform_url", None)) or platform_url
            platform_username = _s(getattr(acc, "platform_username", None)) or platform_username
            platform_password = _s(getattr(acc, "platform_password", None)) or None
            platform_efa = _s(getattr(acc, "platform_efa", None)) or None
    else:
        cands = await db.list_platform_accounts(space_pk)
        target_user = (platform_username or "").strip().lower()
        target_url = (platform_url or "").strip().lower()
        for acc in cands:
            u = _s(getattr(acc, "platform_username", None)).lower()
            u_ok = bool(target_user) and u == target_user
            if not u_ok:
                continue
            a_url = _s(getattr(acc, "platform_url", None)).lower()
            if target_url and a_url and a_url != target_url:
                continue
            platform_url = _s(getattr(acc, "platform_url", None)) or platform_url
            platform_password = _s(getattr(acc, "platform_password", None)) or None
            platform_efa = _s(getattr(acc, "platform_efa", None)) or None
            break

    if not platform_url:
        raise RuntimeError(f"窗口未绑定平台网址：window_pk={window_pk}")
    if not platform_username:
        raise RuntimeError(f"窗口未绑定平台账号(邮箱)：window_pk={window_pk}")
    if not platform_password:
        raise RuntimeError(f"窗口绑定账号缺少密码：window_pk={window_pk}")
    if not platform_efa:
        raise RuntimeError(f"窗口绑定账号缺少 EFA(2FA Secret)：window_pk={window_pk}")

    return {
        "platform_url": platform_url,
        "platform_username": platform_username,
        "platform_password": platform_password,
        "platform_efa": platform_efa,
    }


async def resolve_window_platform_login_creds_optional_efa(db: Database, *, window_pk: int) -> Dict[str, Optional[str]]:
    """与 _resolve_window_platform_credentials 相同来源，但 platform_efa 可选（无则跳过 TOTP 自动填）。"""
    win = await db.get_window(int(window_pk))
    if not win:
        raise RuntimeError(f"窗口不存在：window_pk={window_pk}")

    platform_url = _s(getattr(win, "platform_url", None)) or None
    platform_username = _s(getattr(win, "platform_account", None)) or None
    platform_password: Optional[str] = None
    platform_efa: Optional[str] = None

    space_pk = int(getattr(win, "space_pk", 0) or 0)
    if space_pk <= 0:
        raise RuntimeError(f"窗口缺少 space_pk：window_pk={window_pk}")

    account_id = int(getattr(win, "platform_account_id", 0) or 0)
    if account_id > 0:
        acc = await db.get_platform_account(space_pk=space_pk, account_id=account_id)
        if acc:
            platform_url = _s(getattr(acc, "platform_url", None)) or platform_url
            platform_username = _s(getattr(acc, "platform_username", None)) or platform_username
            platform_password = _s(getattr(acc, "platform_password", None)) or None
            platform_efa = _s(getattr(acc, "platform_efa", None)) or None
    else:
        cands = await db.list_platform_accounts(space_pk)
        target_user = (platform_username or "").strip().lower()
        target_url = (platform_url or "").strip().lower()
        for acc in cands:
            u = _s(getattr(acc, "platform_username", None)).lower()
            u_ok = bool(target_user) and u == target_user
            if not u_ok:
                continue
            a_url = _s(getattr(acc, "platform_url", None)).lower()
            if target_url and a_url and a_url != target_url:
                continue
            platform_url = _s(getattr(acc, "platform_url", None)) or platform_url
            platform_password = _s(getattr(acc, "platform_password", None)) or None
            platform_efa = _s(getattr(acc, "platform_efa", None)) or None
            break

    if not platform_url:
        raise RuntimeError(f"窗口未绑定平台网址：window_pk={window_pk}")
    if not platform_username:
        raise RuntimeError(f"窗口未绑定平台账号(邮箱)：window_pk={window_pk}")
    if not platform_password:
        raise RuntimeError(f"窗口绑定账号缺少密码：window_pk={window_pk}")

    return {
        "platform_url": platform_url,
        "platform_username": platform_username,
        "platform_password": platform_password,
        "platform_efa": platform_efa if (platform_efa or "").strip() else None,
    }


async def _close_other_pages(context: Any, keep_page: Any) -> int:
    """关闭 context 中除 keep_page 以外的所有页面，返回关闭数量。"""
    closed = 0
    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        pages = []
    for p in pages:
        if p is keep_page:
            continue
        try:
            is_closed = bool(getattr(p, "is_closed", lambda: False)())
        except Exception:
            is_closed = True
        if is_closed:
            continue
        try:
            await p.close()
            closed += 1
        except Exception:
            pass
    return closed


async def _bring_sora_drafts_to_front(
    ctx: Any, *, refresh_target: bool = True, headless: bool = False, pure_mode: bool = True
) -> Any:
    """将 `https://chatgpt.com/` 页面置前，不关闭其它页面。"""
    drafts_url = "https://chatgpt.com/"
    drafts_host = "chatgpt.com"

    def _is_page_closed(p: Any) -> bool:
        try:
            return bool(getattr(p, "is_closed", lambda: False)())
        except Exception:
            return False

    def _safe_page_url(p: Any) -> str:
        try:
            return str(getattr(p, "url", "") or "").strip()
        except Exception:
            return ""

    async def _page_has_transient_error(page: Any) -> bool:
        """检测常见临时错误页文案，用于触发刷新自愈。"""
        markers = [
            "something went wrong. please try again in a few minutes.",
            "something went wrong",
            "an error occurred",
            "发生错误",
            "出错了",
        ]
        try:
            html = str(await page.content() or "").lower()
        except Exception:
            return False
        return any(m in html for m in markers)

    async def _maybe_click_login_button_if_prompted(page: Any) -> tuple[bool, bool]:
        """若页面出现登录入口则尝试点击。返回 (clicked, has_login_button)。"""
        labels = ["Log in", "Sign in", "登录", "登入"]
        scopes = [page]
        try:
            scopes.extend(list(getattr(page, "frames", []) or []))
        except Exception:
            pass

        has_login_button = False
        for sc in scopes:
            for text in labels:
                try:
                    loc = sc.locator('button, a, [role="button"], [role="link"]').filter(has_text=text)
                    if (await loc.count()) > 0:
                        has_login_button = True
                        break
                except Exception:
                    continue
            if has_login_button:
                break

        if not has_login_button:
            return False, False

        for sc in scopes:
            for text in labels:
                if hasattr(sc, "get_by_role"):
                    try:
                        await sc.get_by_role("button", name=text).first.click(timeout=2500)
                        return True, True
                    except Exception:
                        pass
                    try:
                        await sc.get_by_role("link", name=text).first.click(timeout=2500)
                        return True, True
                    except Exception:
                        pass
                try:
                    loc = sc.locator('button, a, [role="button"], [role="link"]').filter(has_text=text)
                    await loc.first.click(timeout=2500)
                    return True, True
                except Exception:
                    continue
        return False, True

    async def _is_cloudflare_page(page: Any, *, deep: bool = False) -> bool:
        """判断当前页面是否为 Cloudflare 拦截/挑战页。"""
        if page is None:
            return False
        try:
            u = str(getattr(page, "url", "") or "").strip()
        except Exception:
            u = ""
        ul = u.lower()
        if "/cdn-cgi/" in ul or "challenges.cloudflare.com" in ul:
            return True
        try:
            title = await page.title()
        except Exception:
            title = ""
        tl = (title or "").strip().lower()
        if "just a moment" in tl or "attention required" in tl:
            return True
        if not deep:
            return False
        try:
            html = await page.content()
        except Exception:
            html = ""
        hl = (html or "").lower()
        if "cloudflare" in hl and ("just a moment" in hl or "/cdn-cgi/" in hl or "cf-ray" in hl):
            return True
        if ("turnstile" in hl or "cf-challenge" in hl) and ("/cdn-cgi/" in hl or "cloudflare" in hl):
            return True
        return False

    cf_panel_seq = 0
    cf_panel_entries: list[Dict[str, str]] = []

    async def _push_cf_progress(page: Any, text: str, *, level: str = "info") -> None:
        """向页面注入 Cloudflare 自愈进度面板。"""
        nonlocal cf_panel_seq, cf_panel_entries
        if page is None:
            return
        cf_panel_seq += 1
        cf_panel_entries.append(
            {
                "idx": str(cf_panel_seq),
                "ts": time.strftime("%H:%M:%S"),
                "level": str(level or "info"),
                "text": str(text or ""),
            }
        )
        if len(cf_panel_entries) > 80:
            cf_panel_entries = cf_panel_entries[-80:]
        payload = {
            "title": "Sora Plus Cloudflare 自愈",
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "entries": list(cf_panel_entries),
        }
        try:
            await page.evaluate(_build_cf_progress_panel_script(), payload)
        except Exception:
            pass

    async def _try_cloudflare_click(page: Any) -> bool:
        """尝试点击 Cloudflare Turnstile checkbox。"""
        if page is None:
            return False
        cf_frame = None
        try:
            frames = list(getattr(page, "frames", []) or [])
        except Exception:
            frames = []
        for f in frames:
            try:
                fu = str(getattr(f, "url", "") or "")
            except Exception:
                fu = ""
            if "challenges.cloudflare.com" in fu or "/cdn-cgi/" in fu:
                cf_frame = f
                break
        if cf_frame is None:
            return False

        # 策略1：frame.locator 直接点击
        try:
            loc = cf_frame.locator("input[type='checkbox']")
            if (await loc.count()) > 0:
                await loc.first.click(force=True, timeout=1500)
                return True
        except Exception:
            pass

        # 策略2：坐标法点击（可穿 closed shadow-root）
        try:
            iframe_handle = await cf_frame.frame_element()
            box = await iframe_handle.bounding_box()
            if not (box and box.get("width", 0) > 0 and box.get("height", 0) > 0):
                return False
            target_x = box["x"] + 26.0
            target_y = box["y"] + box["height"] / 2.0
            start_x = target_x + random.uniform(-80, 120)
            start_y = target_y + random.uniform(-50, 50)
            await page.mouse.move(start_x, start_y)
            await asyncio.sleep(random.uniform(0.08, 0.20))
            await page.mouse.move(target_x, target_y, steps=random.randint(6, 12))
            await asyncio.sleep(random.uniform(0.04, 0.12))
            await page.mouse.click(target_x, target_y)
            return True
        except Exception:
            return False

    async def _wait_cloudflare_auto_pass(
        page: Any,
        *,
        max_wait_seconds: float,
        max_success_clicks: int = 3,
        on_progress: Optional[Any] = None,
    ) -> bool:
        """等待 Cloudflare 自动放行；返回 True 表示超时后仍疑似 Cloudflare。"""
        try:
            deadline = time.time() + max(0.0, float(max_wait_seconds))
        except Exception:
            deadline = time.time() + 10.0
        await asyncio.sleep(5.0)
        try:
            max_click_success = max(0, int(max_success_clicks))
        except Exception:
            max_click_success = 2
        poll_after_click = 6.0
        poll_idle = 1.0
        if callable(on_progress):
            try:
                await on_progress("检测到 Cloudflare，开始等待自动放行并尝试点击 checkbox", "warn")
            except Exception:
                pass
        reported_click_fail = False
        consecutive_not_cf = 0
        clicked_success_count = 0
        while time.time() < deadline:
            try:
                is_closed = bool(getattr(page, "is_closed", lambda: False)())
            except Exception:
                is_closed = False
            if is_closed:
                return True

            try:
                still_cf = await _is_cloudflare_page(page, deep=True)
            except Exception:
                still_cf = True
            if not still_cf:
                consecutive_not_cf += 1
                if consecutive_not_cf >= 2:
                    if callable(on_progress):
                        try:
                            await on_progress("Cloudflare 已放行", "ok")
                        except Exception:
                            pass
                    return False
                if callable(on_progress):
                    try:
                        await on_progress("Cloudflare 疑似已放行，进行二次确认", "info")
                    except Exception:
                        pass
                remain = deadline - time.time()
                if remain <= 0:
                    break
                try:
                    await asyncio.sleep(min(poll_idle, max(0.1, remain)))
                except Exception:
                    break
                continue
            consecutive_not_cf = 0

            clicked = False
            try:
                clicked = await _try_cloudflare_click(page)
            except Exception:
                clicked = False
            if clicked:
                clicked_success_count += 1
                if callable(on_progress):
                    try:
                        await on_progress(f"checkbox 已点击（第 {clicked_success_count} 次），等待 Cloudflare 验证结果", "info")
                    except Exception:
                        pass
                if max_click_success > 0 and clicked_success_count >= max_click_success:
                    if callable(on_progress):
                        try:
                            await on_progress(f"checkbox 成功点击已达上限（{max_click_success} 次），提前结束等待", "warn")
                        except Exception:
                            pass
                    try:
                        await asyncio.sleep(1.0)
                    except Exception:
                        pass
                    try:
                        still_cf_after_limit = await _is_cloudflare_page(page, deep=True)
                    except Exception:
                        still_cf_after_limit = True
                    if not still_cf_after_limit:
                        if callable(on_progress):
                            try:
                                await on_progress("Cloudflare 已放行", "ok")
                            except Exception:
                                pass
                        return False
                    return True
            elif not reported_click_fail:
                if callable(on_progress):
                    try:
                        await on_progress("尚未成功点击 checkbox，继续重试", "warn")
                    except Exception:
                        pass
                reported_click_fail = True

            remain = deadline - time.time()
            if remain <= 0:
                break
            sleep_sec = poll_after_click if clicked else poll_idle
            try:
                await asyncio.sleep(min(sleep_sec, max(0.1, remain)))
            except Exception:
                break
        if callable(on_progress):
            try:
                await on_progress("等待超时，Cloudflare 仍存在", "warn")
            except Exception:
                pass
        return True

    async def _reopen_window_and_restore_drafts() -> None:
        """对齐 task_executor：先仅重启窗口，再延迟重连 CDP。"""
        try:
            await ctx.close()
        except Exception:
            pass
        try:
            await asyncio.sleep(0.5)
        except Exception:
            pass

        # browser_open + 稳定等待受并发信号量保护，按 base_url 分服务器限流
        async with acquire_browser_open_slot(ctx.base_url):
            try:
                await ctx.fp_client.browser_open(
                    vendor=ctx.vendor,
                    base_url=ctx.base_url,
                    access_key=ctx.access_key,
                    space_id=ctx.space_id,
                    window_key=ctx.window_key,
                    args=[],
                    force_open=False,
                    headless=headless,
                    pure_mode=pure_mode,
                )
            except Exception:
                pass

            # 清空旧句柄，避免误复用
            try:
                ctx.browser = None
                ctx.context = None
                ctx.page = None
                ctx.cdp_endpoint = None
            except Exception:
                pass

            # 给 Cloudflare 验证窗口留出处理时间
            try:
                await asyncio.sleep(2.0)
            except Exception:
                pass

        # 给 Cloudflare 验证窗口留出处理时间
        try:
            await asyncio.sleep(18.0)
        except Exception:
            pass

        # 仅重连 browser/context，不强制探测/创建 page
        try:
            await ctx.ensure_open(
                args=[], force_open=False, headless=headless, require_page=False, pure_mode=pure_mode
            )
        except Exception:
            pass

    context = getattr(ctx, "context", None)
    browser = getattr(ctx, "browser", None)
    try:
        ctxs = list(getattr(browser, "contexts", []) or [])
    except Exception:
        ctxs = []
    if context is not None and context not in ctxs:
        ctxs.insert(0, context)

    if not ctxs:
        return getattr(ctx, "page", None)

    open_pages: list[tuple[Any, Any, str]] = []
    for c in ctxs:
        try:
            pages = list(getattr(c, "pages", []) or [])
        except Exception:
            pages = []
        for p in pages:
            if _is_page_closed(p):
                continue
            open_pages.append((c, p, _safe_page_url(p)))

    drafts_page = None
    cur_page = getattr(ctx, "page", None)
    if cur_page is not None and not _is_page_closed(cur_page):
        if _safe_page_url(cur_page).startswith(drafts_url):
            drafts_page = cur_page

    if drafts_page is None:
        for _c, p, u in open_pages:
            if u.startswith(drafts_url):
                drafts_page = p
                break

    if drafts_page is None:
        preferred_ctx = context or ctxs[0]
        try:
            drafts_page = await preferred_ctx.new_page()
        except Exception:
            return cur_page
        try:
            await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
        except Exception:
            pass

    try:
        ctx.page = drafts_page
    except Exception:
        pass
    try:
        page_ctx_getter = getattr(drafts_page, "context", None)
        page_ctx = page_ctx_getter() if callable(page_ctx_getter) else page_ctx_getter
        if page_ctx is not None:
            ctx.context = page_ctx
    except Exception:
        pass
    try:
        await drafts_page.bring_to_front()
    except Exception:
        pass

    if refresh_target:
        try:
            await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
        except Exception:
            pass
        try:
            await drafts_page.evaluate("() => { try { window.focus(); } catch(e) {} }")
        except Exception:
            pass
        await asyncio.sleep(1.0)

    # 自愈逻辑：异常页刷新 + 登录入口自动点击（最多重试一次）
    try:
        if await _page_has_transient_error(drafts_page):
            try:
                await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
            except Exception:
                pass
            await asyncio.sleep(2.0)

        clicked, has_login_button = await _maybe_click_login_button_if_prompted(drafts_page)
        if clicked:
            await asyncio.sleep(2.0)
            try:
                await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
            except Exception:
                pass
        elif has_login_button:
            try:
                await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
            except Exception:
                pass
            clicked2, _ = await _maybe_click_login_button_if_prompted(drafts_page)
            if clicked2:
                await asyncio.sleep(2.0)
    except Exception:
        # 自愈失败不阻塞主流程
        pass

    # Cloudflare 自愈：等待自动放行；若持续拦截则重连窗口并恢复目标页。
    try:
        maybe_cf = await _is_cloudflare_page(drafts_page, deep=False)
        if maybe_cf:
            try:
                await drafts_page.goto(drafts_url, wait_until="domcontentloaded")
            except Exception:
                pass
            await asyncio.sleep(3.0)
            await _push_cf_progress(drafts_page, "页面疑似 Cloudflare，进入自愈流程", level="warn")
            still_cf_after_wait = await _wait_cloudflare_auto_pass(
                drafts_page,
                max_wait_seconds=45.0,
                max_success_clicks=3,
                on_progress=lambda text, lv="info": _push_cf_progress(drafts_page, text, level=lv),
            )
            if still_cf_after_wait and await _is_cloudflare_page(drafts_page, deep=True):
                await _push_cf_progress(drafts_page, "Cloudflare 持续存在，准备重启窗口", level="warn")
                await _reopen_window_and_restore_drafts()
    except Exception:
        pass

    return drafts_page


def _is_already_logged_in(current_url: str) -> bool:
    """判断当前页面是否已跳转到 Google 账户页（说明已处于登录态）。"""
    u = str(current_url or "").strip().lower()
    return u.startswith("https://myaccount.google.com")


async def _wait_for_login_redirect(page: Any, *, timeout_ms: int) -> None:
    """等待 Google 登录完成并跳转离开登录页（accounts.google.com）。

    点击 2FA Next 后 Google 需要几秒处理，如果立刻 goto 新页面会打断 session 写入，
    导致到达目标页后仍未登录。这里轮询等待 URL 不再是登录页即可。
    """
    login_prefixes = (
        "https://accounts.google.com/v3/signin",
        "https://accounts.google.com/signin",
        "https://accounts.google.com/servicelog",
        "https://accounts.google.com/checkmyactivity",
    )
    deadline = time.time() + max(5, timeout_ms / 1000)
    while time.time() < deadline:
        try:
            cur = str(page.url or "").strip().lower()
        except Exception:
            cur = ""
        if cur and not cur.startswith("https://accounts.google.com"):
            return
        if cur and not any(cur.startswith(p) for p in login_prefixes):
            return
        await asyncio.sleep(1)
    # 超时也不报错——可能是慢网络，后续 goto 如果失败会自然抛异常


async def google_accounts_autofill_login_steps(
    page: Any,
    *,
    platform_username: str,
    platform_password: str,
    platform_efa: Optional[str],
    timeout_ms: int,
    progress_cb: Optional[ProgressCB] = None,
) -> None:
    """在当前已打开 accounts.google.com 页面上，按可见控件依次自动填邮箱/密码/TOTP（与开号 _do_login_flow 同源策略）。

    platform_efa 为空时：若出现身份验证器输入框则记录提示并中止自动填表（短信/邮箱验证码需人工）。
    """
    per_step = int(max(800, min(12_000, timeout_ms // 6)))

    async def _emit(msg: str, **extra: Any) -> None:
        if progress_cb is None:
            return
        await progress_cb(0, {"stage": "google_autofill", "msg": msg, **extra})

    deadline = time.time() + max(20.0, timeout_ms / 1000.0)
    rounds = 0
    max_rounds = 24

    while time.time() < deadline and rounds < max_rounds:
        rounds += 1
        try:
            cur = str(page.url or "").strip().lower()
        except Exception:
            cur = ""
        if cur and "accounts.google.com" not in cur:
            await _emit(f"已离开 Google 登录域：{cur[:120]}")
            return

        progressed = False

        try:
            pw = page.locator("input[type='password']").first
            if await pw.is_visible(timeout=per_step):
                await pw.fill(platform_password, timeout=per_step)
                await _emit("已自动填写密码")
                await _click_google_signin_next(page, timeout_ms=min(15_000, timeout_ms))
                progressed = True
                await asyncio.sleep(1.0)
        except Exception:
            pass

        if progressed:
            continue

        for sel in ("#identifierId", 'input[name="identifier"]', "input[type='email']"):
            try:
                em = page.locator(sel).first
                if await em.is_visible(timeout=per_step):
                    await em.fill(platform_username, timeout=per_step)
                    await _emit("已自动填写邮箱/账号", selector=sel)
                    await _click_google_signin_next(page, timeout_ms=min(15_000, timeout_ms))
                    progressed = True
                    await asyncio.sleep(1.0)
                    break
            except Exception:
                continue

        if progressed:
            continue

        otp_selectors = (
            'input[name="totpPin"]',
            "#totpPin",
            'input[autocomplete="one-time-code"]',
            "input[type='tel']",
            'input[id="idvPin"]',
            'input[name="pin"]',
        )
        for osp in otp_selectors:
            try:
                loc = page.locator(osp).first
                if not await loc.is_visible(timeout=per_step // 2):
                    continue
                secret = (platform_efa or "").strip()
                if not secret:
                    await _emit("检测到验证码输入框，但未配置 EFA(2FA Secret)，请手动完成验证", selector=osp)
                    return
                code = _generate_totp_code(secret)
                await loc.fill(code, timeout=per_step)
                await _emit("已自动填写身份验证器验证码 (TOTP)", selector=osp)
                await _click_google_signin_next(page, timeout_ms=min(15_000, timeout_ms))
                progressed = True
                await asyncio.sleep(1.2)
                break
            except Exception:
                continue

        if progressed:
            continue

        await asyncio.sleep(0.55)

    await _wait_for_login_redirect(page, timeout_ms=min(35_000, timeout_ms))
    await _emit("Google 登录步骤轮询结束，已等待跳转")


async def _do_login_flow(
    page: Any,
    *,
    platform_username: str,
    platform_password: str,
    platform_efa: str,
    timeout_ms: int,
    progress_cb: ProgressCB,
) -> str:
    """完整的登录流程：邮箱 -> Next -> 密码 -> Next -> 2FA -> Next。

    返回本次使用的 totp_code（供后续"换新 code"逻辑参考）。
    """
    email_input = page.locator('input[type="email"]').first
    await email_input.wait_for(state="visible", timeout=timeout_ms)
    await email_input.fill(platform_username, timeout=timeout_ms)
    await progress_cb(20, {"stage": "email_filled"})

    clicked_email_next = await _click_next_button(page, timeout_ms=timeout_ms)
    await progress_cb(30, {"stage": "email_next_clicked", "selector": clicked_email_next})

    password_input = page.locator('input[type="password"]').first
    await password_input.wait_for(state="visible", timeout=timeout_ms)
    await password_input.fill(platform_password, timeout=timeout_ms)
    await progress_cb(40, {"stage": "password_filled"})

    clicked_password_next = await _click_next_button(page, timeout_ms=timeout_ms)
    await progress_cb(45, {"stage": "password_next_clicked", "selector": clicked_password_next})

    tel_input = page.locator('input[type="tel"]').first
    await tel_input.wait_for(state="visible", timeout=timeout_ms)
    first_totp_code = _generate_totp_code(platform_efa)
    await tel_input.fill(first_totp_code, timeout=timeout_ms)
    await progress_cb(50, {"stage": "totp_filled"})

    clicked_totp_next = await _click_next_button(page, timeout_ms=timeout_ms)
    await progress_cb(52, {"stage": "totp_next_clicked", "selector": clicked_totp_next})

    await _wait_for_login_redirect(page, timeout_ms=timeout_ms)
    await progress_cb(55, {"stage": "login_redirect_done", "url": str(page.url or "")})

    return first_totp_code


CHATGPT_URL = "https://chatgpt.com/"


async def _do_chatgpt_google_login(
    page: Any,
    *,
    platform_username: str,
    platform_efa: str,
    previous_totp_code: str,
    timeout_ms: int,
    progress_cb: ProgressCB,
) -> Dict[str, Any]:
    # 你指定的节点：等 3 秒后预先生成 2FA code，并写入系统剪贴板，方便手动 Ctrl+V。
    await page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    await asyncio.sleep(1)
    await progress_cb(58, {"stage": "chatgpt_page_loaded", "url": str(page.url or "")})

    prefetched_totp_code = await _generate_fresh_totp_code(platform_efa, previous_totp_code)
    copied = _copy_to_system_clipboard(prefetched_totp_code)
    await progress_cb(
        57,
        {
            "stage": "prefetch_2fa_code_ready",
            "copied_to_clipboard": copied,
            "prefetched_totp_code": prefetched_totp_code,
        },
    )
    return {"prefetched_totp_code": prefetched_totp_code, "copied_to_clipboard": copied}


def _build_copy_panel_script() -> str:
    """返回用于注入页面悬浮复制面板的 JS。"""
    return r"""
(payload) => {
  try {
    const PANEL_ID = "__sora_plus_copy_panel__";
    const STYLE_ID = "__sora_plus_copy_panel_style__";
    const safe = (v) => (v === null || v === undefined) ? "" : String(v);

    const state = {
      title: safe(payload && payload.title),
      rows: Array.isArray(payload && payload.rows) ? payload.rows.map((x) => ({
        key: safe(x && x.key),
        label: safe(x && x.label),
        value: safe(x && x.value),
      })) : [],
      updatedAt: safe(payload && payload.updatedAt),
      totpSecret: safe(payload && payload.totpSecret),
    };
    window.__SORA_PLUS_COPY_PANEL_STATE__ = state;

    if (!document.getElementById(STYLE_ID)) {
      const style = document.createElement("style");
      style.id = STYLE_ID;
      style.textContent = `
#${PANEL_ID}{
position:fixed;top:16px;right:16px;z-index:2147483647;width:380px;
background:rgba(17,24,39,.96);color:#e5e7eb;border:1px solid rgba(148,163,184,.35);
border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.35);font-size:12px;font-family:Arial,sans-serif;
}
#${PANEL_ID}.min{width:180px}
#${PANEL_ID} .hdr{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border-bottom:1px solid rgba(148,163,184,.25)}
#${PANEL_ID} .ttl{font-weight:700;color:#f8fafc}
#${PANEL_ID} .btn{background:#334155;color:#f8fafc;border:0;border-radius:8px;padding:4px 8px;cursor:pointer}
#${PANEL_ID} .bd{padding:10px 12px;max-height:55vh;overflow:auto}
#${PANEL_ID} .row{display:grid;grid-template-columns:86px 1fr auto;gap:6px;align-items:start;margin-bottom:8px}
#${PANEL_ID} .k{color:#94a3b8;line-height:1.5}
#${PANEL_ID} .v{white-space:pre-wrap;word-break:break-word;background:rgba(15,23,42,.7);padding:6px 8px;border-radius:8px}
#${PANEL_ID} .ops{display:flex;align-items:center;gap:6px}
#${PANEL_ID} .cp{background:#2563eb}
#${PANEL_ID} .gen{background:#16a34a}
#${PANEL_ID} .fts{padding:8px 12px;border-top:1px solid rgba(148,163,184,.2);color:#94a3b8}
      `.trim();
      document.documentElement.appendChild(style);
    }

    const ensurePanel = () => {
      let panel = document.getElementById(PANEL_ID);
      if (!panel) {
        panel = document.createElement("div");
        panel.id = PANEL_ID;
        panel.innerHTML = `
          <div class="hdr">
            <span class="ttl"></span>
            <button class="btn tg" type="button">收起</button>
          </div>
          <div class="bd"></div>
          <div class="fts"></div>
        `;
        document.documentElement.appendChild(panel);
      }
      return panel;
    };

    const copyText = async (text) => {
      const v = safe(text);
      if (!v) return false;
      try {
        await navigator.clipboard.writeText(v);
        return true;
      } catch (_e1) {
        try {
          const ta = document.createElement("textarea");
          ta.value = v;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.focus();
          ta.select();
          const ok = document.execCommand("copy");
          document.body.removeChild(ta);
          return !!ok;
        } catch (_e2) {
          return false;
        }
      }
    };

    const decodeBase32 = (input) => {
      const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
      const clean = safe(input).replace(/[\s-]/g, "").toUpperCase();
      if (!clean) return new Uint8Array(0);
      let bits = "";
      for (const ch of clean) {
        const val = alphabet.indexOf(ch);
        if (val < 0) continue;
        bits += val.toString(2).padStart(5, "0");
      }
      const out = [];
      for (let i = 0; i + 8 <= bits.length; i += 8) {
        out.push(parseInt(bits.slice(i, i + 8), 2));
      }
      return new Uint8Array(out);
    };

    const generateTotpCode = async (secret) => {
      const keyBytes = decodeBase32(secret);
      if (!keyBytes.length || !window.crypto || !window.crypto.subtle) {
        return "";
      }
      const period = 30;
      const counter = Math.floor(Date.now() / 1000 / period);
      const counterBuffer = new ArrayBuffer(8);
      const view = new DataView(counterBuffer);
      view.setUint32(0, Math.floor(counter / 0x100000000), false);
      view.setUint32(4, counter >>> 0, false);
      const cryptoKey = await window.crypto.subtle.importKey(
        "raw",
        keyBytes,
        { name: "HMAC", hash: "SHA-1" },
        false,
        ["sign"]
      );
      const sig = new Uint8Array(await window.crypto.subtle.sign("HMAC", cryptoKey, counterBuffer));
      const offset = sig[sig.length - 1] & 0x0f;
      const binCode = ((sig[offset] & 0x7f) << 24) | ((sig[offset + 1] & 0xff) << 16) | ((sig[offset + 2] & 0xff) << 8) | (sig[offset + 3] & 0xff);
      return String(binCode % 1000000).padStart(6, "0");
    };

    const render = () => {
      const panel = ensurePanel();
      const ttl = panel.querySelector(".ttl");
      const bd = panel.querySelector(".bd");
      const fts = panel.querySelector(".fts");
      const tg = panel.querySelector(".tg");
      if (!ttl || !bd || !fts || !tg) return;

      ttl.textContent = state.title || "Sora Plus 数据面板";
      bd.innerHTML = "";
      for (const row of state.rows) {
        const wrap = document.createElement("div");
        wrap.className = "row";
        const key = document.createElement("div");
        key.className = "k";
        key.textContent = row.label || row.key || "-";
        const val = document.createElement("div");
        val.className = "v";
        val.textContent = row.value || "";
        const ops = document.createElement("div");
        ops.className = "ops";
        if (row.key === "prefetched_totp_code") {
          const genBtn = document.createElement("button");
          genBtn.className = "btn gen";
          genBtn.type = "button";
          genBtn.textContent = "生成";
          genBtn.onclick = async () => {
            try {
              const code = await generateTotpCode(state.totpSecret || "");
              if (!code) {
                genBtn.textContent = "失败";
                setTimeout(() => { genBtn.textContent = "生成"; }, 1200);
                return;
              }
              row.value = code;
              val.textContent = code;
              genBtn.textContent = "已生成";
              setTimeout(() => { genBtn.textContent = "生成"; }, 1200);
            } catch (_e3) {
              genBtn.textContent = "失败";
              setTimeout(() => { genBtn.textContent = "生成"; }, 1200);
            }
          };
          ops.appendChild(genBtn);
        }
        const btn = document.createElement("button");
        btn.className = "btn cp";
        btn.type = "button";
        btn.textContent = "复制";
        btn.onclick = async () => {
          const ok = await copyText(row.value || "");
          btn.textContent = ok ? "已复制" : "失败";
          setTimeout(() => { btn.textContent = "复制"; }, 1200);
        };
        ops.appendChild(btn);
        wrap.appendChild(key);
        wrap.appendChild(val);
        wrap.appendChild(ops);
        bd.appendChild(wrap);
      }

      fts.textContent = state.updatedAt ? `更新时间: ${state.updatedAt}` : "";
      tg.onclick = () => {
        const min = panel.classList.toggle("min");
        bd.style.display = min ? "none" : "block";
        fts.style.display = min ? "none" : "block";
        tg.textContent = min ? "展开" : "收起";
      };
    };

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", render, { once: true });
    } else {
      render();
    }
  } catch (_e) {
    // 忽略注入失败，避免影响主流程。
  }
}
"""


def _build_cf_progress_panel_script() -> str:
    """返回 Cloudflare 自愈进度面板脚本（与复制面板独立）。"""
    return r"""
(payload) => {
  try {
    const PANEL_ID = "__sora_plus_cf_progress_panel__";
    const STYLE_ID = "__sora_plus_cf_progress_panel_style__";
    const safe = (v) => (v === null || v === undefined) ? "" : String(v);
    const data = {
      title: safe(payload && payload.title),
      updatedAt: safe(payload && payload.updatedAt),
      entries: Array.isArray(payload && payload.entries) ? payload.entries.map((x) => ({
        idx: safe(x && x.idx),
        ts: safe(x && x.ts),
        level: safe(x && x.level),
        text: safe(x && x.text),
      })) : [],
    };

    if (!document.getElementById(STYLE_ID)) {
      const style = document.createElement("style");
      style.id = STYLE_ID;
      style.textContent = `
#${PANEL_ID}{
position:fixed;top:16px;left:16px;z-index:2147483646;width:420px;
background:rgba(15,23,42,.95);color:#e5e7eb;border:1px solid rgba(148,163,184,.35);
border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.35);font-size:12px;font-family:Arial,sans-serif;
}
#${PANEL_ID}.min{width:220px}
#${PANEL_ID} .hdr{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border-bottom:1px solid rgba(148,163,184,.25)}
#${PANEL_ID} .ttl{font-weight:700;color:#f8fafc}
#${PANEL_ID} .btn{background:#334155;color:#f8fafc;border:0;border-radius:8px;padding:4px 8px;cursor:pointer}
#${PANEL_ID} .bd{padding:10px 12px;max-height:45vh;overflow:auto}
#${PANEL_ID} .it{padding:7px 8px;margin-bottom:6px;border-radius:8px;background:rgba(30,41,59,.75)}
#${PANEL_ID} .it.ok{border-left:3px solid #22c55e}
#${PANEL_ID} .it.warn{border-left:3px solid #f59e0b}
#${PANEL_ID} .it.err{border-left:3px solid #ef4444}
#${PANEL_ID} .meta{font-size:11px;color:#94a3b8;margin-bottom:3px}
#${PANEL_ID} .txt{white-space:pre-wrap;word-break:break-word;line-height:1.4}
#${PANEL_ID} .fts{padding:8px 12px;border-top:1px solid rgba(148,163,184,.2);color:#94a3b8}
      `.trim();
      document.documentElement.appendChild(style);
    }

    let panel = document.getElementById(PANEL_ID);
    if (!panel) {
      panel = document.createElement("div");
      panel.id = PANEL_ID;
      panel.innerHTML = `
        <div class="hdr">
          <span class="ttl"></span>
          <button class="btn tg" type="button">收起</button>
        </div>
        <div class="bd"></div>
        <div class="fts"></div>
      `;
      document.documentElement.appendChild(panel);
    }

    const ttl = panel.querySelector(".ttl");
    const bd = panel.querySelector(".bd");
    const fts = panel.querySelector(".fts");
    const tg = panel.querySelector(".tg");
    if (!ttl || !bd || !fts || !tg) return;

    ttl.textContent = data.title || "Sora Plus Cloudflare 自愈";
    bd.innerHTML = "";
    for (const e of data.entries) {
      const item = document.createElement("div");
      const lv = (e.level || "info").toLowerCase();
      let cls = "it";
      if (lv === "ok" || lv === "success") cls += " ok";
      else if (lv === "warn" || lv === "warning") cls += " warn";
      else if (lv === "err" || lv === "error" || lv === "fail") cls += " err";
      item.className = cls;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `#${safe(e.idx)}  ${safe(e.ts)}  [${lv || "info"}]`;
      const txt = document.createElement("div");
      txt.className = "txt";
      txt.textContent = safe(e.text);
      item.appendChild(meta);
      item.appendChild(txt);
      bd.appendChild(item);
    }
    fts.textContent = data.updatedAt ? `更新时间: ${data.updatedAt}` : "";
    try { bd.scrollTop = bd.scrollHeight; } catch (_e1) {}

    tg.onclick = () => {
      const min = panel.classList.toggle("min");
      bd.style.display = min ? "none" : "block";
      fts.style.display = min ? "none" : "block";
      tg.textContent = min ? "展开" : "收起";
    };
  } catch (_e) {
    // 忽略注入失败，避免影响主流程。
  }
}
"""


async def _show_sticky_copy_panel(
    context: Any,
    page: Any,
    *,
    panel_data: Dict[str, Any],
    progress_cb: ProgressCB,
) -> None:
    """在当前页和后续新页面注入常驻复制面板。"""
    script = _build_copy_panel_script()
    try:
        await context.add_init_script(script=script, arg=panel_data)
    except Exception:
        # 某些场景 add_init_script 可能受限，降级为仅当前页注入。
        pass
    pages = []
    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        pages = []
    if page not in pages:
        pages.append(page)
    shown_count = 0
    failed_count = 0
    for p in pages:
        try:
            await p.evaluate(script, panel_data)
            shown_count += 1
        except Exception:
            failed_count += 1
    try:
        await progress_cb(99, {"stage": "copy_panel_shown", "shown_count": shown_count, "failed_count": failed_count})
    except Exception:
        await progress_cb(99, {"stage": "copy_panel_show_failed"})


def _stringify_panel_value(v: Any) -> str:
    """将任意 payload 值转成适合面板展示的字符串。"""
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    try:
        # 非基础类型使用多行 JSON，便于保留并展示结构化内容的换行。
        return json.dumps(v, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(v)


def _build_payload_rows(payload: Dict[str, Any]) -> list[Dict[str, str]]:
    """把 payload 的所有顶层字段转换为面板行。"""
    rows: list[Dict[str, str]] = []
    if not isinstance(payload, dict):
        return rows
    for k in sorted(payload.keys(), key=lambda x: str(x)):
        key = str(k)
        rows.append(
            {
                "key": f"payload.{key}",
                "label": f"Payload.{key}",
                "value": _stringify_panel_value(payload.get(k)),
            }
        )
    return rows


async def _navigate_to_password_page(page: Any, *, timeout_ms: int, progress_cb: ProgressCB) -> None:
    """尝试跳转到 Google 密码修改页；如果 goto 后未到达，则在页面中查找并点击相关入口。"""
    try:
        await page.goto(GOOGLE_PASSWORD_PAGE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    await asyncio.sleep(2)

    cur = str(page.url or "").strip().lower()
    if "signinoptions/password" in cur:
        return

    await progress_cb(62, {"stage": "goto_password_page_fallback", "current_url": cur})

    btn_selectors = [
        'a:has-text("Google password")',
        'button:has-text("Google password")',
        'div[role="link"]:has-text("Google password")',
        'a:has-text("Password")',
        'button:has-text("Password")',
        'a:has-text("Google 密码")',
        'a:has-text("密码")',
    ]
    per_try = int(max(1000, min(5000, timeout_ms // 4)))
    for sel in btn_selectors:
        loc = page.locator(sel).first
        try:
            await loc.wait_for(state="visible", timeout=per_try)
            await loc.click(timeout=per_try)
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            return
        except Exception:
            continue

    await page.goto(GOOGLE_PASSWORD_PAGE_URL, wait_until="domcontentloaded", timeout=timeout_ms)


async def _do_change_password_flow(
    page: Any,
    *,
    platform_efa: str,
    previous_totp_code: str,
    timeout_ms: int,
    progress_cb: ProgressCB,
) -> None:
    """修改密码流程：跳转安全页 -> 2FA -> 填两次新密码 -> Change password。"""
    await _navigate_to_password_page(page, timeout_ms=timeout_ms, progress_cb=progress_cb)
    await progress_cb(65, {"stage": "security_password_page_loaded", "url": str(page.url or "")})

    second_tel_input = page.locator('input[type="tel"]').first
    await second_tel_input.wait_for(state="visible", timeout=timeout_ms)
    second_totp_code = await _generate_fresh_totp_code(platform_efa, previous_totp_code)
    await second_tel_input.fill(second_totp_code, timeout=timeout_ms)
    await progress_cb(75, {"stage": "second_totp_filled"})

    clicked_second_totp_next = await _click_next_button(page, timeout_ms=timeout_ms)
    await progress_cb(80, {"stage": "second_totp_next_clicked", "selector": clicked_second_totp_next})

    new_password_inputs = page.locator('input[type="password"]')
    count = await new_password_inputs.count()
    if count < 2:
        raise RuntimeError(f"密码修改页未找到两个密码输入框，当前匹配数量={count}")
    await new_password_inputs.nth(0).fill(NEW_PASSWORD_VALUE, timeout=timeout_ms)
    await new_password_inputs.nth(1).fill(NEW_PASSWORD_VALUE, timeout=timeout_ms)
    await progress_cb(90, {"stage": "new_passwords_filled"})

    clicked_save_or_next = await _click_save_or_next_button(page, timeout_ms=timeout_ms)
    await progress_cb(100, {"stage": "password_change_submitted", "selector": clicked_save_or_next})


async def sora_plus_register(
    payload: Dict[str, Any],
    progress_cb: ProgressCB,
    *,
    db: Database,
    window_pk: int,
    browser_vendor: str,
    browser_base_url: str,
    browser_access_key: Optional[str],
    space_id: str,
    window_key: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    """打开平台页 -> Google 登录 -> 跳转 ChatGPT -> Continue with Google 登录。"""
    source_payload = payload if isinstance(payload, dict) else {}
    headless = bool(source_payload.get("headless", False))
    pure_mode = bool(source_payload.get("pure_mode", True))
    timeout_ms = int(max(10_000, min(float(timeout_seconds) * 1000, 120_000)))

    await progress_cb(1, {"stage": "resolve_credentials", "window_pk": int(window_pk)})
    creds = await _resolve_window_platform_credentials(db, window_pk=int(window_pk))
    platform_url = str(creds["platform_url"] or "").strip()
    platform_username = str(creds["platform_username"] or "").strip()
    platform_password = str(creds["platform_password"] or "").strip()
    platform_efa = str(creds["platform_efa"] or "").strip()

    
    ctx = get_or_create_ctx(
        vendor=browser_vendor,
        base_url=browser_base_url,
        access_key=browser_access_key,
        space_id=space_id,
        window_key=window_key,
    )
    await ctx.ensure_open(args=[], force_open=False, headless=headless, pure_mode=pure_mode)

    await progress_cb(3, {"stage": "open_browser", "platform_url": platform_url})
    async with ctx.driver_lock:
        if ctx.context is None:
            raise RuntimeError("浏览器上下文不可用：context is None")
        page = await _pick_platform_domain_page(ctx.context, platform_url=platform_url)
        ctx.page = page

        await page.goto(platform_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await progress_cb(10, {"stage": "page_loaded", "url": platform_url})

        current_url = str(page.url or "").strip()
        already_logged_in = _is_already_logged_in(current_url)

        if already_logged_in:
            await progress_cb(55, {"stage": "already_logged_in", "current_url": current_url})
            last_totp_code = ""
        else:
            await progress_cb(12, {"stage": "need_login", "current_url": current_url})
            last_totp_code = await _do_login_flow(
                page,
                platform_username=platform_username,
                platform_password=platform_password,
                platform_efa=platform_efa,
                timeout_ms=timeout_ms,
                progress_cb=progress_cb,
            )

    
    chatgpt_login_result = await _do_chatgpt_google_login(
        page,
        platform_username=platform_username,
        platform_efa=platform_efa,
        previous_totp_code=last_totp_code,
        timeout_ms=timeout_ms,
        progress_cb=progress_cb,
    )
    # 打开目标网址 Payload.video_url 的一个新页面（仅打开，不改变后续主操作页）
    target_video_url = str((source_payload or {}).get("video_url") or "").strip()
    if target_video_url:
        try:
            if ctx.context is not None:
                preview_page = await ctx.context.new_page()
                await preview_page.goto(target_video_url, wait_until="domcontentloaded", timeout=timeout_ms)
                await progress_cb(59, {"stage": "video_url_opened", "url": target_video_url})
        except Exception as e:
            await progress_cb(59, {"stage": "video_url_open_failed", "url": target_video_url, "error": str(e)})
    page = await _bring_sora_drafts_to_front(ctx, refresh_target=False, headless=headless, pure_mode=pure_mode)
    panel_data = {
        "title": "Sora Plus 注册数据",
        "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rows": [
            {"key": "platform_username", "label": "账号邮箱", "value": platform_username},
            {"key": "platform_password", "label": "平台密码", "value": platform_password},
            {
                "key": "prefetched_totp_code",
                "label": "当前2FA验证码",
                "value": str((chatgpt_login_result or {}).get("prefetched_totp_code") or ""),
            },
            {"key": "new_password_value", "label": "新密码模板", "value": NEW_PASSWORD_VALUE},
        ]
        + _build_payload_rows(source_payload),
        "totpSecret": platform_efa,
    }
    await _show_sticky_copy_panel(ctx.context, page, panel_data=panel_data, progress_cb=progress_cb)
    # 仅断开本地 CDP 连接，保留指纹浏览器窗口继续打开，降低后续 Cloudflare 触发概率。
    try:
        br = getattr(ctx, "browser", None)
        pw = getattr(ctx, "playwright", None)
        try:
            ctx.browser = None
            ctx.context = None
            ctx.page = None
            ctx.cdp_endpoint = None
        except Exception:
            pass
        try:
            if br is not None:
                await br.close()
        except Exception:
            pass
        try:
            if pw is not None:
                await pw.stop()
        except Exception:
            pass
        try:
            ctx.playwright = None
        except Exception:
            pass
        await progress_cb(99, {"stage": "cdp_disconnected"})
    except Exception:
        pass

    return {
        "ok": True,
        "stage": "chatgpt_google_login_done",
        "platform_url": platform_url,
        "platform_username": platform_username,
        "already_logged_in": already_logged_in,
        "copy_panel_enabled": True,
        "prefetched_totp_code": str((chatgpt_login_result or {}).get("prefetched_totp_code") or ""),
        "message": "已完成 Google 登录并通过 ChatGPT Continue with Google 登录",
    }
