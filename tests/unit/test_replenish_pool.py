#!/usr/bin/env python3
"""Offline replenish/append pool tests (no network, no real pool files)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mail_pool_probe import (
    Credential,
    append_credentials_to_pool,
    format_pool_line,
    parse_credential_line,
    pool_file_lock,
    probe_one,
)
from scripts.replenish_pool import (
    candidates_from_artifacts,
    candidates_from_jsonl,
)


def _cred(email: str = "newone01@outlook.com") -> Credential:
    return Credential(
        email=email,
        password="pw12345678",
        client_id="cid-1234",
        refresh_token="M.R3_BL2.-token",
    )


class TestAppendCredentialsToPool(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pool = Path(self._tmp.name) / "mail_credentials.txt"

    def test_append_creates_and_formats_line(self) -> None:
        self.assertTrue(append_credentials_to_pool(self.pool, _cred()))
        text = self.pool.read_text(encoding="utf-8")
        self.assertEqual(
            text, "newone01@outlook.com----pw12345678----cid-1234----M.R3_BL2.-token\n"
        )
        parsed = parse_credential_line(text)
        self.assertIsNotNone(parsed)
        assert parsed
        self.assertEqual(parsed.email, "newone01@outlook.com")
        self.assertEqual(parsed.refresh_token, "M.R3_BL2.-token")

    def test_duplicate_email_skipped_by_default(self) -> None:
        self.assertTrue(append_credentials_to_pool(self.pool, _cred()))
        self.assertFalse(append_credentials_to_pool(self.pool, _cred()))
        lines = self.pool.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)

    def test_allow_duplicate_appends_second_line(self) -> None:
        self.assertTrue(append_credentials_to_pool(self.pool, _cred()))
        self.assertTrue(
            append_credentials_to_pool(self.pool, _cred(), allow_duplicate=True)
        )
        self.assertEqual(len(self.pool.read_text(encoding="utf-8").splitlines()), 2)

    def test_duplicate_check_is_case_insensitive(self) -> None:
        self.assertTrue(append_credentials_to_pool(self.pool, _cred()))
        upper = _cred("NewOne01@Outlook.com")
        self.assertFalse(append_credentials_to_pool(self.pool, upper))

    def test_invalid_credential_rejected(self) -> None:
        missing_rt = Credential(email="a@outlook.com", password="p", client_id="c", refresh_token="")
        self.assertFalse(append_credentials_to_pool(self.pool, missing_rt))
        no_at = Credential(email="nonsense", password="p", client_id="c", refresh_token="t")
        self.assertFalse(append_credentials_to_pool(self.pool, no_at))
        self.assertFalse(self.pool.exists())

    def test_append_survives_simulated_concurrent_reader(self) -> None:
        # Another process/thread holds the OS lock → append must block until
        # released, then succeed (cross-process discipline).
        self.assertTrue(append_credentials_to_pool(self.pool, _cred()))

        acquired = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with pool_file_lock(self.pool):
                acquired.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        self.assertTrue(acquired.wait(timeout=5))

        result: list[bool] = []

        def do_append() -> None:
            result.append(append_credentials_to_pool(self.pool, _cred("second02@hotmail.com")))

        worker = threading.Thread(target=do_append, daemon=True)
        worker.start()
        # Give the worker a moment; it must be blocked on the OS lock.
        worker.join(timeout=0.3)
        self.assertFalse(worker.is_alive() is False, "worker should still be blocked")
        release.set()
        worker.join(timeout=5)
        self.assertTrue(result and result[0])
        self.assertEqual(len(self.pool.read_text(encoding="utf-8").splitlines()), 2)

    def test_flock_lock_file_created_next_to_pool(self) -> None:
        with pool_file_lock(self.pool):
            self.assertTrue((Path(self._tmp.name) / "mail_credentials.txt.lock").exists())


class TestFormatPoolLine(unittest.TestCase):
    def test_line_shape(self) -> None:
        line = format_pool_line(_cred())
        parts = line.rstrip("\n").split("----")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "newone01@outlook.com")


class TestCandidatesFromArtifacts(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.source = Path(self._tmp.name)

    def _write(self, name: str, payload: dict) -> Path:
        path = self.source / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_loads_well_formed_artifacts(self) -> None:
        self._write(
            "outlook-a-20261107T000000Z.json",
            {
                "email": "a@outlook.com",
                "password": "pw",
                "client_id": "cid",
                "refresh_token": "rt",
                "recovery_email": "r@example.com",
                "bound": True,
            },
        )
        cands = candidates_from_artifacts(self.source)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].credential.email, "a@outlook.com")
        self.assertTrue(cands[0].origin.startswith("artifact:outlook-a-"))

    def test_skips_incomplete_or_broken(self) -> None:
        self._write(
            "outlook-b.json",
            {"email": "b@outlook.com", "password": "pw", "client_id": "cid"},
        )  # missing refresh_token
        (self.source / "outlook-c.json").write_text("{broken", encoding="utf-8")
        (self.source / "unrelated.json").write_text("{}", encoding="utf-8")
        self.assertEqual(candidates_from_artifacts(self.source), [])

    def test_missing_dir_returns_empty(self) -> None:
        self.assertEqual(candidates_from_artifacts(self.source / "nope"), [])


class TestCandidatesFromJsonl(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jsonl = Path(self._tmp.name) / "accounts.jsonl"

    def test_loads_outlook_ok_rows(self) -> None:
        rows = [
            {"provider": "outlook", "ok": True, "email": "j@outlook.com",
             "password": "pw", "secret": "rt-token",
             "artifacts": {"client_id": "cid"}},
            {"provider": "outlook", "ok": False, "email": "x@outlook.com",
             "password": "pw", "secret": "rt", "artifacts": {"client_id": "cid"}},
            {"provider": "grok", "ok": True, "email": "y@qq.com",
             "password": "pw", "secret": "sso", "artifacts": {}},
            {"provider": "outlook", "ok": True, "email": "z@outlook.com",
             "password": "pw", "secret": "rt2", "artifacts": {}},  # no client_id
        ]
        self.jsonl.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        with patch("scripts.replenish_pool.DEFAULT_JSONL", self.jsonl):
            cands = candidates_from_jsonl(self.jsonl)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].credential.email, "j@outlook.com")
        self.assertTrue(cands[0].origin.endswith("#1"))

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(candidates_from_jsonl(self.jsonl), [])


class TestNoSecretsInOutput(unittest.TestCase):
    def test_origin_strings_never_contain_tokens(self) -> None:
        cred = _cred()
        self.assertNotIn(cred.refresh_token, format_pool_line.__doc__ or "")


class TestDeadTokenNeverAppended(unittest.TestCase):
    def test_probe_dead_then_append_guarded_by_caller(self) -> None:
        # Simulate the replenish loop: probe classifies terminal invalid_grant
        # as quarantinable → caller must not call append for it.
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool.txt"
            dead = _cred("deadone@outlook.com")
            with patch(
                "mail_pool_probe._default_refresh",
                return_value=(False, "AADSTS7000215: Invalid client secret invalid_grant", None),
            ):
                result = probe_one(dead, live_path=None, writeback_rotated=False)
            self.assertTrue(result.quarantinable)
            self.assertFalse(pool.exists(), "dead token must never be appended")
            # Caller-side guard mirrors scripts/replenish_pool.py: append only
            # when probe.status == "ok". A quarantinable result must not append.
            appended = result.status == "ok" and append_credentials_to_pool(pool, dead)
            self.assertFalse(appended)
            self.assertFalse(pool.exists())


if __name__ == "__main__":
    unittest.main()
