"""PayPal 管理页使用的指纹浏览器操作能力。

本模块只做通用的“打开窗口 / 导航 / 尽力预填表单”，真正的验证短信、风控
挑战和最终业务确认仍由页面里的真人操作完成。这样可以复用项目现有的
PlaywrightBrowserContext，又避免把站点强绑定逻辑塞进 admin.py。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .ai_browser_agent import AIBrowserAgent, build_browser_task_prompt, normalize_ai_agent_model
from .browser_automation_base import FingerprintBrowserAutomationBase
from .playwright_broswer_context import get_or_create_ctx, pick_working_page_from_context


DEFAULT_PAYPAL_SIGNIN_URL = "https://www.paypal.com/signin?locale.x=en_US"
DEFAULT_PAYPAL_SIGNUP_URL = "https://www.paypal.com/us/welcome/signup?locale.x=en_US"


def normalize_paypal_url(url: Optional[str], *, default: str = DEFAULT_PAYPAL_SIGNIN_URL) -> str:
    raw = str(url or "").strip() or default
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return default
    return raw


def _account_dict(account: Any) -> Dict[str, Any]:
    if account is None:
        return {}
    if isinstance(account, dict):
        return dict(account)
    dump = getattr(account, "model_dump", None)
    if callable(dump):
        return dict(dump())
    return dict(getattr(account, "__dict__", {}) or {})


async def _try_paypal_login(page: Any, account: Dict[str, Any], *, auto_submit: bool, steps: list[str]) -> Dict[str, Any]:
    ui = FingerprintBrowserAutomationBase(page, step_log=steps)
    email = account.get("paypal_email") or account.get("email")
    password = account.get("paypal_password") or account.get("password")
    filled_email = await ui.fill_first(
        [
            "input#email",
            "input[name='login_email']",
            "input[name='email']",
            "input[type='email']",
            "input[autocomplete='username']",
        ],
        email,
        label="PayPal 邮箱",
    )
    if filled_email:
        # PayPal 登录页常见为邮箱页 -> Next -> 密码页；若密码框已存在则点击会失败并被忽略。
        await ui.click_first(
            [
                "button#btnNext",
                "button[name='btnNext']",
                "button:has-text('Next')",
                "button:has-text('Continue')",
                "input[type='submit'][value*='Next']",
            ],
            label="下一步",
            wait_timeout=1200,
        )
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            await asyncio.sleep(1.2)

    filled_password = await ui.fill_first(
        [
            "input#password",
            "input[name='login_password']",
            "input[name='password']",
            "input[type='password']",
            "input[autocomplete='current-password']",
        ],
        password,
        label="PayPal 密码",
    )
    submitted = False
    if auto_submit and filled_password:
        submitted = await ui.click_first(
            [
                "button#btnLogin",
                "button[name='btnLogin']",
                "button:has-text('Log In')",
                "button:has-text('Log in')",
                "button:has-text('Login')",
                "button[type='submit']",
            ],
            label="登录",
            wait_timeout=2000,
        )

    return {
        "filled_email": filled_email,
        "filled_password": filled_password,
        "submitted": submitted,
        "message": "已尽力预填登录表单" if (filled_email or filled_password) else "未找到可填写的登录表单或未配置邮箱/密码",
    }


async def _try_paypal_register(page: Any, account: Dict[str, Any], *, auto_submit: bool, steps: list[str]) -> Dict[str, Any]:
    ui = FingerprintBrowserAutomationBase(page, step_log=steps)
    full_name = str(account.get("full_name") or "").strip()
    first_name = account.get("first_name") or (full_name.split(" ", 1)[0] if full_name else None)
    last_name = account.get("last_name") or (full_name.split(" ", 1)[1] if " " in full_name else None)

    filled: Dict[str, bool] = {}
    filled["email"] = await ui.fill_first(
        ["input[type='email']", "input[name='email']", "input[id*='email']", "input[autocomplete='email']"],
        account.get("paypal_email"),
        label="注册邮箱",
    )
    filled["password"] = await ui.fill_first(
        [
            "input[type='password']",
            "input[name='password']",
            "input[id*='password']",
            "input[autocomplete='new-password']",
        ],
        account.get("paypal_password"),
        label="注册密码",
    )
    filled["phone"] = await ui.fill_first(
        [
            "input[type='tel']",
            "input[name='phone']",
            "input[name*='phone']",
            "input[id*='phone']",
            "input[autocomplete='tel']",
        ],
        account.get("phone"),
        label="手机号",
    )
    filled["first_name"] = await ui.fill_first(
        [
            "input[name='firstName']",
            "input[name='first_name']",
            "input[id*='firstName']",
            "input[id*='first-name']",
            "input[autocomplete='given-name']",
        ],
        first_name,
        label="First name",
    )
    filled["last_name"] = await ui.fill_first(
        [
            "input[name='lastName']",
            "input[name='last_name']",
            "input[id*='lastName']",
            "input[id*='last-name']",
            "input[autocomplete='family-name']",
        ],
        last_name,
        label="Last name",
    )
    filled["address"] = await ui.fill_first(
        [
            "input[name='address1']",
            "input[name='addressLine1']",
            "input[id*='addressLine1']",
            "input[autocomplete='address-line1']",
        ],
        account.get("address_line1"),
        label="地址",
    )
    filled["city"] = await ui.fill_first(
        ["input[name='city']", "input[id*='city']", "input[autocomplete='address-level2']"],
        account.get("city"),
        label="城市",
    )
    filled["state"] = await ui.fill_first(
        ["input[name='state']", "input[id*='state']", "input[autocomplete='address-level1']"],
        account.get("state"),
        label="州/省",
    )
    filled["postal_code"] = await ui.fill_first(
        [
            "input[name='postalCode']",
            "input[name='zipCode']",
            "input[id*='postal']",
            "input[id*='zip']",
            "input[autocomplete='postal-code']",
        ],
        account.get("postal_code"),
        label="邮编",
    )
    filled["country"] = await ui.select_first(
        ["select[name='country']", "select[id*='country']", "select[autocomplete='country']"],
        account.get("country"),
        label="国家",
    )

    submitted = False
    if auto_submit and any(filled.values()):
        submitted = await ui.click_first(
            [
                "button:has-text('Next')",
                "button:has-text('Continue')",
                "button:has-text('Agree')",
                "button:has-text('Create')",
                "button[type='submit']",
            ],
            label="继续注册",
            wait_timeout=2000,
        )

    return {
        "filled": filled,
        "submitted": submitted,
        "message": "已尽力预填注册表单" if any(filled.values()) else "未找到可填写的注册表单或资料字段不足",
    }


async def run_paypal_window_action(
    *,
    ctx_row: Dict[str, Any],
    action: str,
    target_url: Optional[str],
    register_url: Optional[str] = None,
    account: Any = None,
    headless: bool = False,
    pure_mode: bool = True,
    auto_submit: bool = False,
    ai_model: Optional[str] = None,
    ai_api_key: Optional[str] = None,
    ai_prompt: Optional[str] = None,
    max_steps: int = 14,
    timeout_ms: int = 90_000,
) -> Dict[str, Any]:
    vendor = str(ctx_row.get("vendor") or "roxy")
    base_url = str(ctx_row.get("lan_addr") or "").strip()
    access_key = ctx_row.get("access_key")
    space_id = str(ctx_row.get("space_id") or "").strip()
    window_key = str(ctx_row.get("window_key") or "").strip()
    if not base_url or not space_id or not window_key:
        raise RuntimeError("mapping missing vendor/lan_addr/space_id/window_key")

    action = str(action or "open").strip().lower()
    if action == "register":
        url = normalize_paypal_url(register_url or target_url, default=DEFAULT_PAYPAL_SIGNUP_URL)
    else:
        url = normalize_paypal_url(target_url, default=DEFAULT_PAYPAL_SIGNIN_URL)

    ctx = get_or_create_ctx(
        vendor=vendor,
        base_url=base_url,
        access_key=access_key,
        space_id=space_id,
        window_key=window_key,
    )
    steps: list[str] = []
    final_url = ""
    title = ""
    action_result: Dict[str, Any] = {}

    async with ctx.driver_lock:
        await ctx.ensure_open(args=[], force_open=False, headless=headless, require_page=False, pure_mode=pure_mode)
        if ctx.context is None:
            raise RuntimeError("CDP 已连接但未获得 browser context")
        page = await pick_working_page_from_context(ctx.context)
        await page.bring_to_front()
        ui = FingerprintBrowserAutomationBase(page, step_log=steps)
        await ui.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.bring_to_front()
        except Exception:
            pass

        account_data = _account_dict(account)
        if action == "login":
            # 由通用 AI 浏览器智能体实时扫描页面并决定填表/点击动作；不再使用固定 PayPal 选择器流程。
            agent = AIBrowserAgent(
                page,
                model=normalize_ai_agent_model(ai_model),
                api_key=ai_api_key,
                max_steps=max_steps,
                step_log=steps,
            )
            action_result = await agent.run(
                task=build_browser_task_prompt(
                    action="使用所选 PayPal 资料登录 PayPal 账号",
                    target=url,
                    extra_prompt=ai_prompt,
                ),
                data=account_data,
                auto_submit=auto_submit,
            )
        elif action == "register":
            # 注册同样走统一智能体。账号、手机号、地址、卡等资料通过 value_key 在本地填入，
            # 模型只看到字段清单与脱敏摘要。
            agent = AIBrowserAgent(
                page,
                model=normalize_ai_agent_model(ai_model),
                api_key=ai_api_key,
                max_steps=max_steps,
                step_log=steps,
            )
            action_result = await agent.run(
                task=build_browser_task_prompt(
                    action="使用所选 PayPal 资料注册 PayPal 账号；遇到短信验证码/风控/验证码时停止并提示人工接管",
                    target=url,
                    extra_prompt=ai_prompt,
                ),
                data=account_data,
                auto_submit=auto_submit,
            )

        try:
            final_url = str(getattr(page, "url", "") or "")
        except Exception:
            final_url = ""
        try:
            title = await page.title()
        except Exception:
            title = ""

        # 保留指纹浏览器窗口打开，只断开本地 CDP，和现有管理端手动启动逻辑一致。
        await ctx.disconnect_playwright_only()

    return {
        "success": True,
        "action": action,
        "opened_url": url,
        "final_url": final_url,
        "title": title,
        "steps": steps,
        "action_result": action_result,
    }
