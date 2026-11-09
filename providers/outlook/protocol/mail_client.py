"""Mail token exchange + self-read for freshly registered Outlook accounts.

After CreateAccount, the flow exchanges the authorization code for a mail
refresh token and proves the mailbox is readable BEFORE the account is handed
to the pool (a dead or Junk-only mailbox must never enter mail_credentials.txt).

Scope discipline: Graph-scoped tokens get 401 InvalidAuthenticationToken on the
Outlook REST endpoint and vice versa — the token mode decides the read path.
Both inbox AND junkemail folders are scanned (the nace lesson: OTP mail can
land in Junk; a folder-naive reader starves the OTP wait).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .constants import MS_TOKEN_URL
from .session import request_with_retry

# OTP is a 4–8 digit code; 4-digit years (19xx/20xx) are filtered out.
_OTP_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
# Sender allowlist — Microsoft account protection mails only.
_TRUSTED_SENDERS = (
    "accountprotection.microsoft.com",
    "account-security-noreply@accountprotection.microsoft.com",
    "microsoftaccountteam@microsoftaccount.com",
)

GRAPH_MESSAGES_URL = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages"
    "?$top={top}&$orderby=receivedDateTime desc"
    "&$select=subject,from,receivedDateTime,bodyPreview,body"
)
OUTLOOK_REST_MESSAGES_URL = (
    "https://outlook.office.com/api/v2.0/me/mailfolders/{folder}/messages"
    "?$top={top}&$select=Subject,From,ReceivedDateTime,BodyPreview,Body"
)


@dataclass
class MailToken:
    ok: bool
    access_token: str = ""
    refresh_token: str = ""
    error: str = ""  # error_description passthrough, e.g. "invalid_grant"


@dataclass
class ReadCodeResult:
    ok: bool
    code: str = ""
    folder: str = ""
    error: str = ""


def exchange_refresh_token(
    session: Any,
    *,
    refresh_token: str,
    client_id: str,
    scope: str = "",
    proxy: str = "",
    timeout: float = 30.0,
) -> MailToken:
    """grant_type=refresh_token against the MSA v2 endpoint.

    RT uniqueness discipline (docs/ANTIBAN.md): one in-flight exchange per RT;
    the caller serializes. ``invalid_grant`` passes through verbatim — it is a
    terminal, quarantinable state, never retried.
    """
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if scope:
        data["scope"] = scope
    kwargs: dict[str, Any] = {"data": data}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    resp, err = request_with_retry(
        session, "POST", MS_TOKEN_URL, retry_attempts=1, timeout=timeout, **kwargs
    )
    if resp is None:
        return MailToken(ok=False, error=err or "transport")
    try:
        body = resp.json() if isinstance(resp.json(), dict) else {}
    except Exception:
        body = {}
    if body.get("error"):
        return MailToken(
            ok=False,
            error=str(body.get("error_description") or body.get("error") or "")[:200],
        )
    refresh = str(body.get("refresh_token") or "")
    access = str(body.get("access_token") or "")
    if not access or not refresh:
        return MailToken(ok=False, error="missing_tokens")
    return MailToken(ok=True, access_token=access, refresh_token=refresh)


def _extract_otp(text: str) -> str:
    """First plausible OTP digit run — 4–8 digits, not a year."""
    for match in _OTP_RE.finditer(text or ""):
        candidate = match.group(1)
        if not _YEAR_RE.match(candidate):
            return candidate
    return ""


def _sender_matches(item: dict[str, Any], probe: str) -> bool:
    sender = ""
    from_field = item.get("from") or item.get("From") or {}
    addr = (from_field.get("emailAddress") or from_field.get("EmailAddress") or {})
    if isinstance(addr, dict):
        sender = str(addr.get("address") or "")
    elif isinstance(addr, str):
        sender = addr
    return probe.lower() in sender.lower()


def read_otp_code(
    session: Any,
    *,
    access_token: str,
    mode: str = "graph",
    folders: tuple[str, ...] = ("inbox", "junkemail"),
    top: int = 10,
    trusted_only: bool = True,
    proxy: str = "",
    timeout: float = 30.0,
) -> ReadCodeResult:
    """Scan recent messages across folders for an OTP code.

    mode=graph uses the Graph API; mode=outlook_rest uses the Outlook REST v2
    endpoint (token scopes must match — they are not interchangeable).
    NOTE: both modes need a mail-scope access token; a login_exe (IMAP/POP/
    SMTP scope) token must be read via the TTK IMAP path instead — REST will
    401 it. Junk folder is always scanned: OTP mail lands there routinely.
    """
    if not access_token:
        return ReadCodeResult(ok=False, error="missing_access_token")
    template = (
        GRAPH_MESSAGES_URL if mode == "graph" else OUTLOOK_REST_MESSAGES_URL
    )
    headers = {"authorization": f"Bearer {access_token}"}
    kwargs: dict[str, Any] = {"headers": headers}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    for folder in folders:
        url = template.format(folder=folder, top=top)
        resp, err = request_with_retry(
            session, "GET", url, retry_attempts=2, timeout=timeout, **kwargs
        )
        if resp is None or resp.status_code != 200:
            continue
        try:
            body = resp.json() if isinstance(resp.json(), dict) else {}
        except Exception:
            continue
        items = body.get("value") or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if trusted_only and not any(
                _sender_matches(item, probe) for probe in _TRUSTED_SENDERS
            ):
                continue
            text = " ".join(
                str(item.get(key) or "")
                for key in ("subject", "Subject", "bodyPreview", "BodyPreview")
            )
            body_field = item.get("body") or item.get("Body") or {}
            if isinstance(body_field, dict):
                text += " " + str(body_field.get("content") or body_field.get("Content") or "")
            code = _extract_otp(text)
            if code:
                return ReadCodeResult(ok=True, code=code, folder=folder)
    return ReadCodeResult(ok=False, error=err or "no_code_found")
