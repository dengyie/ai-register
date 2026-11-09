"""Unit tests for the one-mail domain-mailbox EmailSource."""

from __future__ import annotations

import unittest
from unittest import mock

from register_core.contracts import Mailbox
from register_core.email.sources.one_mail import OneMailSource
from register_core.errors import FailFastError, MailMissError, ProviderError


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


class TestOneMailSource(unittest.TestCase):
    def test_requires_base_url(self) -> None:
        with self.assertRaises(FailFastError):
            OneMailSource()

    def test_pool_round_robin_allocation(self) -> None:
        src = OneMailSource(
            base_url="https://m.example.com",
            emails="a@x.tech:pw1, b@x.tech:pw2",
        )
        m1 = src.allocate()
        m2 = src.allocate()
        m3 = src.allocate()
        self.assertEqual(m1.provider, "one_mail")
        self.assertEqual([m1.address, m2.address, m3.address][0], "a@x.tech")
        self.assertEqual(m3.address, "a@x.tech")  # wraps

    def test_no_pool_no_autoreg_fails(self) -> None:
        src = OneMailSource(base_url="https://m.example.com")
        with self.assertRaises(FailFastError):
            src.allocate()

    def _src(self) -> OneMailSource:
        src = OneMailSource(base_url="https://m.example.com", emails="a@x.tech:pw")
        # Pool allocations skip login; jwt comes from the address:password pool
        # at poll time. Patch _login so offline tests never hit the network.
        src._login = lambda email, password: "jwt-test"  # type: ignore[method-assign]
        return src

    def test_poll_otp_matches_sender_and_code(self) -> None:
        src = self._src()
        mb = src.allocate()
        rows = [
            {"from_addr": "noreply@other.com", "code": "9999", "subject": "x"},
            {"from_addr": "account-security-noreply@accountprotection.microsoft.com",
             "code": "123456", "subject": "verify"},
        ]
        with mock.patch.object(src, "_fetch_codes", return_value=rows):
            otp = src.poll_otp(mb, timeout_s=5, poll_interval_s=0.01,
                               sender_hint="accountprotection")
        self.assertEqual(otp.code, "123456")

    def test_poll_otp_timeout_raises_mail_miss(self) -> None:
        src = self._src()
        mb = src.allocate()
        with mock.patch.object(src, "_fetch_codes", return_value=[]):
            with self.assertRaises(MailMissError):
                src.poll_otp(mb, timeout_s=0.2, poll_interval_s=0.05)

    def test_used_code_is_stale_not_matched(self) -> None:
        src = self._src()
        mb = src.allocate()
        rows = [{"from_addr": "s@a.com", "code": "42", "subject": "x"}]
        with mock.patch.object(src, "_fetch_codes", return_value=rows):
            with self.assertRaises(MailMissError):
                src.poll_otp(mb, timeout_s=5, poll_interval_s=0.01,
                             used_codes={"42"})

    def test_login_failure_is_provider_error(self) -> None:
        src = OneMailSource(base_url="https://m.example.com", emails="a@x.tech:pw")
        with mock.patch("register_core.email.sources.one_mail.requests.post",
                        return_value=_Resp(401, {})):
            with self.assertRaises(ProviderError):
                src.poll_otp(Mailbox(address="a@x.tech", password="pw",
                                     provider="one_mail"),
                             timeout_s=1, poll_interval_s=0.01)

    def test_fresh_ms_from_newer_than_epoch(self) -> None:
        src = self._src()
        mb = src.allocate()
        captured: dict = {}

        def fake_fetch(jwt, *, fresh_ms, limit):
            captured["fresh_ms"] = fresh_ms
            return []

        with mock.patch.object(src, "_fetch_codes", side_effect=fake_fetch):
            with self.assertRaises(MailMissError):
                src.poll_otp(mb, timeout_s=0.2, poll_interval_s=0.05,
                             newer_than_epoch=__import__("time").time() - 120)
        # ~2 minutes ago → ~150s window + 30s slack, clamped ≥60s
        self.assertGreaterEqual(captured["fresh_ms"], 60_000)
        self.assertLess(captured["fresh_ms"], 400_000)


class TestOneMailAdminMode(unittest.TestCase):
    """Admin channel: no per-address login; unique catch-all address per run."""

    def _src(self) -> OneMailSource:
        return OneMailSource(
            base_url="https://m.example.com",
            auth_mode="admin",
            admin_key="k",
            domains="a.test,b.test",
        )

    def test_requires_admin_key_and_domains(self) -> None:
        with self.assertRaises(FailFastError):
            OneMailSource(base_url="https://m.example.com", auth_mode="admin")
        with self.assertRaises(FailFastError):
            OneMailSource(
                base_url="https://m.example.com", auth_mode="admin", admin_key="k"
            )

    def test_invalid_auth_mode_fails_fast(self) -> None:
        with self.assertRaises(FailFastError):
            OneMailSource(base_url="https://m.example.com", auth_mode="hmac")

    def test_allocate_mints_unique_addresses(self) -> None:
        src = self._src()
        addrs = [src.allocate().address for _ in range(6)]
        self.assertEqual(len(set(addrs)), 6)
        domains = {a.split("@", 1)[1] for a in addrs}
        self.assertEqual(domains, {"a.test", "b.test"})

    def test_poll_otp_uses_admin_endpoint_with_addr(self) -> None:
        src = self._src()
        mb = src.allocate()
        rows = [{"from_addr": "x@microsoft.com", "code": "654321", "subject": "s"}]
        with mock.patch.object(src, "_fetch_codes_admin", return_value=rows) as fa:
            otp = src.poll_otp(mb, timeout_s=5, poll_interval_s=0.01)
        self.assertEqual(otp.code, "654321")
        fa.assert_called_once()
        self.assertEqual(fa.call_args.kwargs["fresh_ms"], 600_000)

    def test_admin_401_is_provider_error(self) -> None:
        src = self._src()
        mb = src.allocate()
        with mock.patch("register_core.email.sources.one_mail.requests.get",
                        return_value=_Resp(401, {})):
            with self.assertRaises(ProviderError):
                src.poll_otp(mb, timeout_s=1, poll_interval_s=0.01)


if __name__ == "__main__":
    unittest.main()
