"""z.ai 邮箱注册第一段：建号（signup POST）——有头 Chromium + slidex。

流程（2026-11-16 实测）：
    /auth → Continue with Email → 滑块① → Sign up → 填 Full Name/Email/Password
    → Create Account（第一次点击只弹内嵌验证条）→ 滑块② → 再点一次
    → POST /api/v1/auths/signup 200

成功判据 = signup POST 200 在**本会话**被观察到（NET tap），不是 solve 返回值。
真实 token 要等邮件验证链接（两段式），本层只负责把 signup 发出去。
"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import async_playwright

from . import sliders as S

AUTH_URL = "https://chat.z.ai/auth?redirect_uri=https://z.ai/"


class SignupResult:
    __slots__ = ("signup_posted", "token_email", "screenshot_path", "logs")

    def __init__(self) -> None:
        self.signup_posted = False
        self.token_email = ""
        self.logs: list[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(str(msg))
        print(f"[zai-signup] {msg}", flush=True)


async def _run_session(
    email: str, password: str, full_name: str, res: SignupResult, cookie_id: str
) -> bool:
    async with async_playwright() as p_:
        browser = await p_.chromium.launch(headless=False, args=[
            "--disable-blink-features=AutomationControlled", "--lang=en-US"])
        try:
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="en-US", timezone_id="Asia/Shanghai")
            page = await ctx.new_page()
            await page.add_init_script(S.STEALTH)

            async def on_resp(resp: Any) -> None:
                if "z.ai/api" in resp.url and resp.request.method == "POST":
                    try:
                        body = (await resp.text())[:300]
                    except Exception as exc:
                        body = f"<bin/err {type(exc).__name__}>"
                    res.log(f"NET POST {resp.status} {resp.url[:110]} RESP {body}")
                    if "/auths/signup" in resp.url and resp.status == 200:
                        res.signup_posted = True

            page.on("response", lambda r: asyncio.ensure_future(on_resp(r)))

            res.log("goto auth page…")
            await page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2500)
            await S.dump_form(page, "init", res.log)

            res.log("step 1: Email 入口 + 滑块①")
            if not await S.click_email_entry(page):
                res.log("FAILED: 找不到 Continue with Email")
                return False
            await page.wait_for_timeout(900)
            await S.click_start_verification(page)
            if not await S.solve_with_retry(page, "challenge-1", res.log,
                                            cookie_id=cookie_id):
                res.log("FAILED: 滑块①")
                return False
            await page.wait_for_timeout(1500)

            res.log("step 2: Sign up 表单")
            if not await S.click_text(page, "Sign up"):
                res.log("FAILED: 找不到 Sign up")
                return False
            await page.wait_for_timeout(2000)

            res.log("step 3: 填 Full Name / Email / Password")
            filled = (await S.fill_by_placeholder(page, "Full Name", full_name),
                      await S.fill_by_placeholder(page, "Email", email),
                      await S.fill_by_placeholder(page, "Password", password))
            res.log(f"  fill={filled}")
            if not all(filled):
                await S.dump_form(page, "fill-failed", res.log)
                return False
            await page.wait_for_timeout(600)

            res.log("step 4: Create Account ↔ 二次安全验证")
            for attempt in range(1, 5):
                await S.click_text(page, "Create Account")
                await page.wait_for_timeout(3500)
                if res.signup_posted:
                    res.log(f"  [{attempt}] signup POST 200（本会话收据）")
                    return True
                if await S.needs_verification(page):
                    res.log(f"  [{attempt}] 要求安全验证 → 滑块②")
                    await S.click_start_verification(page)
                    await page.wait_for_timeout(1200)
                    await S.solve_with_retry(page, f"challenge-2-{attempt}",
                                             res.log, attempts=2, cookie_id=cookie_id)
                    await page.wait_for_timeout(2500)
                    continue
                await S.dump_form(page, f"attempt-{attempt}", res.log)
            await S.dump_form(page, "after-create", res.log)
            return res.signup_posted
        finally:
            await browser.close()


SESSION_TIMEOUT_S = 480.0  # 战斗验证值：卡死会话 8 分钟强撤（finally 关浏览器）


def run_signup_session(
    email: str, password: str, full_name: str, *,
    cookie_id: str = "zai-reg", timeout_s: float = SESSION_TIMEOUT_S,
) -> SignupResult:
    """跑一个全新会话的建号流程；阻塞返回 SignupResult（超时=会话失败）。"""
    res = SignupResult()

    async def _guarded() -> None:
        await asyncio.wait_for(
            _run_session(email, password, full_name, res, cookie_id), timeout=timeout_s)

    try:
        asyncio.run(_guarded())
    except asyncio.TimeoutError:
        res.log(f"session timeout {timeout_s:.0f}s → 本会话作废，换新会话")
    except Exception as exc:  # 浏览器崩溃 → 会话失败，由 flow 决定换新会话
        res.log(f"session error: {type(exc).__name__}: {exc}")
    return res
