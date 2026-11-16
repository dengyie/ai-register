"""slidex 滑块求解与页面操作助手（z.ai auth 页 2026-11 实测口径）。

纪律（2026-11-16 实证）：
- solve 返回 True ≠ 真过：上游回 ``verifyCode:F001``（success:true,
  verifyResult:false）时 slidex 仍报成功——真过与否只看页面推进 / signup
  是否发出。本模块只提供求解，放行判定由 signup/complete 层负责。
- ``gap=0px travel=0px`` + ``piece alpha 全不透明`` = 图未重置假检测家族，
  会话级换新重跑即过，勿调求解参数。
- conf 低至 0.48 也可能被上游接受，低置信度不必重取图。
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from slidex import SliderSolver

STEALTH = "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
SOLVE_TIMEOUT_S = 110.0  # 单次求解上限（slidex manual_wait 兜底会被此掐断）
SOLVE_RETRIES = 3


def jwt_email(token: str) -> str:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(data.get("email") or "")
    except Exception:
        return ""


def is_real_account_token(token: str) -> bool:
    """guest-<ts>@guest.com 的临时 JWT 一律不算注册完成。"""
    return bool(token) and "guest.com" not in jwt_email(token)


async def dump_form(page: Any, tag: str, log=None) -> dict[str, Any]:
    infos = await page.evaluate("""() => ({
        url: location.href,
        inputs: [...document.querySelectorAll('input')]
            .filter(i => i.offsetParent).map(i => ({type: i.type, ph: i.placeholder || ''})),
        buttons: [...document.querySelectorAll('button')]
            .map(b => (b.innerText || '').trim()).filter(t => t),
        token: localStorage.getItem('token') || '',
        texts: [...new Set([...document.querySelectorAll('div,span,p')]
            .map(e => (e.textContent || '').trim())
            .filter(t => t && t.length <= 70 &&
                    /verif|slider|puzzle|please|sent|code|error|fail|success|pass/i.test(t)))].slice(0, 12),
    })""")
    if log:
        log(f"DUMP[{tag}] url={infos['url']} token_email={jwt_email(infos['token']) or '-'}")
        log(f"DUMP[{tag}] buttons={infos['buttons'][:6]}")
    return infos


async def click_email_entry(page: Any) -> bool:
    try:
        await page.get_by_role("button", name="Continue with Email").click(timeout=8000)
        return True
    except Exception:
        return await page.evaluate("""() => { const b=[...document.querySelectorAll('button')]
            .find(x=>/Email/i.test(x.innerText||'')&&!/Google|Github|Phone/.test(x.innerText));
            if(b){ b.click(); return true; } return false; }""")


async def click_start_verification(page: Any) -> bool:
    return await page.evaluate("""() => { const n=[...document.querySelectorAll('div,span,button,p')]
        .find(e=>(e.textContent||'').trim()==='Click to start verification'&&e.children.length===0);
        if(n){ n.click(); return true; } return false; }""")


async def needs_verification(page: Any) -> bool:
    return await page.evaluate("""() => {
        const texts = [...document.querySelectorAll('div,span,p')]
            .map(e => (e.textContent || '').trim());
        return texts.some(t => t === 'Please complete security verification'
                             || t === 'Click to start verification'
                             || t === 'Please drag the slider to complete the puzzle');
    }""")


async def _solve_once(page: Any, cookie_id: str) -> bool:
    solver = SliderSolver(cookie_id=cookie_id, provider="auto")
    ok, _ = await solver.solve_on_page(page)
    return bool(ok)


async def solve_with_retry(
    page: Any, tag: str, log=None, attempts: int = SOLVE_RETRIES, cookie_id: str = "zai-reg"
) -> bool:
    for i in range(1, attempts + 1):
        try:
            ok = await asyncio.wait_for(
                _solve_once(page, cookie_id), timeout=SOLVE_TIMEOUT_S)
        except asyncio.TimeoutError:
            ok = False
        if log:
            log(f"  [{tag}] solve#{i} -> {ok}")
        if ok:
            return True
        await page.wait_for_timeout(1500)
    return False


async def click_text(page: Any, text: str) -> bool:
    try:
        await page.get_by_role("button", name=text, exact=True).click(timeout=6000)
        return True
    except Exception:
        return await page.evaluate("""(t) => {
            const n=[...document.querySelectorAll('button,a,div,span')]
                .find(e=>(e.textContent||'').trim()===t);
            if(n){ n.click(); return true; } return false; }""", text)


async def fill_by_placeholder(page: Any, placeholder: str, value: str) -> bool:
    try:
        await page.get_by_placeholder(placeholder, exact=False).first.fill(value, timeout=6000)
        return True
    except Exception:
        return await page.evaluate("""({ph, v}) => {
            const i=[...document.querySelectorAll('input')]
                .find(x=>(x.placeholder||'').includes(ph));
            if(!i) return false;
            const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
            setter.call(i, v); i.dispatchEvent(new Event('input',{bubbles:true}));
            return true; }""", {"ph": placeholder, "v": value})


async def fill_visible_inputs(page: Any, values: list[str], log=None) -> list[bool]:
    """按序填可见 input（React 受控组件；disabled 预填框会 fill 失败=预期）。"""
    out = []
    for i, v in enumerate(values):
        try:
            loc = page.locator("input:visible").nth(i)
            await loc.fill(v, timeout=5000)
            out.append(True)
        except Exception as exc:
            if log:
                log(f"  fill#{i} failed: {type(exc).__name__}")
            out.append(False)
    return out


async def fill_password_fields(page: Any, password: str, log=None) -> bool:
    """按 input[type=password] 定位密码两格（username/email disabled 预填框不碰）。

    返回是否成功填满 ≥2 个可见密码框；不足时调用方回退序位填充。
    """
    try:
        locs = page.locator("input[type=password]")
        count = await locs.count()
    except Exception as exc:
        if log:
            log(f"  password locator err: {type(exc).__name__}")
        return False
    results: list[bool] = []
    for i in range(count):
        loc = locs.nth(i)
        try:
            if not await loc.is_visible():
                continue
            await loc.fill(password, timeout=5000)
            results.append(True)
        except Exception as exc:
            if log:
                log(f"  pw#{i} fill failed: {type(exc).__name__}")
            results.append(False)
    ok = sum(1 for r in results if r) >= 2
    if log:
        log(f"  password fill={results}")
    return ok
