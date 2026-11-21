"""Automate adding a real (non plus-) alias to a Microsoft personal account.

Verified chain (2026-11-21, KevinWalters5892@outlook.com):
  login.live.com sign-in
  -> TOU accrue (accept) -> passkey fido/create (cancel) -> stay-signed-in (Yes,
     keeps the OAuth session alive across redirect hops)
  -> proof-confirmation: FULL backup email (auto-read from IMAP inbox owner)
  -> one-time code (auto-read via QQ IMAP, from account-security-noreply)
  -> account.live.com/names/Manage -> 添加电子邮件 -> create new outlook alias.

Only this alias type is a fresh string to xAI (plus-aliases normalize back to
the main address). The new alias delivers into the same mailbox, so the
existing OAuth refresh_token keeps working for OTP reads.

Credentials are CLI args, never embedded.

Usage:
  .venv/Scripts/python.exe scripts/ms_add_alias.py \
      --email KevinWalters5892@outlook.com --password '...' \
      --backup-email 601560588@qq.com \
      --imap-host imap.qq.com --imap-user 601560588@qq.com --imap-pass '<authcode>' \
      --alias-local grokreg7k2m9x
"""
from __future__ import annotations

import argparse
import email as email_lib
import imaplib
import re
import sys
import time
from email.header import decode_header
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

JS_VISIBLE = r"""
function isVisible(n){if(!n)return false;var s=window.getComputedStyle(n);
if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false;
var r=n.getBoundingClientRect();return r.width>0&&r.height>0;}
"""

JS_FILL_FIRST_INPUT = JS_VISIBLE + r"""
var val = arguments[0];
var inp=Array.from(document.querySelectorAll(
  'input[type=text], input[type=tel], input[type=email], input[autocomplete=one-time-code], input[autocomplete=tel], input[inputmode=numeric], input:not([type])'
)).filter(isVisible)
  .find(function(i){return !i.disabled && !i.readOnly;});
if(!inp)return 'no-input';
var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
setter.call(inp,val);
inp.dispatchEvent(new Event('input',{bubbles:true}));
inp.dispatchEvent(new Event('change',{bubbles:true}));
var btn=Array.from(document.querySelectorAll('button[type=submit], button, input[type=submit]')).filter(isVisible)
  .find(function(b){var x=(b.innerText||b.value||'').trim();
    return x.indexOf('发送')>=0||x==='下一步'||x.indexOf('验证')>=0||x.indexOf('提交')>=0||x.indexOf('确认')>=0||x==='接受'||x.indexOf('继续')>=0;});
if(!btn)return 'filled-no-btn';
btn.click();
return 'submitted:'+(btn.innerText||btn.value||'').trim();
"""

JS_CLICK_EXACT = JS_VISIBLE + r"""
var want = arguments[0];
var btns=Array.from(document.querySelectorAll('a, button, input[type=submit], input[type=button], [role=button]')).filter(isVisible);
var t=btns.find(function(b){var x=(b.innerText||b.value||'').trim();return x===want;});
if(t){t.click();return 'clicked:'+want;}
return 'not-found';
"""

JS_INTERSTITIAL_STEP = JS_VISIBLE + r"""
var btns=Array.from(document.querySelectorAll('button, input[type=submit], input[type=button], [role=button]')).filter(isVisible);
var no=btns.find(function(b){var x=(b.innerText||b.value||'').trim();
  return x==='否'||x==='取消'||x.toLowerCase()==='skip'||x.toLowerCase()==='not now';});
if(no){no.click();return 'skip:'+(no.innerText||no.value||'').trim();}
var any=btns.find(function(b){var x=(b.innerText||b.value||'').trim();
  return ['是','接受','同意','继续','下一步','确定','yes','accept','continue','next','ok'].indexOf(x)>=0;});
if(any){any.click();return 'next:'+(any.innerText||any.value||'').trim();}
return '';
"""

