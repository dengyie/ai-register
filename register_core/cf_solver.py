"""Cloudflare shield / Turnstile solver — captcha.run-compatible v2 client.

Talks to the self-hosted slidex captcha station (``slidex/service``, 0.12.3+),
which exposes ``turnstile`` and ``cf_clearance`` task types on the same
``POST /v2/tasks/`` + ``GET /v2/tasks/{id}`` protocol the Outlook PX path
already uses (``providers/outlook/protocol/px_client.py``).

**Same-exit-IP iron law** — read before changing anything
---------------------------------------------------------
A Turnstile token / ``cf_clearance`` cookie is bound to the egress IP and
User-Agent that solved it. Every caller must therefore pass the *upstream*
proxy string with credentials (the same sticky proxy the browser uses), never
the local auth-bridge URL (``127.0.0.1``). The returned ``user_agent`` must be
sent on the solved HTTP side, and the browser must keep its own UA aligned.
Mismatch = still 403, no error message that explains why. See docs/ANTIBAN.md.

Configuration (env)::

    CAPTCHA_RUN_KEY   Bearer token accepted by the station
                      (``SLIDEX_CAPTCHA_KEYS`` on the station side)
    CAPTCHA_RUN_BASE  override station base; when unset the primary + cold
                      standby mesh addresses below are tried in order
    CF_SOLVER_BASE    dedicated override taking precedence over CAPTCHA_RUN_BASE

Failure taxonomy: solver-side failures map to ``kind="captcha"`` (never
quarantines an egress — see ``register_core.util.proxy``), transport failures
to ``kind="network"``. Callers decide retry/fallback from ``retryable``.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("register_core.cf_solver")

# 自建打码站：主 mesh-huawei，冷备 mesh-mango2（HA 拓扑见 vault 运维页）。
# 公网 captcha.run 没有 turnstile/cf_clearance 类型，不能当默认值。
DEFAULT_STATION_BASES: tuple[str, ...] = (
    "http://10.144.144.9:8077",
    "http://10.144.144.88:8077",
)

DEFAULT_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 2.0
REQUEST_TIMEOUT_S = 15.0


class CFSolverError(RuntimeError):
    """CF solve failure. ``kind`` plugs into the contracts error taxonomy."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "captcha",
        retryable: bool = True,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.detail = detail


@dataclass
class CFClearanceResult:
    """``cf_clearance`` cookie bound to (exit IP, User-Agent, proxy)."""

    cookie_value: str
    user_agent: str = ""
    proxy: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.cookie_value)


def _api_key() -> str:
    return os.environ.get("CAPTCHA_RUN_KEY", "").strip()


def _station_bases() -> list[str]:
    override = os.environ.get("CF_SOLVER_BASE", "").strip() or os.environ.get(
        "CAPTCHA_RUN_BASE", ""
    ).strip()
    if override:
        return [override.rstrip("/")]
    return [base.rstrip("/") for base in DEFAULT_STATION_BASES]


def solver_configured() -> bool:
    """True when a station key exists — callers gate on this before enabling."""
    return bool(_api_key())


def _proxy_fields(proxy: str) -> dict[str, Any]:
    """Split ``http://user:pass@host:port`` into the station's proxy object."""
    from urllib.parse import urlparse

    proxy = (proxy or "").strip()
    if not proxy:
        return {}
    try:
        parsed = urlparse(proxy)
    except ValueError:
        return {}
    fields: dict[str, Any] = {
        "host": parsed.hostname or "",
        "port": parsed.port or 0,
    }
    if parsed.username:
        fields["login"] = parsed.username
    if parsed.password:
        fields["password"] = parsed.password
    return fields


def _redact_proxy(proxy: str) -> str:
    """host:port only — credentials must never reach a log line."""
    fields = _proxy_fields(proxy)
    if not fields:
        return ""
    return f"{fields['host']}:{fields['port']}"


def _session_for(base: str) -> Any:
    """Control-channel session: direct, HTTP/1.1 on plaintext station bases.

    The station runs uvicorn/h11: a chrome-impersonated curl_cffi session
    probes h2c upgrade on plaintext http:// and h11 discards the body → 422
    for a valid payload (live-verified in px_client._session_for). The
    solving traffic itself flows through the task ``proxy`` field, so this
    control channel stays direct.
    """
    from providers.outlook.protocol.session import create_session

    session = create_session("")
    if base.startswith("http://"):
        try:
            from curl_cffi.const import CurlHttpVersion

            session.http_version = CurlHttpVersion.V1_1
        except Exception as exc:  # pragma: no cover - requests fallback session
            log.debug("cf_solver: http_version pin skipped: %s", exc)
    return session


def _json(resp: Any) -> dict[str, Any]:
    if resp is None:
        return {}
    try:
        body = resp.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _auth_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {_api_key()}"}


def _create_task(
    session: Any, base: str, payload: dict[str, Any], *, timeout: float
) -> str:
    from providers.outlook.protocol.session import request_with_retry

    resp, err = request_with_retry(
        session,
        "POST",
        f"{base}/v2/tasks/",
        retry_attempts=2,
        timeout=timeout,
        headers={**_auth_headers(), "content-type": "application/json"},
        json=payload,
    )
    if resp is None:
        raise CFSolverError(
            "cf_solver_station_unreachable",
            kind="network",
            retryable=True,
            detail=f"{base}: {err}",
        )
    status = int(getattr(resp, "status_code", 0) or 0)
    body = _json(resp)
    task_id = str(body.get("taskId") or "")
    if status == 401:
        raise CFSolverError(
            "cf_solver_unauthorized",
            retryable=False,
            detail="CAPTCHA_RUN_KEY must match the station SLIDEX_CAPTCHA_KEYS",
        )
    if not task_id:
        raise CFSolverError(
            "cf_solver_task_create_failed",
            retryable=status >= 500,
            detail=f"http={status} {str(body.get('error') or body)[:160]}",
        )
    return task_id


