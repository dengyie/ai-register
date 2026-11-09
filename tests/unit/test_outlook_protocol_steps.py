#!/usr/bin/env python3
"""Offline Outlook protocol step tests (no live Microsoft endpoints).

Patches ``request_with_retry`` inside msa_flow / px_client / mail_client the
way test_chatgpt_protocol_steps.py does, asserting URLs, field names, and flow
ORDER from the protocol fact table (docs/outlook-protocol-notes.md).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from providers.outlook.protocol import msa_flow, px_client, sms_client
from providers.outlook.protocol.constants import (
    SIGNUP_CLIENT_ID,
    TOKEN_MODES,
    mkt_lc_for_country,
    timezone_for_country,
)
from providers.outlook.protocol.mail_client import (
    _extract_otp,
    exchange_refresh_token,
    read_otp_code,
)


class _Resp:
    def __init__(
        self,
        status: int = 200,
        body: dict[str, Any] | None = None,
        text: str = "",
        url: str = "",
    ) -> None:
        self.status_code = status
        self._body = body or {}
        self.text = text or str(body or "")
        self.url = url

    def json(self):
        return self._body


def _serverdata_html() -> str:
    payload = {
        "sUnauthSessionID": "uaid-1234",
        "apiCanary": "canary-1",
        "scid": "scid-9",
        "hpgid": 200225,
        "iScenarioId": 100118,
        "sru": "https://outlook.live.com/owa/",
        "urlRiskInitialize": "/consumers/api/v1.0/risk/initialize",
        "urlRiskVerify": "/consumers/api/v1.0/risk/verify",
        "urlCheckAvailableSigninNames": "https://signup.live.com/API/CheckAvailableSigninNames",
        "oCaptchaInfo": {"urlDfp": "https://dfp.example/x"},
        "sSiteId": "292666",
    }
    return "var ServerData = " + json.dumps(payload) + ";"


class TestConstants(unittest.TestCase):
    def test_token_modes_and_facts(self) -> None:
        self.assertEqual(TOKEN_MODES, ("graph", "login_exe"))
        self.assertEqual(mkt_lc_for_country("US"), ("EN-US", 1033))
        self.assertEqual(mkt_lc_for_country("XX"), ("EN-US", 1033))
        self.assertEqual(timezone_for_country("JP"), "Asia/Tokyo")
        self.assertEqual(timezone_for_country("ZZ"), "America/New_York")

    def test_risk_signature_fields(self) -> None:
        sig = msa_flow.build_risk_verify_signature(
            member_name="a@outlook.com",
            birth_date="1990-05-05T00:00:00.000Z",
            first_name="Alex",
            last_name="Morgan",
            country="US",
        )
        # SPA-exact: the signature is a nested object (a JSON string fails
        # server-side body parse), birthdate uses the $N DD:MM:YYYY format.
        self.assertIsInstance(sig, dict)
        self.assertEqual(sig["appId"], SIGNUP_CLIENT_ID)
        self.assertEqual(sig["siteId"], SIGNUP_CLIENT_ID)
        self.assertEqual(sig["uiFlavor"], "Web")
        self.assertEqual(sig["birthdate"], "05:05:1990")
        self.assertEqual(sig["action"], "SignUp")
        self.assertEqual(sig["deviceDetails"], {"isRdm": False})


class TestServerDataParser(unittest.TestCase):
    def test_parses_serverdata(self) -> None:
        parsed = msa_flow.parse_server_data("x var ServerData= " + '{"a":1} ; y')
        self.assertEqual(parsed, {"a": 1})

    def test_unescaped_html(self) -> None:
        parsed = msa_flow.parse_server_data(
            'var ServerData= {"u":"a\\u0026b\\/c"};'
        )
        self.assertEqual(parsed["u"], "a&b/c")

    def test_garbage_returns_empty(self) -> None:
        self.assertEqual(msa_flow.parse_server_data("no marker"), {})
        self.assertEqual(msa_flow.parse_server_data("var ServerData= {broken"), {})


class TestBootstrap(unittest.TestCase):
    def test_bootstrap_harvests_session_facts(self) -> None:
        session = MagicMock()
        # 1st GET: account.microsoft.com areaConfig; 2nd GET: createAccount page.
        session.request.side_effect = [
            _Resp(200, text='var areaConfig = JSON.stringify({"createAccountUrl": "https://signup.live.com/signup?lic=1"} )'),
            _Resp(200, text=_serverdata_html()),
        ]
        steps: dict[str, Any] = {}
        state = msa_flow.bootstrap_account_session(session, country="DE", steps=steps)
        self.assertEqual(state.uaid, "uaid-1234")
        self.assertEqual(state.api_canary, "canary-1")
        self.assertEqual(state.mkt, "DE-DE")
        self.assertEqual(state.lc, 1031)
        self.assertEqual(
            state.url_risk_initialize,
            "https://login.microsoftonline.com/consumers/api/v1.0/risk/initialize",
        )
        self.assertEqual(state.site_id, "292666")
        self.assertIn("bootstrap", steps)

    def test_bootstrap_missing_risk_urls_fails(self) -> None:
        session = MagicMock()
        session.request.side_effect = [
            _Resp(200, text="var areaConfig = JSON.stringify({})"),
            _Resp(200, text='var ServerData = {"apiCanary":"c"};'),
        ]
        with self.assertRaises(msa_flow.MSAFlowError) as cm:
            msa_flow.bootstrap_account_session(session, steps={})
        self.assertEqual(cm.exception.step, "bootstrap")


class TestCheckAvailable(unittest.TestCase):
    def test_posts_canary_body_and_reads_availability(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(uaid="u", api_canary="c", scid="s")
        state.url_check_available = "https://signup.live.com/API/CheckAvailableSigninNames"
        session.request.side_effect = [
            _Resp(200, body={"isAvailable": True, "apiCanary": "c2"}),
        ]
        steps: dict[str, Any] = {}
        ok = msa_flow.check_available_signin_name(session, state, "x@outlook.com", steps=steps)
        self.assertTrue(ok)
        self.assertEqual(state.api_canary, "c2")
        call = session.request.call_args
        # SPA-exact contract: camelCase signInName (snake_case key gets an HTML
        # errcode=1046 error page) and the ServerData-provided URL.
        self.assertIn("CheckAvailableSigninNames", call.args[1])
        self.assertEqual(call.kwargs["json"]["signInName"], "x@outlook.com")
        self.assertNotIn("sign_inName", call.kwargs["json"])
        self.assertEqual(call.kwargs["json"]["uiflvr"], 1001)
        self.assertEqual(
            call.args[1], "https://signup.live.com/API/CheckAvailableSigninNames"
        )

    def test_unavailable_is_false(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(uaid="u", api_canary="c")
        session.request.return_value = _Resp(200, body={"isAvailable": False})
        self.assertFalse(
            msa_flow.check_available_signin_name(session, state, "x@outlook.com")
        )

    def test_html_error_page_raises_provider_error(self) -> None:
        # errcode=1046 HTML error page must NOT be read as "name taken".
        session = MagicMock()
        state = msa_flow.SignupSession(uaid="u", api_canary="c")
        session.request.return_value = _Resp(
            200, text="<!doctype html><html>errcode=1046</html>", body={}
        )
        with self.assertRaises(msa_flow.MSAFlowError) as cm:
            msa_flow.check_available_signin_name(session, state, "x@outlook.com")
        self.assertEqual(cm.exception.kind, "provider")
        self.assertEqual(cm.exception.step, "check_available")

    def test_bootstrap_captures_check_available_url(self) -> None:
        session = MagicMock()
        session.request.side_effect = [
            _Resp(200, text='var areaConfig = JSON.stringify({"createAccountUrl": "https://signup.live.com/signup?lic=1"} )'),
            _Resp(200, text=_serverdata_html()),
        ]
        state = msa_flow.bootstrap_account_session(session, country="US")
        self.assertEqual(
            state.url_check_available,
            "https://signup.live.com/API/CheckAvailableSigninNames",
        )


class TestRiskChain(unittest.TestCase):
    def test_risk_initialize_captures_sensor_url(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(
            uaid="u",
            url_risk_initialize="https://login.microsoftonline.com/init",
        )
        session.request.return_value = _Resp(
            200, body={"continuationToken": "ct1", "humanSensorUrl": "https://h/1"}
        )
        steps: dict[str, Any] = {}
        msa_flow.risk_initialize(session, state, steps=steps)
        self.assertEqual(state.continuation_token, "ct1")
        self.assertEqual(state.human_sensor_url, "https://h/1")

    def test_risk_initialize_reads_nested_sensor_url(self) -> None:
        # Live shape (2026-11-08): humanSensorUrl is nested at
        # riskInitializationData[0].humanSensorUrl, not top-level.
        session = MagicMock()
        state = msa_flow.SignupSession(
            uaid="u",
            url_risk_initialize="https://login.microsoftonline.com/init",
        )
        session.request.return_value = _Resp(
            200,
            body={
                "continuationToken": "ct2",
                "riskInitializationData": [
                    {"riskProvider": "Human", "humanSensorUrl": "https://h/nested"}
                ],
            },
        )
        msa_flow.risk_initialize(session, state)
        self.assertEqual(state.human_sensor_url, "https://h/nested")

    def test_verify_silent_403_routes_to_challenge(self) -> None:
        # Live fact: silent verify with no PX tokens hard-blocks 403
        # AADSTS7005106 — the SPA's cue to run the sensor + press token,
        # not a proxy fault. Must return the challenge branch.
        session = MagicMock()
        state = msa_flow.SignupSession(url_risk_verify="https://v")
        session.request.return_value = _Resp(403, body={})
        steps: dict[str, Any] = {}
        branch = msa_flow.risk_verify_silent(
            session, state, signature={"memberName": "x"}, steps=steps
        )
        self.assertEqual(branch, "challenge")
        self.assertEqual(steps["risk_verify_silent"]["state"], "riskBlock_403")

    def test_verify_silent_px_metadata_body_shape(self) -> None:
        # z_style parity: when silent PX tokens exist the body switches to
        # {continuationToken, riskProviderMetadata(silent), msaCreateSignature}
        # — metadata carries exactly px3/pxde/pxvid, NO pxcts.
        session = MagicMock()
        state = msa_flow.SignupSession(url_risk_verify="https://v", continuation_token="ct")
        session.request.return_value = _Resp(200, body={"state": "continue"})
        steps: dict[str, Any] = {}
        signature = msa_flow.build_create_signature(
            member_name="x@outlook.com", birth_date="1990-05-05T00:00:00.000Z",
            first_name="Alex", last_name="Morgan", country="US", site_id="292666",
        )
        branch = msa_flow.risk_verify_silent(
            session, state, signature=signature,
            px_metadata=[{"riskProvider": "Human", "px3": "p3", "pxde": "pd", "pxvid": "v"}],
            steps=steps,
        )
        self.assertEqual(branch, "")
        body = session.request.call_args.kwargs["json"]
        self.assertEqual(body["msaCreateSignature"], signature)
        meta = body["riskProviderMetadata"][0]
        self.assertEqual(meta["riskProvider"], "Human")
        self.assertEqual(meta["px3"], "p3")
        self.assertNotIn("pxcts", meta)
        self.assertNotIn("msaRiskVerifySignature", body)

    def test_build_create_signature_fields(self) -> None:
        # Upstream z_style fact: siteId from ServerData sSiteId, appId empty,
        # no action field, DD:MM:YYYY birthdate.
        sig = msa_flow.build_create_signature(
            member_name="x@outlook.com", birth_date="1990-05-05T00:00:00.000Z",
            first_name="Alex", last_name="Morgan", country="US", site_id="292666",
        )
        self.assertEqual(sig["siteId"], "292666")
        self.assertEqual(sig["appId"], "")
        self.assertNotIn("action", sig)
        self.assertEqual(sig["birthdate"], "05:05:1990")
        self.assertEqual(sig["uiFlavor"], "Web")
        self.assertEqual(sig["deviceDetails"], {"isRdm": False})
        # siteId falls back to the account.microsoft.com reference default.
        sig2 = msa_flow.build_create_signature(
            member_name="x", birth_date="1990-05-05", first_name="A",
            last_name="B", country="US", site_id="",
        )
        self.assertEqual(sig2["siteId"], "292666")


class TestSessionCompleteness(unittest.TestCase):
    """P0 absorption (2026-11-08): headers, experiment endpoint, query string."""

    def test_signup_headers_carry_risk_fields(self) -> None:
        state = msa_flow.SignupSession(
            uaid="uaid-abc", api_canary="c", hpgid=300411,
            signup_page_url="https://signup.live.com/signup?lic=1&uaid=uaid-abc",
        )
        headers = msa_flow._signup_headers(state)
        self.assertEqual(headers["correlationId"], "uaid-abc")
        self.assertEqual(headers["client-request-id"], "uaid-abc")
        self.assertEqual(headers["hpgid"], "300411")
        self.assertEqual(headers["hpgact"], "0")
        self.assertEqual(headers["canary"], "c")
        self.assertEqual(
            headers["referer"],
            "https://signup.live.com/signup?lic=1&uaid=uaid-abc",
        )

    def test_evaluate_experiments_posts_and_refreshes_canary(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(uaid="u", api_canary="c1")
        session.request.return_value = _Resp(
            200, body={"apiCanary": "c2", "telemetryContext": "tc-1"}
        )
        steps: dict[str, Any] = {}
        ok = msa_flow.evaluate_experiment_assignments(session, state, steps=steps)
        self.assertTrue(ok)
        self.assertEqual(state.api_canary, "c2")
        self.assertEqual(state.telemetry_context, "tc-1")
        call = session.request.call_args
        self.assertIn("EvaluateExperimentAssignments", call.args[1])
        experiments = call.kwargs["json"]["clientExperiments"]
        self.assertEqual(len(experiments), 2)
        self.assertEqual(experiments[0]["parallax"], "enablesisufeedback")

    def test_evaluate_experiments_failure_is_non_fatal(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(uaid="u", api_canary="keep")
        session.request.return_value = _Resp(200, body={})
        steps: dict[str, Any] = {}
        ok = msa_flow.evaluate_experiment_assignments(session, state, steps=steps)
        self.assertFalse(ok)
        self.assertEqual(state.api_canary, "keep")
        self.assertFalse(steps["evaluate_experiments"]["has_canary"])

    def test_bootstrap_captures_signup_query_and_page_url(self) -> None:
        session = MagicMock()
        page = _Resp(200, text=_serverdata_html())
        page.url = "https://signup.live.com/signup?lic=1&mkt=EN-US&uaid=uaid-1234"
        session.request.side_effect = [
            _Resp(200, text='var areaConfig = JSON.stringify({"createAccountUrl": "https://signup.live.com/signup?lic=1"} )'),
            page,
        ]
        state = msa_flow.bootstrap_account_session(session, country="US")
        self.assertEqual(state.signup_page_url, page.url)
        self.assertIn("uaid=uaid-1234", state.signup_query)

    def test_create_account_url_rides_signup_query(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(
            uaid="u", scid="s", sru="https://sru", country="US",
            signup_query="lic=1&uaid=u",
        )
        session.request.return_value = _Resp(200, body={"slt": "t"})
        msa_flow.create_account(
            session, state, email="x@outlook.com", password="pw",
            birth_date="1990-01-01T00:00:00.000Z", first_name="A", last_name="B",
        )
        url = session.request.call_args.args[1]
        self.assertIn("/CreateAccount?lic=1&uaid=u", url)

    def test_check_available_url_rides_signup_query(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(uaid="u", scid="s", signup_query="lic=1&uaid=u")
        state.url_check_available = "https://signup.live.com/API/CheckAvailableSigninNames"
        session.request.return_value = _Resp(200, body={"isAvailable": True})
        msa_flow.check_available_signin_name(session, state, "x@outlook.com")
        url = session.request.call_args.args[1]
        self.assertEqual(url, "https://signup.live.com/API/CheckAvailableSigninNames?lic=1&uaid=u")

    def test_identity_pools_and_styles(self) -> None:
        # Seller style facts: pure-letter prefix 10–12, lowercase+digit
        # password 11–14, names drawn from pools (never a fixed persona).
        seen_lengths = set()
        for _ in range(30):
            prefix = msa_flow._random_prefix()
            self.assertTrue(prefix.isalpha() and prefix.islower())
            seen_lengths.add(len(prefix))
        self.assertTrue(seen_lengths <= {10, 11, 12} and len(seen_lengths) > 1)
        for _ in range(30):
            password = msa_flow._random_password()
            self.assertTrue(11 <= len(password) <= 14)
            self.assertTrue(all(c.islower() or c.isdigit() for c in password))
            self.assertTrue(any(c.isdigit() for c in password))
        first, last = msa_flow._random_name()
        self.assertIn(first, msa_flow._FIRST_NAMES)
        self.assertIn(last, msa_flow._LAST_NAMES)

    def test_verify_silent_challenge_branch(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(url_risk_verify="https://v")
        session.request.return_value = _Resp(
            200, body={"state": "riskChallengeRequired"}
        )
        steps: dict[str, Any] = {}
        branch = msa_flow.risk_verify_silent(session, state, signature={"memberName": "x"}, steps=steps)
        self.assertEqual(branch, "challenge")
        self.assertEqual(steps["risk_verify_silent"]["state"], "riskChallengeRequired")

    def test_verify_silent_retry_initialize_branch(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(
            url_risk_initialize="https://init", url_risk_verify="https://v"
        )
        session.request.side_effect = [
            _Resp(200, body={"state": "riskInitializationRequired"}),
            _Resp(200, body={"humanSensorUrl": "h"}),
            _Resp(200, body={"state": ""}),
        ]
        branch = msa_flow.risk_verify_silent(session, state, signature={"memberName": "x"})
        self.assertEqual(branch, "retry_initialize")

    def test_press_submit_fields(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(url_risk_verify="https://v")
        token = px_client.PXPressToken(px3="px3v", pxde="pdev", pxvid="vid", pxcts="cts")
        session.request.return_value = _Resp(200, body={"state": "verified"})
        steps: dict[str, Any] = {}
        msa_flow._submit_press_token(session, state, token=token, steps=steps)
        call = session.request.call_args
        # SPA-exact: solution is an object, metadata is an array of objects.
        solution = call.kwargs["json"]["challengeSolution"]
        self.assertEqual(solution["challengeType"], "HumanCaptcha")
        self.assertEqual(solution["px3"], "px3v")
        metadata = call.kwargs["json"]["riskProviderMetadata"]
        self.assertIsInstance(metadata, list)
        self.assertEqual(metadata[0]["riskProvider"], "Human")
        self.assertEqual(metadata[0]["pxvid"], "vid")

    def test_press_unsolved_state_raises_captcha(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(url_risk_verify="https://v")
        token = px_client.PXPressToken(px3="px3v")
        session.request.return_value = _Resp(200, body={"state": "riskChallengeRequired"})
        with self.assertRaises(msa_flow.MSAFlowError) as cm:
            msa_flow._submit_press_token(session, state, token=token)
        self.assertEqual(cm.exception.kind, "captcha")


class TestCreateAccount(unittest.TestCase):
    def test_full_body_fields(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(
            uaid="u", scid="s", sru="https://sru", site_id="292666", country="US"
        )
        session.request.return_value = _Resp(
            200, body={"slt": "slt-token", "redirectUrl": "https://r", "signInName": "x@outlook.com"}
        )
        steps: dict[str, Any] = {}
        created = msa_flow.create_account(
            session,
            state,
            email="x@outlook.com",
            password="pw",
            birth_date="1990-01-01T00:00:00.000Z",
            first_name="Alex",
            last_name="Morgan",
            check_map=["x@outlook.com:true"],
            steps=steps,
        )
        self.assertEqual(created["slt"], "slt-token")
        body = session.request.call_args.kwargs["json"]
        self.assertEqual(body["MemberName"], "x@outlook.com")
        self.assertEqual(body["SuggestedAccountType"], "EASI")
        # SPA JN-builder exact shapes (live-differential 2026-11-08): dmy
        # birthdate, RequestTimeStamp (capital S, ISO), VerificationCodeSlt,
        # boolean LW/IsOptOutEmailShown, PIPL consent False on non-CN.
        self.assertEqual(body["BirthDate"], "01:01:1990")
        self.assertEqual(body["RequestTimeStamp"], body["RequestTimeStamp"])
        self.assertIn("T", body["RequestTimeStamp"])
        self.assertIn("VerificationCodeSlt", body)
        self.assertNotIn("VerificationCodeSalt", body)
        self.assertIs(body["LW"], False)
        self.assertIs(body["IsOptOutEmailShown"], True)
        self.assertIs(body["IsUserConsentedToChinaPIPL"], False)
        self.assertEqual(body["memberNameChangeCount"], 1)
        self.assertIn("CreateAccount", session.request.call_args.args[1])

    def test_create_account_pipl_true_for_cn(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(uaid="u", country="CN")
        session.request.return_value = _Resp(
            200, body={"slt": "slt-token", "redirectUrl": "https://r", "signInName": "x@outlook.com"}
        )
        steps: dict[str, Any] = {}
        msa_flow.create_account(
            session,
            state,
            email="x@outlook.com",
            password="pw",
            birth_date="1990-01-01T00:00:00.000Z",
            first_name="Wei",
            last_name="Chen",
            check_map=["x@outlook.com:true"],
            steps=steps,
        )
        body = session.request.call_args.kwargs["json"]
        self.assertIs(body["IsUserConsentedToChinaPIPL"], True)

    def test_rejected_without_slt(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(uaid="u")
        session.request.return_value = _Resp(200, body={"error": "blocked"})
        with self.assertRaises(msa_flow.MSAFlowError) as cm:
            msa_flow.create_account(
                session, state, email="x@outlook.com", password="pw",
                birth_date="1990-01-01T00:00:00.000Z",
                first_name="A", last_name="B",
            )
        self.assertEqual(cm.exception.kind, "registration_disallowed")

    def test_504_is_success_class(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(uaid="u")
        session.request.return_value = _Resp(504, body={})
        created = msa_flow.create_account(
            session, state, email="x@outlook.com", password="pw",
            birth_date="1990-01-01T00:00:00.000Z",
            first_name="A", last_name="B",
        )
        self.assertEqual(created["signInName"], "x@outlook.com")

    def test_1312_is_phone_gate(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(uaid="u")
        session.request.return_value = _Resp(200, body={
            "error": {"code": "1312", "data": "", "field": "phoneNumber",
                      "stackTrace": "", "telemetry": ""},
        })
        with self.assertRaises(msa_flow.MSAFlowError) as cm:
            msa_flow.create_account(
                session, state, email="x@outlook.com", password="pw",
                birth_date="1990-01-01T00:00:00.000Z",
                first_name="A", last_name="B",
            )
        self.assertEqual(cm.exception.kind, "phone_gate")

    def test_1312_string_error_stays_disallowed(self) -> None:
        session = MagicMock()
        state = msa_flow.SignupSession(uaid="u")
        session.request.return_value = _Resp(200, body={"error": "1312-ish"})
        with self.assertRaises(msa_flow.MSAFlowError) as cm:
            msa_flow.create_account(
                session, state, email="x@outlook.com", password="pw",
                birth_date="1990-01-01T00:00:00.000Z",
                first_name="A", last_name="B",
            )
        self.assertEqual(cm.exception.kind, "registration_disallowed")


class TestPhoneGate(unittest.TestCase):
    """1312 → SendOtt → ConsumeOneTimeToken → CreateAccount retry chain."""

    def _state(self) -> msa_flow.SignupSession:
        return msa_flow.SignupSession(
            uaid="uaid-1", scid="s", country="US", api_canary="canary-1"
        )

    def test_send_ott_body_and_url(self) -> None:
        session = MagicMock()
        session.request.return_value = _Resp(200, body={})
        state = self._state()
        state.signup_query = "lcid=1033&id=292666"
        msa_flow.send_ott(session, state, proof_id="13800138000",
                          proof_country_iso="CN")
        args = session.request.call_args
        self.assertEqual(args.args[0], "POST")
        self.assertTrue(args.args[1].startswith(
            "https://signup.live.com/API/Proofs/SendOtt?lcid=1033"))
        body = args.kwargs["json"]
        self.assertEqual(body["action"], "SignUp")
        self.assertEqual(body["channel"], "SMS")
        self.assertEqual(body["proofId"], "13800138000")
        self.assertEqual(body["proofCountryIso"], "CN")
        self.assertEqual(body["uaid"], "uaid-1")
        self.assertEqual(body["uiflvr"], 1001)
        # AB direct-post shape: no GB consume fields on send
        self.assertNotIn("destinationPii", body)
        self.assertNotIn("channelType", body)
        self.assertNotIn("ott", body)
        self.assertEqual(args.kwargs["headers"]["canary"], state.api_canary)

    def test_consume_ott_body(self) -> None:
        session = MagicMock()
        session.request.return_value = _Resp(200, body={})
        state = self._state()
        msa_flow.consume_ott(session, state, "+15551000123", "654321")
        body = session.request.call_args.kwargs["json"]
        self.assertEqual(body["action"], "OnlyVerifyNoConsume")
        self.assertEqual(body["ottPurpose"], "VerificationCode")
        self.assertEqual(body["ott"], "654321")
        self.assertEqual(body["channelType"], "SMS")
        self.assertEqual(body["destinationPii"], "+15551000123")
        self.assertTrue(session.request.call_args.args[1].startswith(
            "https://signup.live.com/API/ConsumeOneTimeToken"))

    def test_send_ott_rejection_raises_phone_gate(self) -> None:
        session = MagicMock()
        session.request.return_value = _Resp(200, body={"error": {"code": "1"}})
        with self.assertRaises(msa_flow.MSAFlowError) as cm:
            msa_flow.send_ott(session, self._state(), proof_id="13800138000",
                              proof_country_iso="CN")
        self.assertEqual(cm.exception.kind, "phone_gate")
        self.assertEqual(cm.exception.step, "send_ott")

    def test_phone_verify_and_recreate_success(self) -> None:
        session = MagicMock()
        # send_ott 200, consume_ott 200, create_account 200 with slt
        session.request.side_effect = [
            _Resp(200, body={}),
            _Resp(200, body={}),
            _Resp(200, body={"slt": "slt-token", "redirectUrl": "https://r",
                             "signInName": "x@outlook.com"}),
        ]
        state = self._state()
        provider = MagicMock()
        provider.acquire_number.return_value = sms_client.SmsNumber(
            order_id="ord1", phone_e164="+15551000123",
            proof_id="5551000123", proof_country_iso="US")
        provider.wait_for_code.return_value = "654321"
        created = msa_flow.phone_verify_and_recreate(
            session, state, provider,
            email="x@outlook.com", password="pw",
            birth_date="1990-01-01T00:00:00.000Z",
            first_name="A", last_name="B",
        )
        self.assertEqual(created["slt"], "slt-token")
        provider.acquire_number.assert_called_once_with("US")
        provider.wait_for_code.assert_called_once()
        provider.settle.assert_called_once_with("ord1", success=True)

    def test_phone_verify_and_recreate_releases_on_failure(self) -> None:
        session = MagicMock()
        # send_ott ok, consume_ott rejected
        session.request.side_effect = [
            _Resp(200, body={}),
            _Resp(200, body={"error": {"code": "1205"}}),
        ]
        state = self._state()
        provider = MagicMock()
        provider.acquire_number.return_value = sms_client.SmsNumber(
            order_id="ord2", phone_e164="+15551000123",
            proof_id="5551000123", proof_country_iso="US")
        provider.wait_for_code.return_value = "000000"
        with self.assertRaises(msa_flow.MSAFlowError) as cm:
            msa_flow.phone_verify_and_recreate(
                session, state, provider,
                email="x@outlook.com", password="pw",
                birth_date="1990-01-01T00:00:00.000Z",
                first_name="A", last_name="B",
            )
        self.assertEqual(cm.exception.step, "consume_ott")
        provider.settle.assert_called_once_with("ord2", success=False)


class TestSmsClient(unittest.TestCase):
    """sms-activate handler_api protocol adapter (mocked transport)."""

    def _provider(self) -> sms_client.SmsActivateCompatible:
        session = MagicMock()
        return sms_client.SmsActivateCompatible(
            "k", base_url="https://sms.example", service="mp", session=session
        ), session

    def test_acquire_number(self) -> None:
        provider, session = self._provider()
        session.request.return_value = _Resp(200, text="ACCESS:15551000123:ord9")
        number = provider.acquire_number("US")
        self.assertEqual(number.phone_e164, "+15551000123")
        self.assertEqual(number.order_id, "ord9")
        self.assertEqual(number.proof_id, "5551000123")
        self.assertEqual(number.proof_country_iso, "US")
        self.assertIn("action=getNumber", session.request.call_args.args[1])
        self.assertIn("service=mp", session.request.call_args.args[1])

    def test_wait_for_code(self) -> None:
        provider, session = self._provider()
        session.request.return_value = _Resp(200, text="STATUS_OK:654321")
        self.assertEqual(provider.wait_for_code("ord9", timeout_s=5), "654321")

    def test_settle_success_and_cancel(self) -> None:
        provider, session = self._provider()
        session.request.return_value = _Resp(200, text="ACCESS_ACTIVATION")
        provider.settle("ord9", success=True)
        self.assertIn("status=6", session.request.call_args.args[1])
        provider.settle("ord9", success=False)
        self.assertIn("status=1", session.request.call_args.args[1])

    def test_bad_key_raises(self) -> None:
        provider, session = self._provider()
        session.request.return_value = _Resp(200, text="BAD_KEY")
        with self.assertRaises(sms_client.SmsProviderError):
            provider.acquire_number("US")

    def test_factory_requires_key(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("OUTLOOK_SMS_API_KEY", None)
            self.assertIsNone(sms_client.create_sms_provider())
        with patch.dict("os.environ", {"OUTLOOK_SMS_API_KEY": "k"},
                        clear=False):
            provider = sms_client.create_sms_provider()
            self.assertIsNotNone(provider)


class TestMailOAuth(unittest.TestCase):
    def test_authorize_code_extraction(self) -> None:
        session = MagicMock()
        redirected = _Resp(200, body={})
        redirected.url = "https://outlook.live.com/mail/?code=AUTH123&x=1"
        session.request.return_value = redirected
        steps: dict[str, Any] = {}
        code = msa_flow.fetch_mail_oauth_code(
            session, email="x@outlook.com", scope="graph", pkce_verifier="v", steps=steps
        )
        self.assertEqual(code, "AUTH123")
        self.assertIn("oauth20_authorize.srf", session.request.call_args_list[0].args[1])

    def test_code_from_page_fallback(self) -> None:
        session = MagicMock()
        page = _Resp(
            200,
            text='window.location = "https://outlook.live.com/mail/?code=PAGE9&x"',
        )
        session.request.return_value = page
        code = msa_flow.fetch_mail_oauth_code(
            session, email="x@outlook.com", scope="graph", pkce_verifier="v"
        )
        self.assertEqual(code, "PAGE9")

    def test_no_code_raises_oauth_callback(self) -> None:
        session = MagicMock()
        session.request.return_value = _Resp(200, body={})
        with self.assertRaises(msa_flow.MSAFlowError) as cm:
            msa_flow.fetch_mail_oauth_code(
                session, email="x@outlook.com", scope="graph", pkce_verifier="v"
            )
        self.assertEqual(cm.exception.kind, "oauth_callback")
        self.assertEqual(cm.exception.step, "mail_authorize")

    def test_token_exchange_fields_and_missing_refresh(self) -> None:
        session = MagicMock()
        session.request.return_value = _Resp(200, body={"refresh_token": "rt", "access_token": "at"})
        steps: dict[str, Any] = {}
        body = msa_flow.exchange_mail_token(
            session, code="C", pkce_verifier="V", scope="graph",
            client_id="cid", steps=steps,
        )
        self.assertEqual(body["refresh_token"], "rt")
        call = session.request.call_args
        self.assertIn("oauth2/v2.0/token", call.args[1])
        self.assertEqual(call.kwargs["data"]["grant_type"], "authorization_code")
        self.assertEqual(call.kwargs["data"]["code_verifier"], "V")

        session.request.return_value = _Resp(400, body={"error": "invalid_grant"})
        with self.assertRaises(msa_flow.MSAFlowError) as cm:
            msa_flow.exchange_mail_token(
                session, code="C", pkce_verifier="V", scope="graph", client_id="cid"
            )
        self.assertEqual(cm.exception.kind, "token")


class TestPKCEConsistency(unittest.TestCase):
    """The贯通 invariant: the challenge in the authorize URL must equal
    S256(verifier) of the SAME verifier exchange_mail_token later submits."""

    def test_authorize_challenge_matches_caller_verifier(self) -> None:
        import base64
        import hashlib

        class R:
            status_code = 200
            url = "https://outlook.live.com/mail/?code=C1&x=1"
            text = ""

            def json(self):
                return {}

        session = MagicMock()
        session.request.return_value = R()
        verifier = "unit-test-verifier-0123456789abcdef"
        code = msa_flow.fetch_mail_oauth_code(
            session, email="x@outlook.com", scope="graph",
            pkce_verifier=verifier, steps={},
        )
        self.assertEqual(code, "C1")
        from urllib.parse import parse_qs, urlparse

        sent_url = session.request.call_args_list[0].args[1]
        challenge_in_url = parse_qs(urlparse(sent_url).query)["code_challenge"][0]
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode().rstrip("=")
        self.assertEqual(challenge_in_url, expected)
        # and the helper the flow uses derives the same value
        self.assertEqual(msa_flow.challenge_from_verifier(verifier), expected)

    def test_pkce_pair_is_self_consistent(self) -> None:
        verifier, challenge = msa_flow._pkce()
        self.assertEqual(msa_flow.challenge_from_verifier(verifier), challenge)
        self.assertFalse(challenge.endswith("="))


class TestRiskRetryExhausted(unittest.TestCase):
    def test_second_retry_initialize_raises_provider(self) -> None:
        """One retry_initialize is auto-handled; a second consecutive one must
        fail the attempt at risk_verify (F-7), not walk into CreateAccount."""
        with patch.object(
            msa_flow, "create_session", return_value=MagicMock()
        ), patch.object(msa_flow, "human_pause", return_value=0.0), patch.object(
            msa_flow, "_account_gate", return_value=None
        ), patch.object(
            msa_flow,
            "request_with_retry",
            side_effect=[
                (_Resp(200, text=_serverdata_html()), ""),   # bootstrap acct page
                (_Resp(200, text=""), ""),                    # createAccount page (serverdata from 1st? no — areaConfig first)
            ] + [
                (_Resp(200, body={"isAvailable": True}), ""),  # check_available
                (_Resp(200, body={"continuationToken": "ct", "humanSensorUrl": "h"}), ""),  # risk_initialize
                (_Resp(200, body={"state": "riskInitializationRequired"}), ""),  # verify #1 → retry
                (_Resp(200, body={"state": "riskInitializationRequired"}), ""),  # verify #2 → retry → exhausted
            ],
        ):
            # bootstrap needs two GETs: account page (areaConfig) then signup
            # page (ServerData) — give both, then the API calls above.
            with patch.object(
                msa_flow,
                "request_with_retry",
                side_effect=[
                    (_Resp(200, text='var areaConfig = JSON.stringify({"createAccountUrl": "https://signup.live.com/signup?lic=1"})'), ""),
                    (_Resp(200, text=_serverdata_html()), ""),
                    (_Resp(200, body={"continuationToken": "ct", "humanSensorUrl": "h"}), ""),  # risk_initialize (z_style: before check)
                    (_Resp(200, body={"apiCanary": "c2", "telemetryContext": "tc"}), ""),  # evaluate experiments
                    (_Resp(200, text="<html>sensor</html>"), ""),  # human sensor fidelity GET
                    (_Resp(200, body={"isAvailable": True}), ""),  # check_available
                    (_Resp(200, body={"state": "riskInitializationRequired"}), ""),  # verify #1
                    (_Resp(200, body={"continuationToken": "ct2", "humanSensorUrl": "h"}), ""),  # inner risk_initialize
                    (_Resp(200, body={"state": "riskInitializationRequired"}), ""),  # verify #2 → exhausted
                    (_Resp(200, body={"continuationToken": "ct3", "humanSensorUrl": "h"}), ""),  # inner risk_initialize
                ],
            ):
                result = msa_flow.register_one(proxy="http://p", token_mode="graph")
        self.assertFalse(result.ok)
        self.assertEqual(result.fail_step, "risk_verify")
        self.assertEqual(result.error_kind, "provider")


class TestTopLevelFlow(unittest.TestCase):
    def test_unknown_token_mode_fails_fast(self) -> None:
        result = msa_flow.register_one(proxy="http://p", token_mode="nonsense")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "provider")
        self.assertEqual(result.fail_step, "config")

    def test_network_failure_maps_to_network_kind(self) -> None:
        with patch.object(
            msa_flow,
            "create_session",
            return_value=MagicMock(),
        ), patch.object(
            msa_flow,
            "request_with_retry",
            return_value=(None, "conn refused"),
        ), patch.object(
            msa_flow, "human_pause", return_value=0.0
        ), patch.object(
            msa_flow, "_account_gate", return_value=None
        ):
            result = msa_flow.register_one(proxy="http://p", token_mode="graph")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "network")


class TestPXSolver(unittest.TestCase):
    def test_no_key_fails_fast(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("CAPTCHA_RUN_KEY", None)
            with self.assertRaises(px_client.PXSolverError) as cm:
                px_client.solve_press_token(
                    px_client.PXChallengeContext(uaid="u"), proxy="http://p"
                )
        self.assertEqual(cm.exception.args[0], "captcha_solver_unconfigured")

    def test_proxy_fields_parse(self) -> None:
        fields = px_client._proxy_fields("http://me:pw@host:8080")
        self.assertEqual(fields, {"host": "host", "port": 8080, "login": "me", "password": "pw"})
        self.assertEqual(px_client._proxy_fields(""), {})

    def test_press_success_returns_token(self) -> None:
        session = MagicMock()
        create_resp = _Resp(200, body={"taskId": "T1"})
        poll_resp = _Resp(200, body={"status": "success", "response": {"px3": "P3", "pxvid": "V"}})
        session.request.side_effect = [create_resp, poll_resp]
        with patch.dict("os.environ", {"CAPTCHA_RUN_KEY": "k"}), patch.object(
            px_client, "_session_for", return_value=session
        ), patch.object(px_client.time, "sleep"):
            token = px_client.solve_press_token(
                px_client.PXChallengeContext(uaid="u", country="US"), proxy="http://p"
            )
        self.assertTrue(token.ok)
        self.assertEqual(token.px3, "P3")

    def test_failed_task_raises(self) -> None:
        session = MagicMock()
        session.request.side_effect = [
            _Resp(200, body={"taskId": "T1"}),
            _Resp(200, body={"status": "failed", "error": "no workers"}),
        ]
        with patch.dict("os.environ", {"CAPTCHA_RUN_KEY": "k"}), patch.object(
            px_client, "_session_for", return_value=session
        ), patch.object(px_client.time, "sleep"):
            with self.assertRaises(px_client.PXSolverError) as cm:
                px_client.solve_press_token(
                    px_client.PXChallengeContext(uaid="u"), proxy="http://p"
                )
        self.assertEqual(cm.exception.args[0], "captcha_task_failed")

    def test_silent_fallback_create_carries_app_id(self) -> None:
        # slidex requires appId on EVERY create (PxRequestError otherwise);
        # the silent fallback payload must therefore include it too.
        session = MagicMock()
        session.request.side_effect = [
            _Resp(200, body={"taskId": "T1"}),   # press create
            _Resp(200, body={"status": "fail", "error": "px_token_timeout"}),  # press poll
            _Resp(200, body={"taskId": "T2"}),   # silent create
            _Resp(200, body={"status": "success", "response": {"px3": "S3"}}),  # silent poll
        ]
        with patch.dict("os.environ", {"CAPTCHA_RUN_KEY": "k"}), patch.object(
            px_client, "_session_for", return_value=session
        ), patch.object(px_client.time, "sleep"):
            token = px_client.solve_press_token(
                px_client.PXChallengeContext(
                    uaid="u", challenge_url="https://iframe.hsprotect.net/index.html?app_id=x"
                ),
                proxy="http://p",
            )
        self.assertTrue(token.ok)
        self.assertEqual(token.px3, "S3")
        creates = [
            c for c in session.request.call_args_list if c.args[0] == "POST"
        ]
        self.assertEqual(len(creates), 2)
        for call in creates:
            payload = call.kwargs["json"]
            self.assertEqual(payload.get("appId"), px_client.PX_APP_ID)
        # press create has challengeUrl (mode=press), silent has only websiteURL
        self.assertTrue(creates[0].kwargs["json"].get("challengeUrl"))
        self.assertNotIn("challengeUrl", creates[1].kwargs["json"])
        self.assertTrue(creates[1].kwargs["json"].get("websiteURL"))

    def test_solve_silent_token_direct(self) -> None:
        # Standalone silent entry: no challengeUrl/uuid/vid — sensor on the
        # protected page only; polls with captchaType=silent.
        session = MagicMock()
        session.request.side_effect = [
            _Resp(200, body={"taskId": "S1"}),
            _Resp(200, body={"status": "success", "response": {"px3": "PX", "pxde": "D", "pxvid": "V"}}),
        ]
        with patch.dict("os.environ", {"CAPTCHA_RUN_KEY": "k"}), patch.object(
            px_client, "_session_for", return_value=session
        ), patch.object(px_client.time, "sleep"):
            token = px_client.solve_silent_token(
                px_client.PXChallengeContext(uaid="u", country="US"), proxy="http://p"
            )
        self.assertTrue(token.ok)
        self.assertEqual(token.px3, "PX")
        self.assertEqual(token.pxvid, "V")
        create = [c for c in session.request.call_args_list if c.args[0] == "POST"][0]
        payload = create.kwargs["json"]
        self.assertEqual(payload["appId"], px_client.PX_APP_ID)
        self.assertNotIn("challengeUrl", payload)
        self.assertEqual(payload["websiteURL"], "https://signup.live.com/")
        poll = [c for c in session.request.call_args_list if c.args[0] == "GET"][0]
        self.assertEqual(poll.kwargs["params"]["captchaType"], "silent")

    def test_solve_silent_token_unconfigured(self) -> None:
        with patch.dict("os.environ", {}, clear=False), patch.object(
            px_client, "_api_key", return_value=""
        ):
            with self.assertRaises(px_client.PXSolverError) as cm:
                px_client.solve_silent_token(
                    px_client.PXChallengeContext(uaid="u"), proxy="http://p"
                )
        self.assertEqual(cm.exception.args[0], "captcha_solver_unconfigured")


class TestMailClient(unittest.TestCase):
    def test_refresh_exchange_ok(self) -> None:
        session = MagicMock()
        session.request.return_value = _Resp(
            200, body={"access_token": "at", "refresh_token": "rt2"}
        )
        token = exchange_refresh_token(
            session, refresh_token="rt1", client_id="cid", scope="graph"
        )
        self.assertTrue(token.ok)
        call = session.request.call_args
        self.assertEqual(call.kwargs["data"]["grant_type"], "refresh_token")

    def test_invalid_grant_passthrough(self) -> None:
        session = MagicMock()
        session.request.return_value = _Resp(
            400,
            body={
                "error": "invalid_grant",
                "error_description": "AADSTS7000215: Invalid client secret",
            },
        )
        token = exchange_refresh_token(session, refresh_token="rt", client_id="cid")
        self.assertFalse(token.ok)
        self.assertIn("AADSTS7000215", token.error)

    def test_otp_extract_filters_years(self) -> None:
        self.assertEqual(_extract_otp("Your code is 482913"), "482913")
        self.assertEqual(_extract_otp("(c) 2026 Microsoft. Use 774411"), "774411")
        self.assertEqual(_extract_otp("since 2019 only 2020 code 554433"), "554433")
        self.assertEqual(_extract_otp("no digits beyond 1234567890"), "")

    def test_read_otp_scans_junk_after_inbox(self) -> None:
        session = MagicMock()
        inbox = _Resp(
            200,
            body={"value": [
                {
                    "from": {"emailAddress": {"address": "someone@example.com"}},
                    "subject": "hi 998877",
                }
            ]},
        )
        junk = _Resp(
            200,
            body={"value": [
                {
                    "from": {"emailAddress": {"address": "noreply@accountprotection.microsoft.com"}},
                    "subject": "Microsoft account code 445566",
                    "body": {"content": "Your single-use code is 445566"},
                }
            ]},
        )
        session.request.side_effect = [inbox, junk]
        result = read_otp_code(session, access_token="at", mode="graph")
        self.assertTrue(result.ok)
        self.assertEqual(result.code, "445566")
        self.assertEqual(result.folder, "junkemail")

    def test_read_otp_trusted_sender_filter(self) -> None:
        session = MagicMock()
        junk = _Resp(
            200,
            body={"value": [{"from": {"emailAddress": {"address": "spam@evil.com"}},
                             "subject": "code 111222"}]},
        )
        session.request.return_value = junk
        result = read_otp_code(session, access_token="at", mode="graph")
        self.assertFalse(result.ok)

    def test_graph_and_rest_urls_differ(self) -> None:
        session = MagicMock()
        session.request.return_value = _Resp(200, body={"value": []})
        read_otp_code(session, access_token="a", mode="graph")
        graph_url = session.request.call_args.args[1]
        session.request.return_value = _Resp(200, body={"value": []})
        read_otp_code(session, access_token="a", mode="outlook_rest")
        rest_url = session.request.call_args.args[1]
        self.assertIn("graph.microsoft.com", graph_url)
        self.assertIn("outlook.office.com", rest_url)


class TestPacing(unittest.TestCase):
    def test_step_jitter_bounds(self) -> None:
        from providers.outlook.protocol import pacing

        with patch.dict(
            "os.environ",
            {"OUTLOOK_STEP_JITTER_MIN": "1", "OUTLOOK_STEP_JITTER_MAX": "2"},
        ):
            self.assertEqual(pacing.step_jitter_range(), (1.0, 2.0))
        # minutes → seconds for account spacing
        with patch.dict(
            "os.environ",
            {"OUTLOOK_REG_JITTER_MIN": "0.01", "OUTLOOK_REG_JITTER_MAX": "0.02"},
        ):
            lo, hi = pacing.account_delay_range_minutes()
            self.assertEqual((lo, hi), (0.6, 1.2))
            seconds = pacing.account_pause(log=lambda _m: None)
            self.assertGreaterEqual(seconds, 0.6)
            self.assertLessEqual(seconds, 1.2)

    def test_human_pause_logs(self) -> None:
        from providers.outlook.protocol import pacing

        logs: list[str] = []
        with patch.dict(
            "os.environ",
            {"OUTLOOK_STEP_JITTER_MIN": "0", "OUTLOOK_STEP_JITTER_MAX": "0.05"},
        ), patch.object(pacing.time, "sleep"):
            pacing.human_pause(after_step="bootstrap", log=logs.append)
        self.assertTrue(len(logs) == 1 and "human_pace" in logs[0] and "bootstrap" in logs[0])


class TestAdapterProtocolMode(unittest.TestCase):
    def _provider(self, **opts: Any) -> Any:
        from register_core.providers.outlook_adapter import OutlookProvider

        import os

        os.environ.pop("OUTLOOK_AUTHS_DIR", None)
        return OutlookProvider(**opts)

    def test_mode_and_token_mode_validation(self) -> None:
        from register_core.providers.outlook_adapter import OutlookProvider

        with self.assertRaises(ValueError):
            OutlookProvider(mode="nonsense")
        with self.assertRaises(ValueError):
            OutlookProvider(mail_token_mode="nonsense")
        self.assertEqual(self._provider()._mode, "browser")
        self.assertEqual(self._provider(mode="protocol")._mode, "protocol")
        self.assertEqual(self._provider(mail_token_mode="login_exe")._token_mode, "login_exe")

    def test_live_gate_first_before_protocol_flow(self) -> None:
        import os

        provider = self._provider()
        old = os.environ.pop("GROK_REGISTER_OUTLOOK_LIVE", None)
        try:
            result = provider.register_one(extra={"proxy": "http://p"})
        finally:
            if old is not None:
                os.environ["GROK_REGISTER_OUTLOOK_LIVE"] = old
        self.assertFalse(result.ok)
        self.assertIn("GROK_REGISTER_OUTLOOK_LIVE", result.error)

    def test_protocol_missing_proxy(self) -> None:
        import os

        os.environ["GROK_REGISTER_OUTLOOK_LIVE"] = "1"
        try:
            provider = self._provider(mode="protocol")
            result = provider.register_one(extra={})
            self.assertFalse(result.ok)
            self.assertEqual(result.error_kind, "proxy")
        finally:
            os.environ.pop("GROK_REGISTER_OUTLOOK_LIVE", None)

    def test_protocol_failure_maps_fail_step(self) -> None:
        import os

        os.environ["GROK_REGISTER_OUTLOOK_LIVE"] = "1"
        try:
            provider = self._provider(mode="protocol")
            fake = msa_flow.SignupResult(
                ok=False, error="server_data_missing", error_kind="provider",
                fail_step="bootstrap", steps={"bootstrap": {}},
            )
            with patch.object(msa_flow, "register_one", return_value=fake):
                result = provider.register_one(extra={"proxy": "http://p"})
            self.assertFalse(result.ok)
            self.assertEqual(result.artifacts["fail_step"], "bootstrap")
            self.assertEqual(result.artifacts["mode"], "protocol")
        finally:
            os.environ.pop("GROK_REGISTER_OUTLOOK_LIVE", None)

    def test_protocol_success_writes_0600_artifact(self) -> None:
        import os
        import stat

        os.environ["GROK_REGISTER_OUTLOOK_LIVE"] = "1"
        try:
            with tempfile.TemporaryDirectory() as td:
                provider = self._provider(mode="protocol", outlook_auths_dir=td)
                fake = msa_flow.SignupResult(
                    ok=True, email="fresh11@outlook.com", password="pw9",
                    refresh_token="rt-token", steps={"bootstrap": {}, "create_account": {}},
                )
                with patch.object(msa_flow, "register_one", return_value=fake):
                    result = provider.register_one(extra={"proxy": "http://p"})
                self.assertTrue(result.ok)
                self.assertEqual(result.secret_kind, "refresh_token")
                path = Path(result.artifacts["outlook_auth_path"])
                self.assertTrue(path.exists())
                mode = stat.S_IMODE(path.stat().st_mode)
                self.assertEqual(mode, 0o600)
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["email"], "fresh11@outlook.com")
                self.assertIn("mail_token_mode", result.artifacts)
                self.assertIn("create_account", result.artifacts["outlook_steps"])
        finally:
            os.environ.pop("GROK_REGISTER_OUTLOOK_LIVE", None)


if __name__ == "__main__":
    unittest.main()
