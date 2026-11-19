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


def test_email_signup_unavailable_detection_wired_at_email_stage() -> None:
    """fill_email_and_submit must raise EmailExistsError (with email) on the 400.

    Production (instabox3 2026-11-19): registered main → send-verification-code
    400 account:email-signup-unavailable + "Email sign-up isn't available right
    now" (U+2019 apostrophe). Old path raised ARN (transient, not burned) and
    the picker re-handed the SAME dead main until slots ran out.
    """
    src = _load_ttk_source()
    assert "_page_email_signup_unavailable" in src
    # U+2019-safe regex + zh variants.
    assert "isn.t available" in src
    assert "邮箱注册暂不可用" in src
    fn_start = src.index("def fill_email_and_submit(")
    fn_end = src.index("\ndef ", fn_start + 10)
    body = src[fn_start:fn_end]
    assert body.count("_page_email_signup_unavailable(page)") >= 1
    assert "raise EmailExistsError(" in body
    assert "email=email" in body
    # Detection must fire BEFORE the generic ARN raise in the same branch.
    assert body.index("_page_email_signup_unavailable(page)") < body.index(
        "邮箱提交后未进入验证码页"
    )
    print("PASS email-stage email-signup-unavailable → EmailExistsError(email=...)")


def test_email_exists_error_class_defined() -> None:
    """EmailExistsError exists next to AccountRetryNeeded with docstring evidence."""
    src = _load_ttk_source()
    tree = ast.parse(src)
    names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    assert "EmailExistsError" in names
    # Docstring pins both production signatures (chooser + 400) and carries email.
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EmailExistsError")
    doc = ast.get_docstring(cls) or ""
    assert "Existing account" in doc
    assert "EMAIL_PASSWORD" in doc
    assert "email-signup-unavailable" in doc
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    args = [a.arg for a in init.args.args]
    assert "email" in args, "EmailExistsError must carry the allocated address"
    assert "stage" in args, "EmailExistsError must carry send/chooser stage"
    print("PASS EmailExistsError defined (both signatures docstring + email/stage attrs)")


def test_send_stage_400_is_egress_aware_not_blind_burn() -> None:
    """register_cli: send-stage 400 burns ONLY when this egress proved sends work.

    Production disambiguation (instabox3 2026-11-19): deep-pool ZERO-consumed
    main DominicNoles6439081 400'd identically to the registered cohort → the
    send gate is egress-wide. Blind burn would eat the whole pool; burn must
    be gated on _send_ok_egress_seen(current label), and gate-class ARNs must
    never burn via email_failure_should_burn_mailbox.
    """
    src = _load_cli_source()
    # Stage tracking helpers exist with TTL.
    assert "_send_ok_egress_mark" in src and "_send_ok_egress_seen" in src
    assert "_SEND_OK_EGRESS_TTL" in src
    fn_start = src.index("def register_one(")
    fn_end = src.index("\ndef ", fn_start + 10)
    body = src[fn_start:fn_end]
    # Send-ok marked right after a successful fill_email_and_submit.
    assert "_send_ok_egress_mark(current_egress_label())" in body
    # send-stage gate: no-proof → transient ARN carrying the address.
    assert 'if stage == "send" and not _send_ok_egress_seen(' in body
    assert 'f"email_signup_gate: {msg[:160]}", email=burn_email' in body
    # Chooser-stage and proven-egress path still burns.
    assert "_existing_burn_streak_inc()" in body
    # Gate-class messages are exempt from mailbox burn.
    burn_start = src.index("def email_failure_should_burn_mailbox(")
    burn_end = src.index("\ndef ", burn_start + 10)
    burn_body = src[burn_start:burn_end]
    assert '"email_signup_gate" in text' in burn_body
    assert '"email-signup-unavailable" in text' in burn_body
    print("PASS send-stage 400 egress-aware; gate ARN exempt from burn")


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
    assert '_mark_email_stage_error(burn_email, f"existing_account:' in body
    assert "_advance_mail_provider_on_miss()" in body
    # Transport failover branch (PR #7) still present with email=="" gate.
    assert "not email and is_mail_alloc_transport_error(msg)" in body
    # Global-pause circuit breaker: streak counter with fatal stop.
    assert "_existing_burn_streak_inc()" in body
    assert "existing_account_fatal_streak" in body
    assert "request_fatal_stop(fatal_msg)" in body
    # Streak resets: success path + non-existing failure paths.
    assert "_existing_burn_streak_reset()" in body
    print("PASS register_cli burn+swap on EmailExistsError + streak breaker + resets")


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