def _poll_task(
    session: Any,
    base: str,
    task_id: str,
    *,
    captcha_type: str,
    timeout_s: float,
    timeout: float,
) -> dict[str, Any]:
    from providers.outlook.protocol.session import request_with_retry

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp, err = request_with_retry(
            session,
            "GET",
            f"{base}/v2/tasks/{task_id}",
            retry_attempts=1,
            timeout=timeout,
            params={"captchaType": captcha_type},
            headers=_auth_headers(),
        )
        body = _json(resp)
        status = str(body.get("status") or "").lower()
        if status == "success":
            solution = body.get("response") or body.get("solution") or {}
            return solution if isinstance(solution, dict) else {}
        if status in ("fail", "failed", "error"):
            raise CFSolverError(
                "cf_solver_task_failed",
                retryable=True,  # solver-side failure: swap egress / retry
                detail=str(body.get("error") or status)[:160],
            )
        if resp is None:
            # Control channel blip: the station task may still be running.
            log.debug("cf_solver: poll transport error tolerated: %s", err)
        time.sleep(POLL_INTERVAL_S)
    raise CFSolverError(
        "cf_solver_task_timeout",
        retryable=True,
        detail=f"{captcha_type} task {task_id} not solved in {timeout_s:.0f}s",
    )


def _run_task(
    payload: dict[str, Any],
    *,
    captcha_type: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Create + poll one task, failing over across station bases on transport."""
    key = _api_key()
    if not key:
        raise CFSolverError(
            "cf_solver_unconfigured",
            retryable=False,
            detail="set CAPTCHA_RUN_KEY (slidex station key) to enable CF solving",
        )
    if timeout_s <= 0:
        raise ValueError(f"timeout_s must be positive, got {timeout_s}")

    last: CFSolverError | None = None
    for base in _station_bases():
        session = _session_for(base)
        try:
            task_id = _create_task(
                session, base, payload, timeout=REQUEST_TIMEOUT_S
            )
        except CFSolverError as exc:
            # Only transport-level create failures fail over; auth/config
            # errors would repeat on the standby for the same reason.
            if exc.kind != "network":
                raise
            last = exc
            log.warning("cf_solver: station %s unreachable, trying standby", base)
            continue
        log.info(
            "cf_solver: task created type=%s task_id=%s", captcha_type, task_id
        )
        return _poll_task(
            session,
            base,
            task_id,
            captcha_type=captcha_type,
            timeout_s=timeout_s,
            timeout=REQUEST_TIMEOUT_S,
        )
    raise last or CFSolverError("cf_solver_no_station", kind="network")


def solve_turnstile_token(
    url: str,
    sitekey: str,
    *,
    proxy: str = "",
    action: str = "",
    cdata: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Solve one Turnstile widget and return the token string.

    ``proxy`` must be the registration's upstream proxy **with credentials**
    (same-exit-IP iron law). Empty ``proxy`` solves through the station's own
    egress — only correct for local smoke tests, never for a registration.
    """
    url = (url or "").strip()
    sitekey = (sitekey or "").strip()
    if not url:
        raise ValueError("url is required for turnstile")
    if not sitekey:
        raise ValueError("sitekey is required for turnstile")

    payload: dict[str, Any] = {
        "captchaType": "turnstile",
        "url": url,
        "sitekey": sitekey,
    }
    if action:
        payload["action"] = action
    if cdata:
        payload["cdata"] = cdata
    proxy_fields = _proxy_fields(proxy)
    if proxy_fields:
        payload["proxy"] = proxy_fields

    solution = _run_task(
        payload, captcha_type="turnstile", timeout_s=timeout_s
    )
    token = str(solution.get("token") or "")
    if not token:
        raise CFSolverError(
            "cf_solver_empty_token",
            retryable=True,
            detail=f"solution keys={sorted(solution)}",
        )
    log.info(
        "cf_solver: turnstile solved url=%s proxy=%s len=%d",
        url,
        _redact_proxy(proxy) or "direct",
        len(token),
    )
    return token


def solve_cf_clearance(
    url: str,
    *,
    proxy: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> CFClearanceResult:
    """Solve the Cloudflare challenge and return the ``cf_clearance`` cookie.

    The cookie is valid only from the returned ``proxy`` egress with the
    returned ``user_agent``; anything else is still 403.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("url is required for cf_clearance")

    payload: dict[str, Any] = {"captchaType": "cf_clearance", "url": url}
    proxy_fields = _proxy_fields(proxy)
    if proxy_fields:
        payload["proxy"] = proxy_fields

    solution = _run_task(
        payload, captcha_type="cf_clearance", timeout_s=timeout_s
    )
    cookies = solution.get("cookies")
    cookie_value = ""
    if isinstance(cookies, dict):
        cookie_value = str(
            cookies.get("cf_clearance")
            or cookies.get("value")
            or ""
        )
    result = CFClearanceResult(
        cookie_value=cookie_value,
        user_agent=str(solution.get("userAgent") or ""),
        proxy=str(solution.get("proxy") or proxy),
    )
    if not result.ok:
        raise CFSolverError(
            "cf_solver_empty_cookie",
            retryable=True,
            detail=f"solution keys={sorted(solution)}",
        )
    log.info(
        "cf_solver: cf_clearance solved url=%s proxy=%s",
        url,
        _redact_proxy(proxy) or "direct",
    )
    return result
