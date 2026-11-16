"""Typed failures for the register pipeline.

Fatal vs retriable:
  FailFastError  — stop the whole batch (alias exhausted, config broken, captcha infra dead)
  MailMissError  — this mailbox got no OTP; pipeline may allocate another (bounded)
  CaptchaError   — captcha solve failed for this attempt
  ProviderError  — signup page / product flow failed
  VerifyError    — account created but capability probe failed
"""

from __future__ import annotations


class RegisterCoreError(Exception):
    """Base for framework errors."""


class FailFastError(RegisterCoreError):
    """Unrecoverable for this batch — do not spin or open more browsers."""


class MailMissError(RegisterCoreError):
    """OTP not received for the allocated mailbox (bounded retry OK)."""

    def __init__(
        self,
        message: str = "",
        *,
        diagnostics: object | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class CaptchaError(RegisterCoreError):
    """Captcha challenge failed for this attempt."""


class ProviderError(RegisterCoreError):
    """Product registration flow failed."""


class QuotaLimitedError(RegisterCoreError):
    """上游返回 503 配额/限流协议（D1_DAILY_*_LIMIT + Retry-After）。

    与 ProviderError 的区别：这是**有明确恢复时间**的暂时态——在 OTP 轮询
    窗口内按 Retry-After 退避重试是正确行为，而不是立即放弃整个注册。
    retry_after 取服务器 Retry-After 头（秒），缺省 0 表示立即重试。
    """

    def __init__(self, message: str = "", *, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after))


class VerifyError(RegisterCoreError):
    """Post-register verification failed."""
