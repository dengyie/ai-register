#!/usr/bin/env python3
"""Offline tests for register_core.cf_solver (no station, no network).

Patches ``request_with_retry`` inside ``providers.outlook.protocol.session`` —
cf_solver imports it lazily, so the patch covers every call site. Covers the
create/poll state machine, proxy passthrough, HTTP/1.1 pinning on plaintext
bases, station failover, and the unconfigured-key deterministic failure.
"""

from __future__ import annotations

from typing import Any

import pytest

import providers.outlook.protocol.session as session_mod
from register_core import cf_solver


class _Resp:
    def __init__(self, status_code: int = 200, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self) -> Any:
        return self._body


def _patch_session(monkeypatch: pytest.MonkeyPatch, handler) -> list[tuple]:
    """Route every request_with_retry call through ``handler``; record calls."""
    calls: list[tuple] = []

    def fake(session, method, url, *, retry_attempts=3, timeout=30.0, **kwargs):
        calls.append((method, url, kwargs))
        return handler(method, url, kwargs)

    monkeypatch.setattr(session_mod, "request_with_retry", fake)
    return calls


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPTCHA_RUN_KEY", "sk-test")
    monkeypatch.delenv("CAPTCHA_RUN_BASE", raising=False)
    monkeypatch.delenv("CF_SOLVER_BASE", raising=False)
    # Poll loop sleeps between attempts; keep the suite fast.
    monkeypatch.setattr(cf_solver, "POLL_INTERVAL_S", 0.0)


# ────────────────────── turnstile ──────────────────────


def test_turnstile_create_poll_success(monkeypatch):
    def handler(method, url, kwargs):
        if method == "POST":
            return _Resp(200, {"taskId": "t_1"}), ""
        return _Resp(200, {"status": "success", "response": {"token": "tok-1"}}), ""

    calls = _patch_session(monkeypatch, handler)
    token = cf_solver.solve_turnstile_token(
        "https://example.com", "0x4AAA", proxy="http://u:p@1.2.3.4:8000"
    )
    assert token == "tok-1"
    post = calls[0]
    assert post[1].endswith("/v2/tasks/")
    assert post[2]["json"]["captchaType"] == "turnstile"
    assert post[2]["json"]["sitekey"] == "0x4AAA"
    # Same-exit-IP iron law: credentials must reach the station as an object.
    assert post[2]["json"]["proxy"] == {
        "host": "1.2.3.4", "port": 8000, "login": "u", "password": "p",
    }
    assert post[2]["headers"]["authorization"] == "Bearer sk-test"
    get = calls[1]
    assert get[1].endswith("/v2/tasks/t_1")
    assert get[2]["params"] == {"captchaType": "turnstile"}


def test_turnstile_action_and_cdata_forwarded(monkeypatch):
    seen = {}

    def handler(method, url, kwargs):
        if method == "POST":
            seen.update(kwargs["json"])
            return _Resp(200, {"taskId": "t_1"}), ""
        return _Resp(200, {"status": "success", "response": {"token": "t"}}), ""

    _patch_session(monkeypatch, handler)
    cf_solver.solve_turnstile_token(
        "https://example.com", "sk", action="manage", cdata="c-1"
    )
    assert seen["action"] == "manage"
    assert seen["cdata"] == "c-1"
    assert "proxy" not in seen


def test_turnstile_task_fail_is_captcha_retryable(monkeypatch):
    def handler(method, url, kwargs):
        if method == "POST":
            return _Resp(200, {"taskId": "t_1"}), ""
        return _Resp(200, {"status": "fail", "error": "unsolvable"}), ""

    _patch_session(monkeypatch, handler)
    with pytest.raises(cf_solver.CFSolverError) as excinfo:
        cf_solver.solve_turnstile_token("https://example.com", "sk")
    err = excinfo.value
    assert err.kind == "captcha"
    assert err.retryable is True