def test_skip_email_provider_failed_rr_repoint() -> None:
    """skip_email_provider_failed re-points RR past the failed member.

    Extracted via AST exec with stubbed pool/strategy/bind (no ttk import —
    execve re-exec under pytest). pool [cloudflare, hotmail], failed=cloudflare
    → rr_index becomes 1 → next pick hotmail. Single pool / unknown member /
    failover strategy keep prior behavior.
    """
    src = _load_ttk_source()
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "skip_email_provider_failed"
    )
    adv = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "advance_email_provider_failover"
    )
    mod = ast.Module(body=[fn, adv], type_ignores=[])
    ast.fix_missing_locations(mod)

    def run_case(pool, strategy, bound, rr_index):
        ns = {
            "get_email_providers_pool": lambda: pool,
            "get_email_provider_strategy": lambda: strategy,
            "get_bound_email_provider": lambda: bound,
            "_email_provider_tls": type("T", (), {"failover_index": 0})(),
            "clear_email_provider_bind": lambda: None,
            "_email_provider_rr_index": rr_index,
            "_email_provider_rr_lock": __import__("threading").Lock(),
        }
        exec(compile(mod, "<skip>", "exec"), ns)
        ns["skip_email_provider_failed"]()
        return ns["_email_provider_rr_index"], ns["_email_provider_tls"].failover_index

    # RR: failed cloudflare → index re-pointed to 1 (hotmail next).
    rr, fi = run_case(["cloudflare", "hotmail"], "round_robin", "cloudflare", 7)
    assert rr == 1 and fi == 0
    # Unknown/absent bind → untouched.
    rr, fi = run_case(["cloudflare", "hotmail"], "round_robin", None, 5)
    assert rr == 5
    # Single-member pool → untouched.
    rr, fi = run_case(["hotmail"], "round_robin", "hotmail", 3)
    assert rr == 3
    # Failover strategy → failover index raised, RR untouched.
    rr, fi = run_case(["cloudflare", "hotmail"], "failover", "cloudflare", 9)
    assert fi == 1 and rr == 9
    print("PASS skip_email_provider_failed RR re-point + failover + no-ops")


def test_cli_stays_on_provider_after_mailbox_burn() -> None:
    """EmailExists branch must NOT clear/advance the provider bind.

    Mailbox burned ≠ channel broken: clearing made RR bounce back to the dead
    cloudflare member; staying bound keeps hotmail for the next mail_try.
    Transport branch keeps the skip (bias away from failed channel).
    """
    src = _load_cli_source()
    fn_start = src.index("def register_one(")
    fn_end = src.index("\ndef ", fn_start + 10)
    body = src[fn_start:fn_end]
    ex_start = body.index("if EmailExistsError is not None and isinstance(exc, EmailExistsError):")
    ex_end = body.index("# Mailbox ALLOCATION transport error")
    email_exists_block = body[ex_start:ex_end]
    # Gate path (no send-proof) may clear bind (transient rotate class); the
    # BURN path must NOT clear/advance (stay bound on the healthy channel).
    burn_start = email_exists_block.index("existing account/{stage}")
    burn_block = email_exists_block[burn_start:]
    assert "_clear_mail_provider_bind()" not in burn_block
    assert "_advance_mail_provider_on_miss()" not in burn_block
    # Transport branch skips the failed channel before clearing the bind.
    tr_start = body.index("# Mailbox ALLOCATION transport error")
    tr_end = body.index("kind = classify_email_stage_failure(msg)", tr_start)
    transport_block = body[tr_start:tr_end]
    assert "_skip_failed_mail_provider()" in transport_block
    # Helper defined and clears AFTER skip.
    helper_pos = body.index("def _skip_failed_mail_provider()")
    assert "_clear_mail_provider_bind()" in body[helper_pos:helper_pos + 1100]
    print("PASS EmailExists keeps bind; transport branch skips failed channel")
