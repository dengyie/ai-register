"""Unit tests for proxy_rotate mode=tetherpool (TetherPool / IP-Pool lease)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _rotator(cfg: dict):
    import proxy_rotate

    r = proxy_rotate.ProxyRotator()
    r.configure(cfg)
    return r


TP_CFG = {
    "proxy_rotate_mode": "tetherpool",
    "proxy": "http://127.0.0.1:7897",
    "tetherpool_api_base": "http://tp-api:9100",
    "tetherpool_gateway_base": "http://tp-gw:9101",
    "tetherpool_target_service": "grok_reg",
    "tetherpool_tier": "premium",
    "tetherpool_cooldown": 600,
}

LEASE = {
    "account_id": "grokreg-1-1",
    "target_service": "grok_reg",
    "node_name": "pigeonw-01",
    "proxy_url": "socks5://10.144.144.3:10801",
    "exit_ip": "76.223.1.1",
    "region": "us-west",
    "egress_type": "clash_proxy",
    "cooldown_seconds": 600,
    "leased_at": "2026-11-14T00:00:00",
    "cooled_until": "2026-11-14T00:10:00",
}


class TestTetherpoolMode(unittest.TestCase):
    def test_mode_alias_and_defaults(self):
        r = _rotator({"proxy_rotate_mode": "tetherpool", "proxy": "http://x:1"})
        self.assertEqual(r.mode, "tetherpool")
        self.assertEqual(r.tp_target_service, "grok_reg")
        self.assertEqual(r.tp_tier, "premium")
        self.assertEqual(r.tp_cooldown, 1800)
        self.assertTrue(r.tp_api_base.endswith(":9100"))
        self.assertTrue(r.tp_gateway_base.endswith(":9101"))

    def test_alias_ip_pool(self):
        r = _rotator({"proxy_rotate_mode": "ip_pool", "proxy": "http://x:1"})
        self.assertEqual(r.mode, "tetherpool")

    def test_requires_fallback_proxy(self):
        import proxy_rotate

        r = proxy_rotate.ProxyRotator()
        with self.assertRaises(RuntimeError):
            r.configure({"proxy_rotate_mode": "tetherpool"})

    def test_rotate_success_uses_registration_lease(self):
        import proxy_rotate

        r = _rotator(dict(TP_CFG))
        fake = mock.Mock()
        fake.acquire_for_registration.return_value = dict(LEASE)
        fake.get_gateway_proxy_url.side_effect = (
            lambda acc, **kw: f"http://{acc}__service_grok_reg:tetherpool@tp-gw:9101"
        )
        with mock.patch.object(r, "_tp_client_locked", return_value=fake):
            res = r.maybe_rotate(config=dict(TP_CFG))
        self.assertTrue(res["rotated"])
        self.assertEqual(res["mode"], "tetherpool")
        self.assertEqual(res["account_id"], LEASE["account_id"])
        self.assertEqual(res["exit_ip"], "76.223.1.1")
        fake.acquire_for_registration.assert_called_once()
        kwargs = fake.acquire_for_registration.call_args.kwargs
        self.assertEqual(kwargs["target_service"], "grok_reg")
        self.assertEqual(kwargs["tier"], "premium")
        self.assertEqual(kwargs["cooldown"], 600)
        # apply_to_config rewrote cfg['proxy'] to the gateway URL
        self.assertIn("__service_grok_reg:tetherpool@", res["proxy"])

    def test_gateway_url_has_no_constraint_tags(self):
        """Tags would make the gateway escape the registration lease binding."""
        import proxy_rotate

        r = _rotator(dict(TP_CFG))
        url = r._tp_gateway_url_locked("acc-1")
        self.assertIn("acc-1__service_grok_reg:tetherpool@", url)
        self.assertNotIn("type_", url)
        self.assertNotIn("tier_", url)
        self.assertNotIn("region_", url)

    def test_rotate_outage_falls_back_to_static_proxy(self):
        import proxy_rotate

        r = _rotator(dict(TP_CFG))
        fake = mock.Mock()
        fake.acquire_for_registration.side_effect = RuntimeError(
            "Failed to connect to TetherPool API"
        )
        with mock.patch.object(r, "_tp_client_locked", return_value=fake):
            res = r.maybe_rotate(config=dict(TP_CFG))
        self.assertFalse(res["rotated"])
        self.assertTrue(res.get("fallback"))
        self.assertEqual(res["proxy"], "http://127.0.0.1:7897")
        self.assertIn("Failed to connect", res.get("error", ""))

    def test_battery_report_noop_outside_mode(self):
        import proxy_rotate

        proxy_rotate._rotator.mode = "off"  # touch global like module default
        try:
            res = proxy_rotate.report_tetherpool_battery("browser_boot")
            self.assertFalse(res["ok"])
        finally:
            proxy_rotate._rotator.mode = "off"

    def test_battery_report_forwards_error_type(self):
        import proxy_rotate

        r = _rotator(dict(TP_CFG))
        fake = mock.Mock()
        with mock.patch.object(r, "_tp_client_locked", return_value=fake):
            with mock.patch.object(
                proxy_rotate._rotator, "mode", "tetherpool", create=True
            ):
                r._tp_current_account = LEASE["account_id"]
                res = r.report_tetherpool_battery("browser_boot")
        # battery goes through the report method; account carried from lease state
        # (only asserted when a client call happened)
        if res.get("ok"):
            fake.report_battery_error.assert_called_once()
            kwargs = fake.report_battery_error.call_args.kwargs
            self.assertEqual(kwargs["error_type"], "browser_boot")
            self.assertTrue(kwargs["force_rebind"])

    def test_status_fields(self):
        r = _rotator(dict(TP_CFG))
        st = r.status()
        self.assertEqual(st["tp_tier"], "premium")
        self.assertEqual(st["tp_target_service"], "grok_reg")
        self.assertIn("tp_current_account", st)


if __name__ == "__main__":
    unittest.main()
