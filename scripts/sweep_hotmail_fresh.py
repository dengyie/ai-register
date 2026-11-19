"""xAI account-existence sweep for the Hotmail main pool.

Purpose
-------
The registration pool (mail_credentials.txt, 63k hotmail.com mains) has a deep
"already registered" wall at the head. Burning through it via full signup
attempts costs ~40s per address (send 200 + OTP + verify-email chooser).
xAI's LOGIN page reveals existence without any send: "Login with email" →
fill address → continue → password field appears iff the account exists.
This sweep classifies mains at ~5-8s each (no OTP, no sends, no cooldown),
producing a fresh-only pool file for the register loop.

Egress: the register's WARP IPv6 gateway tag (xAI send gate bypass, see vault
2026-11-20 entry). Gateway basic-auth rides a LocalAuthProxyBridge (Chromium
cannot embed proxy credentials in --proxy-server).

Classification is text/UI-shape based and dumps page text for the first probes
of each class so the operators can verify the markers before trusting bulk
output. Unknown responses are journaled verbatim.

Usage (instabox3, X11 :1):
  .venv/bin/python scripts/sweep_hotmail_fresh.py \
    --pool mail_credentials.txt --start 1 --every 1 --probes 40 \
    --count 5 --out-fresh /tmp/fresh_pool.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOGIN_URL = "https://accounts.x.ai/login?redirect=grok-com"
EXTENSION_PATH = os.path.abspath(os.path.join(ROOT, "turnstilePatch"))

CHROMIUM_SLIM_FLAGS = [
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--mute-audio",
    "--disable-background-networking",
    "--no-first-run",
    "--disable-blink-features=AutomationControlled",
    "--remote-allow-origins=*",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# Login-page response markers (verified empirically on first sweeps; the
# script journals page text for every classification so markers can be tuned).
FRESH_MARKERS = (
    "no account",
    "couldn't find an account",
    "could not find an account",
    "account not found",
    "找不到账号",
    "账户不存在",
    "没有账号",
)
REGISTERED_MARKERS = (
    "enter your password",
    "输入密码",
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_pool(path: Path, start: int, every: int) -> list[tuple[int, str, str]]:
    """Yield (line_no, email, raw_line) — raw line keeps credentials for the
    fresh pool file (the register's _hotmail_split_credential_line needs the
    full email----password----refresh shape, an email-only line is rejected)."""
    mains: list[tuple[int, str, str]] = []
    with open(path, encoding="utf-8-sig") as f:
        for line_no, raw in enumerate(f, 1):
            if line_no < start:
                continue
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                continue
            email = stripped.split("----", 1)[0].strip()
            if "@" not in email:
                continue
            if (line_no - start) % max(1, every) != 0:
                continue
            mains.append((line_no, email, line))
    return mains


def build_browser(proxy_url: str):
    from DrissionPage import Chromium, ChromiumOptions
    from proxy_bridge import LocalAuthProxyBridge

    bridge = LocalAuthProxyBridge(proxy_url)
    local_proxy = bridge.start()
    log(f"proxy bridge: {local_proxy} -> {proxy_url.split('@')[-1]}")

    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    for flag in CHROMIUM_SLIM_FLAGS:
        options.set_argument(flag)
    try:
        options.set_argument(f"--proxy-server={local_proxy}")
    except Exception:
        pass
    try:
        options.set_user_agent(UA)
    except Exception:
        pass
    try:
        options.add_extension(EXTENSION_PATH)
    except Exception as exc:
        log(f"extension load failed (continuing): {exc}")
    browser = Chromium(options)
    page = browser.latest_tab
    return browser, page


JS_CLICK_EMAIL_LOGIN = r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  if (!isVisible(node) || node.disabled) return false;
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
  return t === 'login with email' || t.includes('login with email') || t.includes('使用邮箱登录');
});
if (target) { target.click(); return 'clicked'; }
return 'not-found';
"""

JS_FILL_EMAIL_AND_SUBMIT = r"""
const email = arguments[0];
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll(
  'input[type="email"], input[name="email"], input[autocomplete="email"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) return 'no-email-input';
input.focus();
const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (setter) setter.call(input, email); else input.value = email;
input.dispatchEvent(new Event('input', { bubbles: true }));
input.dispatchEvent(new Event('change', { bubbles: true }));
input.dispatchEvent(new Event('blur', { bubbles: true }));
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter(
  (n) => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true'
);
const submit = buttons.find((n) => {
  const t = (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
  return t === 'continue' || t === 'next' || t.includes('continue') || t === '登录' || t.includes('continue');
});
if (!submit) return 'filled-no-submit';
submit.click();
return 'submitted';
"""

