"""Outlook / MSA signup protocol flow (in-process, HTTP-only).

Z-style route (default): account.microsoft.com → createAccountUrl ServerData →
PX warmup → CheckAvailableSignInNames → risk/initialize → risk/verify (#1
silent, #2 press on challenge) → CreateAccount → slt auto-post chain →
mail OAuth (authorize + code exchange) → proofs binding. Skipping proofs is
deliberately NOT implemented (cancel == high ban rate); recovery binding rides
the existing adapter path.

Research-then-rewrite discipline: the flow ORDER and field names are protocol
facts (docs/outlook-protocol-notes.md); the implementation is this repo's own.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from .constants import (
    ACCOUNT_BASE,
    CONSUME_OTT_URL,
    COBRAND_ID,
    COMMON_HEADERS,
    DEFAULT_LC,
    DEFAULT_MKT,
    LOGIN_BASE,
    LOGIN_MS_BASE,
    MS_TOKEN_URL,
    OUTLOOK_CLIENT_ID,
    OUTLOOK_REDIRECT_URI,
    SEND_OTT_URL,
    SIGNUP_API_BASE,
    SIGNUP_CLIENT_ID,
    TOKEN_MODE_CLIENT_ID,
    TOKEN_MODE_SCOPE,
    USER_AGENT,
    mkt_lc_for_country,
)
from .pacing import (
    account_delay_range_minutes,
    cold_start_delay_s,
    human_pause,
)
from .px_client import (
    PXChallengeContext,
    PXPressToken,
    apply_px_cookies,
    solve_press_token,
    solve_silent_token as px_client_solve_silent_token,
)
from .session import create_session, request_with_retry, response_json, response_text

# ServerData extraction: the signup SPA embeds a JS object literal after
# "var ServerData = "; raw_decode picks the balanced JSON value.
_SERVERDATA_MARK = "var ServerData"
_AREA_CONFIG_RE = re.compile(r"var areaConfig = JSON\.stringify\((\{.*?\})\)")
_UNESCAPES = (("\\u0026", "&"), ("\\/", "/"))


class MSAFlowError(RuntimeError):
    """Single-attempt signup failure (adapter maps to RegisterResult)."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "provider",
        step: str = "",
        steps: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.step = step or ""
        self.steps: dict[str, Any] = dict(steps or {})


@dataclass
class SignupSession:
    """Facts captured from bootstrap, threaded through the whole flow."""

    uaid: str = ""
    api_canary: str = ""
    scid: str = ""
    hpgid: int = 200225
    i_scenario_id: int = 100118
    sru: str = ""
    url_risk_initialize: str = ""
    url_risk_verify: str = ""
    url_check_available: str = ""
    px_fpt_url: str = ""
    site_id: str = ""
    continuation_token: str = ""
    human_sensor_url: str = ""
    country: str = "US"
    mkt: str = DEFAULT_MKT
    lc: int = DEFAULT_LC
    # Session-completeness facts (upstream z_style parity, 2026-11-08): the
    # final signup page URL (Referer), its raw query string (appended to the
    # CheckAvailable/CreateAccount endpoints), and the telemetry context the
    # experiment endpoint refreshes.
    signup_page_url: str = ""
    signup_query: str = ""
    telemetry_context: str = ""


@dataclass
class SignupResult:
    ok: bool
    email: str = ""
    password: str = ""
    refresh_token: str = ""
    error: str = ""
    error_kind: str = ""
    fail_step: str = ""
    steps: dict[str, Any] = field(default_factory=dict)


def _unescape(text: str) -> str:
    for old, new in _UNESCAPES:
        text = text.replace(old, new)
    return text


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    return verifier, challenge_from_verifier(verifier)


