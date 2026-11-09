"""Inter-step pacing for the Outlook protocol register.

ANTIBAN discipline (docs/ANTIBAN.md): minute-scale account spacing, second-scale
step jitter, and a cold-start delay before the first attempt of a batch. All
values tune via OUTLOOK_* env without code changes.
"""

from __future__ import annotations

import os
import random
import time
from typing import Callable


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError:
        return default


def step_jitter_range() -> tuple[float, float]:
    """(min, max) seconds between protocol steps; default 3–8s."""
    lo = _env_float("OUTLOOK_STEP_JITTER_MIN", 3.0)
    hi = _env_float("OUTLOOK_STEP_JITTER_MAX", 8.0)
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def account_delay_range_minutes() -> tuple[float, float]:
    """(min, max) minutes between accounts; default 3–8 minutes."""
    lo = _env_float("OUTLOOK_REG_JITTER_MIN", 3.0)
    hi = _env_float("OUTLOOK_REG_JITTER_MAX", 8.0)
    if hi < lo:
        lo, hi = hi, lo
    return lo * 60.0, hi * 60.0


def cold_start_delay_s() -> float:
    """Delay before the very first account of a batch; default 30–90s."""
    return random.uniform(
        _env_float("OUTLOOK_COLD_START_MIN", 30.0),
        _env_float("OUTLOOK_COLD_START_MAX", 90.0),
    )


def human_pause(
    *,
    after_step: str = "",
    log: Callable[[str], None] | None = None,
) -> float:
    """Sleep a randomized step jitter; returns the seconds actually slept."""
    lo, hi = step_jitter_range()
    seconds = random.uniform(lo, hi)
    if log is not None and after_step:
        log(f"human_pace after_{after_step} sleep={seconds:.1f}s")
    time.sleep(seconds)
    return seconds


def account_pause(
    *,
    log: Callable[[str], None] | None = None,
) -> float:
    """Sleep the minute-scale inter-account delay; returns seconds slept."""
    lo, hi = account_delay_range_minutes()
    seconds = random.uniform(lo, hi)
    if log is not None:
        log(f"account_pace sleep={seconds:.0f}s")
    time.sleep(seconds)
    return seconds
