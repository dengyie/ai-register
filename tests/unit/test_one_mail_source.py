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

    def test_subaddress_variants(self) -> None:
        from register_core.email.sources.one_mail import _subaddress_variants

        self.assertEqual(
            _subaddress_variants("u+grk1@gmail.com"), ["u+grk1@gmail.com", "u@gmail.com"]
        )
        self.assertEqual(_subaddress_variants("u@gmail.com"), ["u@gmail.com"])
        self.assertEqual(_subaddress_variants(""), [])
        # 没有任何 "+" 的多段 local 保持原样（catch-all 自造地址）
        self.assertEqual(_subaddress_variants("a.b@c.test"), ["a.b@c.test"])

    def test_subaddress_variants_gmail_dot_trick(self) -> None:
        """gmail 点号变体的查询归一：x.ai 限流同样归一 gmail（变体绕不开
        rate-limit，已实测证伪），D1 入库底址是规范形，查询必须落到底址。"""
        from register_core.email.sources.one_mail import _subaddress_variants

        self.assertEqual(
            _subaddress_variants("jane.doe+grk9@gmail.com"),
            ["jane.doe+grk9@gmail.com", "janedoe@gmail.com"],
        )
        self.assertEqual(
            _subaddress_variants("Jane.Doe@googlemail.com"),
            ["Jane.Doe@googlemail.com", "janedoe@gmail.com"],
        )
        # 点号剥离只对 gmail/googlemail 生效：其它域名点号是语义的一部分
        self.assertEqual(
            _subaddress_variants("a.b+x@c.test"), ["a.b+x@c.test", "a.b@c.test"]
        )

    def test_subaddress_variants_do_not_duplicate_lowercase(self) -> None:
        """客户端不自己造小写候选：worker 的 buildVerifAddrClause 会把
        ``addr`` 入参统一 lower()，且读侧按 ``to_addr COLLATE NOCASE`` 比较
        （one-mail PR #180）。客户端多打一个小写候选只是白花一次 HTTP。"""
        from register_core.email.sources.one_mail import _subaddress_variants

        self.assertEqual(
            _subaddress_variants("Jane.Doe+grk9@Gmail.com"),
            ["Jane.Doe+grk9@Gmail.com", "janedoe@gmail.com"],
        )
        self.assertEqual(
            _subaddress_variants("Jane.Doe@Outlook.com"), ["Jane.Doe@Outlook.com"]
        )

    def test_admin_query_sends_addr_verbatim(self) -> None:
        """客户端按原样发 addr（含大小写）：大小写归一是**服务端**职责，
        客户端擅自改写会掩盖「服务端没归一」的真实故障。

        轮询会重试多次，所以断言的是「每次都用原样地址」，不是调用次数。"""
        src = self._src()
        with mock.patch.object(src, "_fetch_codes_admin", return_value=[]) as fa:
            with self.assertRaises(MailMissError):
                src.poll_otp(
                    Mailbox(address="KevinWalters5892@outlook.com", provider="one_mail"),
                    timeout_s=0.2, poll_interval_s=0.05,
                )
        sent = [c.args[0] for c in fa.call_args_list]
        self.assertTrue(sent)
        self.assertEqual(set(sent), {"KevinWalters5892@outlook.com"})

    def test_poll_otp_alias_falls_back_to_base_address(self) -> None:
        """x.ai 投递到聚合账号底址：别名查询空 → 用底址再查一次。"""
        src = self._src()
        rows = [{"from_addr": "noreply@x.ai", "code": "116-010", "subject": "s"}]

        def fake_fetch(address, *, fresh_ms, limit):
            return rows if address == "u@gmail.com" else []

        with mock.patch.object(src, "_fetch_codes_admin", side_effect=fake_fetch) as fa:
            otp = src.poll_otp(
                Mailbox(address="u+grk1@gmail.com", provider="one_mail"),
                timeout_s=5, poll_interval_s=0.01,
            )
        self.assertEqual(otp.code, "116-010")
        self.assertEqual(
            [c.args[0] for c in fa.call_args_list],
            ["u+grk1@gmail.com", "u@gmail.com"],
        )

    def test_poll_otp_exact_address_hit_skips_fallback(self) -> None:
        src = self._src()
        rows = [{"from_addr": "s@x.ai", "code": "42", "subject": "s"}]
        with mock.patch.object(src, "_fetch_codes_admin", return_value=rows) as fa:
            otp = src.poll_otp(
                Mailbox(address="u+grk1@gmail.com", provider="one_mail"),
                timeout_s=5, poll_interval_s=0.01,
            )
        self.assertEqual(otp.code, "42")
        self.assertEqual([c.args[0] for c in fa.call_args_list], ["u+grk1@gmail.com"])


if __name__ == "__main__":
    unittest.main()