def challenge_from_verifier(verifier: str) -> str:
    """S256 code_challenge for a verifier — the ONLY derivation used both in
    the authorize URL and at token exchange (PKCE pair must stay same-origin)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _random_prefix() -> str:
    # Seller style (upstream-aligned): 10–12 lowercase letters, no digits —
    # digit/year suffixes are the loudest farm signature.
    length = 10 + secrets.randbelow(3)
    return "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(length))


def _random_password() -> str:
    # Seller style: 11–14 chars, lowercase + digits only (satisfies the MSA
    # two-character-class rule; symbol-heavy passwords are out-of-population).
    length = 11 + secrets.randbelow(4)
    chars = "abcdefghijkmnopqrstuvwxyz23456789"
    while True:
        password = "".join(secrets.choice(chars) for _ in range(length))
        if any(c.isdigit() for c in password):
            return password


def _birth_date() -> str:
    # 1985–2000 → adult, low-risk age band.
    year = 1985 + secrets.randbelow(16)
    month = 1 + secrets.randbelow(12)
    day = 1 + secrets.randbelow(28)
    return f"{year:04d}-{month:02d}-{day:02d}T00:00:00.000Z"


# Common Anglo-American first/last names (own pool, same principle as the
# seller style: no fixed persona, no out-of-population names — a repeated
# "Alex Morgan" across accounts is a farm signal).
_FIRST_NAMES = (
    "James", "John", "Robert", "Michael", "William", "David", "Richard",
    "Thomas", "Daniel", "Matthew", "Anthony", "Christopher", "Andrew",
    "Joshua", "Kevin", "Brian", "George", "Edward", "Ryan", "Jacob",
    "Nicholas", "Eric", "Jonathan", "Stephen", "Justin", "Scott", "Brandon",
    "Benjamin", "Samuel", "Gregory", "Alexander", "Patrick", "Jack", "Adam",
    "Henry", "Peter", "Nathan", "Douglas", "Zachary", "Tyler",
    "Aaron", "Jose", "Keith", "Walter", "Lawrence", "Wayne", "Vincent",
    "Mary", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan", "Jessica",
    "Sarah", "Karen", "Nancy", "Lisa", "Betty", "Margaret", "Sandra",
    "Ashley", "Kimberly", "Emily", "Donna", "Michelle", "Carol", "Amanda",
    "Melissa", "Deborah", "Stephanie", "Rebecca", "Laura", "Sharon",
    "Cynthia", "Kathleen", "Amy", "Angela", "Anna", "Brenda", "Pamela",
    "Nicole", "Samantha", "Katherine", "Christine", "Rachel", "Lauren",
)
_LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green",
    "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner", "Diaz",
    "Parker", "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris",
    "Morales", "Murphy", "Cook", "Rogers", "Gutierrez", "Ortiz", "Morgan",
    "Cooper", "Peterson", "Bailey", "Reed", "Kelly", "Howard", "Cox",
    "Ward", "Richardson", "Watson", "Brooks", "Wood", "Bennett", "Gray",
    "Fisher", "Price", "Barnes", "Bell",
)


def _random_name() -> tuple[str, str]:
    return secrets.choice(_FIRST_NAMES), secrets.choice(_LAST_NAMES)


def _full_url(base: str, value: str) -> str:
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return base.rstrip("/") + "/" + value.lstrip("/")


def parse_server_data(html: str) -> dict[str, Any]:
    """Extract the ``var ServerData = {...}`` object from a signup page."""
    idx = (html or "").find(_SERVERDATA_MARK)
    if idx < 0:
        return {}
    idx = html.find("=", idx)
    if idx < 0:
        return {}
    start = html.find("{", idx)
    if start < 0:
        return {}
    decoder = json.JSONDecoder()
    try:
        value, _end = decoder.raw_decode(html[start:])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------
# bootstrap: steps 1–2
# --------------------------------------------------------------------------


def bootstrap_account_session(
    session: Any,
    *,
    country: str = "US",
    steps: dict[str, Any] | None = None,
) -> SignupSession:
    """Resolve the createAccountUrl and harvest ServerData facts (steps 1–2)."""
    steps = steps if steps is not None else {}
    mkt, lc = mkt_lc_for_country(country)
    state = SignupSession(country=country.upper(), mkt=mkt, lc=lc)

    resp, err = request_with_retry(session, "GET", ACCOUNT_BASE + "/", retry_attempts=3)
    if resp is None:
        raise MSAFlowError(
            f"bootstrap_account_page: {err}", kind="network", step="bootstrap", steps=steps
        )
    create_url = ""
    match = _AREA_CONFIG_RE.search(response_text(resp))
    if match:
        try:
            area = json.loads(match.group(1))
            create_url = str(area.get("createAccountUrl") or "").replace(":443", "")
        except ValueError:
            create_url = ""
    if not create_url:
        create_url = f"https://signup.live.com/signup?lic=1&mkt={mkt}"
    steps["bootstrap"] = {"create_url_host": urlparse(create_url).netloc}

    resp, err = request_with_retry(session, "GET", create_url, retry_attempts=3)
    if resp is None:
        raise MSAFlowError(
            f"create_account_page: {err}", kind="network", step="bootstrap", steps=steps
        )
    # The page may redirect (createAccountUrl → final signup URL); the final
    # URL's query (cobrandid/uaid/lic/mkt/...) rides the CheckAvailable /
    # CreateAccount endpoints (upstream z_style parity) and the page URL is
    # the API Referer.
    signup_page_url = str(getattr(resp, "url", "") or create_url) or create_url
    state.signup_page_url = signup_page_url
    state.signup_query = urlparse(signup_page_url).query
    server = parse_server_data(_unescape(response_text(resp)))
    if not server:
        raise MSAFlowError("server_data_missing", kind="provider", step="bootstrap", steps=steps)

    state.uaid = str(server.get("sUnauthSessionID") or server.get("uaid") or "")
    state.api_canary = str(server.get("apiCanary") or "")
    state.scid = str(server.get("scid") or "")
    for key, target in (("hpgid", "hpgid"), ("iScenarioId", "i_scenario_id")):
        try:
            setattr(state, target, int(server.get(key) or getattr(state, target)))
        except (TypeError, ValueError):
            pass
    state.sru = str(server.get("sru") or "")
    raw_init = str(server.get("urlRiskInitialize") or "")
    raw_verify = str(server.get("urlRiskVerify") or "")
    raw_check = str(server.get("urlCheckAvailableSigninNames") or "")
    state.url_risk_initialize = _full_url(LOGIN_MS_BASE, raw_init) if raw_init else ""
    state.url_risk_verify = _full_url(LOGIN_MS_BASE, raw_verify) if raw_verify else ""
    state.url_check_available = _full_url(SIGNUP_API_BASE, raw_check) if raw_check else ""
    captcha_info = server.get("oCaptchaInfo") or {}
    if isinstance(captcha_info, dict):
        state.px_fpt_url = str(captcha_info.get("urlDfp") or "")
    state.site_id = str(server.get("sSiteId") or "")
    if not state.url_risk_initialize or not state.url_risk_verify:
        raise MSAFlowError("risk_urls_missing", kind="provider", step="bootstrap", steps=steps)
    if steps is not None:
        steps.setdefault("bootstrap", {})["signup_query"] = bool(state.signup_query)
    return state


# --------------------------------------------------------------------------
# availability + risk chain: steps 6–10
# --------------------------------------------------------------------------


def _signup_headers(state: SignupSession) -> dict[str, str]:
    # Header set mirrors the signup SPA's fetch on every signup API call
    # (upstream z_style parity, 2026-11-08): the uaid rides correlationId /
    # client-request-id and hpgid/hpgact accompany the canary — the risk
    # engine scores these alongside the body.
    headers = dict(COMMON_HEADERS)
    headers["origin"] = "https://signup.live.com"
    headers["referer"] = state.signup_page_url or "https://signup.live.com/"
    if state.api_canary:
        headers["canary"] = state.api_canary
    if state.uaid:
        headers["correlationId"] = state.uaid
        headers["client-request-id"] = state.uaid
    headers["hpgid"] = str(state.hpgid)
    headers["hpgact"] = "0"
    return headers


def _signup_api_url(state: SignupSession, endpoint: str, fallback: str = "") -> str:
    """signup API endpoint with the bootstrap page's query string attached.

    The SPA posts to signup.live.com/API/<Endpoint>?<page query> (cobrandid /
    uaid / mkt / ... ride along); the ServerData-provided URL is the fallback
    when no query was captured.
    """
    if state.signup_query:
        return f"{SIGNUP_API_BASE}/{endpoint}?{state.signup_query}"
    return fallback or f"{SIGNUP_API_BASE}/{endpoint}"


# Client experiments the signup SPA evaluates immediately after risk
# initialize (z_style order, upstream liveiaxion). Field name/value facts,
# reimplemented; the response's apiCanary refresh is the operative part —
# telemetryContext is stored but no further endpoint consumes it.
_CLIENT_EXPERIMENTS = [
    {
        "parallax": "enablesisufeedback",
        "control": "enablesisufeedback_control",
        "treatments": ["enablesisufeedback_treatment"],
    },
    {
        "parallax": "addprivatebrowsingtexttofabricfooter",
        "control": "addprivatebrowsingtexttofabricfooter_control",
        "treatments": ["addprivatebrowsingtexttofabricfooter_treatment"],
    },
]


def evaluate_experiment_assignments(
    session: Any,
    state: SignupSession,
    *,
    steps: dict[str, Any] | None = None,
) -> bool:
    """EvaluateExperimentAssignments → canary/telemetry refresh.

    Posted right after risk initialize, before the availability check (the
    SPA's z_style order). Returns True when the response carried an apiCanary
    (which is applied); failure response or unparsable body just returns
    False — the step is best-effort, the pre-existing canary remains valid.
    """
    resp, err = request_with_retry(
        session,
        "POST",
        _signup_api_url(state, "EvaluateExperimentAssignments"),
        retry_attempts=2,
        headers=_signup_headers(state),
        json={"clientExperiments": _CLIENT_EXPERIMENTS},
    )
    if resp is None:
        if steps is not None:
            steps["evaluate_experiments"] = {"error": (err or "")[:80]}
        return False
    body = response_json(resp)
    canary = str(body.get("apiCanary") or "")
    telemetry = str(body.get("telemetryContext") or "")
    if canary:
        state.api_canary = canary
    if telemetry:
        state.telemetry_context = telemetry
    if steps is not None:
        steps["evaluate_experiments"] = {
            "has_canary": bool(canary),
            "has_telemetry": bool(telemetry),
        }
    return bool(canary)


def check_available_signin_name(
    session: Any,
    state: SignupSession,
    email: str,
    *,
    steps: dict[str, Any] | None = None,
) -> bool:
    """Step 6: CheckAvailableSignInNames — True when the name is free.

    Body field MUST be camelCase ``signInName`` (the SPA's exact key): a
    snake_case key makes the API return an HTML error page (errcode=1046)
    instead of JSON, which would misreport every name as taken. The URL is
    the ServerData-provided ``urlCheckAvailableSigninNames``, not a hardcoded
    path — the SPA resolves it the same way.
    """
    url = _signup_api_url(
        state, "CheckAvailableSigninNames", state.url_check_available
    )
    resp, err = request_with_retry(
        session,
        "POST",
        url,
        retry_attempts=2,
        headers=_signup_headers(state),
        json={
            "includeSuggestions": True,
            "signInName": email,
            "uiflvr": 1001,
            "scid": state.scid,
            "uaid": state.uaid,
            "hpgid": state.hpgid,
        },
    )
    if steps is not None:
        steps.setdefault("check_available", {})["domain"] = (
            email.rsplit("@", 1)[-1] if "@" in email else ""
        )
    if resp is None:
        raise MSAFlowError(
            f"check_available: {err}", kind="network", step="check_available", steps=steps
        )
    body = response_json(resp)
    if body.get("apiCanary"):
        state.api_canary = str(body["apiCanary"])
    if not body:
        # HTML error page (e.g. errcode=1046): not "taken" — the protocol
        # broke. Fail loudly so the retry/burn machinery sees it.
        raise MSAFlowError(
            "check_available: non-JSON response", kind="provider", step="check_available", steps=steps
        )
    return bool(body.get("isAvailable") or body.get("available"))


def risk_initialize(
    session: Any,
    state: SignupSession,
    *,
    steps: dict[str, Any] | None = None,
) -> None:
    """Step 7: risk/initialize → continuation token + human sensor url."""
    resp, err = request_with_retry(
        session,
        "POST",
        state.url_risk_initialize,
        retry_attempts=2,
        timeout=120.0,
        headers=_signup_headers(state),
        json={"continuationToken": state.continuation_token},
    )
    if resp is None:
        raise MSAFlowError(
            f"risk_initialize: {err}", kind="network", step="risk_initialize", steps=steps
        )
    body = response_json(resp)
    state.continuation_token = str(
        body.get("continuationToken") or state.continuation_token
    )
    # Live fact (2026-11-08): the sensor URL is nested at
    # riskInitializationData[0].humanSensorUrl; older responses exposed it
    # top-level, so keep the flat read as fallback.
    sensor_url = str(body.get("humanSensorUrl") or "")
    if not sensor_url:
        init_data = body.get("riskInitializationData")
        if isinstance(init_data, list) and init_data:
            first = init_data[0]
            if isinstance(first, dict):
                sensor_url = str(first.get("humanSensorUrl") or "")
    state.human_sensor_url = sensor_url
    if steps is not None:
        steps["risk_initialize"] = {"has_sensor_url": bool(state.human_sensor_url)}


def build_risk_verify_signature(
    *,
    member_name: str,
    birth_date: str,
    first_name: str,
    last_name: str,
    country: str,
) -> dict[str, Any]:
    """SPA-exact msaRiskVerifySignature object (the ``SR`` builder in the
    signup bundle). Must be a nested JSON object, not a string — a string
    fails server-side body parse ("Failed to parse request body"). Field
    facts (live-verified 2026-11-07):

    - ``birthdate`` is the SPA's ``$N`` format ``DD:MM:YYYY`` (day/month
      zero-padded, year last), NOT ISO-8601;
    - ``siteId``/``appId`` come from ServerData ``sClientId`` (falling back
      to ``sSiteId`` for siteId); signup.live.com flows carry
      SIGNUP_CLIENT_ID as appId;
    - ``uiFlavor`` is the enum *name* for ServerData ``iUiFlavor`` (1001 →
      "Web"), not the number.
    """
    year, month, day = birth_date[:10].split("-")
    return {
        "memberName": member_name,
        "siteId": SIGNUP_CLIENT_ID,
        "uiFlavor": "Web",
        "appId": SIGNUP_CLIENT_ID,
        "birthdate": f"{day}:{month}:{year}",
        "firstName": first_name,
        "lastName": last_name,
        "countryCode": country,
        "verificationCode": "",
        "deviceDetails": {"isRdm": False},
        "action": "SignUp",
    }


def build_create_signature(
    *,
    member_name: str,
    birth_date: str,
    first_name: str,
    last_name: str,
    country: str,
    site_id: str = "",
) -> dict[str, Any]:
    """The z_style verify #1 signature (``msaCreateSignature``).

    Distinct from the msaRiskVerifySignature object: siteId is ServerData
    ``sSiteId`` (fallback 292666), appId is empty, and there is no ``action``
    field — this is the account.microsoft.com reference shape submitted when
    the verify #1 body already carries silent PX tokens (upstream z_style
    parity, 2026-11-08).
    """
    year, month, day = birth_date[:10].split("-")
    return {
        "memberName": member_name,
        "siteId": site_id or "292666",
        "uiFlavor": "Web",
        "appId": "",
        "birthdate": f"{day}:{month}:{year}",
        "firstName": first_name,
        "lastName": last_name,
        "countryCode": country,
        "verificationCode": "",
        "deviceDetails": {"isRdm": False},
    }


def risk_verify_silent(
    session: Any,
    state: SignupSession,
    *,
    signature: dict[str, Any],
    px_metadata: list[dict[str, str]] | None = None,
    steps: dict[str, Any] | None = None,
) -> str:
    """Step 9: silent risk/verify. Returns '' when cleared, else a branch hint.

    Protocol facts (live-verified 2026-11-07): the body is
    ``{continuationToken, msaRiskVerifySignature}`` — the signature is the
    SR object. ``riskProviderMetadata`` is the array of PX tokens
    (``[{riskProvider:"Human", px3, pxde, pxvid}]``) harvested by the human
    sensor iframe; pure-HTTP clients have none, so it is omitted here and
    the challenge branch (captcha.run press token) is the designed path.

    z_style variant (upstream parity, 2026-11-08): when silent PX tokens ARE
    available (solver service), the body switches to
    ``{continuationToken, riskProviderMetadata(silent), msaCreateSignature}``
    — the metadata array carries exactly px3/pxde/pxvid (no pxcts) and the
    signature is the msaCreateSignature object (siteId=sSiteId, appId="",
    no action). A clean silent score can clear verify #1 outright.
    """
    if px_metadata:
        body: dict[str, Any] = {
            "continuationToken": state.continuation_token,
            "riskProviderMetadata": px_metadata,
            "msaCreateSignature": signature,
        }
    else:
        body = {
            "continuationToken": state.continuation_token,
            "msaRiskVerifySignature": signature,
        }
    resp, err = request_with_retry(
        session,
        "POST",
        state.url_risk_verify,
        retry_attempts=1,
        timeout=120.0,
        headers=_signup_headers(state),
        json=body,
    )
    if resp is None:
        raise MSAFlowError(
            f"risk_verify: {err}", kind="network", step="risk_verify", steps=steps
        )
    if resp.status_code == 403:
        # AADSTS7005106 riskBlock: the server hard-blocks a silent verify
        # carrying no PX tokens (live-verified twice). This is the SPA's cue
        # to run the human sensor and retry WITH a press token — not a proxy
        # fault. Route to the challenge branch instead of failing the attempt.
        if steps is not None:
            steps["risk_verify_silent"] = {"state": "riskBlock_403"}
        return "challenge"
    body = response_json(resp)
    if body.get("continuationToken"):
        state.continuation_token = str(body["continuationToken"])
    status = str(body.get("state") or "")
    if steps is not None:
        steps["risk_verify_silent"] = {"state": status or "no_state"}
    if status == "riskInitializationRequired":
        risk_initialize(session, state, steps=steps)
        return "retry_initialize"
    if status == "riskChallengeRequired":
        return "challenge"
    return ""


def solve_challenge(
    session: Any,
    state: SignupSession,
    *,
    proxy: str,
    user_agent: str,
    timezone: str,
    steps: dict[str, Any] | None = None,
) -> None:
    """Step 10: press challenge — solver token, then risk/verify #2."""
    challenge_meta: dict[str, Any] = {}
    if state.human_sensor_url:
        resp, _err = request_with_retry(
            session, "GET", state.human_sensor_url, retry_attempts=2
        )
        try:
            body = resp.json() if resp is not None and isinstance(resp.json(), dict) else {}
            challenge_meta = body.get("challengeDetails") or {}
        except Exception:
            challenge_meta = {}
    ctx = PXChallengeContext(
        uaid=state.uaid,
        uuid=str(challenge_meta.get("leadingChallengeId") or ""),
        vid=str(challenge_meta.get("deviceId") or ""),
        challenge_url=str(challenge_meta.get("challengeUrl") or state.human_sensor_url),
        country=state.country,
        timezone=timezone,
        user_agent=user_agent,
    )
    token = solve_press_token(ctx, proxy=proxy)
    _submit_press_token(session, state, token=token, steps=steps)