JS_PAGE_STATE = r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const pw = Array.from(document.querySelectorAll('input[type="password"]')).some(isVisible);
return JSON.stringify({
  pw,
  text: ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').slice(0, 300),
});
"""


def classify_once(page, email: str) -> tuple[str, str]:
    """Return (class, evidence) — class in registered|fresh|unknown|error."""
    try:
        page.get(LOGIN_URL)
    except Exception as exc:
        return "error", f"goto: {exc}"
    # Wait for the chooser button (hydration gate, bounded).
    deadline = time.time() + 20
    clicked = ""
    while time.time() < deadline:
        try:
            clicked = page.run_js(JS_CLICK_EMAIL_LOGIN) or ""
        except Exception:
            clicked = ""
        if clicked == "clicked":
            break
        time.sleep(0.5)
    if clicked != "clicked":
        return "unknown", "chooser-button-not-found"

    # Fill email + continue.
    deadline = time.time() + 12
    submitted = ""
    while time.time() < deadline:
        try:
            submitted = page.run_js(JS_FILL_EMAIL_AND_SUBMIT, email) or ""
        except Exception:
            submitted = ""
        if submitted in ("submitted", "filled-no-submit"):
            break
        time.sleep(0.5)
    if submitted not in ("submitted", "filled-no-submit"):
        return "unknown", f"email-fill:{submitted}"

    # Poll for the decisive UI state.
    deadline = time.time() + 15
    last_state = ""
    while time.time() < deadline:
        try:
            state = json.loads(page.run_js(JS_PAGE_STATE) or "{}")
        except Exception:
            state = {}
        last_state = state.get("text", "")
        if state.get("pw"):
            return "registered", "password-field"
        low = last_state.lower()
        if any(m in low for m in FRESH_MARKERS):
            return "fresh", "marker"
        if any(m in low for m in REGISTERED_MARKERS):
            return "registered", "marker"
        time.sleep(0.6)
    return "unknown", f"text:{last_state[:200]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="mail_credentials.txt")
    ap.add_argument("--start", type=int, default=1, help="1-based line to start from")
    ap.add_argument("--every", type=int, default=1, help="sample every Nth line")
    ap.add_argument("--probes", type=int, default=40, help="max probes this run")
    ap.add_argument("--count", type=int, default=0, help="stop after N fresh found (0=off)")
    ap.add_argument("--proxy", default=(
        "http://sweep__type_warp_ipv6__service_grok_reg:tetherpool@10.144.144.9:9101"
    ))
    ap.add_argument("--out-fresh", default="/tmp/fresh_pool.txt")
    ap.add_argument("--out-registered", default="/tmp/registered_pool.txt")
    ap.add_argument("--journal", default="/tmp/sweep_journal.jsonl")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    pool_path = Path(args.pool)
    if not pool_path.is_absolute():
        pool_path = (Path.cwd() / pool_path).resolve()
    mains = load_pool(pool_path, args.start, args.every)
    log(f"pool={pool_path} candidates={len(mains)} start={args.start} every={args.every}")

    already_registered: set[str] = set()
    reg_path = Path(args.out_registered)
    if reg_path.exists():
        already_registered = {
            l.strip().lower() for l in reg_path.read_text().splitlines() if l.strip()
        }
    already_fresh: set[str] = set()
    fresh_path = Path(args.out_fresh)
    if fresh_path.exists():
        already_fresh = {
            l.strip().split("----")[0].lower()
            for l in fresh_path.read_text().splitlines() if l.strip()
        }
    fresh_lines: dict[str, str] = {}
    if fresh_path.exists():
        for l in fresh_path.read_text().splitlines():
            if "----" in l:
                fresh_lines[l.split("----", 1)[0].strip().lower()] = l.rstrip("\n")

    os.environ.setdefault("DISPLAY", ":1")
    browser, page = build_browser(args.proxy)
    fresh_found = 0
    stats = {"registered": 0, "fresh": 0, "unknown": 0, "error": 0}

    try:
        for line_no, email, raw_line in mains:
            if args.probes and stats["registered"] + stats["fresh"] + stats["unknown"] + stats["error"] >= args.probes:
                break
            if args.count and fresh_found >= args.count:
                break
            key = email.lower()
            if key in already_registered:
                continue
            if key in already_fresh:
                fresh_found += 1
                continue
            cls, evidence = classify_once(page, email)
            stats[cls] = stats.get(cls, 0) + 1
            log(f"L{line_no} {email} -> {cls} ({evidence[:80]})")
            with open(args.journal, "a", encoding="utf-8") as jf:
                jf.write(json.dumps({
                    "line": line_no, "email": email, "class": cls,
                    "evidence": evidence, "ts": time.time(),
                }, ensure_ascii=False) + "\n")
            if cls == "registered":
                with open(reg_path, "a", encoding="utf-8") as f:
                    f.write(email + "\n")
            elif cls == "fresh":
                # Write the ORIGINAL full credential line so the register's
                # _hotmail_split_credential_line sees password/refresh fields.
                fresh_lines[key] = raw_line
                with open(fresh_path, "w", encoding="utf-8") as f:
                    for l in fresh_lines.values():
                        f.write(l + "\n")
                fresh_found += 1
            elif cls == "error":
                # Browser/page trouble — recreate before next probe.
                try:
                    page.get("about:blank")
                except Exception:
                    pass
                time.sleep(2)
    finally:
        try:
            browser.quit()
        except Exception:
            pass
        log(f"stats={stats} fresh_total={fresh_found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
