"""z.ai 注册第二段：验证链接 → Complete Registration → 真实 JWT。

流程（2026-11-16 实测）：
    /auth/verify_email?token=…&email=…&username=…&language=en
    → Complete Registration 表单（username/email disabled 预填，只填密码两格）
    → 可能再要一次滑块 → verify_email + finish_signup 双 200 → 真实 JWT

链接必须全参数（缺参 = Invalid Parameters 空表单）；过期 token 页面文案
"Verification Failed / token is invalid or has expired"（400），需重触发。
"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import async_playwright

from . import sliders as S
from .sliders import is_real_account_token, jwt_email

AUTH_URL = "https://chat.z.ai/auth?redirect_uri=https://z.ai/"


class CompleteResult:
    __slots__ = ("token", "logs", "screenshot_path")

    def __init__(self) -> None:
        self.token = ""
        self.logs: list[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(str(msg))
        print(f"[zai-complete] {msg}", flush=True)


async def _run_session(
    verify_url: str, email: str, password: str, username: str, res: CompleteResult,
    cookie_id: str,
) -> None:
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
                        body = (await resp.text())[:200]
                    except Exception as exc:
                        body = f"<bin/err {type(exc).__name__}>"
                    res.log(f"NET POST {resp.status} {resp.url[:100]} RESP {body}")

            page.on("response", lambda r: asyncio.ensure_future(on_resp(r)))

            res.log("1) 打开验证链接…")
            await page.goto(verify_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
            await S.dump_form(page, "after-verify-link", res.log)

            res.log("2) 填 Complete Registration（密码两格按 type=password 定位）")
            pw_ok = await S.fill_password_fields(page, password, res.log)
            if not pw_ok:
                res.log("  密码定位未满 2 格 → 回退序位填充")
                await S.fill_visible_inputs(page, [username, email, password, password],
                                            res.log)
            await page.wait_for_timeout(600)

            res.log("3) 提交（含滑块兜底）")
            token = ""
            for attempt in range(1, 5):
                await S.click_text(page, "Complete Registration")
                await page.wait_for_timeout(4000)
                token = await page.evaluate("() => localStorage.getItem('token') || ''")
                if is_real_account_token(token):
                    res.log(f"  [{attempt}] 注册完成")
                    break
                if await S.needs_verification(page):
                    res.log(f"  [{attempt}] 要求安全验证 → 滑块")
                    await S.click_start_verification(page)
                    await page.wait_for_timeout(1200)
                    await S.solve_with_retry(page, f"complete-{attempt}", res.log,
                                             attempts=2, cookie_id=cookie_id)
                    await page.wait_for_timeout(2500)
                    continue
                await S.dump_form(page, f"attempt-{attempt}", res.log)

            if not is_real_account_token(token):
                res.log("4) Complete Registration 未给 token → 登录兜底")
                await page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2500)
                await S.click_email_entry(page)
                await page.wait_for_timeout(900)
                await S.click_start_verification(page)
                await S.solve_with_retry(page, "login-challenge", res.log,
                                         cookie_id=cookie_id)
                await page.wait_for_timeout(1500)
                await S.fill_visible_inputs(page, [email, password], res.log)
                for attempt in range(1, 4):
                    await S.click_text(page, "Sign in")
                    await page.wait_for_timeout(4000)
                    token = await page.evaluate("() => localStorage.getItem('token') || ''")
                    if is_real_account_token(token):
                        break
                    if await S.needs_verification(page):
                        await S.click_start_verification(page)
                        await page.wait_for_timeout(1200)
                        await S.solve_with_retry(page, f"login-{attempt}", res.log,
                                                 attempts=2, cookie_id=cookie_id)
                        await page.wait_for_timeout(2500)

            if is_real_account_token(token):
                res.token = token
                res.log(f"TOKEN ok len={len(token)} email={jwt_email(token)}")
            else:
                res.log(f"FAILED: 无真实 token（{jwt_email(token) or 'guest/无'}）")
        finally:
            await browser.close()


SESSION_TIMEOUT_S = 480.0


def run_complete_session(
    verify_url: str, email: str, password: str, username: str,
    *, cookie_id: str = "zai-reg", timeout_s: float = SESSION_TIMEOUT_S,
) -> CompleteResult:
    """打开验证链接补完注册；阻塞返回 CompleteResult（token 可为空=失败）。"""
    res = CompleteResult()

    async def _guarded() -> None:
        await asyncio.wait_for(
            _run_session(verify_url, email, password, username, res, cookie_id),
            timeout=timeout_s)

    try:
        asyncio.run(_guarded())
    except asyncio.TimeoutError:
        res.log(f"session timeout {timeout_s:.0f}s → 本会话作废")
    except Exception as exc:
        res.log(f"session error: {type(exc).__name__}: {exc}")
    return res