def _submit_press_token(
    session: Any,
    state: SignupSession,
    *,
    token: PXPressToken,
    steps: dict[str, Any] | None = None,
) -> None:
    # SPA-exact shapes (bundle builders KU/YU): challengeSolution is an OBJECT
    # and riskProviderMetadata is an ARRAY of objects — a JSON-string in either
    # field fails server-side body parse, same as the string-signature bug.
    # Same press token goes into BOTH fields; pxvid pins the challenge vid.
    metadata = [
        {
            "riskProvider": "Human",
            "px3": token.px3,
            "pxde": token.pxde,
            "pxvid": token.pxvid,
            "pxcts": token.pxcts,
        }
    ]
    solution = {
        "challengeType": "HumanCaptcha",
        "px3": token.px3,
        "pxde": token.pxde,
        "pxvid": token.pxvid,
    }
    resp, err = request_with_retry(
        session,
        "POST",
        state.url_risk_verify,
        retry_attempts=1,
        timeout=120.0,
        headers=_signup_headers(state),
        json={
            "continuationToken": state.continuation_token,
            "challengeSolution": solution,
            "riskProviderMetadata": metadata,
        },
    )
    if resp is None:
        raise MSAFlowError(
            f"risk_verify_press: {err}", kind="network", step="risk_verify", steps=steps
        )
    if resp.status_code == 403:
        raise MSAFlowError("risk_blocked", kind="proxy", step="risk_verify", steps=steps)
    body = response_json(resp)
    if body.get("continuationToken"):
        state.continuation_token = str(body["continuationToken"])
    status = str(body.get("state") or "")
    if steps is not None:
        steps["risk_verify_press"] = {
            "state": status or "no_state",
            "has_token": bool(token.px3),
        }
    if status not in ("", "continue", "verified", "NO_ERROR"):
        raise MSAFlowError(
            f"risk_challenge_unsolved:{status}", kind="captcha", step="risk_verify", steps=steps
        )


