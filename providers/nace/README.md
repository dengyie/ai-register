# console.nace.ai (Nace.AI / Drex) provider

In-process browser register for **console.nace.ai** accounts. Signs up through
the referral invite, clears the Cloudflare Turnstile on the signup form, reads
the Supabase email OTP, and produces a this-run `nace_sk_…` **API key** with
$25 starter credit.

## Stack

| Layer | Choice |
|-------|--------|
| Style | **In-process** (consumes `EmailSource`) |
| Browser | DrissionPage 4.x + real Chromium + bundled **MV3** Turnstile patch |
| Auth | Supabase email OTP (6-digit, no password) |
| Mail | `hotmail` default — a **real inbox is required** |
| CPA | **none** — never auto-inject production |

### Why a browser, and why DrissionPage

The console is a Next.js app on Vercel. The signup form POSTs a **Next.js
Server Action** (no REST route to replay) and is gated by a Cloudflare
Turnstile widget in **invisible (PRIME CONTROL)** mode. Playwright's default
CDP profile trips Turnstile (`/pat/` 401, no token); DrissionPage with the
bundled MV3 extension clears it in ~3s on the pinned Chromium build (Chrome
149 / ms-playwright `chromium-1228`).

The extension at `providers/nace/extension/` is **MV3**. The repo-root
`turnstilePatch/` is also MV3 since 2026-11-16 (backported from the
production host); keep both content patches in sync — do not swap one in
for the other unless you verify it against the pinned Chromium build.

## Hub

```bash
./register.sh nace [count]
./register.sh core run -p nace -n 1 --email-source hotmail
# aliases: nace-ai | nace.ai | drex
```

`NACE_LEGACY` is not needed — the hub always goes through `register_core`.

```bash
./register.sh smoke nace                 # reachability + static shape, no browser
NACE_SMOKE_LIVE=1 ./register.sh smoke nace   # n=1 live register, this-run api_key
```

## Env

| Var | Default | Meaning |
|-----|---------|---------|
| `NACE_EMAIL_SOURCE` | `hotmail` | Must be a real inbox (CF temp-mail drops nace OTP) |
| `NACE_INVITE_URL` | invite `adf5acm5` | Referral link (the "类目" invite) |
| `NACE_OTP_TIMEOUT` | `240` | OTP poll window (seconds) |
| `NACE_CHROME_PATH` | auto | Pin the Chromium that clears Turnstile |
| `NACE_EXTENSION_DIR` | bundled | Override the Turnstile-patch extension |
| `NACE_PROXY` | empty | Fixed register (browser) egress |
| `NACE_KEEP_BROWSER` | `0` | `1` = leave browser open (attended debug) |
| `REGISTER_EGRESS` | `direct` | `list` \| `core` \| `clash` \| `direct` \| `auto` |

Serial only (`--threads 1`). Do **not** fan this out — each attempt owns a real
browser.

## Host requirements

- A **browser-capable** host with a display or Xvfb. Never run on an
  OOM-hostile box (pxed): Chromium gets SIGKILLed there under tenant OOM.
- A Chromium build on disk. Auto-discovery prefers
  `~/.cache/ms-playwright/chromium-1228` (Linux) /
  `%LOCALAPPDATA%\ms-playwright\chromium-1228` (Windows); pin with
  `NACE_CHROME_PATH`.
- The Hotmail pool (`mail_credentials.txt`) present where the mailbox source
  reads it.

## Artifacts

- `providers/nace/output/` (0700)
- `providers/nace/output/nace-<email>-<ts>.json` (0600) — this-run deliverable
  (`ok`, `email`, `api_key`, `api_base`, `model`)
- Pipeline sink: `providers/nace/output/pipeline.jsonl`

Success = this-run `secret_kind=api_key` plus a new 0600 json. Do **not** append
full keys to `accounts.jsonl`; historical tails do not count.

## Flow (verified 2026-10-11)

1. open `https://console.nace.ai/invite/adf5acm5` (sets `drex_ref`)
2. wait for the invisible Turnstile token (`input[name=cf-turnstile-response]`)
3. fill `#email`, submit → Supabase `signInWithOtp`
4. poll the Hotmail inbox for `no-reply@nace.ai` / "Your Drex sign-in code"
5. type the 6-digit code → redirect to `/dashboard`
6. go to **`/dashboard/api-keys`** → **Create key** → name the key → confirm;
   the modal reveals the value once in a readonly
   `input[aria-label="New API key"]` (read `input.value`, not the masked table
   cell). A first-visit onboarding card (`Show a new key` → eye) is kept as a
   fallback only.

Live key verified: `POST https://console.nace.ai/v1/systemone` (model
`drex-v1.5`) → HTTP 200. A full provider run (`ok=True`,
`secret_kind=api_key`, this-run `0600` json) plus a live call was confirmed
end-to-end 2026-10-11.

## Compliance

Education / personal automation only. May violate third-party ToS. No
mass-farm defaults, no plus-alias, no CPA inject.
