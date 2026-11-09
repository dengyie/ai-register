"""PerimeterX press-token solver for the Outlook signup flow.

One-shot production path: a captcha-solving service (captcha.run) returns the
press token quadruple (px3/pxde/pxvid/pxcts) that risk/verify #2 embeds in
``challengeSolution``. Local solver stacks (wasm/JS emulation) are a stretch
goal and deliberately absent here.

Same-IP discipline: the service must resolve through the SAME sticky proxy the
registration uses — the PX collector validates egress IP against the risk
verify submission. See docs/ANTIBAN.md.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from .constants import PX_APP_ID
from .session import request_with_retry

CAPTCHA_RUN_API_BASE = "https://apicn.captcha.run"
CAPTCHA_RUN_API_BASE_GLOBAL = "https://api.captcha-run.com"

# press polls every 3s up to 180s; silent every 2s up to 90s (service SLAs).
PRESS_POLL_INTERVAL_S = 3.0
PRESS_POLL_TIMEOUT_S = 180.0
SILENT_POLL_INTERVAL_S = 2.0
SILENT_POLL_TIMEOUT_S = 90.0


class PXSolverError(RuntimeError):
    """Captcha service failure (kind=captcha for the adapter)."""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


@dataclass
class PXPressToken:
    px3: str = ""
    pxde: str = ""
    pxvid: str = ""
    pxcts: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.px3)


@dataclass
class PXChallengeContext:
    """Facts the solver needs, all captured from ServerData / risk responses."""

    uaid: str = ""
    uuid: str = ""
    vid: str = ""
    challenge_url: str = ""
    country: str = "US"
    timezone: str = "America/New_York"
    user_agent: str = ""
    cookies: dict[str, str] = field(default_factory=dict)


def _api_key() -> str:
    return os.environ.get("CAPTCHA_RUN_KEY", "").strip()


def _api_bases() -> list[str]:
    override = os.environ.get("CAPTCHA_RUN_BASE", "").strip()
    if override:
        return [override]
    return [CAPTCHA_RUN_API_BASE, CAPTCHA_RUN_API_BASE_GLOBAL]


def _proxy_fields(proxy: str) -> dict[str, Any]:
    """Split a http://user:pass@host:port URL into the service's proxy object."""
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


