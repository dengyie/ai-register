"""Outlook.com gate probe (WARP IPv6 egress).

This probe is **domain-level only** — it fills an arbitrary @outlook.com address
on the xAI signup page and inspects the send-verification-code API response via
in-page netlog hook. It does **not** create an account or burn any mailbox.

Verdict mapping (mirrors grok_register_ttk._page_email_signup_unavailable):
  - send-verification-code 200 (or body contains "ok":true) → domain ALLOWED
  - 400 with "account:email-signup-unavailable" → domain GATED (same as all
    7 self-hosted domains tested 2026-11-20)

Usage:
  .venv/Scripts/python.exe scripts/probe_outlook_gate.py --email user@outlook.com

Dependencies (run once):
  pip install DrissionPage curl_cffi
  Ensure WARP IPv6 gateway is active (proxy tag sweep__type_warp_ipv6__service_grok_reg).
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

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
EXTENSION_PATH = os.path.abspath(os.path.join(ROOT, "turnstilePatch"))
PROXY_DEFAULT = "http://sweep__type_warp_ipv6__service_grok_reg:tetherpool@10.144.144.9:9101"

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

_NETLOG_INSTALL = r"""
window.__authLog = window.__authLog || [];
if (!window.__authLogPatched) {
  window.__authLogPatched = true;
  const origFetch = window.fetch;
  window.fetch = async function(...args) {
    const resp = await origFetch.apply(this, args);
    try {
      const url = String(args[0] && args[0].url || args[0] || '');
      if (url.includes('/api/auth/')) {
        const body = await resp.clone().text().catch(() => '');
        window.__authLog.push({u: url, s: resp.status, b: body.slice(0, 400)});
      }
    } catch (e) {}
    return resp;
  };
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) { this.__url = String(u); return origOpen.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function() {
    this.addEventListener('load', () => {
      try {
        if ((this.__url || '').includes('/api/auth/')) {
          window.__authLog.push({u: this.__url, s: this.status, b: String(this.responseText || '').slice(0, 400)});
        }
      } catch (e) {}
    });
    return origSend.apply(this, arguments);
  };
}
return 'installed';
"""

_JS_DISMISS_COOKIE_BANNER = r"""
// Mirrors cpa_xai/browser_confirm._dismiss_cookie_banner exact-label logic.
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const want = new Set([
  '全部允许','接受所有','接受全部','Accept all','Accept All','Allow all','Allow All','I agree','Agree'
]);
const btns = Array.from(document.querySelectorAll('button, [role="button"], a')).filter(isVisible);
const match = btns.find((b) => want.has(String(b.innerText || b.textContent || '').trim()));
if (match) { match.click(); return String(match.innerText || '').trim(); }
// Onetrust-style "accept" + "reject" pair: click accept side.
const accept = btns.find((b) => {
  const t = String(b.innerText || '').trim().toLowerCase();
  return t === 'accept' || t === 'allow all' || t.includes('accept all') || t.includes('全部接受');
});
if (accept) { accept.click(); return 'accept'; }
// fallback: reject also clears the overlay.
const reject = btns.find((b) => {
  const t = String(b.innerText || '').trim().toLowerCase();
  return t === 'reject' || t === 'reject all' || t === 'decline';
});
if (reject) { reject.click(); return 'rejected'; }
return '';
"""

_JS_CLICK_EMAIL_SIGNUP = r"""
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
  return t.includes('continue with email') || t.includes('sign up with email')
    || t.includes('使用邮箱注册') || t.includes('使用邮箱') || t.includes('邮箱注册')
    || (t.includes('email') && !t.includes('cookie') && !t.includes('policy'));
});
if (target) { target.click(); return 'clicked'; }
return 'not-found';
"""

_JS_FILL_EMAIL = r"""
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
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter(
  (n) => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true'
);
const submit = buttons.find((n) => {
  const t = (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
  return t === 'continue' || t === 'next' || t === '注册' || t === 'sign up'
    || t.includes('continue') || t.includes('继续');
});
if (!submit) return 'filled-no-submit';
submit.click();
return 'submitted';
"""

_JS_DUMP = r"""
return JSON.stringify({
  log: (window.__authLog || []).slice(-8),
  text: ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').slice(0, 400),
});
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True, help="outlook.com address to probe")
    ap.add_argument("--proxy", default=PROXY_DEFAULT)
    args = ap.parse_args()

    from DrissionPage import Chromium, ChromiumOptions
    from proxy_bridge import LocalAuthProxyBridge

    bridge = LocalAuthProxyBridge(args.proxy)
    local_proxy = bridge.start()
    log(f"proxy bridge: {local_proxy} -> {args.proxy.split('@')[-1]}")

    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    for flag in CHROMIUM_SLIM_FLAGS:
        options.set_argument(flag)
    options.set_argument(f"--proxy-server={local_proxy}")
    options.set_user_agent(UA)
    options.add_extension(EXTENSION_PATH)
    browser = Chromium(options)
    page = browser.latest_tab
    verdict = "unknown"
    try:
        page.get(SIGNUP_URL)
        # Hydration gate: dismiss cookie banner, then wait for actionable button.
        deadline = time.time() + 25
        clicked = ""
        banner = ""
        while time.time() < deadline:
            if not banner:
                try:
                    banner = page.run_js(_JS_DISMISS_COOKIE_BANNER) or ""
                    if banner:
                        log(f"cookie banner dismissed via {banner!r}")
                        time.sleep(1.0)
                except Exception:
                    banner = ""
            try:
                clicked = page.run_js(_JS_CLICK_EMAIL_SIGNUP) or ""
            except Exception:
                clicked = ""
            if clicked == "clicked":
                break
            time.sleep(0.5)
        log(f"email-signup button: {clicked}")

        try:
            page.run_js(_NETLOG_INSTALL)
        except Exception as exc:
            log(f"netlog install failed: {exc}")

        deadline = time.time() + 15
        filled = ""
        while time.time() < deadline:
            try:
                filled = page.run_js(_JS_FILL_EMAIL, args.email) or ""
            except Exception:
                filled = ""
            if filled in ("submitted", "filled-no-submit"):
                break
            time.sleep(0.5)
        log(f"fill+submit: {filled}")

        # Wait for the send API roundtrip to land in the log.
        deadline = time.time() + 20
        entries = []
        while time.time() < deadline:
            try:
                state = json.loads(page.run_js(_JS_DUMP) or "{}")
            except Exception:
                state = {}
            entries = state.get("log", [])
            if any("send-verification-code" in e.get("u", "") for e in entries):
                break
            time.sleep(1.0)
        for e in entries:
            log(f"API {e.get('s')} {e.get('u')}: {e.get('b', '')[:160]}")
        send = [e for e in entries if "send-verification-code" in e.get("u", "")]
        if send:
            status = send[-1].get("s")
            body = str(send[-1].get("b", ""))
            if status == 200 or '"ok":true' in body:
                verdict = "ALLOWED (send 200)"
            elif "email-signup-unavailable" in body:
                verdict = "GATED (400 email-signup-unavailable)"
            else:
                verdict = f"OTHER ({status} {body[:120]})"
        else:
            verdict = f"NO-SEND-CALL (page text: {state.get('text', '')[:150]})"
    finally:
        try:
            browser.quit()
        except Exception:
            pass
    log(f"VERDICT: {args.email} -> {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