def test_turnstile_timeout_is_retryable(monkeypatch):
    def handler(method, url, kwargs):
        if method == "POST":
            return _Resp(200, {"taskId": "t_1"}), ""
        return _Resp(200, {"status": "processing"}), ""

    _patch_session(monkeypatch, handler)
    with pytest.raises(cf_solver.CFSolverError) as excinfo:
        cf_solver.solve_turnstile_token(
            "https://example.com", "sk", timeout_s=0.05
        )
    assert "timeout" in str(excinfo.value)
    assert excinfo.value.retryable is True


def test_turnstile_empty_token_is_retryable(monkeypatch):
    def handler(method, url, kwargs):
        if method == "POST":
            return _Resp(200, {"taskId": "t_1"}), ""
        return _Resp(200, {"status": "success", "response": {}}), ""

    _patch_session(monkeypatch, handler)
    with pytest.raises(cf_solver.CFSolverError) as excinfo:
        cf_solver.solve_turnstile_token("https://example.com", "sk")
    assert "empty_token" in str(excinfo.value)


# ────────────────────── cf_clearance ──────────────────────


def test_cf_clearance_returns_cookie_and_ua(monkeypatch):
    def handler(method, url, kwargs):
        if method == "POST":
            return _Resp(200, {"taskId": "t_2"}), ""
        return _Resp(
            200,
            {
                "status": "success",
                "response": {
                    "cookies": {"cf_clearance": "cv-2"},
                    "userAgent": "UA/2",
                    "proxy": "http://u:p@1.2.3.4:8000",
                },
            },
        ), ""

    calls = _patch_session(monkeypatch, handler)
    result = cf_solver.solve_cf_clearance(
        "https://example.com", proxy="http://u:p@1.2.3.4:8000"
    )
    assert result.ok
    assert result.cookie_value == "cv-2"
    assert result.user_agent == "UA/2"
    assert calls[0][2]["json"]["captchaType"] == "cf_clearance"
    assert "sitekey" not in calls[0][2]["json"]


def test_cf_clearance_empty_cookie_is_retryable(monkeypatch):
    def handler(method, url, kwargs):
        if method == "POST":
            return _Resp(200, {"taskId": "t_2"}), ""
        return _Resp(200, {"status": "success", "response": {}}), ""

    _patch_session(monkeypatch, handler)
    with pytest.raises(cf_solver.CFSolverError) as excinfo:
        cf_solver.solve_cf_clearance("https://example.com")
    assert "empty_cookie" in str(excinfo.value)


# ────────────────────── config / transport ──────────────────────


def test_unconfigured_key_fails_deterministically(monkeypatch):
    monkeypatch.delenv("CAPTCHA_RUN_KEY", raising=False)
    assert cf_solver.solver_configured() is False
    with pytest.raises(cf_solver.CFSolverError) as excinfo:
        cf_solver.solve_turnstile_token("https://example.com", "sk")
    err = excinfo.value
    assert "unconfigured" in str(err)
    assert err.retryable is False


def test_unauthorized_is_not_retryable(monkeypatch):
    def handler(method, url, kwargs):
        return _Resp(401, {"error": "unauthorized"}), ""

    _patch_session(monkeypatch, handler)
    with pytest.raises(cf_solver.CFSolverError) as excinfo:
        cf_solver.solve_turnstile_token("https://example.com", "sk")
    assert "unauthorized" in str(excinfo.value)
    assert excinfo.value.retryable is False
    # Auth errors never fail over to the standby station.
    assert excinfo.value.kind == "captcha"


def test_transport_failure_fails_over_to_standby(monkeypatch):
    seen_bases = []

    def handler(method, url, kwargs):
        seen_bases.append(url)
        if "10.144.144.9" in url:
            return None, "connection refused"
        if method == "POST":
            return _Resp(200, {"taskId": "t_3"}), ""
        return _Resp(200, {"status": "success", "response": {"token": "tok-3"}}), ""

    _patch_session(monkeypatch, handler)
    token = cf_solver.solve_turnstile_token("https://example.com", "sk")
    assert token == "tok-3"
    assert any("10.144.144.9" in u for u in seen_bases)
    assert any("10.144.144.88" in u for u in seen_bases)


