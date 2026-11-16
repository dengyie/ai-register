"""z.ai (ZCode Coding Plan) provider — in-process，自持 one-mail 邮箱读取。

black-box 模式：邮箱分配 = catch-all 域前缀发明；收信读取 = one-mail
unified admin API 按 to_addr 客户端过滤（与地址行/JWT 解耦，immune to
one-mail 写入侧 name 改写）。因此**不接受外部 EmailSource**（传了即 fail-fast）。

滑块求解依赖 slidex（含 aliyun_captcha_js provider，0.11.x）+ playwright
有头 Chromium——须在 GUI 会话下运行；解释器由 run-register.sh 解析。

预算语义（框架约定）：CLI/extra 的 ``timeout_s`` 为单次注册整体预算，
经 flow 在 attempt 间检查剩余并收缩单会话超时。

成功产物：chat.z.ai 真实 JWT（secret_kind="jwt"）。hub（zcode2api）入池属
操作员显式命令（providers/zai/join-hub.sh），绝不自动写生产池。
"""

from __future__ import annotations

import os
import random
import secrets
import sys
from pathlib import Path
from typing import Any

from register_core.contracts import RegisterResult, normalize_error_kind
from register_core.email.base import EmailSource
from register_core.errors import FailFastError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

__all__ = ["ZaiProvider"]


class ZaiProvider:
    """chat.z.ai 两段式注册（signup → verify link → Complete Registration）。

    成功收据链 immune to slidex 假成功（signup POST NET tap + 验证链接到信
    + JWT email claim 三重门禁）；会话级换新重试（cookie_id 轮换）。
    """

    name = "zai"

    def __init__(
        self,
        *,
        domain: str | None = None,
        handle: str | None = None,
        attempts: int | None = None,
        link_wait_s: float | None = None,
        session_timeout_s: float | None = None,
        timeout_s: float | None = None,
        onemail_base_url: str | None = None,
        onemail_admin_key: str | None = None,
        hub_url: str | None = None,
        hub_key: str | None = None,
        **_: Any,
    ) -> None:
        self.domain = str(
            domain or os.environ.get("ZAI_DOMAIN", "dengyie.tech")).strip()
        self.handle = str(handle or os.environ.get("ZAI_HANDLE", "")).strip() or None
        self.attempts = int(
            attempts or os.environ.get("ZAI_ATTEMPTS") or 3)
        self.link_wait_s = float(
            link_wait_s or os.environ.get("ZAI_LINK_WAIT") or 150)
        self.session_timeout_s = float(
            session_timeout_s or os.environ.get("ZAI_SESSION_TIMEOUT") or 480)
        # 框架约定：extra.timeout_s（CLI --timeout）= 单次注册整体预算；0/缺省 = 不设整体预算
        self.timeout_s = float(timeout_s or os.environ.get("ZAI_BUDGET") or 0) or None
        self.onemail_base_url = str(
            onemail_base_url
            or os.environ.get("ZAI_MAIL_API")
            or os.environ.get("ONEMAIL_API_BASE")
            or "https://mail-api.mangoqwq.cc.cd").rstrip("/")
        self.onemail_admin_key = str(
            onemail_admin_key
            or os.environ.get("ZAI_ADMIN_KEY")
            or os.environ.get("ONEMAIL_ADMIN_KEY")
            or "").strip()
        self.hub_url = str(
            hub_url or os.environ.get("ZAI_HUB_URL")
            or "https://zcode.mangoqwq.com").rstrip("/")
        self.hub_key = str(
            hub_key or os.environ.get("ZAI_HUB_KEY")
            or "").strip()

    def register_one(
        self,
        *,
        email_source: EmailSource | None = None,
        extra: dict[str, Any] | None = None,
    ) -> RegisterResult:
        from providers.zai.protocol.flow import register_zai_account, validate_this_run
        from providers.zai.protocol.mailbox import OneMailLinkMailbox
        from providers.zai.protocol.naming import generate_unique_handle, hub_label

        extra = dict(extra or {})
        if email_source is not None:
            raise FailFastError(
                "zai manages its own one-mail catch-all mailbox "
                "(unified admin API, to_addr-filtered); do not pass --email-source")
        if not self.onemail_admin_key:
            raise FailFastError(
                "zai provider requires ONEMAIL_ADMIN_KEY / ZAI_ADMIN_KEY "
                "(unified admin API is the mailbox read path)")
        if not self.hub_key:
            raise FailFastError(
                "zai provider requires ZAI_HUB_KEY (hub admin key: "
                "naming uniqueness preflight + pool verify both need it)")
        if os.environ.get("DISPLAY", "") == "" and sys.platform == "linux":
            # 有头 Chromium 在无 GUI 主机需 xvfb；此处显式 fail-fast 防黑跑
            raise FailFastError(
                "zai signup needs a headed Chromium: no DISPLAY on linux "
                "(run under xvfb-run or a GUI session)")

        rnd = random.Random(secrets.randbits(64))
        handle = self.handle or str(extra.get("handle") or "").strip()
        if not handle:
            handle = generate_unique_handle(
                self.domain,
                onemail_base_url=self.onemail_base_url,
                onemail_admin_key=self.onemail_admin_key,
                hub_url=self.hub_url,
                hub_key=self.hub_key,
                rnd=rnd,
            )
        email = f"{handle}@{self.domain}"
        label = str(extra.get("hub_label") or hub_label(handle, self.domain))
        password = str(extra.get("password") or "").strip() or "".join(
            secrets.choice("abcdefghijklmnopqrstuvwxyz"
                           "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
            for _ in range(16))

        def log(msg: str) -> None:
            print(f"[zai] {msg}", flush=True)

        log(f"handle={handle} email={email} hub_label={label}")
        mailbox = OneMailLinkMailbox(
            base_url=self.onemail_base_url, admin_key=self.onemail_admin_key,
            domain=self.domain)
        budget = float(extra.get("timeout_s") or self.timeout_s or 0) or None
        result = register_zai_account(
            email=email, password=password, full_name=handle, mailbox=mailbox,
            attempts=self.attempts, link_wait_s=self.link_wait_s,
            session_timeout_s=self.session_timeout_s,
            overall_budget_s=budget, log=log)
        result.artifacts.update({
            "handle": handle,
            "hub_label": label,
            "hub_join": "operator-explicit (providers/zai/join-hub.sh)",
            "note": f"handle={handle} hub_label={label} attempts_used="
                    f"{result.artifacts.get('attempts_used')}",
        })
        if result.ok and not validate_this_run(result):
            # 防伪护栏：标记缺失视为内部错误，绝不放行伪造成功
            result.ok = False
            result.secret = ""
            result.secret_kind = "none"
            result.error = "this-run markers missing (internal guard)"
            result.error_kind = normalize_error_kind("fatal")
        return result
