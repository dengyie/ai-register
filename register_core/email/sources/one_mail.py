"""one-mail (Cloudflare Worker) domain-mailbox source.

Bridges the project's one-mail deployment into the EmailSource protocol so
domain mailboxes (recovery/proof targets, OTP receivers) allocate and read
codes. Two auth channels:

- **password** (default): ``POST /api/address_login`` {email, password} →
  {jwt, address} then ``GET /api/verifcodes`` with the address JWT. Requires
  the worker's ``ENABLE_ADDRESS_PASSWORD`` to be on.
- **admin** (``auth_mode: admin``): no per-address login — one-mail's
  catch-all domains ingest every prefix, so each registration simply invents
  a fresh unique address (``<token>@<domain>``) and reads its codes via
  ``GET /api/unified/verifcodes?addr=<address>`` with the shared
  ``x-admin-auth`` header (env ``ONEMAIL_ADMIN_KEY``). Uniqueness comes from
  the random token; the admin key is read-only for codes.

Config keys (profile ``mailbox.options`` or env):
- ``base_url``  — one-mail worker origin, e.g. ``https://mail.example.com``
  (env ``ONEMAIL_API_BASE``)
- ``auth_mode`` — ``password`` (default) or ``admin``
- ``admin_key`` — admin password for the admin channel (env
  ``ONEMAIL_ADMIN_KEY``)
- ``domains``   — catch-all domain(s) for address minting in admin mode
  (env ``ONEMAIL_DOMAINS``, CSV); one is picked per allocation
- ``email`` / ``password`` — password mode: a single address credential; or
  ``emails`` / ``ONEMAIL_EMAILS`` — ``address:password`` CSV pool to rotate
  across allocations.
- ``allow_autoreg`` — when true and no static pool is given (password mode),
  allocate via one-mail's ``POST /api/new_address`` (off by default: vault
  discipline is 一号一箱, prefer a real pool over auto-throwaway addresses).
"""

from __future__ import annotations

import os
import secrets
import time as _time
from typing import Any

import requests

from register_core.contracts import Mailbox, OtpCode, OtpWaitDiagnostics
from register_core.errors import FailFastError, MailMissError, ProviderError