def test_all_stations_unreachable_is_network_kind(monkeypatch):
    def handler(method, url, kwargs):
        return None, "unreachable"

    _patch_session(monkeypatch, handler)
    with pytest.raises(cf_solver.CFSolverError) as excinfo:
        cf_solver.solve_turnstile_token("https://example.com", "sk")
    assert excinfo.value.kind == "network"
    assert excinfo.value.retryable is True


def test_base_override_precedence(monkeypatch):
    monkeypatch.setenv("CAPTCHA_RUN_BASE", "http://station-b:8077")
    monkeypatch.setenv("CF_SOLVER_BASE", "http://station-a:8077")
    assert cf_solver._station_bases() == ["http://station-a:8077"]
    monkeypatch.delenv("CF_SOLVER_BASE")
    assert cf_solver._station_bases() == ["http://station-b:8077"]


def test_plaintext_base_pins_http11(monkeypatch):
    """h2c upgrade on plaintext is discarded by the station's h11 server."""
    sessions = []

    def handler(method, url, kwargs):
        if method == "POST":
            return _Resp(200, {"taskId": "t_1"}), ""
        return _Resp(200, {"status": "success", "response": {"token": "t"}}), ""

    _patch_session(monkeypatch, handler)

    real_create_session = session_mod.create_session

    def spy(proxy=""):
        session = real_create_session(proxy)
        sessions.append(session)
        return session

    monkeypatch.setattr(session_mod, "create_session", spy)
    cf_solver.solve_turnstile_token("https://example.com", "sk")

    assert sessions, "expected a session"
    try:
        from curl_cffi.const import CurlHttpVersion

        assert sessions[0].http_version == CurlHttpVersion.V1_1
    except ImportError:  # requests fallback has no http_version knob
        pass


def test_proxy_never_logged_with_credentials(monkeypatch):
    assert cf_solver._redact_proxy("http://u:p@1.2.3.4:8000") == "1.2.3.4:8000"
    assert cf_solver._redact_proxy("") == ""


# ────────────────────── grok 遗留路径门控（grok_register_ttk） ──────────────────────


class _FakePage:
    """Minimal DrissionPage stand-in: run_js records calls, ele() finds nothing."""

    url = "https://accounts.x.ai/sign-up"

    def __init__(self, js_results=None):
        self.js_results = dict(js_results or {})
        self.js_calls = []

    def run_js(self, script, *args):
        self.js_calls.append((script, args))
        if "data-sitekey" in script:
            return self.js_results.get("sitekey", "")
        if "cf-turnstile-response" in script and args:
            return self.js_results.get("write_back", 0)
        return self.js_results.get("default", "")

    def ele(self, *_args, **_kwargs):
        return None

    def eles(self, *_args, **_kwargs):
        return []


@pytest.fixture()
def ttk(monkeypatch):
    # grok_register_ttk.ensure_stable_python_runtime() os.execve's into Python312
    # on 3.14+, which segfaults inside a pytest process (Windows). The module's
    # own escape hatch skips the re-exec; harmless on 3.13 CI.
    monkeypatch.setenv("DPE_REEXEC_DONE", "1")
    return pytest.importorskip("grok_register_ttk")


def _enable(monkeypatch, m, **overrides):
    cfg = dict(m.DEFAULT_CONFIG)
    cfg.update({"cf_solver_enabled": True, **overrides})
    monkeypatch.setattr(m, "config", cfg)