def _create_task(
    session: Any,
    base: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> str:
    resp, err = request_with_retry(
        session,
        "POST",
        f"{base}/v2/tasks/",
        retry_attempts=2,
        timeout=timeout,
        headers={
            "authorization": f"Bearer {_api_key()}",
            "content-type": "application/json",
        },
        json=payload,
    )
    body = {}
    if resp is not None:
        try:
            body = resp.json() if isinstance(resp.json(), dict) else {}
        except Exception:
            body = {}
    task_id = str(body.get("taskId") or "")
    if not task_id:
        raise PXSolverError("captcha_task_create_failed", detail=err or str(body)[:120])
    return task_id


def _poll_task(
    session: Any,
    base: str,
    task_id: str,
    *,
    captcha_type: str,
    interval_s: float,
    timeout_s: float,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp, _err = request_with_retry(
            session,
            "GET",
            f"{base}/v2/tasks/{task_id}",
            retry_attempts=2,
            timeout=timeout,
            params={"captchaType": captcha_type},
            headers={"authorization": f"Bearer {_api_key()}"},
        )
        body = {}
        if resp is not None:
            try:
                body = resp.json() if isinstance(resp.json(), dict) else {}
            except Exception:
                body = {}
        status = str(body.get("status") or "").lower()
        if status == "success":
            solution = body.get("response") or body.get("solution") or {}
            return solution if isinstance(solution, dict) else {}
        if status in ("fail", "failed", "error"):
            raise PXSolverError(
                "captcha_task_failed", detail=str(body.get("error") or status)[:120]
            )
        time.sleep(interval_s)
    raise PXSolverError("captcha_task_timeout", detail=f"captchaType={captcha_type}")


def _token_from_solution(solution: dict[str, Any]) -> PXPressToken:
    press = solution.get("pressToken")
    if isinstance(press, str) and press:
        # pressToken carries the full quadruple in one packed string on some
        # service plans; the risk verify accepts it directly as px3.
        return PXPressToken(px3=press)
    return PXPressToken(
        px3=str(solution.get("px3") or solution.get("_px3") or ""),
        pxde=str(solution.get("pxde") or solution.get("_pxde") or ""),
        pxvid=str(solution.get("pxvid") or solution.get("_pxvid") or ""),
        pxcts=str(solution.get("pxcts") or solution.get("_pxcts") or ""),
    )


def solve_press_token(
    ctx: PXChallengeContext,
    *,
    proxy: str,
    timeout: float = 30.0,
) -> PXPressToken:
    """Solve one press challenge via captcha.run (press → silent fallback).

    Raises PXSolverError when no key is configured or every variant fails.
    """
    key = _api_key()
    if not key:
        raise PXSolverError(
            "captcha_solver_unconfigured",
            detail="set CAPTCHA_RUN_KEY (captcha.run) for the protocol path",
        )
    session = _session_for(proxy)
    payload: dict[str, Any] = {
        # slidex (self-hosted, captcha.run-compatible) requires appId even for
        # silent tasks; real captcha.run accepts it on both variants too.
        "appId": PX_APP_ID,
        "captchaType": "PxCaptcha2",
        "uaid": ctx.uaid,
        "uuid": ctx.uuid,
        "vid": ctx.vid,
        "userAgent": ctx.user_agent,
        "country": ctx.country,
        "timezone": ctx.timezone,
        "developer": "grok-register",
        # silent variant needs the protected page too: PX only mints a token
        # where the sensor for this appId actually runs. Without it a
        # captcha.run-compatible station answers 422 and the fallback dies.
        "websiteURL": "https://signup.live.com/",
    }
    proxy_fields = _proxy_fields(proxy)
    if proxy_fields:
        payload["proxy"] = proxy_fields
    last_error: PXSolverError | None = None
    for base in _api_bases():
        # press variant: full challenge context (appId moved to base payload).
        try:
            task_id = _create_task(
                session,
                base,
                {
                    **payload,
                    "websiteURL": "https://signup.live.com/",
                    "challengeUrl": ctx.challenge_url,
                    "cookies": ctx.cookies,
                },
                timeout=timeout,
            )
            solution = _poll_task(
                session,
                base,
                task_id,
                captcha_type="press",
                interval_s=PRESS_POLL_INTERVAL_S,
                timeout_s=PRESS_POLL_TIMEOUT_S,
                timeout=timeout,
            )
            token = _token_from_solution(solution)
            if token.ok:
                return token
            last_error = last_error or PXSolverError("captcha_empty_press_token")
        except PXSolverError as exc:
            last_error = last_error or exc
        # silent variant fallback: challengeUrl absent → slidex infers
        # mode=silent from websiteURL alone.
        try:
            task_id = _create_task(
                session, base, dict(payload, captchaType="PxCaptcha2"), timeout=timeout
            )
            solution = _poll_task(
                session,
                base,
                task_id,
                captcha_type="silent",
                interval_s=SILENT_POLL_INTERVAL_S,
                timeout_s=SILENT_POLL_TIMEOUT_S,
                timeout=timeout,
            )
            token = _token_from_solution(solution)
            if token.ok:
                return token
            last_error = last_error or PXSolverError("captcha_empty_silent_token")
        except PXSolverError as exc:
            last_error = last_error or exc
    raise last_error or PXSolverError("captcha_all_variants_failed")


def solve_silent_token(
    ctx: PXChallengeContext,
    *,
    proxy: str,
    timeout: float = 30.0,
) -> PXPressToken:
    """Solve the silent variant: sensor token minted on the protected page
    (signup.live.com), no challenge iframe involved.

    Used for the z_style verify #1 body (riskProviderMetadata with
    px3/pxde/pxvid) — a clean silent score can clear verify #1 without ever
    reaching the press challenge. Same service contract as the press path:
    slidex-style stations infer mode=silent from websiteURL alone.
    """
    key = _api_key()
    if not key:
        raise PXSolverError(
            "captcha_solver_unconfigured",
            detail="set CAPTCHA_RUN_KEY (captcha.run) for the silent path",
        )
    session = _session_for(proxy)
    payload: dict[str, Any] = {
        "appId": PX_APP_ID,
        "captchaType": "PxCaptcha2",
        "uaid": ctx.uaid,
        "uuid": "",
        "vid": "",
        "userAgent": ctx.user_agent,
        "country": ctx.country,
        "timezone": ctx.timezone,
        "developer": "grok-register",
        "websiteURL": "https://signup.live.com/",
    }
    proxy_fields = _proxy_fields(proxy)
    if proxy_fields:
        payload["proxy"] = proxy_fields
    last_error: PXSolverError | None = None
    for base in _api_bases():
        try:
            task_id = _create_task(session, base, dict(payload), timeout=timeout)
            solution = _poll_task(
                session,
                base,
                task_id,
                captcha_type="silent",
                interval_s=SILENT_POLL_INTERVAL_S,
                timeout_s=SILENT_POLL_TIMEOUT_S,
                timeout=timeout,
            )
            token = _token_from_solution(solution)
            if token.ok:
                return token
            last_error = last_error or PXSolverError("captcha_empty_silent_token")
        except PXSolverError as exc:
            last_error = last_error or exc
    raise last_error or PXSolverError("captcha_silent_all_bases_failed")


def apply_px_cookies(session: Any, token: "PXPressToken", *, preserve_vid: str = "") -> None:
    """Seed the solved PX tokens into the HTTP session's cookie jar.

    Upstream z_style fact (2026-11-08): the silent/press tokens ride BOTH the
    risk/verify body AND the session cookies — the risk engine validates the
    verify submission against the `_px*` cookies the session carries. Missing
    cookies make a body-carrying verify read as token-less (observed live:
    silent metadata in body + no `_px3` cookie → 403 riskBlock anyway).
    Domains: .live.com / .microsoftonline.com / .hsprotect.net (the three
    origins the signup chain touches).
    """
    mapping = {
        "_px3": token.px3,
        "_pxde": token.pxde,
        "_pxvid": preserve_vid or token.pxvid,
        "pxcts": token.pxcts,
    }
    for name, value in mapping.items():
        if not value:
            continue
        for domain in (".live.com", ".microsoftonline.com", ".hsprotect.net"):
            try:
                session.cookies.set(name, str(value), domain=domain)
            except Exception:  # noqa: BLE001 — requests vs curl_cffi jar diffs
                session.cookies.set(name, str(value))


def _session_for(proxy: str) -> Any:
    """Solver API session — direct (no egress proxy), HTTP/1.1 on plaintext.

    Two constraints, both live-verified (2026-11-08, gz-cvm n=2/n=3):
    - No egress proxy: the same-IP iron law applies to the SOLVED PAGE's
      collector traffic, which the service itself executes through the task
      ``proxy`` field. The create/poll calls are our own control channel; when
      the solver is a mesh-internal station (slidex on 10.144.144.8), routing
      them through an external egress breaks the route entirely (curl 52).
    - HTTP/1.1 forced on plaintext http:// bases: a chrome-impersonated
      curl_cffi session probes h2c upgrade on plaintext, and uvicorn's h11
      rejects the upgrade while discarding the request body — the service
      then answers 422 "request body must be valid JSON" for a perfectly
      valid payload. Only affects http:// (real captcha.run is https and
      keeps the full chrome fingerprint).
    """
    from .session import create_session

    session = create_session("")
    if _api_bases() and _api_bases()[0].startswith("http://"):
        from curl_cffi.const import CurlHttpVersion

        session.http_version = CurlHttpVersion.V1_1
    return session