JS_ADD_ALIAS_CLICK = JS_VISIBLE + r"""
var links=Array.from(document.querySelectorAll('a, button')).filter(isVisible);
var t=links.find(function(b){var x=(b.innerText||'').trim();
  return x==='添加电子邮件'||x==='添加别名'||x.toLowerCase()==='add an alias'||x.toLowerCase()==='add email';});
if(t){t.click();return 'clicked';}
return 'not-found';
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def dsub(raw) -> str:
    out = ""
    for s, enc in decode_header(raw or ""):
        if isinstance(s, bytes):
            s = s.decode(enc or "utf-8", "ignore")
        out += s
    return out


def imap_wait_code(host: str, user: str, pwd: str, *, sender_part: str,
                   subject_part: str, timeout_s: int = 180) -> str | None:
    """Poll an IMAP INBOX for a FRESH (≤10min) MS one-time code. Marks seen mail read.

    Freshness guard: yesterday's untrashed OTC mail otherwise matches the
    sender/subject filters and feeds a dead code into the proof page.
    """
    m = imaplib.IMAP4_SSL(host)
    m.login(user, pwd)
    m.select("INBOX")
    deadline = time.time() + timeout_s
    code = None
    seen: set[bytes] = set()
    while time.time() < deadline and not code:
        typ, data = m.search(None, "UNSEEN")
        ids = data[0].split() if typ == "OK" and data[0] else []
        for mid in reversed(ids[-10:]):
            if mid in seen:
                continue
            seen.add(mid)
            typ, d = m.fetch(mid, "(RFC822)")
            msg = email_lib.message_from_bytes(d[0][1])
            frm = msg.get("From", "").lower()
            subj = dsub(msg.get("Subject", ""))
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body += part.get_payload(decode=True).decode("utf-8", "ignore")
            else:
                body = msg.get_payload(decode=True).decode("utf-8", "ignore")
            if sender_part not in frm or subject_part not in subj:
                continue
            # reject stale mails (Date header older than 10 minutes)
            try:
                from email.utils import parsedate_to_datetime
                mail_dt = parsedate_to_datetime(msg.get("Date", ""))
                age_s = time.time() - mail_dt.timestamp()
                if age_s > 600:
                    log(f"imap: skip stale code mail ({int(age_s) // 60}min old)")
                    continue
            except Exception:
                pass
            mm = re.search(r"\b(\d{4,8})\b", subj + " " + body)
            if mm:
                code = mm.group(1)
                log(f"imap code: {code} ({subj[:30]} @ {msg.get('Date','')[:22]})")
                break
        if not code:
            time.sleep(8)
    m.logout()
    return code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True, help="MS account main address")
    ap.add_argument("--password", required=True)
    ap.add_argument("--backup-email", required=True, help="full backup address for proof confirmation")
    ap.add_argument("--imap-host", default="imap.qq.com")
    ap.add_argument("--imap-user", required=True)
    ap.add_argument("--imap-pass", required=True)
    ap.add_argument("--alias-local", required=True)
    ap.add_argument("--user-data-dir", default=str(Path.home() / ".ms_alias_profile"),
                    help="persistent Chromium profile: keep stay-signed-in cookies so "
                    "later runs skip the proof/OTC login chain entirely (MS rate-limits "
                    "the proof method, ~30min window)")
    ap.add_argument("--fresh-profile", action="store_true", help="ignore saved profile")
    args = ap.parse_args()

    from DrissionPage import Chromium, ChromiumOptions

    options = ChromiumOptions()
    if args.fresh_profile:
        options.auto_port()
    else:
        # Fixed port + persistent user dir: same profile == logged-in session reuse.
        options.set_local_port(9333)
        options.set_user_data_path(args.user_data_dir)
    options.set_timeouts(base=1)
    for flag in (
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--mute-audio",
        "--disable-blink-features=AutomationControlled",
        "--remote-allow-origins=*",
    ):
        options.set_argument(flag)
    options.set_user_agent(UA)
    browser = Chromium(options)
    page = browser.latest_tab

    def js(script, *a):
        try:
            return page.run_js(script, *a) or ""
        except Exception as exc:
            log(f"js error: {exc}")
            return ""

    def interstitial_loop(max_rounds: int = 10) -> None:
        """Clear TOU / fido / stay-signed-in / protection hops."""
        for _ in range(max_rounds):
            url = page.url or ""
            if "account.microsoft.com" in url and "complete-client-signin" not in url and "/fido/" not in url:
                return
            if "/fido/" in url:
                cancel = page.ele("@id=idBtn_Back", timeout=3)
                if cancel:
                    cancel.click()
                    log("fido cancelled")
                    time.sleep(5)
                    continue
            r = js(JS_INTERSTITIAL_STEP)
            if r:
                log(f"interstitial: {r}")
            time.sleep(6)

    try:
        # ── sign-in (skipped when the persistent profile still holds a session) ──
        page.get("https://account.live.com/names/Manage")
        time.sleep(6)
        # Logged-in marker: names/Manage shows "添加电子邮件" only with a session;
        # anon visitors get redirected to login (no account.microsoft.com hop).
        page_text = (page.run_js("return document.body.innerText") or "")
        session_live = ("添加电子邮件" in page_text or "account.microsoft.com" in (page.url or "")) \
            and "/interrupt/" not in (page.url or "")
        if session_live:
            log(f"session reuse: already logged in ({page.url[:70]})")
        else:
            page.get("https://login.live.com/")
            time.sleep(5)
        # Session may resume mid-proof (SA_20MIN slide): the proof page shows
        # "验证你的电子邮件 / 发送验证码" and NO email input at all. Handle it
        # like the proof-confirmation branch below instead of aborting. Also the
        # second login.live.com navigation can auto-advance straight to the
        # account portal (live session) — re-check before declaring abort.
        user_el = page.ele('css:input[type="email"]', timeout=8) or page.ele("@id=usernameEntry", timeout=4)
        if not user_el:
            body_txt = (page.run_js("return document.body.innerText") or "")
            if session_live:
                log("session live, no login form — skip to alias step")
                user_el = None  # fall through to interstitial/alias section
            elif "验证你的电子邮件" in body_txt or "发送验证码" in body_txt:
                log("resume: proof page without email input — treat as proof flow")
                user_el = None
            elif "account.microsoft.com" in (page.url or ""):
                log("second nav auto-advanced to portal — treat as logged in")
                session_live = True
                user_el = None
            else:
                log("no email input; abort")
                return 1
        if user_el:
            user_el.input(args.email)
            js(JS_CLICK_EXACT, "下一步") or (page.ele('css:button[type="submit"]', timeout=3) or None and 0)
            time.sleep(7)

        # proof-confirmation may appear instead of password (backup email check)
        txt = (page.run_js("return document.body.innerText") or "")
        if "备选电子邮件" in txt or "验证你的电子邮件" in txt or "proof-confirmation" in page.html:
            # Method rate limit guard: MS caps proof attempts per window (~30min).
            if "次数已达到上限" in txt or "已达到上限" in txt:
                log("proof method rate-limited by MS; abort now, rerun in ~30min")
                return 5
            r = js(JS_FILL_FIRST_INPUT, args.backup_email)
            log(f"proof step1 (backup email): {r}")
            time.sleep(9)
            # MS may refuse the proof attempt here (rate limit): page shows the
            # limit text and no new OTC mail will ever arrive.
            txt2 = (page.run_js("return document.body.innerText") or "")
            if "已达到上限" in txt2:
                log("proof rate-limited after step1; abort, rerun in ~30min")
                return 5
            code = imap_wait_code(
                args.imap_host, args.imap_user, args.imap_pass,
                sender_part="account-security-noreply@accountprotection.microsoft.com",
                subject_part="一次性代码",
            ) or imap_wait_code(
                args.imap_host, args.imap_user, args.imap_pass,
                sender_part="accountprotection.microsoft.com",
                subject_part="安全代码",
            )
            if not code:
                log("no IMAP code; abort")
                return 2
            r = js(JS_FILL_FIRST_INPUT, code)
            log(f"proof step2 (code {code}): {r}")
            time.sleep(9)
            # stay signed in: YES to keep the OAuth session across hops
            r = js(JS_CLICK_EXACT, "是") or js(JS_CLICK_EXACT, "Yes")
            log(f"stay-signed-in: {r}")
            time.sleep(8)

        # password step
        pw = page.ele('css:input[type="password"]', timeout=8) or page.ele("@name=passwd", timeout=4)
        if pw:
            pw.input(args.password)
            btn = page.ele('css:button[type="submit"]', timeout=3)
            if btn:
                btn.click()
            time.sleep(7)
            r = js(JS_CLICK_EXACT, "是") or js(JS_CLICK_EXACT, "Yes")
            if r:
                log(f"stay-signed-in: {r}")
            time.sleep(8)

        interstitial_loop()
        log(f"landing: {page.url[:90]}")
        # Hard gate: the alias ops live on account.live.com; require either the
        # account.microsoft.com portal or the logged-in names/Manage page
        # (anon visitors get bounced to login.live.com instead).
        landing = page.url or ""
        if "account.microsoft.com" not in landing and not (
            "account.live.com/names/Manage" in landing and "login" not in landing
        ):
            log("session did not reach account portal; abort")
            return 6

        # ── alias page ──
        page.get("https://account.live.com/names/Manage")
        time.sleep(8)
        # SA_20MIN slide: names/Manage can bounce to a fresh proof challenge even
        # though the portal landing succeeded moments ago. Re-run the proof flow
        # once here, then return to names/Manage.
        for _proof_round in range(2):
            body_txt = (page.run_js("return document.body.innerText") or "")
            if "验证你的电子邮件" not in body_txt and "发送验证码" not in body_txt:
                break
            log("names/Manage bounced to proof challenge; completing it")
            if "次数已达到上限" in body_txt or "已达到上限" in body_txt:
                log("proof rate-limited at alias stage; rerun in ~30min")
                return 5
            r = js(JS_FILL_FIRST_INPUT, args.backup_email)
            log(f"late proof step1: {r}")
            time.sleep(9)
            txt2 = (page.run_js("return document.body.innerText") or "")
            if "已达到上限" in txt2:
                log("proof rate-limited after late step1; rerun in ~30min")
                return 5
            code = imap_wait_code(
                args.imap_host, args.imap_user, args.imap_pass,
                sender_part="account-security-noreply@accountprotection.microsoft.com",
                subject_part="一次性代码",
            ) or imap_wait_code(
                args.imap_host, args.imap_user, args.imap_pass,
                sender_part="accountprotection.microsoft.com",
                subject_part="安全代码",
            )
            if not code:
                log("no late IMAP code; abort")
                return 2
            r = js(JS_FILL_FIRST_INPUT, code)
            log(f"late proof step2: {r}")
            time.sleep(9)
            r = js(JS_CLICK_EXACT, "是") or js(JS_CLICK_EXACT, "Yes")
            log(f"stay-signed-in: {r}")
            time.sleep(8)
            interstitial_loop()
            page.get("https://account.live.com/names/Manage")
            time.sleep(8)
        if "/interrupt/" in (page.url or ""):
            log("still gated by interrupt page; abort")
            return 3
        # Confirmed logged-in names/Manage must show the add-email control.
        body_txt = (page.run_js("return document.body.innerText") or "")
        if "添加电子邮件" not in body_txt:
            log(f"names/Manage still not logged in ({page.url[:70]}); abort")
            return 3
        r = js(JS_ADD_ALIAS_CLICK)
        log(f"add-alias: {r}")
        time.sleep(8)
        log(f"add-alias page: {page.url[:90]}")
        body = (page.run_js("return document.body.innerText") or "")[:600]
        log(f"page: {body[:300].replace(chr(10), ' | ')}")

        # choose "create a new email address" + fill local part + submit
        r = js(JS_VISIBLE + r"""