def test_gate_disabled_never_calls_solver(monkeypatch, ttk):
    monkeypatch.delenv("CAPTCHA_RUN_KEY", raising=False)
    _enable(monkeypatch, ttk, cf_solver_enabled=False)
    called = []
    monkeypatch.setattr(cf_solver, "solve_turnstile_token", lambda *a, **k: called.append(1))
    page = _FakePage({"sitekey": "0x4AAA"})
    monkeypatch.setattr(ttk, "_get_page", lambda: page)
    assert ttk._solve_turnstile_with_cf_solver() == ""
    assert called == []
    assert page.js_calls == []  # 门控在碰页面之前就返回


def test_gate_enabled_without_key_falls_back(monkeypatch, ttk):
    monkeypatch.delenv("CAPTCHA_RUN_KEY", raising=False)
    _enable(monkeypatch, ttk)
    called = []
    monkeypatch.setattr(cf_solver, "solve_turnstile_token", lambda *a, **k: called.append(1))
    monkeypatch.setattr(ttk, "_get_page", lambda: _FakePage({"sitekey": "0x4AAA"}))
    assert ttk._solve_turnstile_with_cf_solver() == ""
    assert called == []


def test_gate_success_solves_and_writes_back(monkeypatch, ttk):
    monkeypatch.setenv("CAPTCHA_RUN_KEY", "sk-test")
    _enable(monkeypatch, ttk)
    monkeypatch.setattr(ttk, "current_proxy_override", lambda: "http://u:p@1.2.3.4:8000")
    seen = {}

    def fake_solve(url, sitekey, *, proxy="", timeout_s=120.0):
        seen.update(url=url, sitekey=sitekey, proxy=proxy, timeout_s=timeout_s)
        return "tok-cf"

    monkeypatch.setattr(cf_solver, "solve_turnstile_token", fake_solve)
    page = _FakePage({"sitekey": "0x4AAA", "write_back": 86})
    monkeypatch.setattr(ttk, "_get_page", lambda: page)

    assert ttk._solve_turnstile_with_cf_solver() == "tok-cf"
    assert seen["url"] == page.url
    assert seen["sitekey"] == "0x4AAA"
    # Same-exit-IP: credentials reach the station verbatim.
    assert seen["proxy"] == "http://u:p@1.2.3.4:8000"
    assert seen["timeout_s"] == 120.0


def test_gate_solve_failure_returns_empty(monkeypatch, ttk):
    monkeypatch.setenv("CAPTCHA_RUN_KEY", "sk-test")
    _enable(monkeypatch, ttk)
    monkeypatch.setattr(ttk, "current_proxy_override", lambda: "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr(
        cf_solver, "solve_turnstile_token",
        lambda *a, **k: (_ for _ in ()).throw(cf_solver.CFSolverError("boom")),
    )
    monkeypatch.setattr(ttk, "_get_page", lambda: _FakePage({"sitekey": "0x4AAA"}))
    assert ttk._solve_turnstile_with_cf_solver() == ""


def test_gate_skips_loopback_proxy(monkeypatch, ttk):
    """本地代理桥/Clash 端口：打码站无法共用出口，必须跳过而不是白烧一单。"""
    monkeypatch.setenv("CAPTCHA_RUN_KEY", "sk-test")
    _enable(monkeypatch, ttk)
    monkeypatch.setattr(ttk, "current_proxy_override", lambda: "http://127.0.0.1:7890")
    called = []
    monkeypatch.setattr(cf_solver, "solve_turnstile_token", lambda *a, **k: called.append(1))
    monkeypatch.setattr(ttk, "_get_page", lambda: _FakePage({"sitekey": "0x4AAA"}))
    assert ttk._solve_turnstile_with_cf_solver() == ""
    assert called == []


def test_gate_skips_without_sitekey(monkeypatch, ttk):
    monkeypatch.setenv("CAPTCHA_RUN_KEY", "sk-test")
    _enable(monkeypatch, ttk)
    called = []
    monkeypatch.setattr(cf_solver, "solve_turnstile_token", lambda *a, **k: called.append(1))
    monkeypatch.setattr(ttk, "_get_page", lambda: _FakePage({"sitekey": ""}))
    assert ttk._solve_turnstile_with_cf_solver() == ""
    assert called == []