class OneMailSource:
    """Allocate a one-mail address and poll its server-extracted OTP codes."""

    name = "one_mail"

    def __init__(self, **kwargs: Any) -> None:
        self.base_url = str(
            kwargs.get("base_url")
            or os.environ.get("ONEMAIL_API_BASE", "")
        ).rstrip("/")
        if not self.base_url:
            raise FailFastError(
                "one_mail email source requires base_url (options or ONEMAIL_API_BASE)"
            )
        self.auth_mode = str(
            kwargs.get("auth_mode") or os.environ.get("ONEMAIL_AUTH_MODE", "password")
        ).strip().lower()
        if self.auth_mode not in ("password", "admin"):
            raise FailFastError(
                f"one_mail auth_mode must be 'password' or 'admin', got {self.auth_mode!r}"
            )
        self.admin_key = str(
            kwargs.get("admin_key") or os.environ.get("ONEMAIL_ADMIN_KEY", "")
        )
        self._domains = self._parse_domains(
            kwargs.get("domains") or os.environ.get("ONEMAIL_DOMAINS", "")
        )
        if self.auth_mode == "admin":
            if not self.admin_key:
                raise FailFastError(
                    "one_mail admin mode requires admin_key (options or ONEMAIL_ADMIN_KEY)"
                )
            if not self._domains:
                raise FailFastError(
                    "one_mail admin mode requires domains (options or ONEMAIL_DOMAINS)"
                )
        self._pool: list[tuple[str, str]] = self._build_pool(kwargs)
        self._cursor = 0
        self.allow_autoreg = bool(kwargs.get("allow_autoreg", False))
        self.last_wait_diagnostics: OtpWaitDiagnostics | None = None

    # ------------------------------------------------------------------
    # credential pool
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_domains(raw: Any) -> list[str]:
        if isinstance(raw, (list, tuple)):
            return [str(d).strip() for d in raw if str(d).strip()]
        return [
            chunk.strip() for chunk in str(raw or "").split(",") if chunk.strip()
        ]

    @staticmethod
    def _parse_pool(raw: str) -> list[tuple[str, str]]:
        pool: list[tuple[str, str]] = []
        for chunk in (raw or "").split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            email, _, password = chunk.partition(":")
            email = email.strip()
            password = password.strip()
            if email and password:
                pool.append((email, password))
        return pool

    def _build_pool(self, kwargs: dict[str, Any]) -> list[tuple[str, str]]:
        emails = kwargs.get("emails") or os.environ.get("ONEMAIL_EMAILS", "")
        if isinstance(emails, (list, tuple)):
            pool = [
                tuple(x) for x in emails
                if isinstance(x, (list, tuple)) and len(x) == 2
            ]
        else:
            pool = self._parse_pool(str(emails or ""))
        if not pool:
            email = str(kwargs.get("email") or "").strip()
            password = str(kwargs.get("password") or "").strip()
            if email and password:
                pool = [(email, password)]
        return pool

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _login(self, email: str, password: str) -> str:
        try:
            resp = requests.post(
                f"{self.base_url}/api/address_login",
                json={"email": email, "password": password},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"one_mail login transport: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"one_mail login failed: http {resp.status_code} "
                f"(address={email[:3]}***; check ENABLE_ADDRESS_PASSWORD)"
            )
        body = resp.json() if isinstance(resp.json(), dict) else {}
        jwt = str(body.get("jwt") or "")
        if not jwt:
            raise ProviderError("one_mail login: missing jwt")
        return jwt

    def _fetch_codes(self, jwt: str, *, fresh_ms: int, limit: int) -> list[dict[str, Any]]:
        try:
            resp = requests.get(
                f"{self.base_url}/api/verifcodes",
                headers={"authorization": f"Bearer {jwt}"},
                params={"fresh": fresh_ms, "limit": limit},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"one_mail verifcodes transport: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"one_mail verifcodes: http {resp.status_code}")
        body = resp.json() if isinstance(resp.json(), dict) else {}
        results = body.get("results")
        return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []

    def _fetch_codes_admin(
        self, address: str, *, fresh_ms: int, limit: int
    ) -> list[dict[str, Any]]:
        """Admin channel: catch-all domain view filtered to one exact address."""
        try:
            resp = requests.get(
                f"{self.base_url}/api/unified/verifcodes",
                headers={"x-admin-auth": self.admin_key},
                params={"addr": address, "fresh": fresh_ms, "limit": limit},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"one_mail admin verifcodes transport: {exc}") from exc
        if resp.status_code == 401:
            raise ProviderError(
                "one_mail admin verifcodes: http 401 (check ONEMAIL_ADMIN_KEY)"
            )
        if resp.status_code != 200:
            raise ProviderError(f"one_mail admin verifcodes: http {resp.status_code}")
        body = resp.json() if isinstance(resp.json(), dict) else {}
        results = body.get("results")
        return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []

    # ------------------------------------------------------------------
    # EmailSource protocol
    # ------------------------------------------------------------------

    def allocate(self) -> Mailbox:
        if self.auth_mode == "admin":
            # Catch-all domain: mint a unique address per allocation. Uniqueness
            # is the entropy source (vault 一号一箱 — never reuse an address).
            domain = self._domains[self._cursor % len(self._domains)]
            self._cursor += 1
            local = secrets.token_hex(8)
            return Mailbox(
                address=f"{local}@{domain}",
                token="",  # admin channel needs no per-address jwt
                password="",
                provider=self.name,
            )
        if self._pool:
            email, password = self._pool[self._cursor % len(self._pool)]
            self._cursor += 1
        elif self.allow_autoreg:
            return self._allocate_new()
        else:
            raise FailFastError(
                "one_mail source needs options.emails (address:password CSV pool) "
                "or allow_autoreg: true"
            )
        return Mailbox(
            address=email,
            token="",  # jwt is fetched lazily per poll (short-lived)
            password=password,
            provider=self.name,
        )

    def _allocate_new(self) -> Mailbox:
        try:
            resp = requests.post(
                f"{self.base_url}/api/new_address",
                json={"enablePrefix": False},
                timeout=15,
            )
            body = resp.json() if isinstance(resp.json(), dict) else {}
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"one_mail new_address failed: {exc}") from exc
        jwt = str(body.get("jwt") or "")
        address = str(body.get("address") or "")
        if not jwt or not address:
            raise ProviderError("one_mail new_address: missing jwt/address")
        return Mailbox(address=address, token=jwt, password="", provider=self.name)

    def poll_otp(
        self,
        mailbox: Mailbox,
        *,
        timeout_s: float = 180,
        poll_interval_s: float = 3,
        used_codes: set[str] | None = None,
        newer_than_epoch: float | None = None,
        sender_hint: str | None = None,
    ) -> OtpCode:
        started = _time.time()
        diag = OtpWaitDiagnostics(
            timeout_s=float(timeout_s),
            provider=self.name,
            sender_hint=(sender_hint or ""),
        )
        self.last_wait_diagnostics = diag

        fresh_ms = 10 * 60 * 1000
        if newer_than_epoch:
            fresh_ms = max(60_000, int((_time.time() - newer_than_epoch + 30) * 1000))

        def _fetch() -> list[dict[str, Any]]:
            if self.auth_mode == "admin":
                return self._fetch_codes_admin(
                    mailbox.address, fresh_ms=fresh_ms, limit=5
                )
            jwt = mailbox.token or self._login(mailbox.address, mailbox.password)
            return self._fetch_codes(jwt, fresh_ms=fresh_ms, limit=5)

        deadline = started + timeout_s
        while _time.time() < deadline:
            rows = _fetch()
            diag.poll_count += 1
            diag.message_scan_count += len(rows)
            for row in rows:
                code = str(row.get("code") or "")
                if not code:
                    continue
                sender = str(row.get("from_addr") or "")
                if sender_hint and sender_hint.lower() not in sender.lower():
                    continue
                if used_codes and code in used_codes:
                    diag.failure_class = "stale_code"
                    self.last_wait_diagnostics = diag
                    raise MailMissError(
                        f"one_mail OTP already used for {mailbox.address}",
                        diagnostics=diag,
                    )
                diag.matched_at = _time.time()
                diag.matched_after_seconds = diag.matched_at - started
                diag.failure_class = ""
                self.last_wait_diagnostics = diag
                return OtpCode(
                    code=code,
                    source=self.name,
                    raw_subject=str(row.get("subject") or ""),
                )
            _time.sleep(poll_interval_s)

        diag.elapsed_seconds = _time.time() - started
        diag.failure_class = diag.failure_class or "no_mail"
        self.last_wait_diagnostics = diag
        raise MailMissError(
            f"one_mail OTP timeout for {mailbox.address} after {timeout_s:.0f}s",
            diagnostics=diag,
        )

    def release(self, mailbox: Mailbox, *, success: bool) -> None:
        return
