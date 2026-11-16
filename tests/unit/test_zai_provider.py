"""zai provider 离线单测——收据链 / 防伪门禁 / 命名铁律 / 黑盒邮箱语义。

全部离线：不触网、不开浏览器。战斗语义见 providers/zai/README.md。
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from register_core.contracts import RegisterResult
from register_core.errors import FailFastError


def _result(ok: bool, **overrides) -> RegisterResult:
    base = dict(
        ok=ok, provider="zai", email="ashisle87@dengyie.tech",
        password="p" * 16, secret="j" * 268 if ok else "",
        secret_kind="jwt" if ok else "none",
        error="" if ok else "x", error_kind="" if ok else "captcha",
        artifacts={}, created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return RegisterResult(**base)


# --- this-run 防伪（ADDING_PROVIDER 必测 1/2：历史产物不算成功） ---

def test_validate_this_run_rejects_unmarked_success() -> None:
    from providers.zai.protocol.flow import validate_this_run

    r = _result(True)  # ok=True 但无会话标记
    assert not validate_this_run(r)


def test_validate_this_run_accepts_marked_success() -> None:
    from providers.zai.protocol.flow import validate_this_run

    r = _result(True, artifacts={
        "cookie_id": "zai-reg-ab12cd",
        "signup_posted_at": "05:15:00",
        "verify_link_sha8": "deadbeef",
        "token_email": "ashisle87@dengyie.tech",
    })
    assert validate_this_run(r)


def test_validate_this_run_rejects_token_email_mismatch() -> None:
    from providers.zai.protocol.flow import validate_this_run

    r = _result(True, artifacts={
        "cookie_id": "zai-reg-ab12cd",
        "signup_posted_at": "05:15:00",
        "verify_link_sha8": "deadbeef",
        "token_email": "someone.else@dengyie.tech",  # 与 result.email 不一致
    })
    assert not validate_this_run(r)


def test_adapter_guards_unmarked_success(monkeypatch) -> None:
    """adapter 出口护栏：无会话标记的 ok=True 被翻转为失败（case 2）。

    本用例验证的是**出口护栏**，不启动浏览器：flow 被整体打桩。但 adapter
    的 preflight 在有头 Chromium 探测上会先 fail-fast（Linux 无 DISPLAY），
    导致护栏根本走不到。这里显式声明 xvfb 环境（DISPLAY 非空）以隔离
    preflight——生产上该守卫照常生效，测试不再依赖 runner 是否带 GUI。
    """
    from register_core.providers.zai_adapter import ZaiProvider

    monkeypatch.setenv("DISPLAY", ":99")

    prov = ZaiProvider(
        onemail_admin_key="k", hub_key="k", handle="ashisle87",
        attempts=1, link_wait_s=1.0, session_timeout_s=1.0, timeout_s=60)
    from providers.zai.protocol import flow as flow_mod

    unmarked = _result(True)
    orig = flow_mod.register_zai_account
    flow_mod.register_zai_account = lambda **kw: unmarked
    try:
        out = prov.register_one()
    finally:
        flow_mod.register_zai_account = orig
    assert out.ok is False
    assert out.secret == ""
    assert "this-run markers" in out.error


# --- to_public_dict 防泄漏（必测 3） ---

def test_public_dict_never_leaks_secret_or_password() -> None:
    pub = _result(True).to_public_dict()
    dumped = str(pub)
    assert "j" * 268 not in dumped
    assert "p" * 16 not in dumped
    assert pub["secret"] != _result(True).secret
    assert pub["password"] != _result(True).password


# --- 黑盒邮箱语义（必测 4） ---

def test_register_one_rejects_external_email_source() -> None:
    from register_core.providers.zai_adapter import ZaiProvider

    prov = ZaiProvider(onemail_admin_key="k")
    with pytest.raises(FailFastError):
        prov.register_one(email_source=object())


def test_register_one_requires_admin_key() -> None:
    import os

    from register_core.providers.zai_adapter import ZaiProvider

    saved = os.environ.pop("ONEMAIL_ADMIN_KEY", None)
    os.environ.pop("ZAI_ADMIN_KEY", None)
    try:
        prov = ZaiProvider(onemail_admin_key="")
        with pytest.raises(FailFastError):
            prov.register_one()
    finally:
        if saved is not None:
            os.environ["ONEMAIL_ADMIN_KEY"] = saved


# --- 命名铁律（点号事故教训） ---

def test_generate_handle_is_alnum_only() -> None:
    from providers.zai.protocol.naming import HANDLE_RE, generate_handle

    rnd = random.Random(42)
    for _ in range(200):
        h = generate_handle(rnd)
        assert HANDLE_RE.fullmatch(h), f"非法网名: {h}"
        assert "." not in h and "_" not in h


def test_hub_label_derivation() -> None:
    from providers.zai.protocol.naming import hub_label

    assert hub_label("zcodereg4", "dengyie.tech") == "zcodereg4-dengyietech"
    assert hub_label("sableashby", "dengyie.tech") == "sableashby-dengyietech"


# --- 链接提取门禁（&amp; 实体 / 全参数） ---

def test_extract_verify_link_requires_full_params() -> None:
    from providers.zai.protocol.mailbox import extract_verify_link

    body = json_dumps_stub("https://chat.z.ai/auth/verify_email?token=abc")
    assert extract_verify_link(body) is None  # 缺 email/username/language


def test_extract_verify_link_unescapes_entities() -> None:
    from providers.zai.protocol.mailbox import extract_verify_link, is_full_verify_link

    raw = ("https://chat.z.ai/auth/verify_email?token=verify-1"
           "&amp;email=ash%40dengyie.tech&amp;username=ash&amp;language=en")
    body = json_dumps_stub(raw)
    link = extract_verify_link(body)
    assert link is not None
    assert is_full_verify_link(link)
    assert "&amp;" not in link
    assert "&email=" in link


def test_mailbox_wait_rejects_baseline_links(monkeypatch) -> None:
    """baseline 内的旧链接不算新收据（同地址重跑防误判）。"""
    from providers.zai.protocol.mailbox import OneMailLinkMailbox

    old_link = ("https://chat.z.ai/auth/verify_email?token=old"
                "&email=x@dengyie.tech&username=x&language=en")
    box = OneMailLinkMailbox(base_url="https://mail.example", admin_key="k")
    monkeypatch.setattr(box, "_list_page", lambda: [
        {"id": "m1", "to_addr": "ashisle87@dengyie.tech", "subject": "Verify Your Email",
         "received_at": 1}])
    monkeypatch.setattr(box, "fetch_body", lambda _id: json_dumps_stub(old_link))
    out = box.wait_verify_link("ashisle87@dengyie.tech", baseline={old_link},
                               timeout_s=1, poll_interval_s=0.2)
    assert out is None


def test_mailbox_wait_accepts_new_link(monkeypatch) -> None:
    from providers.zai.protocol.mailbox import OneMailLinkMailbox

    new_link = ("https://chat.z.ai/auth/verify_email?token=new"
                "&email=ash%40dengyie.tech&username=ashisle87&language=en")
    box = OneMailLinkMailbox(base_url="https://mail.example", admin_key="k")
    monkeypatch.setattr(box, "_list_page", lambda: [
        {"id": "m2", "to_addr": "ashisle87@dengyie.tech", "subject": "Verify Your Email",
         "received_at": 2}])
    monkeypatch.setattr(box, "fetch_body", lambda _id: json_dumps_stub(new_link))
    out = box.wait_verify_link("ashisle87@dengyie.tech", baseline=set(),
                               timeout_s=2, poll_interval_s=0.2)
    assert out == new_link


def test_mailbox_wait_filters_by_to_addr(monkeypatch) -> None:
    """别人的信（如 catch-all 邻居）绝不误取。"""
    from providers.zai.protocol.mailbox import OneMailLinkMailbox

    other_link = ("https://chat.z.ai/auth/verify_email?token=other"
                  "&email=other@dengyie.tech&username=other&language=en")
    box = OneMailLinkMailbox(base_url="https://mail.example", admin_key="k")
    monkeypatch.setattr(box, "_list_page", lambda: [
        {"id": "m3", "to_addr": "someone_else@dengyie.tech",
         "subject": "Verify Your Email", "received_at": 3}])
    monkeypatch.setattr(box, "fetch_body", lambda _id: json_dumps_stub(other_link))
    out = box.wait_verify_link("ashisle87@dengyie.tech", baseline=set(),
                               timeout_s=1, poll_interval_s=0.2)
    assert out is None


# --- registry / verify 接线 ---

def test_zai_registered_in_provider_registry() -> None:
    from register_core.providers.registry import list_providers

    assert "zai" in list_providers()


def test_zai_verifier_resolves() -> None:
    from register_core.verify.registry import get_verifier

    v = get_verifier("zai", hub_key="")
    assert v.name == "zai_hub"


def test_error_kinds_normalize() -> None:
    from register_core.contracts import normalize_error_kind

    assert normalize_error_kind("captcha") == "captcha"
    assert normalize_error_kind("mail_miss") == "mail_miss"
    assert normalize_error_kind("verify") == "verify"


# --- helpers ---

def json_dumps_stub(text: str) -> str:
    """模拟 unified API 返回的 JSON 序列化邮件体（含转义）。"""
    import json

    return json.dumps({"body_html": text, "body_text": text}, ensure_ascii=False)


# ===== 2026-11-16 review 修复回归（naming P1 / hub-key / budget / 层迁移 / 真值表） =====

def test_generate_unique_handle_raises_failfast_on_exhaustion(monkeypatch) -> None:
    """P1 回归：50 次全碰撞必须抛 FailFastError（而非带 kind kwarg 的 TypeError）。"""
    from providers.zai.protocol import naming
    from register_core.errors import FailFastError

    monkeypatch.setattr(naming, "onemail_address_exists", lambda *a, **k: True)
    monkeypatch.setattr(naming, "hub_label_exists", lambda *a, **k: True)
    with pytest.raises(FailFastError) as ei:
        naming.generate_unique_handle(
            "dengyie.tech", onemail_base_url="https://mail.example",
            onemail_admin_key="k", hub_url="https://hub.example", hub_key="k")
    assert "50" in str(ei.value)


def test_naming_quotes_email_in_address_query(monkeypatch) -> None:
    """地址查重的 query 必须经 quote 编码（@/点号）。"""
    from providers.zai.protocol import naming

    captured = {}

    def fake_http(url, headers, timeout=20):
        captured["url"] = url
        return {"results": []}

    monkeypatch.setattr(naming, "_http_json", fake_http)
    naming.onemail_address_exists("https://mail.example", "k", "iris.finch28@dengyie.tech")
    assert "iris.finch28%40dengyie.tech" in captured["url"]


def test_adapter_requires_hub_key(monkeypatch) -> None:
    """P2 回归：缺 ZAI_HUB_KEY 必须 FailFastError（此前是深层 401 HTTPError）。"""
    from register_core.providers.zai_adapter import ZaiProvider

    prov = ZaiProvider(onemail_admin_key="k", hub_key="")
    monkeypatch.delenv("ZAI_HUB_KEY", raising=False)
    monkeypatch.setattr("sys.platform", "darwin")  # 绕过 DISPLAY 门（按平台分支判断）
    with pytest.raises(FailFastError) as ei:
        prov.register_one()
    assert "ZAI_HUB_KEY" in str(ei.value)


def test_adapter_consumes_timeout_budget(monkeypatch) -> None:
    """P2 回归：CLI/extra 的 timeout_s 必须作为整体预算传进 flow（不得被 **_ 吞掉）。"""
    from register_core.providers.zai_adapter import ZaiProvider
    from providers.zai.protocol import flow as flow_mod

    captured = {}

    def fake_register(**kw):
        captured.update(kw)
        return _result(False, error="stop", error_kind="captcha")

    monkeypatch.setattr(flow_mod, "register_zai_account", fake_register)
    prov = ZaiProvider(
        onemail_admin_key="k", hub_key="k", timeout_s=2100, handle="ashisle87")
    monkeypatch.setattr("sys.platform", "darwin")
    prov.register_one()
    assert captured.get("overall_budget_s") == 2100.0


def test_adapter_cli_timeout_overrides_env_budget(monkeypatch) -> None:
    from register_core.providers.zai_adapter import ZaiProvider
    from providers.zai.protocol import flow as flow_mod

    captured = {}

    def fake_register(**kw):
        captured.update(kw)
        return _result(False, error="stop", error_kind="captcha")

    monkeypatch.setattr(flow_mod, "register_zai_account", fake_register)
    prov = ZaiProvider(
        onemail_admin_key="k", hub_key="k", handle="ashisle87")
    monkeypatch.setattr("sys.platform", "darwin")
    prov.register_one(extra={"timeout_s": 900})
    assert captured.get("overall_budget_s") == 900.0


def test_adapter_success_artifacts_carry_public_note(monkeypatch) -> None:
    """Suggestion 回归：note 键进 _public_artifacts 白名单，stdout 可见号名。"""
    from register_core.providers.zai_adapter import ZaiProvider
    from providers.zai.protocol import flow as flow_mod

    marked = _result(True, artifacts={
        "cookie_id": "zai-reg-ab12cd",
        "signup_posted_at": "05:15:00",
        "verify_link_sha8": "deadbeef",
        "token_email": "ashisle87@dengyie.tech",
        "attempts_used": 1,
    })
    monkeypatch.setattr(flow_mod, "register_zai_account", lambda **kw: marked)
    prov = ZaiProvider(
        onemail_admin_key="k", hub_key="k", timeout_s=2100, handle="ashisle87")
    monkeypatch.setattr("sys.platform", "darwin")
    out = prov.register_one()
    pub = out.to_public_dict()
    assert "handle=ashisle87" in pub["artifacts"].get("note", "")


# --- pool_verify_account 真值表（迁移到 core 后的门禁回归） ---

def _mk_accounts(label="t1", *, status="active", selectable=True, enabled=True,
                 strikes=0, glm=None, flash=None):
    def accounts_response(method, url, headers=None, data=None, **kw):
        return 200, {"accounts": [{
            "name": label, "status": status, "selectable": selectable,
            "enabled": enabled, "risk_strikes": strikes,
            "quota": {
                "GLM-5.3": glm if glm is not None else
                {"remaining": 3000000, "total": 3000000},
                "GLM-5.3-Flash": flash if flash is not None else
                {"remaining": 5000000, "total": 5000000},
            },
        }]}
    return accounts_response


def _run_pool_verify(monkeypatch, responder, *, wait=1.0, interval=0.05):
    from register_core.verify import zai_hub

    monkeypatch.setattr(zai_hub, "hub_http_json", responder)
    logs: list[str] = []
    ok, detail = zai_hub.pool_verify_account(
        "t1", hub_url="https://hub.example", hub_key="k",
        max_wait_s=wait, interval_s=interval, log=logs.append)
    return ok, detail, logs


def test_pool_verify_passes_when_gate_and_quota_ok(monkeypatch) -> None:
    ok, detail, _ = _run_pool_verify(monkeypatch, _mk_accounts())
    assert ok and detail == "pool_verified"


def test_pool_verify_fails_when_gate_false(monkeypatch) -> None:
    ok, _, _ = _run_pool_verify(monkeypatch, _mk_accounts(selectable=False))
    assert not ok


def test_pool_verify_fails_when_quota_empty(monkeypatch) -> None:
    ok, _, _ = _run_pool_verify(monkeypatch, _mk_accounts(glm={"remaining": None}))
    assert not ok


def test_pool_verify_fails_when_flash_exhausted(monkeypatch) -> None:
    ok, _, _ = _run_pool_verify(monkeypatch, _mk_accounts(flash={"remaining": 0, "total": 5000000}))
    assert not ok


def test_pool_verify_fails_when_label_missing(monkeypatch) -> None:
    def responder(method, url, headers=None, data=None, **kw):
        return 200, {"accounts": [{"name": "someone-else"}]}
    ok, _, _ = _run_pool_verify(monkeypatch, responder)
    assert not ok


def test_pool_verify_warns_once_on_non200(monkeypatch) -> None:
    def responder(method, url, headers=None, data=None, **kw):
        return 401, {"error": "unauthorized"}
    ok, _, logs = _run_pool_verify(monkeypatch, responder, wait=0.3, interval=0.05)
    assert not ok
    warns = [l for l in logs if "401" in l]
    assert len(warns) == 1  # 只告警一次，不刷屏


# --- hub_join 四步顺序（收据：approve 后必须立刻 GET callback） ---

def test_hub_join_happy_path_sequence(monkeypatch) -> None:
    from providers.zai.protocol import hub as hub_mod

    calls: list[tuple[str, str]] = []

    def responder(method, url, headers=None, data=None, **kw):
        calls.append((method, url.split("?")[0]))
        if "/login/start" in url:
            assert method == "POST"
            return 200, {"flow_id": "flow-1", "authorize_url":
                         "https://chat.z.ai/api/oauth/authorize?state=st1"}
        if "api/oauth/authorize" in url:
            assert method == "POST"
            assert (data or {}).get("action") == "approve"
            return 200, {"redirect_url": "https://zcode.z.ai/callback?code=c1"}
        if "zcode.z.ai/callback" in url:
            assert method == "GET"  # 四步之③：立刻 GET 落码
            return 200, "<html>Authorization Successful</html>"
        if "/login/poll/flow-1" in url:
            assert method == "GET"
            return 200, {"status": "ready", "account": {"id": "a1"}}
        raise AssertionError(f"unexpected call {method} {url}")

    monkeypatch.setattr(hub_mod, "hub_http_json", responder)
    ok, detail = hub_mod.hub_join(
        "tok", "t1-dengyietech", hub_url="https://hub.example", hub_key="k")
    assert ok and detail == "ready"
    order = [u for _, u in calls]
    assert order.index("https://zcode.z.ai/callback") \
        == order.index("https://chat.z.ai/api/oauth/authorize") + 1


def test_hub_join_poll_failed_stops(monkeypatch) -> None:
    from providers.zai.protocol import hub as hub_mod

    def responder(method, url, headers=None, data=None, **kw):
        if "/login/start" in url:
            return 200, {"flow_id": "flow-2",
                         "authorize_url": "https://chat.z.ai/api/oauth/authorize?state=s"}
        if "api/oauth/authorize" in url:
            return 200, {"redirect_url": "https://zcode.z.ai/callback?code=c"}
        if "zcode.z.ai/callback" in url:
            return 200, "<html/>"
        if "/login/poll/flow-2" in url:
            return 200, {"status": "failed", "message": "bad state"}
        raise AssertionError("unexpected")

    monkeypatch.setattr(hub_mod, "hub_http_json", responder)
    ok, detail = hub_mod.hub_join(
        "tok", "t1-dengyietech", hub_url="https://hub.example", hub_key="k")
    assert not ok and "failed" in detail


# --- mailbox：domain 服务端过滤 + seen_ids 不重复拉 body ---

def test_mailbox_list_page_applies_domain_filter(monkeypatch) -> None:
    from providers.zai.protocol.mailbox import OneMailLinkMailbox

    captured = {}

    def fake_get_json(url, admin_key, timeout=20):
        captured["url"] = url
        return {"results": []}

    monkeypatch.setattr("providers.zai.protocol.mailbox._get_json", fake_get_json)
    box = OneMailLinkMailbox(base_url="https://mail.example", admin_key="k",
                             domain="dengyie.tech")
    box._list_page()
    assert "domain=dengyie.tech" in captured["url"]


def test_mailbox_seen_ids_avoid_refetch(monkeypatch) -> None:
    """P3 回归：baseline 内的旧信只拉一次 body，不随轮询重复 GET。"""
    from providers.zai.protocol.mailbox import OneMailLinkMailbox

    old_link = ("https://chat.z.ai/auth/verify_email?token=old"
                "&email=x@dengyie.tech&username=x&language=en")
    fetches = {"n": 0}
    box = OneMailLinkMailbox(base_url="https://mail.example", admin_key="k",
                             domain="dengyie.tech")
    monkeypatch.setattr(box, "_list_page", lambda: [
        {"id": "m1", "to_addr": "ashisle87@dengyie.tech", "subject": "Verify Your Email",
         "received_at": 1}])

    def fake_fetch(mail_id):
        fetches["n"] += 1
        return json_dumps_stub(old_link)

    monkeypatch.setattr(box, "fetch_body", fake_fetch)
    out = box.wait_verify_link("ashisle87@dengyie.tech", baseline={old_link},
                               timeout_s=1.5, poll_interval_s=0.2)
    assert out is None
    assert fetches["n"] == 1  # 一次缓存后不再重复


def test_flow_budget_exhaustion_stops_attempts(monkeypatch) -> None:
    """P2 预算语义回归：预算耗尽后不再开新会话（95s 预算 + 每会话睡 3s → 恰两轮）。"""
    import time as _time

    from providers.zai.protocol import flow as flow_mod
    from providers.zai.protocol.signup import SignupResult

    calls = {"signup": 0}

    def fake_signup(email, password, full_name, *, cookie_id, timeout_s):
        calls["signup"] += 1
        _time.sleep(3.0)  # 消耗预算（< session_timeout=收缩值，超时不触发）
        r = SignupResult()
        r.signup_posted = False
        return r

    monkeypatch.setattr(flow_mod, "run_signup_session", fake_signup)
    box = flow_mod.OneMailLinkMailbox.__new__(flow_mod.OneMailLinkMailbox)
    monkeypatch.setattr(box, "scan_existing_verify_links", lambda email: set())
    res = flow_mod.register_zai_account(
        email="ashisle87@dengyie.tech", password="p" * 16, full_name="ashisle87",
        mailbox=box, attempts=5, link_wait_s=1, session_timeout_s=10,
        overall_budget_s=95, log=None)
    assert calls["signup"] == 2  # 第 3 轮前剩余 < 90s → MIN_ATTEMPT_BUDGET_S 终止
    assert not res.ok
