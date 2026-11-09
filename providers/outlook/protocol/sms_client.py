"""SMS verification source for the Outlook phone gate (1312).

CreateAccount answers ``1312 field=phoneNumber`` when the risk engine
demands a phone proof. The SPA then collects a mobile number, sends an SMS
one-time code (``Proofs/SendOtt``) and verifies it (``ConsumeOneTimeToken``)
— this module supplies the number and the received code from a commercial
SMS-receiving platform, so the protocol flow can pass the gate end-to-end.

Provider model: one abstract interface (acquire → wait → settle) plus a
concrete adapter for the sms-activate handler_api.php protocol — the de-facto
standard its resellers clone (base URL and service code configurable, so
clones work unchanged). Credentials ride environment variables only and are
never logged.

Cost discipline: a number is held only for ``WAIT_TIMEOUT_S``; timeout,
verify failure or flow failure always settles the order (cancel — the
platform refunds cancels that received no SMS).
"""

from __future__ import annotations

import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from .session import request_with_retry

# Poll cadence: SMS codes typically land in 20–90s.
SMS_POLL_INTERVAL_S = 7.0
SMS_WAIT_TIMEOUT_S = 240.0

_STATUS_RE = re.compile(r"STATUS_OK:(\d{3,10})")
_ACCESS_RE = re.compile(r"ACCESS:(\+?\d{6,18}):(\w+)")


class SmsProviderError(RuntimeError):
    """SMS platform failure (kind=sms for the adapter)."""


@dataclass(frozen=True)
class SmsNumber:
    """One rented number: platform order id + E.164 destination.

    ``proof_id``/``proof_country_iso`` carry the SPA SendOtt shape — proofId
    is the national number WITHOUT the dial code (the SPA's phone dropdown
    keeps the country separate).
    """

    order_id: str
    phone_e164: str
    proof_id: str = ""
    proof_country_iso: str = ""

    @property
    def masked(self) -> str:
        """Log-safe form — middle digits redacted."""
        tail = self.phone_e164[-4:]
        return f"{self.phone_e164[:3]}****{tail}" if len(self.phone_e164) > 7 else "+****"


class SmsProvider(ABC):
    """SMS source interface the phone-gate step drives."""

    @abstractmethod
    def acquire_number(self, country: str) -> SmsNumber:
        """Rent a number for the signup flow's country ('US' → '+1…')."""

    @abstractmethod
    def wait_for_code(self, order_id: str, timeout_s: float = SMS_WAIT_TIMEOUT_S) -> str:
        """Block until the OTP code arrives; raise on timeout."""

    @abstractmethod
    def settle(self, order_id: str, *, success: bool) -> None:
        """Report the outcome: success → activate/complete, else cancel."""


class SmsActivateCompatible(SmsProvider):
    """sms-activate handler_api.php protocol (works for its clones too).

    ``service`` is the platform's per-vendor code — verify the Outlook/
    Microsoft code in the platform UI before production (commonly ``mp``);
    the default here is a placeholder the profile overrides.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://sms-activate.org",
        service: str = "mp",
        session: Any = None,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._service = service
        self._provided_session = session

    def _get_session(self) -> Any:
        if self._provided_session is None:
            from .session import create_session

            self._provided_session = create_session("")
        return self._provided_session

    def _call(self, params: dict[str, Any]) -> str:
        params = {"api_key": self._key, **params}
        url = f"{self._base}/stubs/handler_api.php?{urlencode(params)}"
        resp, err = request_with_retry(self._get_session(), "GET", url, retry_attempts=3)
        if resp is None:
            raise SmsProviderError(f"sms_api_failed: {err[:80]}")
        text = str(resp.text or "").strip()
        if text.startswith(("BAD_KEY", "ERROR_SQL", "NO_NUMBERS", "NO_BALANCE",
                            "WRONG_SERVICE", "BANNED")):
            raise SmsProviderError(f"sms_api_rejected: {text[:80]}")
        return text

    def acquire_number(self, country: str) -> SmsNumber:
        # country=0 lets the platform default; a mapped ISO code narrows it.
        country_map = {"US": "187", "GB": "16", "CA": "36", "PH": "4"}
        iso = (country or "").upper()
        text = self._call({
            "action": "getNumber",
            "service": self._service,
            "country": country_map.get(iso, "0"),
        })
        match = _ACCESS_RE.search(text)
        if not match:
            raise SmsProviderError(f"sms_get_number_unparsed: {text[:60]}")
        raw_phone, order_id = match.groups()
        phone = raw_phone if raw_phone.startswith("+") else f"+{raw_phone}"
        # proofId = national number (strip the dial code the platform includes)
        dial_map = {"US": "1", "CA": "1", "GB": "44", "PH": "63"}
        dial = dial_map.get(iso, "")
        national = raw_phone.lstrip("+")
        if dial and national.startswith(dial):
            national = national[len(dial):]
        return SmsNumber(order_id=order_id, phone_e164=phone,
                         proof_id=national, proof_country_iso=iso or "US")

    def wait_for_code(self, order_id: str, timeout_s: float = SMS_WAIT_TIMEOUT_S) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            text = self._call({"action": "getStatus", "id": order_id})
            match = _STATUS_RE.search(text)
            if match:
                return match.group(1)
            if text.startswith(("STATUS_CANCEL", "BAD_KEY", "ERROR")):
                raise SmsProviderError(f"sms_status_failed: {text[:60]}")
            time.sleep(SMS_POLL_INTERVAL_S)
        raise SmsProviderError("sms_code_timeout")

    def settle(self, order_id: str, *, success: bool) -> None:
        # 6 = complete (activate), 1 = cancel (refund if no SMS received).
        self._call({"action": "setStatus", "id": order_id,
                    "status": 6 if success else 1})


def create_sms_provider() -> SmsProvider | None:
    """Env-driven factory; None keeps today's phone-gate-fails behaviour.

    Keys only via env — ``OUTLOOK_SMS_API_KEY`` (secret), with
    ``OUTLOOK_SMS_BASE`` / ``OUTLOOK_SMS_SERVICE`` optional overrides.
    """
    key = os.environ.get("OUTLOOK_SMS_API_KEY", "").strip()
    if not key:
        return None
    base = os.environ.get("OUTLOOK_SMS_BASE", "").strip() or "https://sms-activate.org"
    service = os.environ.get("OUTLOOK_SMS_SERVICE", "").strip() or "mp"
    return SmsActivateCompatible(key, base_url=base, service=service)


def new_flow_id() -> str:
    """Correlation id for one phone-gate attempt (never contains secrets)."""
    return uuid4().hex[:12]
