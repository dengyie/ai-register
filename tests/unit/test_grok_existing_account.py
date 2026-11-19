"""Existing-account (xAI chooser fallback) detection + fast-fail wiring.

Production evidence (instabox3 2026-11-16 smoke): verify-email returns
200 ``{"ok":true,"signInMethods":["EMAIL_PASSWORD"]}`` but the SPA falls back
to the "Existing account found → Login with email" chooser when the Hotmail
MAIN behind a random alias already has an xAI account. The old code treated
"code input disappeared" as auto-advance and spun the retry loop for minutes
per slot (2/3 slots dead in the observed batch).

Tests avoid importing grok_register_ttk / register_cli wholesale (import-time
``os.execve`` re-exec under pytest — see docs in test_browser_reliability_fixes).
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_ttk_source() -> str:
    return (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")


def _load_cli_source() -> str:
    return (ROOT / "register_cli.py").read_text(encoding="utf-8")


def test_email_exists_error_class_defined() -> None:
    """EmailExistsError exists next to AccountRetryNeeded with docstring evidence."""
    src = _load_ttk_source()
    tree = ast.parse(src)
    names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    assert "EmailExistsError" in names
    # Docstring pins the production signature (200 + signInMethods + chooser).
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EmailExistsError")
    doc = ast.get_docstring(cls) or ""
    assert "Existing account" in doc
    assert "EMAIL_PASSWORD" in doc
    print("PASS EmailExistsError defined with production-evidence docstring")


def test_existing_account_js_covers_en_and_chooser_confirmation() -> None:
    """The detection JS must require BOTH the banner text and (or) login chooser.

    Single-signal detection (button text only) would false-positive on the
    initial provider-chooser page which also shows "Login with email" for
    returning users; banner text alone would false-positive on any page that
    mentions the phrase in a hidden div. The JS requires the banner regex and
    reports 'maybe-existing' when only the banner is present, letting the
    caller decide; caller treats only truthy return as terminal.
    """
    src = _load_ttk_source()
    assert "_JS_EXISTING_ACCOUNT_CHECK" in src
    assert "Existing account found" in src
    assert "Login with email" in src
    # Chinese variants for a potential localized SPA.
    assert "已有账号" in src or "账号已存在" in src
    print("PASS existing-account JS signals present (EN banner + chooser + zh)")


def test_fill_code_checks_existing_account_before_retries() -> None:
    """fill_code_and_submit must check the chooser page at 3 points.

    1. guard at loop top after first fill (dead-page early exit)
    2. advanced branch (the historical misclassification point)
    3. clicked branch (post-submit fallback)
    Each raises EmailExistsError, not a bare Exception.
    """
    src = _load_ttk_source()
    fn_start = src.index("def fill_code_and_submit(")
    fn_end = src.index("\ndef ", fn_start + 10)
    body = src[fn_start:fn_end]
    checks = body.count("_page_is_existing_account(page)")
    assert checks >= 3, f"expected >=3 chooser checks in fill_code_and_submit, got {checks}"
    assert body.count("raise EmailExistsError(") >= 3
    print(f"PASS fill_code_and_submit has {checks} chooser checks raising EmailExistsError")


def test_cli_swaps_mailbox_on_email_exists_without_slot_retry() -> None:
    """register_cli must burn + swap mailbox on EmailExistsError, keep ARN semantics.

    except-clause ORDER is load-bearing: the precise AccountRetryNeeded clause
    must come FIRST (its cleanups run before the outer slot-retry handler); the
    single generic Exception clause handles EmailExists (positive isinstance,
    burn+swap) and allocation transport errors BEFORE classify(). Sibling
    except clauses never catch a re-raise from a prior clause, so no
    re-raise-based narrowing is allowed here.
    """
    src = _load_cli_source()
    fn_start = src.index("def register_one(")
    fn_end = src.index("\ndef ", fn_start + 10)
    body = src[fn_start:fn_end]
    # ARN clause precedes any broad Exception clause in the mail_try chain.
    assert body.index("except AccountRetryNeeded:") < body.index(
        "except Exception as exc:\n                    msg = str(exc)"
    ), "ARN clause must be first so its cleanups run"
    # EmailExists handled by positive isinstance inside the generic clause.
    assert "if EmailExistsError is not None and isinstance(exc, EmailExistsError):" in body
    # Terminal burn + provider advance (swap mailbox, not slot).
    assert '_mark_email_stage_error(email, f"existing_account:' in body
    assert "_advance_mail_provider_on_miss()" in body
    # Allocation transport failover branch (email=="" gate).
    assert "is_mail_alloc_transport_error" in body
    assert "not email and is_mail_alloc_transport_error(msg)" in body
    print("PASS register_cli burn+swap on EmailExistsError + transport failover, ARN first")


def test_mail_alloc_transport_error_matches_production_shapes() -> None:
    """The transport matcher must catch the real NXDOMAIN / SSL error shapes."""
    src = _load_cli_source()
    tree = ast.parse(src)
    ns: dict = {}
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "is_mail_alloc_transport_error"
    )
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<alloc>", "exec"), ns)
    match = ns["is_mail_alloc_transport_error"]
    # Real production error (instabox3 2026-11-19, curl_cffi via tetherpool).
    assert match(
        "Failed to perform, curl: (35) BoringSSL SSL_connect: Connection closed "
        "abruptly (SSL_ERROR_SYSCALL; error queue empty) in connection to "
        "temp-mail.mangoqwq.com:443"
    )
    assert match("Could not resolve host: temp-mail.mangoqwq.com")
    assert match("Connection refused by peer")
    assert match("The read operation timed out")
    # Non-transport failures must NOT match.
    assert not match("验证码已填写，但未进入资料页")
    assert not match("未找到「使用邮箱注册」按钮或邮箱表单未出现")
    assert not match("")
    print("PASS is_mail_alloc_transport_error production shapes + negatives")


def test_existing_account_not_classified_as_progress_fail() -> None:
    """classify_email_stage_failure fallback semantics stay intact.

    EmailExistsError is handled by type before classify() runs; if a message
    ever falls through, '未进入资料页' must still map to progress_fail (existing
    behavior relied on by other paths) — this test only guards that our new
    type-based handler does not need classify() to know the marker.
    """
    src = _load_cli_source()
    tree = ast.parse(src)
    ns: dict = {"is_fatal_register_error": lambda _m: False}
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "classify_email_stage_failure"
    )
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<classify>", "exec"), ns)
    classify = ns["classify_email_stage_failure"]
    assert classify("验证码已填写，但未进入资料页: code=1") == "progress_fail"
    assert classify("Outlook REST 无可用邮件: 获取验证码失败") == "mail_miss"
    print("PASS classify_email_stage_failure baseline semantics unchanged")


def test_diag_emitted_at_existing_account_sites() -> None:
    """Each raise site dumps a netlog diag first so ops can see the 200 evidence."""
    src = _load_ttk_source()
    fn_start = src.index("def fill_code_and_submit(")
    fn_end = src.index("\ndef ", fn_start + 10)
    body = src[fn_start:fn_end]
    assert body.count('_diag("existing-account")') >= 3
    print("PASS netlog diag dumped at every existing-account raise site")