# --------------------------------------------------------------------------
# CreateAccount: step 11
# --------------------------------------------------------------------------


def create_account(
    session: Any,
    state: SignupSession,
    *,
    email: str,
    password: str,
    birth_date: str,
    first_name: str,
    last_name: str,
    check_map: list[str] | None = None,
    steps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Step 11: CreateAccount → {redirectUrl, slt, signInName} response facts.

    Body is the SPA's exact JN-builder shape (read from the signup-fluent_v2
    bundle, live-differential 2026-11-08):

    - ``BirthDate`` is ``$N(day,month,year)`` = "DD:MM:YYYY" (day/month
      padStart(2), year raw). ISO-8601 → error 1039 field=birthdate.
    - ``RequestTimeStamp`` with a capital S and an ISO string — not
      ``RequestTimestamp``/epoch (name+format both part of the 1350 shape
      reject; SPA never emits either variant).
    - ``VerificationCodeSlt`` (the slt field) — NOT ``VerificationCodeSalt``.
    - ``LW``/``IsOptOutEmailShown`` are booleans; ``IsUserConsentedToChinaPIPL``
      is the CN consent flag — True only when ``state.country == "CN"``
      (PIPL flow), False elsewhere.
    - ``ContinuationToken`` is appended by the caller wrapper (eM) when the
      acma flow holds one — required server-side (its absence → 1349).

    Known gate (2026-11-08 live): with the exact body the server still may
    answer ``1312 field=phoneNumber`` — a risk-engine decision demanding
    phone proof for this signup, NOT a body fault: a real headless browser
    driving the true SPA from the same egresses is hard-blocked earlier
    (AADSTS7005106 riskBlock at silent risk/verify), and the SPA's error
    switch has no 1312 case (it never expects it). px-cookie injection and
    a full fpt.live.com dfp handshake replay do not lift it.
    """
    year, month, day = birth_date[:10].split("-")
    member_counts = {"memberNameChangeCount": 1 if check_map else 0}
    body = {
        "BirthDate": f"{day}:{month}:{year}",
        "CheckAvailStateMap": check_map or [f"{email}:true"],
        "Country": state.country,
        "EvictionWarningShown": [],
        "FirstName": first_name,
        "IsRDM": False,
        "IsOptOutEmailDefault": True,
        "IsOptOutEmailShown": True,
        "IsOptOutEmail": True,
        "IsUserConsentedToChinaPIPL": (state.country or "").upper() == "CN",
        "LastName": last_name,
        "LW": False,
        "MemberName": email,
        "Password": password,
        "RequestTimeStamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "ReturnUrl": "",
        "SignupReturnUrl": state.sru or "https://outlook.live.com/owa/",
        "SuggestedAccountType": "EASI",
        "SiteId": state.site_id or "",
        "VerificationCodeSlt": "",
        "WReply": "",
        "uiflvr": 1001,
        "scid": state.scid,
        "uaid": state.uaid,
        "hpgid": state.hpgid,
        **member_counts,
    }
    if state.continuation_token:
        body["ContinuationToken"] = state.continuation_token
    resp, err = request_with_retry(
        session,
        "POST",
        _signup_api_url(state, "CreateAccount", f"{SIGNUP_API_BASE}/CreateAccount"),
        retry_attempts=1,
        timeout=120.0,
        headers=_signup_headers(state),
        json=body,
    )
    if steps is not None:
        steps["create_account"] = {"status": resp.status_code if resp else 0}
    if resp is None:
        raise MSAFlowError(
            f"create_account: {err}", kind="network", step="create_account", steps=steps
        )
    # 504 reads as success-class (account created, response gateway timed out).
    if resp.status_code == 504:
        return {"slt": "", "redirectUrl": "", "signInName": email}
    result = response_json(resp)
    if result.get("apiCanary"):
        state.api_canary = str(result["apiCanary"])
    slt = str(result.get("slt") or "")
    if not slt:
        error = result.get("error")
        # 1312 field=phoneNumber = risk-engine phone gate (SPA AddPhone view
        # handles it by SMS proof: SendOtt → ConsumeOneTimeToken → retry).
        if isinstance(error, dict) and str(error.get("code") or "") == "1312":
            raise MSAFlowError(
                "phone_gate_1312", kind="phone_gate", step="create_account", steps=steps
            )
        raise MSAFlowError(
            f"create_account_rejected:{str(error or '')[:80]}",
            kind="registration_disallowed",
            step="create_account",
            steps=steps,
        )
    return result


# --------------------------------------------------------------------------
# phone gate (1312): SMS proof chain — steps 11b
# --------------------------------------------------------------------------


def _ott_body(
    state: SignupSession,
    *,
    phone_e164: str,
    action: str,
    ott_purpose: str,
    ott: str = "",
    channel_type: str = "SMS",
) -> dict[str, Any]:
    """ConsumeOneTimeToken body (signup-fluent_v2 GB builder, verbatim).

    The SPA's consume call passes ``{action, channelType, destinationPii,
    ott, ottPurpose}`` straight through GB (no phoneCountryCode/smsDestination
    → destinationPii survives as the E.164 string), then spreads the wB()
    session keys — uiflvr/scid/uaid/hpgid ride every signup API body.
    """
    body: dict[str, Any] = {
        "action": action,
        "channelType": channel_type,
        "destinationPii": phone_e164,
        "uiflvr": 1001,
        "scid": state.i_scenario_id,
        "uaid": state.uaid,
        "hpgid": state.hpgid,
    }
    if ott_purpose:
        body["ottPurpose"] = ott_purpose
    if ott:
        body["ott"] = ott
    return body


def _send_ott_body(
    state: SignupSession,
    *,
    proof_id: str,
    proof_country_iso: str,
) -> dict[str, Any]:
    """SendOtt body (signup-fluent_v2 AB handler, verbatim — different shape
    from consume!).

    The SPA's send call builds ``{action, channel, proofId}`` and, for phone
    signups, adds ``proofCountryIso = <selected iso || defaultPhoneCountry>``
    + ``channel = "SMS"`` — then POSTs the JSON directly (AB handler, no GB
    destinationPii conversion; the country code is NOT part of proofId, it
    rides proofCountryIso).
    """
    return {
        "action": "SignUp",
        "channel": "SMS",
        "proofId": proof_id,
        "proofCountryIso": proof_country_iso,
        "uiflvr": 1001,
        "scid": state.i_scenario_id,
        "uaid": state.uaid,
        "hpgid": state.hpgid,
    }


def send_ott(
    session: Any,
    state: SignupSession,
    *,
    proof_id: str,
    proof_country_iso: str,
    steps: dict[str, Any] | None = None,
) -> None:
    """Send the SMS one-time code (AddPhone view step 1, AB shape)."""
    resp, err = request_with_retry(
        session,
        "POST",
        _signup_api_url(state, "Proofs/SendOtt", SEND_OTT_URL),
        retry_attempts=1,
        timeout=60.0,
        headers=_signup_headers(state),
        json=_send_ott_body(state, proof_id=proof_id,
                            proof_country_iso=proof_country_iso),
    )
    if steps is not None:
        steps["send_ott"] = {"status": resp.status_code if resp else 0}
    if resp is None:
        raise MSAFlowError(
            f"send_ott: {err}", kind="network", step="send_ott", steps=steps
        )
    result = response_json(resp)
    if resp.status_code != 200 or result.get("error"):
        raise MSAFlowError(
            f"send_ott_rejected:{str(result.get('error') or resp.status_code)[:80]}",
            kind="phone_gate",
            step="send_ott",
            steps=steps,
        )


def consume_ott(
    session: Any,
    state: SignupSession,
    phone_e164: str,
    code: str,
    *,
    steps: dict[str, Any] | None = None,
) -> None:
    """Verify the SMS code (AddPhone view step 2, action=OnlyVerifyNoConsume).

    The proof stays unconsumed so the CreateAccount retry consumes it
    server-side.
    """
    resp, err = request_with_retry(
        session,
        "POST",
        _signup_api_url(state, "ConsumeOneTimeToken", CONSUME_OTT_URL),
        retry_attempts=1,
        timeout=60.0,
        headers=_signup_headers(state),
        json=_ott_body(
            state,
            phone_e164=phone_e164,
            action="OnlyVerifyNoConsume",
            ott_purpose="VerificationCode",
            ott=code,
        ),
    )
    if steps is not None:
        steps["consume_ott"] = {"status": resp.status_code if resp else 0}
    if resp is None:
        raise MSAFlowError(
            f"consume_ott: {err}", kind="network", step="consume_ott", steps=steps
        )
    result = response_json(resp)
    if resp.status_code != 200 or result.get("error"):
        raise MSAFlowError(
            f"consume_ott_rejected:{str(result.get('error') or resp.status_code)[:80]}",
            kind="phone_gate",
            step="consume_ott",
            steps=steps,
        )


def phone_verify_and_recreate(
    session: Any,
    state: SignupSession,
    sms_provider: Any,
    *,
    email: str,
    password: str,
    birth_date: str,
    first_name: str,
    last_name: str,
    check_map: list[str] | None = None,
    steps: dict[str, Any] | None = None,
    log: Any = None,
) -> dict[str, Any]:
    """Pass the 1312 phone gate with a rented number, then retry CreateAccount.

    Number discipline: acquire → send → wait → consume → retry-create; every
    exit settles the order (complete on success, cancel otherwise — the
    platform refunds an SMS-less cancel). Phone values are logged masked only.
    """
    number = sms_provider.acquire_number(state.country)
    if steps is not None:
        steps["sms_number"] = {"masked": number.masked}
    if log is not None:
        log(f"sms number acquired {number.masked}")
    proof_id = getattr(number, "proof_id", "") or number.phone_e164.lstrip("+")
    proof_country_iso = (getattr(number, "proof_country_iso", "")
                         or state.country)
    try:
        human_pause(after_step="sms_acquire", log=log)
        send_ott(
            session,
            state,
            proof_id=proof_id,
            proof_country_iso=proof_country_iso,
            steps=steps,
        )
        code = sms_provider.wait_for_code(number.order_id)
        consume_ott(session, state, number.phone_e164, code, steps=steps)
        created = create_account(
            session,
            state,
            email=email,
            password=password,
            birth_date=birth_date,
            first_name=first_name,
            last_name=last_name,
            check_map=check_map,
            steps=steps,
        )
    except MSAFlowError:
        try:
            sms_provider.settle(number.order_id, success=False)
        except Exception:  # noqa: BLE001 — settle is best-effort cleanup
            pass
        raise
    except Exception:
        try:
            sms_provider.settle(number.order_id, success=False)
        except Exception:  # noqa: BLE001
            pass
        raise
    try:
        sms_provider.settle(number.order_id, success=True)
    except Exception:  # noqa: BLE001
        pass
    return created


# --------------------------------------------------------------------------
# slt auto-post chain + mail OAuth: steps 12–14
# --------------------------------------------------------------------------


def _form_autopost_url(html: str) -> str:
    """First <form method="post" action="..."> URL in a redirect page."""
    match = re.search(
        r"<form[^>]+method=[\"']post[\"'][^>]+action=[\"']([^\"']+)[\"']", html
    )
    if not match:
        match = re.search(
            r"<form[^>]+action=[\"']([^\"']+)[\"'][^>]+method=[\"']post[\"']", html
        )
    return _full_url("https://login.live.com", match.group(1)) if match else ""


def submit_slt_login(
    session: Any,
    *,
    redirect_url: str,
    slt: str,
    steps: dict[str, Any] | None = None,
) -> None:
    """Step 12: POST the slt and follow the fmHF auto-submit chain."""
    target = redirect_url or "https://outlook.live.com/owa/"
    for hop in range(6):
        form_url = target
        html = ""
        # Auto-post pages carry hidden inputs; repost when one is detected.
        resp, err = request_with_retry(
            session, "POST", form_url, retry_attempts=2,
            headers={"content-type": "application/x-www-form-urlencoded"},
            data={"slt": slt} if hop == 0 else {},
        )
        if resp is None:
            raise MSAFlowError(
                f"slt_login: {err}", kind="network", step="slt_login", steps=steps
            )
        html = response_text(resp)
        next_form = _form_autopost_url(html)
        if next_form and next_form != form_url:
            target = next_form
            continue
        break
    if steps is not None:
        steps["slt_login"] = {"hops": hop + 1, "final_host": urlparse(target).netloc}


def fetch_mail_oauth_code(
    session: Any,
    *,
    email: str,
    scope: str,
    pkce_verifier: str,
    country: str = "US",
    steps: dict[str, Any] | None = None,
) -> str:
    """Step 13: authorize hop with the mail client → extract ``code=``.

    Drives login.live.com/oauth20_authorize.srf first (MSA route); a bounce to
    login.microsoftonline.com/common/oauth2/v2.0/authorize is followed too.
    """
    challenge = challenge_from_verifier(pkce_verifier)
    query = urlencode(
        {
            "client_id": OUTLOOK_CLIENT_ID,
            "cobrandid": COBRAND_ID,
            "response_type": "code",
            "redirect_uri": OUTLOOK_REDIRECT_URI,
            "scope": scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "login_hint": email,
            "mkt": country and mkt_lc_for_country(country)[0] or "EN-US",
            "lc": str(mkt_lc_for_country(country)[1]),
        }
    )
    authorize_urls = (
        f"{LOGIN_BASE}/oauth20_authorize.srf?{query}",
        f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize?{query}",
    )
    last_html = ""
    for url in authorize_urls:
        resp, err = request_with_retry(session, "GET", url, retry_attempts=2)
        if resp is None:
            continue
        final_url = str(getattr(resp, "url", "") or "")
        html = response_text(resp)
        code = _extract_code(final_url)
        if not code:
            code = _extract_code_from_page(html)
        if code:
            if steps is not None:
                steps["mail_authorize"] = {"host": urlparse(final_url).netloc}
            return code
        last_html = html
    raise MSAFlowError(
        "mail_oauth_no_code", kind="oauth_callback", step="mail_authorize", steps=steps
    )


def _extract_code(url: str) -> str:
    try:
        params = parse_qs(urlparse(url or "").query)
    except ValueError:
        return ""
    return str((params.get("code") or [""])[0])


def _extract_code_from_page(html: str) -> str:
    match = re.search(r"[?&]code=([A-Za-z0-9._*-]+)", html or "")
    return match.group(1) if match else ""


def exchange_mail_token(
    session: Any,
    *,
    code: str,
    pkce_verifier: str,
    scope: str,
    client_id: str,
    steps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Step 14: authorization_code + code_verifier → token payload."""
    resp, err = request_with_retry(
        session,
        "POST",
        MS_TOKEN_URL,
        retry_attempts=2,
        headers={"content-type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": OUTLOOK_REDIRECT_URI,
            "code_verifier": pkce_verifier,
            "scope": scope,
        },
    )
    if resp is None:
        raise MSAFlowError(
            f"mail_token: {err}", kind="network", step="mail_token", steps=steps
        )
    body = response_json(resp)
    if not body.get("refresh_token"):
        raise MSAFlowError(
            f"mail_token_missing_refresh:{str(body.get('error') or '')[:60]}",
            kind="token",
            step="mail_token",
            steps=steps,
        )
    if steps is not None:
        steps["mail_token"] = {"has_refresh": True}
    return body


