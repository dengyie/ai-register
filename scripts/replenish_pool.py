#!/usr/bin/env python3
"""Replenish the live mail pool from registered Outlook artifacts.

Self-sustaining pool loop (replenish): accounts produced by the outlook
provider (0600 JSON artifacts in ``outlook_auths/``) are verified live
(refresh_token → access_token exchange) and appended to the live
``mail_credentials.txt`` pool, deduped by email. Dead tokens are never
appended; they are reported and skipped (artifacts stay untouched).

Sources scanned:
- ``outlook_auths/*.json`` artifacts (email/password/client_id/refresh_token)
- ``accounts.jsonl`` RegisterResult rows with ``provider=outlook`` and a
  ``secret`` of kind refresh_token (when ``--from-jsonl`` is given)

Usage:
    python scripts/replenish_pool.py                     # dry-run: report only
    python scripts/replenish_pool.py --write             # append verified rows
    python scripts/replenish_pool.py --pool PATH --write
    python scripts/replenish_pool.py --source DIR --from-jsonl --limit 50

Security: never prints secrets; probe failures show only error classes.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mail_pool_probe import (  # noqa: E402
    Credential,
    append_credentials_to_pool,
    classify_error,
    load_pool,
    probe_one,
    resolve_pool_path,
)

DEFAULT_SOURCE_DIR = REPO_ROOT / "outlook_auths"
DEFAULT_JSONL = REPO_ROOT / "accounts.jsonl"


@dataclass
class Candidate:
    credential: Credential
    origin: str  # "artifact:<path>" or "jsonl:<path>#<lineno>"


def candidates_from_artifacts(source_dir: Path) -> list[Candidate]:
    """Load outlook artifact JSONs into candidates (sorted by filename)."""
    out: list[Candidate] = []
    if not source_dir.is_dir():
        return out
    for path in sorted(source_dir.glob("outlook-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        email = str(payload.get("email") or "").strip()
        password = str(payload.get("password") or "").strip()
        client_id = str(payload.get("client_id") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        if not email or not password or not client_id or not refresh_token:
            continue
        cred = Credential(
            email=email,
            password=password,
            client_id=client_id,
            refresh_token=refresh_token,
        )
        out.append(Candidate(credential=cred, origin=f"artifact:{path.name}"))
    return out


def candidates_from_jsonl(jsonl_path: Path) -> list[Candidate]:
    """Load RegisterResult rows (provider=outlook, refresh_token secret)."""
    out: list[Candidate] = []
    if not jsonl_path.is_file():
        return out
    for line_no, raw in enumerate(jsonl_path.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if str(row.get("provider") or "") != "outlook":
            continue
        if not row.get("ok"):
            continue
        secret = str(row.get("secret") or "").strip()
        email = str(row.get("email") or "").strip()
        password = str(row.get("password") or "").strip()
        if not secret or not email or not password:
            continue
        client_id = str((row.get("artifacts") or {}).get("client_id") or "").strip()
        if not client_id:
            continue
        cred = Credential(
            email=email,
            password=password,
            client_id=client_id,
            refresh_token=secret,
        )
        out.append(Candidate(credential=cred, origin=f"jsonl:{jsonl_path.name}#{line_no}"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pool",
        default=None,
        help="Live pool path (default: HOTMAIL_ACCOUNTS_FILE env > config > mail_credentials.txt)",
    )
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE_DIR),
        help="Directory of outlook-*.json artifacts",
    )
    parser.add_argument(
        "--from-jsonl",
        action="store_true",
        help="Also scan accounts.jsonl outlook rows",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max candidates to probe (0=all)")
    parser.add_argument("--write", action="store_true", help="Append verified rows (default dry-run)")
    parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="Append even if email already in pool (default skip)",
    )
    args = parser.parse_args()

    source_dir = Path(args.source).expanduser()
    pool_path = Path(args.pool).expanduser() if args.pool else resolve_pool_path(REPO_ROOT)

    candidates = candidates_from_artifacts(source_dir)
    if args.from_jsonl:
        candidates.extend(candidates_from_jsonl(DEFAULT_JSONL))
    if args.limit and args.limit > 0:
        candidates = candidates[: args.limit]
    if not candidates:
        print("no candidates found (nothing to do)")
        return 0

    # Emails already in the live pool are skipped unless --allow-duplicate.
    already: set[str] = set()
    if pool_path.is_file():
        try:
            already = {c.email.strip().lower() for c in load_pool(pool_path)}
        except FileNotFoundError:
            already = set()

    existing = [c for c in candidates if c.credential.email.strip().lower() in already]
    todo = [c for c in candidates if c.credential.email.strip().lower() not in already]
    if existing and not args.allow_duplicate:
        print(f"skip {len(existing)} already-in-pool email(s); {len(todo)} to probe")
    else:
        print(f"{len(todo)} candidate(s) to probe")

    appended = 0
    dead = 0
    errors = 0
    for cand in todo:
        result = probe_one(cand.credential, live_path=None, writeback_rotated=False)
        masked = cand.credential.email[:3] + "***@" + cand.credential.domain
        if result.status == "ok":
            # writeback_rotated=False: probe_one only VERIFIES the RT (no
            # rotation exchange), so the appended token is exactly the one
            # that was just proven live — RT-uniqueness discipline, no
            # duplicate in-flight exchange.
            if args.write:
                ok = append_credentials_to_pool(
                    pool_path,
                    cand.credential,
                    allow_duplicate=args.allow_duplicate,
                )
                if ok:
                    appended += 1
                    print(f"[APPEND] {masked} ({cand.origin})")
                else:
                    print(f"[SKIP] {masked} duplicate-or-invalid ({cand.origin})")
            else:
                appended += 1
                print(f"[DRY-OK] {masked} verified live ({cand.origin})")
        elif result.quarantinable:
            dead += 1
            print(f"[DEAD] {masked} {result.status}: {result.reason} ({cand.origin})")
        else:
            errors += 1
            print(f"[ERR ] {masked} {result.status}: {result.reason} ({cand.origin})")

    mode = "appended" if args.write else "verified (dry-run; pass --write to append)"
    print(f"summary: {mode}={appended} dead={dead} errors={errors} pool={pool_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
