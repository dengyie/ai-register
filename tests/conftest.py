"""Pytest root for tests/ tree.

Legacy root-level test_*.py remain discoverable via pyproject testpaths.
Prefer adding new unit tests under tests/unit/.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest


@pytest.fixture(autouse=True)
def _fresh_proxy_rotation():
    """Reset the proxy_rotate module singleton around every test.

    Some test paths (e.g. resolve_attempt_proxy auto mode) configure the
    singleton from the real nodes.json and leave it in list mode; tests that
    use their own ProxyRotator still read override state from the singleton,
    so leaked config makes order-dependent failures. Reset is pure memory ops.
    """
    try:
        from register_core.util.proxy import reset_rotation_for_tests

        reset_rotation_for_tests()
        yield
        reset_rotation_for_tests()
    except Exception:
        yield