# --------------------------------------------------------------------------
# top-level orchestration
# --------------------------------------------------------------------------


# Process-wide account pacing (ANTIBAN §3): the first register_one call of a
# batch pays the cold-start delay; every later call pays the remaining
# minute-scale inter-account delay (elapsed-aware, so pipeline-adjacent
# attempts don't double-sleep). Tests patch the two pacing functions.
_LAST_REGISTER_MONO = 0.0


def _account_gate() -> None:
    global _LAST_REGISTER_MONO
    now = time.monotonic()
    if _LAST_REGISTER_MONO <= 0.0:
        delay = cold_start_delay_s()
    else:
        lo, hi = account_delay_range_minutes()
        delay = max(0.0, random.uniform(lo, hi) - (now - _LAST_REGISTER_MONO))
    if delay > 0:
        time.sleep(delay)
    _LAST_REGISTER_MONO = time.monotonic()


def register_one(
    *,
    proxy: str,
    domain: str = "outlook.com",
    country: str = "US",
    token_mode: str = "graph",
    timezone: str | None = None,
    log: Any = None,
    sms_provider: Any = None,
) -> SignupResult:
    """Full protocol registration for one Outlook account.

    Returns a SignupResult; failures raise MSAFlowError mid-flow and surface
    here as ok=False with fail_step. The refresh token is returned in memory
    only — persistence is the adapter's 0600 artifact sink's job.

    ``sms_provider`` (optional) passes the 1312 phone gate with a rented
    number; when omitted, ``sms_client.create_sms_provider()`` resolves one
    from the environment (None → the gate stays a hard failure).
    """
    steps: dict[str, Any] = {}

    def _fail(exc: MSAFlowError) -> SignupResult:
        merged = dict(steps)
        merged.update(exc.steps)
        return SignupResult(
            ok=False,
            error=str(exc),
            error_kind=exc.kind,
            fail_step=exc.step or "unknown",
            steps=merged,
        )

    if token_mode not in TOKEN_MODE_SCOPE:
        return SignupResult(
            ok=False, error=f"unknown token mode {token_mode}", error_kind="provider",
            fail_step="config", steps=steps,
        )
    # ANTIBAN §3 pacing: cold start before the batch's first account, then
    # minute-scale spacing between accounts (elapsed-aware).
    _account_gate()
    session = create_session(proxy)
    if timezone is None:
        timezone = country and _tz_for(country)
    try:
        state = bootstrap_account_session(session, country=country, steps=steps)
        human_pause(after_step="bootstrap", log=log)

        password = _random_password()
        birth_date = _birth_date()
        first_name, last_name = _random_name()

        # z_style order (upstream parity): risk initialize + experiment
        # evaluation BEFORE the availability check, so the session carries a
        # fresh canary/telemetry when the name check and verify #1 fire.
        risk_initialize(session, state, steps=steps)
        try:
            evaluate_experiment_assignments(session, state, steps=steps)
        except MSAFlowError as exc:
            # Best-effort: a stale canary remains valid (live probes never
            # ran this endpoint at all).
            steps.setdefault("evaluate_experiments", {})["deferred_error"] = str(exc)[:80]
        if state.human_sensor_url:
            # Sensor page fidelity GET (the SPA loads it right after init;
            # the response is the HUMAN iframe HTML — content is ignored).
            request_with_retry(session, "GET", state.human_sensor_url, retry_attempts=1)

        # Silent PX (z_style core): solver-minted tokens let verify #1 carry
        # riskProviderMetadata + msaCreateSignature — a clean silent score
        # clears verify #1 outright. Optional: without a key the proven
        # bare-signature → challenge-branch path runs as before.
        silent_token: Any = None
        if os.environ.get("CAPTCHA_RUN_KEY", "").strip():
            try:
                silent_token = px_client_solve_silent_token(
                    PXChallengeContext(
                        uaid=state.uaid,
                        country=state.country,
                        timezone=timezone or "",
                        user_agent=USER_AGENT,
                    ),
                    proxy=proxy,
                )
            except Exception as exc:  # noqa: BLE001 — silent is optional
                steps["silent_px"] = {"error": str(exc)[:100]}
        if silent_token is not None and silent_token.ok:
            # Upstream fact: tokens ride the session cookies too — a
            # body-only verify reads as token-less (live: 403 riskBlock).
            apply_px_cookies(session, silent_token)
            steps["silent_px"] = {"ok": True, "has_px3": bool(silent_token.px3)}

        # Pick an available name; on a collision re-draw a fresh prefix
        # (never prefix+digit — the seller style keeps pure-letter locals).
        email = ""
        check_map: list[str] = []
        for attempt in range(3):
            candidate = f"{_random_prefix()}@{domain}"
            available = check_available_signin_name(
                session, state, candidate, steps=steps
            )
            check_map.append(f"{candidate}:{str(not available).lower()}")
            if available:
                email = candidate
                break
            human_pause(after_step="check_available_retry", log=log)
        if not email:
            raise MSAFlowError(
                "signin_name_taken", kind="provider", step="check_available", steps=steps
            )
        human_pause(after_step="check_available", log=log)

        if silent_token is not None and silent_token.ok:
            signature = build_create_signature(
                member_name=email,
                birth_date=birth_date,
                first_name=first_name,
                last_name=last_name,
                country=state.country,
                site_id=state.site_id,
            )
            metadata = [{
                "riskProvider": "Human",
                "px3": silent_token.px3,
                "pxde": silent_token.pxde,
                "pxvid": silent_token.pxvid,
            }]
        else:
            signature = build_risk_verify_signature(
                member_name=email,
                birth_date=birth_date,
                first_name=first_name,
                last_name=last_name,
                country=state.country,
            )
            metadata = None
        branch = risk_verify_silent(
            session, state, signature=signature, px_metadata=metadata, steps=steps
        )
        if branch == "retry_initialize":
            branch = risk_verify_silent(
                session, state, signature=signature, px_metadata=metadata, steps=steps
            )
            if branch == "retry_initialize":
                raise MSAFlowError(
                    "risk_retry_exhausted",
                    kind="provider",
                    step="risk_verify",
                    steps=steps,
                )
        if branch == "challenge":
            human_pause(after_step="risk_challenge", log=log)
            solve_challenge(
                session,
                state,
                proxy=proxy,
                user_agent=USER_AGENT,
                timezone=timezone,
                steps=steps,
            )
        human_pause(after_step="risk", log=log)

        try:
            created = create_account(
                session,
                state,
                email=email,
                password=password,
                birth_date=birth_date,
                first_name=first_name,
                last_name=last_name,
                check_map=check_map,
                steps=steps,
            )
        except MSAFlowError as exc:
            if exc.kind != "phone_gate":
                raise
            provider = sms_provider
            if provider is None:
                from .sms_client import create_sms_provider

                provider = create_sms_provider()
            if provider is None:
                steps["phone_gate"] = {"resolved": False}
                raise
            steps["phone_gate"] = {"resolved": True}
            human_pause(after_step="phone_gate", log=log)
            created = phone_verify_and_recreate(
                session,
                state,
                provider,
                email=email,
                password=password,
                birth_date=birth_date,
                first_name=first_name,
                last_name=last_name,
                check_map=check_map,
                steps=steps,
                log=log,
            )
        human_pause(after_step="create_account", log=log)

        submit_slt_login(
            session,
            redirect_url=str(created.get("redirectUrl") or ""),
            slt=str(created.get("slt") or ""),
            steps=steps,
        )

        scope = TOKEN_MODE_SCOPE[token_mode]
        client_id = TOKEN_MODE_CLIENT_ID[token_mode]
        verifier, _challenge = _pkce()
        code = fetch_mail_oauth_code(
            session, email=email, scope=scope, pkce_verifier=verifier,
            country=state.country, steps=steps,
        )
        token = exchange_mail_token(
            session, code=code, pkce_verifier=verifier, scope=scope,
            client_id=client_id, steps=steps,
        )
        return SignupResult(
            ok=True,
            email=email,
            password=password,
            refresh_token=str(token.get("refresh_token") or ""),
            steps=steps,
        )
    except MSAFlowError as exc:
        return _fail(exc)
    except Exception as exc:  # transport surprises surface as provider failures
        return _fail(MSAFlowError(f"unexpected:{exc}", kind="provider", step="unknown", steps=steps))


def _tz_for(country: str) -> str:
    from .constants import timezone_for_country

    return timezone_for_country(country)
