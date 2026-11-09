"""Outlook / MSA registration protocol constants.

Every value here is a public Microsoft endpoint fact (well-known public client
ids, URL bases, scope strings) distilled from protocol traffic research —
see docs/outlook-protocol-notes.md for the full table and its source discipline.
"""

from __future__ import annotations

# --- public client identifiers -------------------------------------------

# Registration-side OAuth PKCE client.
OUTLOOK_CLIENT_ID = "9199bf20-a13f-4107-85dc-02114787ef48"
# cobranded authorize hop.
COBRAND_ID = "ab0455a0-8d03-46b9-b18b-df2f57b9e44c"
# risk/verify body siteId (signup web app id).
SIGNUP_CLIENT_ID = "00000000487A244A"
# Thunderbird public mail client — mail tokens and login token#2. Same value
# register_core.providers.outlook_oauth.PUBLIC_CLIENT_ID carries; duplicated
# here so the protocol package stays dependency-free.
THUNDERBIRD_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
# PerimeterX app id protecting signup (collector host derives from it).
PX_APP_ID = "PXzC5j78di"

# --- URL bases ------------------------------------------------------------

SIGNUP_API_BASE = "https://signup.live.com/API"
LOGIN_BASE = "https://login.live.com"
LOGIN_MS_BASE = "https://login.microsoftonline.com"
ACCOUNT_BASE = "https://account.microsoft.com"
MS_TOKEN_URL = f"{LOGIN_MS_BASE}/common/oauth2/v2.0/token"
OUTLOOK_REDIRECT_URI = "https://outlook.live.com/mail/"

# --- mail OAuth scopes ----------------------------------------------------

GRAPH_MAIL_SCOPE = (
    "https://graph.microsoft.com/Mail.ReadWrite "
    "https://graph.microsoft.com/Mail.Send "
    "https://graph.microsoft.com/User.Read "
    "offline_access openid profile"
)
OUTLOOK_REST_SCOPE = (
    "https://outlook.office.com/Mail.ReadWrite "
    "https://outlook.office.com/Mail.Send "
    "offline_access openid profile"
)
# Thunderbird IMAP/POP/SMTP set — the login_exe token mode.
THUNDERBIRD_IMAP_SCOPE = (
    "https://outlook.office.com/IMAP.AccessAsUser.All "
    "https://outlook.office.com/POP.AccessAsUser.All "
    "https://outlook.office.com/SMTP.Send offline_access"
)
# Full Thunderbird set used for the authorize hop in login_exe mode.
THUNDERBIRD_MAIL_SCOPE = (
    f"{THUNDERBIRD_IMAP_SCOPE} "
    "https://outlook.office.com/EWS.AccessAsUser.All "
    "https://outlook.office.com/Mail.ReadWrite "
    "https://outlook.office.com/Mail.Send"
)

# --- token modes ----------------------------------------------------------

TOKEN_MODES = ("graph", "login_exe")

# mode → (authorize scope, resulting pool client_id)
TOKEN_MODE_SCOPE: dict[str, str] = {
    "graph": GRAPH_MAIL_SCOPE,
    "login_exe": THUNDERBIRD_MAIL_SCOPE,
}
TOKEN_MODE_CLIENT_ID: dict[str, str] = {
    "graph": THUNDERBIRD_CLIENT_ID,
    "login_exe": THUNDERBIRD_CLIENT_ID,
}

# --- locale facts ---------------------------------------------------------

# country → (mkt, lc). Defaults EN-US/1033; timezone must match egress IP.
COUNTRY_MKT_LC: dict[str, tuple[str, int]] = {
    "US": ("EN-US", 1033),
    "CA": ("EN-CA", 4105),
    "GB": ("EN-GB", 2057),
    "AU": ("EN-AU", 3081),
    "SG": ("EN-SG", 18441),
    "PH": ("EN-PH", 34313),
    "DE": ("DE-DE", 1031),
    "FR": ("FR-FR", 1036),
    "JP": ("JA-JP", 1041),
    "KR": ("KO-KR", 1042),
    "CN": ("ZH-CN", 2052),
    "TW": ("ZH-TW", 1028),
    "HK": ("ZH-HK", 3076),
}
DEFAULT_MKT = "EN-US"
DEFAULT_LC = 1033

COUNTRY_TIMEZONE: dict[str, str] = {
    "US": "America/New_York",
    "CA": "America/Toronto",
    "GB": "Europe/London",
    "AU": "Australia/Sydney",
    "SG": "Asia/Singapore",
    "PH": "Asia/Manila",
    "HK": "Asia/Hong_Kong",
    "JP": "Asia/Tokyo",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "CN": "Asia/Shanghai",
}
DEFAULT_TIMEZONE = "America/New_York"

# Signup domains accepted by the signup SPA (adapter validates against this
# pair; full 33-domain list is a data table, not a protocol fact).
SIGNUP_DOMAINS = ("outlook.com", "hotmail.com")

# --- phone-gate (1312) endpoints ------------------------------------------

# SPA ServerData facts (signup-fluent_v2 bundle + live page ServerData,
# 2026-11-10): after CreateAccount answers ``1312 field=phoneNumber`` the
# AddPhone view sends an SMS one-time code through ``Proofs/SendOtt`` and
# verifies it with ``ConsumeOneTimeToken`` — ``action: OnlyVerifyNoConsume``
# keeps the proof valid for the CreateAccount retry that consumes it
# server-side. Both take JSON bodies ({action, channelType, destinationPii,
# ott, ottPurpose}) and the wB() session keys (uiflvr/scid/uaid/hpgid).
SEND_OTT_URL = f"{SIGNUP_API_BASE}/Proofs/SendOtt"
CONSUME_OTT_URL = f"{SIGNUP_API_BASE}/ConsumeOneTimeToken"

# --- HTTP facts -----------------------------------------------------------

# Chrome/Windows UA — signup risk engine scores UA vs egress geo together.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30.0

COMMON_HEADERS: dict[str, str] = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "user-agent": USER_AGENT,
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def mkt_lc_for_country(country: str) -> tuple[str, int]:
    """(mkt, lc) for an ISO country code, falling back to EN-US/1033."""
    return COUNTRY_MKT_LC.get((country or "").upper(), (DEFAULT_MKT, DEFAULT_LC))


def timezone_for_country(country: str) -> str:
    """IANA timezone for an ISO country code, US fallback."""
    return COUNTRY_TIMEZONE.get((country or "").upper(), DEFAULT_TIMEZONE)