def test_get_turnstile_token_uses_solver_result(monkeypatch, ttk):
    _enable(monkeypatch, ttk)
    monkeypatch.setattr(ttk, "_solve_turnstile_with_cf_solver", lambda **k: "tok-active")
    monkeypatch.setattr(ttk, "_get_page", lambda: _FakePage())
    assert ttk.getTurnstileToken() == "tok-active"


def test_get_turnstile_token_falls_back_to_passive_loop(monkeypatch, ttk):
    """主动求解落空时，既有被动轮询必须原样生效（零回归）。"""
    _enable(monkeypatch, ttk)
    monkeypatch.setattr(ttk, "_solve_turnstile_with_cf_solver", lambda **k: "")
    passive_token = "x" * 90
    page = _FakePage({"default": passive_token})
    monkeypatch.setattr(ttk, "_get_page", lambda: page)
    assert ttk.getTurnstileToken() == passive_token


# ────────────────────── sitekey 抓取模式 ──────────────────────
#
# 2026-11-16 真 Chrome + 真实 x.ai 页面存档实测：浏览器 innerHTML 里的
# RSC payload 是**转义**形态 sitekey\":\"0x4…（引号前带反斜杠），不是未
# 转义的纯 JSON。主正则若不兼容转义形态，就只能靠"整页任意 0x4 令牌"
# 兜底——页面上出现第二个 0x4 形状 token 时会抓错。


def _rsc_match(ttk, text: str):
    import re

    m = re.search(ttk._SITEKEY_RSC_PATTERN, text)
    return m.group(1) if m else None


def test_sitekey_pattern_matches_escaped_and_plain_shapes(ttk):
    escaped = 'sitekey\\":\\"0x4AAAAAAAhr9JGVDZbrZOo0\\",\\"nonce\\":\\"abc\\"'
    plain = '"sitekey":"0x4AAAAAAAhr9JGVDZbrZOo0","nonce":"abc"'
    assert _rsc_match(ttk, escaped) == "0x4AAAAAAAhr9JGVDZbrZOo0"
    assert _rsc_match(ttk, plain) == "0x4AAAAAAAhr9JGVDZbrZOo0"


def test_sitekey_pattern_prefers_sitekey_over_decoy_token(ttk):
    """页面同时有别的 0x4 形状 token 时，正则必须抓 sitekey 那个。"""
    text = (
        '{\\"nonce\\":\\"0x4DECOYVALUE0000000\\",'
        '\\"sitekey\\":\\"0x4REALKEY000000000000\\"}'
    )
    assert _rsc_match(ttk, text) == "0x4REALKEY000000000000"


def test_sitekey_pattern_ignores_pages_without_sitekey(ttk):
    assert _rsc_match(ttk, '{"nonce":"0x4DECOYVALUE0000000"}') is None


def test_sitekey_script_embeds_the_shared_pattern(ttk):
    """JS 脚本里的正则必须来自同一个常量（避免两边漂移）。"""
    script = ttk._SITEKEY_SCRIPT.replace("__SITEKEY_RSC__", ttk._SITEKEY_RSC_PATTERN)
    assert "__SITEKEY_RSC__" not in script
    assert ttk._SITEKEY_RSC_PATTERN in script


def test_sitekey_helper_uses_shared_script(monkeypatch, ttk):
    """_cf_solver_sitekey 走共享脚本，且脚本里没有裸占位符。"""
    seen: list[str] = []

    class _Page:
        url = "https://accounts.x.ai/sign-up"

        def run_js(self, script, *args):
            seen.append(script)
            return "0x4AAAAAAAhr9JGVDZbrZOo0"

    page = _Page()
    monkeypatch.setattr(ttk, "_get_page", lambda: page)
    assert ttk._cf_solver_sitekey(page) == "0x4AAAAAAAhr9JGVDZbrZOo0"
    assert seen and "__SITEKEY_RSC__" not in seen[0]
