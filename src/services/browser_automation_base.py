"""Reusable browser automation primitives for fingerprint-browser pages.

这个模块只封装“页面级自动化”的通用动作，不包含任何具体站点业务：
- 查找第一个可见元素
- 安全点击（普通点击 -> force 点击 -> DOM click 兜底）
- 输入/清空文案
- 选择 select 下拉
- 在可编辑元素中写入长文本

站点执行器（PayPal / Dreamina / Sora / Veo 等）应该在自己的文件里组合这些
基础能力，保留业务流程和选择器，避免重复写容易出错的 Playwright 操作。
"""

from __future__ import annotations

import asyncio
import random
import string
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    from .playwright_broswer_context import append_log, safe_trim
except Exception:  # pragma: no cover - 允许独立导入测试
    append_log = None  # type: ignore

    def safe_trim(s: Optional[str], max_len: int = 300) -> str:  # type: ignore
        if not s:
            return ""
        ss = str(s)
        return ss if len(ss) <= max_len else ss[:max_len] + "...(truncated)"


class FingerprintBrowserAutomationBase:
    """指纹浏览器页面自动化基础类。

    设计原则：
    - 方法默认“尽力而为”，适合管理台半自动流程；需要强校验时调用方检查 bool。
    - `click_locator(..., raise_on_fail=True)` 可用于必须成功的动作。
    - `step_log` 是可选 list，会记录用户可读的操作步骤，便于前端展示。
    """

    def __init__(
        self,
        page: Any,
        *,
        log_file: Optional[Path] = None,
        step_log: Optional[List[str]] = None,
        default_timeout: int = 5000,
    ) -> None:
        self.page = page
        self.log_file = log_file
        self.step_log = step_log
        self.default_timeout = int(default_timeout or 5000)

    def log_step(self, message: str) -> None:
        msg = str(message or "").strip()
        if not msg:
            return
        if self.step_log is not None:
            self.step_log.append(msg)
        if self.log_file is not None and append_log is not None:
            try:
                append_log(self.log_file, msg)  # type: ignore[misc]
            except Exception:
                pass

    @staticmethod
    def first(locator: Any) -> Any:
        """兼容 Playwright locator.first 属性/方法差异，返回第一个 locator。"""
        first_attr = getattr(locator, "first", None)
        if callable(first_attr):
            return first_attr()
        return first_attr or locator

    def locator(self, selector: str, *, first: bool = True) -> Any:
        loc = self.page.locator(selector)
        return self.first(loc) if first else loc

    async def wait_visible(self, locator: Any, *, timeout: Optional[int] = None) -> bool:
        try:
            await locator.wait_for(state="visible", timeout=int(timeout or self.default_timeout))
            return True
        except Exception:
            return False

    async def first_visible_locator(
        self,
        selectors: Sequence[str] | Iterable[str],
        *,
        timeout: int = 1500,
    ) -> Optional[Any]:
        """从一组 CSS/text selector 中返回第一个可见 locator。"""
        for selector in selectors:
            try:
                loc = self.locator(str(selector), first=True)
                if await self.wait_visible(loc, timeout=timeout):
                    return loc
            except Exception:
                continue
        return None

    async def click_locator(
        self,
        locator: Any,
        *,
        timeout: int = 7000,
        force_timeout: int = 2000,
        js_timeout: int = 2000,
        raise_on_fail: bool = False,
        label: str = "",
    ) -> bool:
        """安全点击：普通 click -> force click -> DOM click。

        Args:
            raise_on_fail: True 时最终失败会抛出最后一个异常。
        """
        last_err: Optional[Exception] = None
        try:
            await locator.click(timeout=timeout)
            if label:
                self.log_step(f"已点击 {label}")
            return True
        except Exception as e:
            last_err = e

        try:
            await locator.click(timeout=force_timeout, force=True)
            if label:
                self.log_step(f"已点击 {label}")
            return True
        except Exception as e:
            last_err = e

        try:
            await locator.evaluate("(el) => el.click()", timeout=js_timeout)
            if label:
                self.log_step(f"已点击 {label}")
            return True
        except Exception as e:
            last_err = e

        if raise_on_fail and last_err is not None:
            raise last_err
        return False

    async def safe_click(self, locator: Any, *, timeout: int = 7000, label: str = "") -> None:
        """必须成功的点击动作；失败时抛出异常。

        保留这个短方法名，方便站点执行器表达“这里就是要点成功”。
        """
        await self.click_locator(locator, timeout=timeout, raise_on_fail=True, label=label)

    async def click_first(
        self,
        selectors: Sequence[str] | Iterable[str],
        *,
        label: str = "",
        wait_timeout: int = 2500,
        click_timeout: int = 5000,
        raise_on_fail: bool = False,
    ) -> bool:
        """点击一组 selector 中第一个可见元素。"""
        last_err: Optional[Exception] = None
        for selector in selectors:
            try:
                loc = self.locator(str(selector), first=True)
                await loc.wait_for(state="visible", timeout=wait_timeout)
                ok = await self.click_locator(
                    loc,
                    timeout=click_timeout,
                    force_timeout=min(2000, click_timeout),
                    raise_on_fail=False,
                    label=label,
                )
                if ok:
                    return True
            except Exception as e:
                last_err = e
                continue
        if raise_on_fail and last_err is not None:
            raise last_err
        return False

    async def fill_locator(
        self,
        locator: Any,
        value: Any,
        *,
        timeout: int = 5000,
        clear_first: bool = False,
        label: str = "",
        raise_on_fail: bool = False,
    ) -> bool:
        text = str(value or "")
        if text == "":
            return False
        last_err: Optional[Exception] = None
        try:
            if clear_first:
                try:
                    await locator.click(timeout=min(timeout, 3000), force=True)
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                except Exception:
                    pass
            await locator.fill(text, timeout=timeout)
            if label:
                self.log_step(f"已填写 {label}")
            return True
        except Exception as e:
            last_err = e

        try:
            await locator.click(timeout=min(timeout, 3000), force=True)
            if clear_first:
                await self.page.keyboard.press("Control+A")
                await self.page.keyboard.press("Backspace")
            await self.page.keyboard.insert_text(text)
            if label:
                self.log_step(f"已填写 {label}")
            return True
        except Exception as e:
            last_err = e

        if raise_on_fail and last_err is not None:
            raise last_err
        return False

    async def fill_first(
        self,
        selectors: Sequence[str] | Iterable[str],
        value: Any,
        *,
        label: str = "",
        wait_timeout: int = 2500,
        fill_timeout: int = 5000,
        clear_first: bool = False,
        raise_on_fail: bool = False,
    ) -> bool:
        """填写一组 selector 中第一个可见输入控件。"""
        text = str(value or "").strip()
        if not text:
            return False
        last_err: Optional[Exception] = None
        for selector in selectors:
            try:
                loc = self.locator(str(selector), first=True)
                await loc.wait_for(state="visible", timeout=wait_timeout)
                if await self.fill_locator(
                    loc,
                    text,
                    timeout=fill_timeout,
                    clear_first=clear_first,
                    label=label,
                    raise_on_fail=False,
                ):
                    return True
            except Exception as e:
                last_err = e
                continue
        if raise_on_fail and last_err is not None:
            raise last_err
        return False

    async def select_first(
        self,
        selectors: Sequence[str] | Iterable[str],
        value: Any,
        *,
        label: str = "",
        wait_timeout: int = 1500,
        select_timeout: int = 3000,
        raise_on_fail: bool = False,
    ) -> bool:
        """对一组 selector 中第一个可见 select 执行 select_option。"""
        text = str(value or "").strip()
        if not text:
            return False
        last_err: Optional[Exception] = None
        for selector in selectors:
            try:
                loc = self.locator(str(selector), first=True)
                await loc.wait_for(state="visible", timeout=wait_timeout)
                await loc.select_option(text, timeout=select_timeout)
                if label:
                    self.log_step(f"已选择 {label}")
                return True
            except Exception as e:
                last_err = e
                continue
        if raise_on_fail and last_err is not None:
            raise last_err
        return False

    async def fill_editable_text(
        self,
        text: str,
        *,
        locators: Optional[Sequence[Any]] = None,
        selector_fallback: str = 'textarea,input:not([type="hidden"]):not([type="file"]),[contenteditable="true"],[role="textbox"]',
        pick_last: bool = True,
        label: str = "",
        timeout: int = 3000,
        raise_on_fail: bool = False,
    ) -> bool:
        """向 textarea/input/contenteditable/role=textbox 写入长文本。

        适合提示词、备注、搜索框等文本输入；常规账号密码可优先使用 fill_first。
        """
        val = str(text or "")
        if not val:
            return False

        candidates: List[Any] = list(locators or [])
        if not candidates:
            try:
                loc = self.page.locator(selector_fallback)
                candidates.append(loc.last if pick_last else loc.first)
            except Exception:
                pass

        last_err: Optional[Exception] = None
        for loc in candidates:
            try:
                # locator.count 不是所有对象都有；有则先判断。
                count_fn = getattr(loc, "count", None)
                if callable(count_fn) and await count_fn() == 0:
                    continue
                await loc.click(timeout=timeout, force=True)
                await self.page.keyboard.press("Control+A")
                await self.page.keyboard.press("Backspace")
                try:
                    await loc.fill(val, timeout=timeout)
                except Exception:
                    await self.page.keyboard.insert_text(val)
                if label:
                    self.log_step(f"已填写 {label}")
                return True
            except Exception as e:
                last_err = e
                continue

        ok = await self.page.evaluate(
            """(args) => {
                const {selector, text, pickLast} = args;
                const els = Array.from(document.querySelectorAll(selector)).filter(el => {
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    return r.width > 20 && r.height > 10 && st.visibility !== 'hidden' && st.display !== 'none';
                });
                const el = pickLast ? els[els.length - 1] : els[0];
                if (!el) return false;
                el.focus();
                if (el.isContentEditable) {
                    el.textContent = text;
                } else {
                    el.value = text;
                }
                el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:text}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                return true;
            }""",
            {"selector": selector_fallback, "text": val, "pickLast": bool(pick_last)},
        )
        if ok:
            if label:
                self.log_step(f"已填写 {label}")
            return True

        if raise_on_fail and last_err is not None:
            raise last_err
        return False

    async def press_escape(self, *, delay_ms: int = 0) -> None:
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    async def goto(self, url: str, *, wait_until: str = "domcontentloaded", timeout: int = 90_000) -> bool:
        try:
            await self.page.goto(url, wait_until=wait_until, timeout=timeout)
            self.log_step(f"已打开 {safe_trim(url, 160)}")
            return True
        except Exception as e:
            self.log_step(f"打开页面超时/失败，但窗口已打开：{e}")
            return False

    # ------------------------------------------------------------------
    # 通用“拟人化”页面动作
    # ------------------------------------------------------------------
    @staticmethod
    def random_human_text(*, min_len: int = 5, max_len: int = 14) -> str:
        """生成一段短随机文本，供空闲窗口拟人输入使用。"""
        lo = max(1, int(min_len or 1))
        hi = max(lo, int(max_len or lo))
        n = random.randint(lo, hi)
        alphabet = string.ascii_lowercase + string.digits
        # 加一个较自然的前缀，避免纯乱码过于机械。
        return "note " + "".join(random.choice(alphabet) for _ in range(n))

    async def _viewport_size(self) -> tuple[int, int]:
        """获取页面 viewport，CDP 复用窗口下 `page.viewport_size` 可能为 None。"""
        try:
            vs = getattr(self.page, "viewport_size", None)
            if isinstance(vs, dict):
                w = int(vs.get("width") or 0)
                h = int(vs.get("height") or 0)
                if w > 0 and h > 0:
                    return w, h
        except Exception:
            pass
        try:
            res = await self.page.evaluate(
                "() => ({width: window.innerWidth || 1280, height: window.innerHeight || 720})"
            )
            w = int((res or {}).get("width") or 1280)
            h = int((res or {}).get("height") or 720)
            return max(320, w), max(240, h)
        except Exception:
            return 1280, 720

    async def human_mouse_move(
        self,
        *,
        moves: Optional[int] = None,
        min_moves: int = 1,
        max_moves: int = 3,
    ) -> bool:
        """在页面内随机平滑移动鼠标。"""
        try:
            cnt = int(moves) if moves is not None else random.randint(max(1, min_moves), max(max(1, min_moves), max_moves))
        except Exception:
            cnt = random.randint(1, 3)
        cnt = max(1, min(20, cnt))
        try:
            w, h = await self._viewport_size()
            for _ in range(cnt):
                x = random.uniform(20, max(21, w - 20))
                y = random.uniform(20, max(21, h - 20))
                await self.page.mouse.move(x, y, steps=random.randint(5, 18))
                await asyncio.sleep(random.uniform(0.05, 0.25))
            self.log_step(f"已模拟鼠标移动 {cnt} 次")
            return True
        except Exception:
            return False

    async def human_scroll_page(
        self,
        *,
        scrolls: Optional[int] = None,
        min_scrolls: int = 1,
        max_scrolls: int = 4,
    ) -> bool:
        """随机滚动页面；优先走 mouse.wheel，失败时回退到 DOM scrollBy。"""
        try:
            cnt = int(scrolls) if scrolls is not None else random.randint(max(1, min_scrolls), max(max(1, min_scrolls), max_scrolls))
        except Exception:
            cnt = random.randint(1, 4)
        cnt = max(1, min(30, cnt))
        ok = False
        for _ in range(cnt):
            dy = random.randint(180, 760) * (1 if random.random() >= 0.25 else -1)
            dx = 0 if random.random() >= 0.10 else random.randint(-80, 80)
            try:
                await self.page.mouse.wheel(dx, dy)
                ok = True
            except Exception:
                try:
                    await self.page.evaluate(
                        "(args) => { try { window.scrollBy({left: args.dx, top: args.dy, behavior: 'smooth'}); } catch(e) { window.scrollBy(args.dx, args.dy); } }",
                        {"dx": dx, "dy": dy},
                    )
                    ok = True
                except Exception:
                    pass
            await asyncio.sleep(random.uniform(0.15, 0.65))
        if ok:
            self.log_step(f"已模拟页面滚动 {cnt} 次")
        return ok

    async def human_type_random_input(
        self,
        text: Optional[str] = None,
        *,
        selector: str = (
            "textarea:not([disabled]):not([readonly]),"
            "input:not([type='hidden']):not([type='file']):not([type='password']):not([disabled]):not([readonly]),"
            "[contenteditable='true'],[contenteditable=''],[role='textbox']"
        ),
        max_candidates: int = 40,
    ) -> bool:
        """在可见输入框/文本框中输入随机内容（不提交表单）。"""
        val = str(text or "").strip() or self.random_human_text()
        try:
            indexes = await self.page.evaluate(
                """(args) => {
                    const selector = args.selector;
                    const limit = Math.max(1, args.maxCandidates || 40);
                    const allowedInputTypes = new Set(['text', 'search', 'email', 'url', 'tel', 'number']);
                    const out = [];
                    const els = Array.from(document.querySelectorAll(selector));
                    for (let i = 0; i < els.length && out.length < limit; i++) {
                        const el = els[i];
                        const tag = (el.tagName || '').toLowerCase();
                        const type = ((el.getAttribute('type') || 'text') + '').toLowerCase();
                        if (tag === 'input' && !allowedInputTypes.has(type)) continue;
                        if (el.disabled || el.readOnly) continue;
                        if ((el.getAttribute('aria-disabled') || '').toLowerCase() === 'true') continue;
                        const st = window.getComputedStyle(el);
                        if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || 1) === 0) continue;
                        const r = el.getBoundingClientRect();
                        if (!r || r.width < 40 || r.height < 14) continue;
                        if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) continue;
                        out.push(i);
                    }
                    return out;
                }""",
                {"selector": selector, "maxCandidates": int(max_candidates or 40)},
            )
        except Exception:
            indexes = []

        if not indexes:
            return False

        random.shuffle(indexes)
        loc_all = None
        try:
            loc_all = self.page.locator(selector)
        except Exception:
            loc_all = None
        if loc_all is None:
            return False

        for idx in indexes[: min(len(indexes), 8)]:
            try:
                loc = loc_all.nth(int(idx))
                await loc.click(timeout=2000, force=True)
                await asyncio.sleep(random.uniform(0.05, 0.20))
                # 不按 Enter、不提交，仅输入文本。若已有内容，前置一个空格更像追加输入。
                prefix = ""
                try:
                    existing = await loc.evaluate(
                        "(el) => el.isContentEditable ? (el.textContent || '') : (el.value || '')",
                        timeout=1000,
                    )
                    if str(existing or "").strip():
                        prefix = " "
                except Exception:
                    pass
                try:
                    await self.page.keyboard.type(prefix + val, delay=random.randint(25, 120))
                except Exception:
                    await self.page.keyboard.insert_text(prefix + val)
                self.log_step("已在输入框中模拟随机输入")
                return True
            except Exception:
                continue
        return False

    async def human_refresh_once(
        self,
        *,
        wait_until: str = "domcontentloaded",
        timeout: int = 45_000,
        settle_seconds: Optional[float] = None,
    ) -> bool:
        """刷新当前页面一次。调用方应保证每轮最多调用一次。"""
        ok = False
        try:
            await self.page.reload(wait_until=wait_until, timeout=int(timeout or 45_000))
            ok = True
        except Exception:
            try:
                await self.page.evaluate("() => { try { window.location.reload(); return true; } catch(e) { return false; } }")
                ok = True
            except Exception:
                ok = False
        if settle_seconds is None:
            settle_seconds = random.uniform(1.0, 3.0)
        if settle_seconds and settle_seconds > 0:
            await asyncio.sleep(float(settle_seconds))
        if ok:
            self.log_step("已刷新页面一次")
        return ok

    async def perform_human_like_activity(
        self,
        *,
        include_refresh: bool = True,
        max_refreshes: int = 1,
        mouse_moves: Optional[int] = None,
        scrolls: Optional[int] = None,
        input_attempts: Optional[int] = None,
    ) -> Dict[str, Any]:
        """执行一轮通用拟人操作。

        默认包含：鼠标移动、页面滚动、随机输入、刷新页面。
        其中刷新每轮最多尝试一次（`max_refreshes` 默认 1），其它动作次数随机。
        """
        try:
            mm = int(mouse_moves) if mouse_moves is not None else random.randint(1, 3)
        except Exception:
            mm = random.randint(1, 3)
        try:
            sc = int(scrolls) if scrolls is not None else random.randint(1, 4)
        except Exception:
            sc = random.randint(1, 4)
        try:
            inp = int(input_attempts) if input_attempts is not None else random.randint(1, 2)
        except Exception:
            inp = random.randint(1, 2)
        mm = max(1, min(20, mm))
        sc = max(1, min(30, sc))
        inp = max(0, min(10, inp))
        refresh_budget = max(0, min(1, int(max_refreshes or 0))) if include_refresh else 0

        ops: List[str] = []
        ops.extend(["mouse"] * mm)
        ops.extend(["scroll"] * sc)
        ops.extend(["input"] * inp)
        if refresh_budget > 0:
            ops.append("refresh")
        random.shuffle(ops)

        result: Dict[str, Any] = {
            "mouse_moves": 0,
            "scrolls": 0,
            "inputs": 0,
            "refreshes": 0,
            "refresh_attempted": 0,
        }
        for op in ops:
            if op == "mouse":
                if await self.human_mouse_move(moves=1):
                    result["mouse_moves"] = int(result["mouse_moves"]) + 1
            elif op == "scroll":
                if await self.human_scroll_page(scrolls=1):
                    result["scrolls"] = int(result["scrolls"]) + 1
            elif op == "input":
                if await self.human_type_random_input():
                    result["inputs"] = int(result["inputs"]) + 1
            elif op == "refresh":
                # 每轮最多一次：记录 attempt 后不做任何重试。
                if int(result["refresh_attempted"]) >= refresh_budget:
                    continue
                result["refresh_attempted"] = int(result["refresh_attempted"]) + 1
                if await self.human_refresh_once():
                    result["refreshes"] = int(result["refreshes"]) + 1
            await asyncio.sleep(random.uniform(0.08, 0.35))
        return result