var local = arguments[0];
var radios = Array.from(document.querySelectorAll('input[type=radio]')).filter(isVisible);
var labels = radios.map(function(r){
  var lab = r.closest('label') || document.querySelector('label[for="'+r.id+'"]');
  return {r: r, text: ((lab && lab.innerText) || '').replace(/\s+/g,' ').toLowerCase()};
});
var pick = labels.find(function(l){return l.text.indexOf('新')>=0 || l.text.indexOf('new')>=0;});
if(pick){pick.r.click();return 'radio-ok';}
if(radios.length===1){radios[0].click();return 'radio-only';}
return 'no-radio';
""", args.alias_local)
        log(f"radio: {r}")
        time.sleep(2)
        # AddAssocId is a classic server-side form (verified 2026-11-21):
        #   radio idLiveOption = "创建新的电子邮件地址"
        #   text  AssociatedIdLive = local part (SingleDomain says the domain)
        #   submit SubmitYes ("添加用户名") / ButtonNo
        # The generic "usernameEntry/下一步" matchers never fit this page.
        alias_submitted = ""
        for _attempt in range(6):
            r = js(JS_VISIBLE + r"""
var local = arguments[0];
var inp = document.getElementById('AssociatedIdLive');
if (!inp) {
  inp = document.getElementById('usernameEntry')
    || Array.from(document.querySelectorAll('input[type=text], input[type=email], input:not([type])')).filter(isVisible)
      .find(function(i){return !i.disabled && !i.readOnly;});
}
if(!inp)return 'no-input';
var liveRadio = document.getElementById('idLiveOption');
if (liveRadio) liveRadio.checked = true;
inp.focus();
var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
var tracker=inp._valueTracker; if(tracker) tracker.setValue('');
setter.call(inp,local);
inp.dispatchEvent(new Event('input',{bubbles:true}));
inp.dispatchEvent(new Event('change',{bubbles:true}));
var btn = document.getElementById('SubmitYes')
  || Array.from(document.querySelectorAll('button[type=submit], input[type=submit], button')).filter(isVisible)
    .find(function(b){var x=(b.innerText||b.value||'').trim();
      return x==='添加别名'||x==='添加'||x==='添加用户名'||x==='下一步'||x==='add alias'||x==='add'||x.indexOf('创建')>=0;});
if(!btn)return 'filled-no-btn';
btn.click();
return 'submitted:'+(btn.innerText||btn.value||'').trim();
""", args.alias_local)
            log(f"alias submit attempt {_attempt + 1}: {r}")
            if str(r).startswith("submitted"):
                alias_submitted = str(r)
                break
            time.sleep(3)
        time.sleep(10)
        body = (page.run_js("return document.body.innerText") or "")[:1000]
        log(f"final: {body[:400].replace(chr(10), ' | ')}")
        if args.alias_local.lower() in body.lower():
            log(f"ALIAS-CREATED: {args.alias_local}@outlook.com")
            return 0
        log("ALIAS-STATUS-UNCLEAR")
        return 4
    finally:
        try:
            browser.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
