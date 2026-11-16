"""z.ai 注册编排：N 个全新会话的建号→收据→补完，产出 RegisterResult。

成功收据链（2026-11-16 战斗验证口径，全链 immune to slidex 假成功）：
    signup POST 200（本会话 NET tap 观察）→ 验证链接到信（unified admin API
    按 to_addr 客户端过滤 + baseline 去重 + 全参数门禁）→ Complete Registration
    真实 JWT（email claim 必须等于本号邮箱）。

this-run 防伪：ok=True 的 RegisterResult 必带 artifacts 会话标记
（cookie_id / signup_posted_at / verify_link_sha8 / token_email），
``validate_this_run`` 供调用方（测试/adapter）复核，伪造历史产物直接拒收。

预算语义（对齐框架 extra.timeout_s）：overall_budget_s 为本次注册的整体
秒数预算——单会话超时取 min(session_timeout_s, 剩余)，剩余不足一轮
（< MIN_ATTEMPT_BUDGET_S）即终止，不再开新会话。
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from register_core.contracts import RegisterResult, normalize_error_kind

from . import sliders as S
from .complete import run_complete_session
from .mailbox import OneMailLinkMailbox
from .signup import run_signup_session

DEFAULT_ATTEMPTS = 3
MIN_ATTEMPT_BUDGET_S = 90.0  # 剩余预算低于此值不再开新会话（会话+等信都来不及）


def verify_link_sha8(link: str) -> str:
    return hashlib.sha256(link.encode()).hexdigest()[:8]


def validate_this_run(result: RegisterResult) -> bool:
    """ok=True 的结果必须携带本会话标记；否则视为历史产物伪造，拒收。"""
    if not result.ok:
        return True  # 失败结果无此要求
    arts = result.artifacts or {}
    return bool(
        arts.get("cookie_id")
        and arts.get("signup_posted_at")
        and arts.get("verify_link_sha8")
        and arts.get("token_email") == result.email
        and result.secret
    )


def _failure(
    kind: str, error: str, email: str, password: str, arts: dict[str, Any]
) -> RegisterResult:
    return RegisterResult(
        ok=False, provider="zai", email=email, password=password,
        secret="", secret_kind="none",
        error=error, error_kind=normalize_error_kind(kind), artifacts=arts,
    )


def register_zai_account(
    *,
    email: str,
    password: str,
    full_name: str,
    mailbox: OneMailLinkMailbox,
    attempts: int = DEFAULT_ATTEMPTS,
    link_wait_s: float = 150,
    session_timeout_s: float = 480.0,
    overall_budget_s: float | None = None,
    log=None,
) -> RegisterResult:
    """跑完整两段式注册；返回 RegisterResult（hub join 不在本层，属操作员显式命令）。"""

    def say(msg: str) -> None:
        if log:
            log(msg)

    deadline = time.time() + overall_budget_s if overall_budget_s else None

    def remaining_s() -> float | None:
        return None if deadline is None else max(0.0, deadline - time.time())

    say(f"baseline 扫描已有验证链接（{email}）…")
    baseline = mailbox.scan_existing_verify_links(email)
    say(f"baseline 链接 {len(baseline)} 条（只认其后的新链接）")

    arts: dict[str, Any] = {
        "email_mode": "catch_all_invention",
        "attempts_planned": attempts,
        "session_timeout_s": session_timeout_s,
        "link_wait_s": link_wait_s,
        "overall_budget_s": overall_budget_s,
        "attempts": [],
    }

    for i in range(1, attempts + 1):
        rem = remaining_s()
        if rem is not None and rem < MIN_ATTEMPT_BUDGET_S:
            say(f"预算耗尽（剩余 {rem:.0f}s < {MIN_ATTEMPT_BUDGET_S:.0f}s）→ 终止重试")
            break
        session_timeout = (min(session_timeout_s, rem)
                           if rem is not None else session_timeout_s)
        cookie_id = f"zai-reg-{secrets.token_hex(3)}"
        say(f"--- attempt {i}/{attempts}（换新会话 cookie_id={cookie_id}, "
            f"session_timeout={session_timeout:.0f}s"
            + (f", 预算剩余 {rem:.0f}s" if rem is not None else "") + "）---")
        att: dict[str, Any] = {"n": i, "cookie_id": cookie_id,
                               "started_at": time.strftime("%H:%M:%S")}

        signup = run_signup_session(email, password, full_name,
                                    cookie_id=cookie_id, timeout_s=session_timeout)
        att["signup_posted"] = signup.signup_posted
        if not signup.signup_posted:
            say(f"attempt {i}: signup 未发出（滑块/验证门未过）→ 换新会话")
            arts["attempts"].append(att)
            continue

        att["signup_posted_at"] = time.strftime("%H:%M:%S")
        rem2 = remaining_s()
        link_wait = min(link_wait_s, rem2) if rem2 is not None else link_wait_s
        link_wait = max(link_wait, 0.0)
        say(f"attempt {i}: signup POST 200（本会话收据）→ 等验证链接（≤{link_wait:.0f}s）")
        link = mailbox.wait_verify_link(email, baseline=baseline,
                                        timeout_s=link_wait, log=say)
        if not link:
            say(f"attempt {i}: 无新验证链接 → 换新会话")
            arts["attempts"].append(att)
            continue

        att["verify_link_sha8"] = verify_link_sha8(link)
        say(f"attempt {i}: 验证链接到信（sha8={att['verify_link_sha8']}）→ 补完注册")
        # 已消费的链接进 baseline：本轮补完失败后，后续轮只认更新的链接
        baseline.add(link)
        rem3 = remaining_s()
        complete_timeout = (min(session_timeout_s, rem3)
                            if rem3 is not None else session_timeout_s)
        comp = run_complete_session(link, email, password, full_name,
                                    cookie_id=cookie_id, timeout_s=complete_timeout)
        arts["attempts"].append(att)
        token = comp.token
        token_email = S.jwt_email(token)
        if token and token_email.lower() == email.lower():
            say(f"SUCCESS: 真实 JWT（{token_email}, len={len(token)}）")
            arts.update({
                "cookie_id": cookie_id,
                "signup_posted_at": att["signup_posted_at"],
                "verify_link_sha8": att["verify_link_sha8"],
                "token_email": token_email,
                "token_len": len(token),
                "attempts_used": i,
            })
            return RegisterResult(
                ok=True, provider="zai", email=email, password=password,
                secret=token, secret_kind="jwt",
                error="", error_kind="", artifacts=arts,
            )
        say(f"attempt {i}: 补完未产出本号真实 token（{token_email or 'guest/无'}）")

    arts["attempts_used"] = attempts
    if any(a.get("signup_posted") for a in arts["attempts"]):
        return _failure("mail_miss", "signup 已发出但始终未收到新验证链接（或补完失败）",
                        email, password, arts)
    return _failure("captcha", "所有会话均未通过滑块/验证门（signup 从未发出）",
                    email, password, arts)
