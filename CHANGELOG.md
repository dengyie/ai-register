# Changelog

All notable changes to this project are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
project versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **通用 Cloudflare 盾求解层 `register_core/cf_solver.py`**：接入 slidex
  打码站（captcha.run 兼容 v2 协议）新增的 `turnstile` / `cf_clearance`
  任务类型。`solve_turnstile_token()` 返回 token，
  `solve_cf_clearance()` 返回绑定了出口 IP + UA 的 `CFClearanceResult`。
  站点默认走自建主站 `10.144.144.9:8077`，连接失败自动切冷备
  `10.144.144.88:8077`（`CAPTCHA_RUN_BASE` / `CF_SOLVER_BASE` 可覆盖）；
  key 走 `CAPTCHA_RUN_KEY`。**同出口 IP 铁律**：调用方必须传注册浏览器
  同一条上游代理串（带凭据），不是本地代理桥。失败分类：求解侧失败 →
  `kind="captcha"`（永不隔离出口 IP，与 `util/proxy.py` 既有语义一致）、
  传输失败 → `kind="network"`；401 确定性不可重试。
- **grok 遗留路径主动求解接线（默认关闭）**：`getTurnstileToken()` 开头新增
  `cf_solver_enabled` 门控分支——开启且 `CAPTCHA_RUN_KEY` 已配置时，从
  DOM 抓 `[data-sitekey]` 主动求解并回填 `cf-turnstile-response`（React
  native setter，与既有二次复用同款）；求解失败/超时/无 sitekey/本地代理
  端口一律原样回落既有被动轮询，现有逻辑一行未动。新配置键
  `cf_solver_enabled`（默认 `false`）、`cf_solver_timeout`（默认 120s，
  钳制 15-150s），env `CF_SOLVER_ENABLED` / `CF_SOLVER_TIMEOUT` 可覆盖。
  关闭时行为零变更。

### Added

- **console.nace.ai provider** (in-process browser): referral invite →
  Cloudflare Turnstile (invisible/PRIME CONTROL) → Supabase email OTP →
  `/dashboard` → reveal a fresh `nace_sk_…` **API key**. Hub
  `./register.sh nace [count]`, aliases `nace-ai` / `nace.ai` / `drex`. Drives
  DrissionPage with a bundled **MV3** Turnstile patch (the repo-root
  `turnstilePatch/` is MV2 and crashes Chrome 149+). Default mailbox is
  `hotmail` — a real inbox is required because Cloudflare Email Routing drops
  nace.ai OTP for the project temp domains. Serial only (each attempt owns a
  browser); requires a browser-capable host with a display/Xvfb. Success =
  this-run `secret_kind=api_key` + new `0600` `nace-*.json`. No CPA inject.
  Key is minted on `/dashboard/api-keys` via the **Create key** modal (revealed
  once in a readonly `input[aria-label="New API key"]`). Full provider run
  verified 2026-10-11: `ok=True`, `secret_kind=api_key`, live key
  `POST https://console.nace.ai/v1/systemone` (model `drex-v1.5`) → HTTP 200.
- **z.ai (ZCode Coding Plan) provider** (in-process, `providers/zai/`): two-stage
  email signup (signup → verify-link → Complete Registration) producing a chat.z.ai
  JWT (`secret_kind="jwt"`). Mailbox = one-mail catch-all prefix invention read via
  unified admin API with client-side `to_addr` filtering (decoupled from address-row
  name alignment). Receipt chain immune to slidex false-success (signup POST NET tap
  → fresh-link baseline dedup → JWT email-claim gate); fresh-session rotation with
  cookie_id churn and per-session hard timeouts. Operator-explicit hub join
  (`./register.sh zai-join`) with pool-verify gate (`register_core/verify/zai_hub.py`).
  Handles are natural-word alnum-only (dot-blackhole lesson) with dual uniqueness
  preflight. Tests: `tests/unit/test_zai_provider.py`.

- **typesafe.ai / jev console provider** (in-process): Stytch magic-link →
  `/api/auth/callback` → `/api/api-keys`. Hub `./register.sh typesafe [count]`,
  aliases `jev` / `typesafe-ai`. Default mailbox is tinyhost (full body).
  Success = this-run `secret_kind=api_key`. Does **not** copy the original
  512/256 farm defaults. No CPA inject. pxed smoke 2026-09-22: `./register.sh
  typesafe 1` → `CONTRACT_EXIT:0` this-run `api_key` (rsync to `/data/grok-register`;
  not GitHub CI — §7 only deploys SPA).
- **typesafe probe-before-allocate**: GET+parse `console.typesafe.ai/login`
  before tinyhost allocate (Clash preflight is skipped for `egress=clash`).
  Hub `./register.sh smoke typesafe` is probe-only; `TYPESAFE_SMOKE_LIVE=1`
  for n=1. Control-plane `StartRunRequest.product` accepts `typesafe`.

### Changed

- typesafe no longer appends full `api_key` to `accounts.jsonl`. Deliverable is
  this-run `0600` `typesafe-*.json` under a `0700` output dir. Session parse
  failures attach a short `$ACTION` HTML snippet.
- typesafe console probe now classifies 5xx as `network` / 4xx as `session`
  (same as login GET) and forwards the parsed `$ACTION` into `register_one`,
  so allocate is not followed by a second login GET unless magic-link POST
  needs a refresh. Mailbox timeout stays `mail_miss`. Verifier `live=True`
  is still shape-only (no spend). Magic-link extract parses query order.

### Fixed

- **grok CF 主动求解的 sitekey 抓取走错路径（2026-11-16 实测修正）**：
  真实 x.ai 注册页在浏览器 `innerHTML` 里的 RSC flight payload 是**转义**
  形态 `sitekey\":\"0x4…`（引号前带反斜杠），而旧主正则要求未转义引号，
  **从未命中**——实际是靠兜底的「整页任意 0x4 令牌」兜住的。页面上一旦出现
  第二个 0x4 形状 token（nonce 等），兜底就会抓错。主正则改为引号前允许
  一个可选反斜杠（`sitekey\\?["']\s*:\s*\\?["'](0x…)`），转义/未转义两种
  形态都命中。sitekey 模式提为 `_SITEKEY_RSC_PATTERN` / `_SITEKEY_SCRIPT`
  共享常量（JS 字面量与 Python `re` 同源，避免两边漂移），并新增 5 条离线
  回归测试（转义/未转义/decoy 优先/无 sitekey/脚本与常量一致）。
  验证方式：真 Chrome + 真实页面存档实测主正则命中（非兜底）。
- **one-mail admin 通道别名查询的 gmail 归一（2026-11-16）**：x.ai 把验证码
  投给聚合账号底址，one-mail D1 入库也是底址，注册侧只有自己造的
  `u+grk1@gmail.com`。查询候选现按 gmail 三件套归一（剥 `+tag`、剥 `.`、
  `googlemail.com`→`gmail.com`），别名查询空结果后退回规范底址再查。
  注：点号变体**绕不开** x.ai 的同地址 rate-limit（x.ai 限流前同样归一
  gmail 地址），本归一只解决查询侧对齐。
- **one-mail 收件人大小写契约（2026-11-16，客户端侧复核）**：one-mail 的
  `emails.to_addr` 读侧已改 `COLLATE NOCASE`（one-mail PR #180），写侧归一
  成小写；worker 的 `buildVerifAddrClause` 会把 `addr` 入参统一 `lower()`。
  **客户端因此不需要自己造小写候选**——多打一个候选只是白花一次 HTTP，
  且会掩盖「服务端没归一」的真实故障。`_subaddress_variants` 保持只做
  gmail 底址归一，`addr` 按原样发送；新增 2 条离线回归测试锁死该契约
  （不产生重复小写候选 / `addr` 原样发送）。
- typesafe batch (`./register.sh typesafe N` with N>1): continue on
  `mail_miss`/`network` (hard kinds still stop), in-process workers default 8
  capped at 32, unique `typesafe-*.json` names. Pipeline fan-out is only for
  typesafe/chatgpt — grok `--threads` still belongs to the child runner.
  pxed this-run 2026-09-22: `./register.sh typesafe 100` → `CONTRACT_EXIT:0`
  ok=97 fail=3 (`mail_miss`×3) `stopped_reason=""`; 97 new `0600`
  `typesafe-*.json`; no `accounts.jsonl` append; no CPA inject.
- typesafe mail poll with `sender_hint=typesafe` returns the Stytch URL
  before any OTP-shaped token in the same body. Mail-miss diagnostics come
  from this attempt's `MailMissError`, not the shared EmailSource slot
  (n=100 workers were overwriting each other's wait stats). Deployment drift
  is logged once per process.
- pxed this-run 2026-09-22 (after the mail-parse fix): `./register.sh typesafe
  100` → `CONTRACT_EXIT:0` ok=98 fail=2 (`session` / send_magic_link HTTP 500).
  Then `./register.sh typesafe 1000` (8 workers, `--no-fail-fast`,
  `rotate=off`) → `CONTRACT_EXIT:0` ok=976 fail=24
  (`mail_miss`×17, `session`×5, `oauth_callback`×2); 976 new `0600`
  `typesafe-*.json`; no `accounts.jsonl` append; no CPA inject.
- README acknowledges the [LINUX DO](https://linux.do/) community.

## [1.6.4] - 2026-07-16

### Added

- **Register ↔ nodes adaptation**: preflight probe gate before batch registration
  - `NodeManager.preflight` / `mark_result` / quarantine after `REGISTER_NODES_MAX_FAIL`
  - `preflight_nodes_for_register` seeds healthy-only `proxy_list` for list/auto
  - `report_attempt_proxy_result` feeds register outcomes back into catalog + live rotator
  - Pipeline runs preflight once, then marks/drops dead proxies per attempt
- Heuristic `is_proxy_network_failure` — never quarantines mail_miss / captcha / registration_disallowed
- Fail-fast when `egress=list` has zero healthy nodes after probe
- Tests: preflight, quarantine, pipeline skip-dead, pool drop

### Changed

- `enabled_nodes(healthy_only=True)` now means `last_ok is True` only (register path)
- Hard-failed nodes excluded from rotation until they pass probe again

## [1.6.3] - 2026-07-16

### Changed

- **Purity pass for egress UX**
  - `nodes import` **merges by URL** into `nodes.json` (add/update/keep); `--replace` for full overwrite
  - Clash Verge profile scan is **opt-in** (`--from-clash-verge`), not default
  - `nodes list` prints a **sample summary** by default (`--all` for full dump)
  - `nodes clear --yes` empties HTTP catalog without touching protocol runtime
  - `auto` egress only uses **known-healthy** (`last_ok=true`) catalog entries; unprobed bulk no longer blocks core
  - Docs/CLI primary backends: `list|core|direct` (auto/clash advanced)
  - Canonical script: `scripts/import_nodes.py`; `import_clash_to_nodes.py` deprecated alias

### Added

- Merge plan in import/dry-run JSON (`added`/`updated`/`kept`/`dropped`)
- Tests for merge/replace and auto healthy-list behavior

## [1.6.2] - 2026-07-16

### Added

- **Light node convert pipeline** (`register_core/nodes/convert/`): parse → validate → split → pack
- Inputs: Clash/mihomo YAML, V2Ray/Xray JSON outbounds, share URI lines (ss/vmess/vless/trojan/http/socks)
- CLI: `python -m register_core nodes import|validate` (dry-run, format force, no register-path weight)
- Legality report rejects missing type/server/port/uuid/…; dialable → `nodes.json`, protocol → `runtime.yaml`
- Compat: `scripts/import_clash_to_nodes.py` is a thin wrapper over the same pipeline
- Tests: `test_nodes_convert.py`

### Notes

- mihomo remains optional runtime for protocol nodes only; HTTP/SOCKS path needs no core
- Conversion is opt-in CLI — register pipeline only consumes artifacts

## [1.6.1] - 2026-07-16

### Added

- **Egress backend switch**: `core` | `clash` | `list` | `direct` | `auto`
- CLI: `python -m register_core nodes egress show|set|which`
- CLI/env: `--egress` / `REGISTER_EGRESS` / `CHATGPT_EGRESS`
- Persist preference: `.nodes/config/egress.mode`
- Tests cover clash vs core isolation and direct no-proxy

## [1.6.0] - 2026-07-16

### Added

- **Embedded mihomo mini-core** under `.nodes/bin/mihomo` + `.nodes/config/runtime.yaml`
- `register_core/nodes/core_runtime.py` + CLI: `nodes core start|stop|status|select|proxies|url`
- `scripts/import_clash_to_nodes.py` — copy Clash Verge YAMLs into project path and pack runtime
- `scripts/bootstrap_nodes_core.sh` — download official MetaCubeX mihomo for the host arch
- `util/proxy.py` can auto-start core and use `http://127.0.0.1:17897` when HTTP catalog empty

### Notes

- Protocol nodes (vless/ss/vmess/trojan/…) live in project runtime YAML; dialable HTTP/SOCKS still in `nodes.json`
- No dependency on Clash Verge UI / system TUN / `:7897` for ChatGPT path
- `.nodes/` is gitignored (binary + credentials)

## [1.5.0] - 2026-07-16

### Added

- **Project-owned node runtime** (`register_core/nodes/`): catalog (`nodes.json` / `nodes.txt`), health probe (`curl_cffi` → ipify), `NodeManager` rotation, CLI `python -m register_core nodes list|check|add|urls`
- `register_core/util/proxy.py` seeds list-mode pool from nodes catalog when `PROXY_LIST` empty
- Examples: `nodes.example.json`, `nodes.example.txt`; gitignore for credential catalogs
- Tests: `test_register_core_nodes.py` (catalog, manager, proxy util, pipeline wiring)

### Changed

- ChatGPT runner / adapter: **no default Clash `127.0.0.1:7897`** — egress from `nodes.json` / `PROXY_LIST` / explicit `CHATGPT_PROXY`
- Docs: ARCHITECTURE egress section; ChatGPT README + hub help describe nodes-first path

### Notes

- Operators supply HTTP/SOCKS proxy endpoints they control; Clash mode remains optional legacy for Grok browser only

## [1.4.1] - 2026-07-15

### Added

- **Self-controlled egress for layered providers**: `register_core/util/proxy.py` wires existing `proxy_rotate` **list** mode into `Pipeline` + `register_core` CLI + ChatGPT runner
- CLI: `--proxy`, `--proxy-list`, `--proxy-rotate {off,list,clash}`, `--proxy-rotate-every`, `--proxy-rotate-required`
- Env: `CHATGPT_PROXY_LIST` / `PROXY_LIST` auto-enables list mode (no Clash node selection)

### Notes

- Preferred path is **list** of explicit upstream URLs — register machine owns the node pool; Clash may only be a local forwarder, not a selector dependency
- Clash domain-group mode remains available for Grok browser paths; ChatGPT docs steer operators to list mode

## [1.4.0] - 2026-07-15

### Added

- **ChatGPT / OpenAI platform provider** (`providers/chatgpt/`): in-process protocol register via `curl_cffi` + Sentinel PoW + `EmailSource` (default `gmail_imap`); hub `./register.sh chatgpt`
- `register_core/providers/chatgpt_adapter.py` + `verify/chatgpt_token.py` (offline token shape gate)
- Unit tests: `test_chatgpt_provider.py` (attribution, redact, verifier, registry)
- Gmail OTP extractor: HTML strip + OpenAI “temporary verification code” patterns (was poisoned by CSS hex numbers)

### Notes

- ChatGPT path does **not** auto-inject tebi/CPA; secrets land in `providers/chatgpt/output/` (0600, gitignored)
- Live path verified through authorize → OTP validate; final `create_account` may still return `registration_disallowed` (IP/domain risk) — Manual-required

## [1.3.0] - 2026-07-15

### Added

- **Desktop GUI deep polish**: thread-safe `ui_queue` pump, form lock while running, validation focus/tab jump, log cap, copy log / open results, persist `register_count`
- **Desktop GUI redesign** (`grok_register_ttk.py`): Notebook tabs (基础 / 邮箱 / 进阶)、邮箱服务商字段动态显隐、右侧进度条与彩色日志
- **Multi-provider hub**: `./register.sh` (`grok` / `mimo` / `core` / `smoke`)
- **`register_core/`** layered framework: contracts, email sources, provider adapters, verify, sink, pipeline, CLI
- **`providers/mimo/`**: Xiaomi MiMo API Key registration (Node/Playwright `register-one.js`, Geetest slide, tinyhost OTP)
- MiMo → CPA OpenAI-compat helper (optional, local): `providers/mimo/inject_cpa_openai.py` (does **not** touch production by default)
- Unit tests: `test_register_core_layers.py`, `test_mimo_cpa_openai_inject.py`
- Grok Gmail IMAP: HTTP CONNECT via configured proxy (container/no-public egress)
- Positioning docs vs [ThinkerWen/ai-register](https://github.com/ThinkerWen/ai-register): higher production usability, richer pipeline, **TTK UI** (form input + live log/status)
- **Monorepo skeleton** (inspired by ai-register product dirs + LiteLLM-style ARCHITECTURE/Makefile):
  - `ARCHITECTURE.md`, `docs/LAYOUT.md`, `docs/ADDING_PROVIDER.md`
  - `providers/_template/`, `providers/grok/README.md`, `providers/README.md`
  - `apps/{cli,gui}/`, `examples/minimal_pipeline.py`, `tests/unit/`, `Makefile`

### Changed

- Project rename path: **`grok-register` → `register-machine` → `ai-register-machine`** (GitHub + package metadata)
- README highlights desktop UI (`grok_register_ttk.py`): count/threads/mail/proxy form, start/stop, status, scrolling logs, tutorial
- Fail-fast + this-run result attribution (no historical tail as success); public redact password/secret
- pytest `pythonpath` + `tests/` tree; gitignore all `providers/*/output|node_modules`

### Fixed

- Align `sk-` patterns across adapter / verifier / inject / redaction (hyphen & underscore vendor keys)
- Grok adapter no longer reports `ok=True` with empty SSO (`pending`); default Grok verifier requires SSO
- `inject_cpa_openai.py` no longer defaults to production CPA path; requires `--config`/`CPA_CONFIG` and `--i-understand-production` for prod
- `register.sh` / MiMo clash starter resolve monorepo root via `GROK_CODE_ROOT` and multi-name candidates (not hard-only `/personal/grok-register`)
- README no longer links a non-existent `v1.3.0` GitHub Release tag

### Security

- Jsonl sink `O_CREAT|0600`; black-box providers reject fake external `--email-source`
- Ignore MiMo runtime output / node_modules; never commit keys or mail ledgers
- Production CPA inject is opt-in with explicit ack (no silent default path)

## [1.2.3] - 2026-07-13

### Added

- `scripts/doctor_secrets.sh` — local secret hygiene (tracked paths, modes, cloud-sync path warn; never prints contents)
- CI: `bash -n` on setup/doctor scripts; py_compile quality scripts; run doctor_secrets in guard step
- README education banner + local secret hygiene; CONTRIBUTING ttk refactor caution + chat-gate rule

### Changed

- Package description emphasizes OIDC + chat gate (not Hotmail-only / not a quota farm)

## [1.2.2] - 2026-07-13

### Fixed

- Simple default email channel is **duckmail** (not hotmail four-segment) for lower onboarding friction
- `setup_simple.sh` doctor: Python/Chrome/proxy port, duckmail key / hotmail placeholder warnings; `uv sync` failure no longer aborts bootstrap
- README honest **最短路径** timing, 常见卡点 table, production inject key checklist
- Packaging tests: `bash -n`, setup smoke in temp dir (no overwrite)

## [1.2.1] - 2026-07-13

### Added

- Outsider-friendly packaging: `config.simple.example.json` + `scripts/setup_simple.sh`
- README quickstart (simple local path; tebi inject off by default)
- Product success table: chat probe required; `entitlement_denied` ≠ remint

## [1.2.0] - 2026-07-13

### Added

- One-click CPA chain: `cpa_auth_priority` end-to-end, multi remote auth-dirs (live + inventory)
- `cpa_remote_live_dir` / `cpa_remote_live_required` — **live inject is the product success gate**
- Free Build **chat entitlement gate**: default-on `/v1/responses` probe; 403 → `entitlement_denied`, skip live inject, remint ledger skip
- Transient chat probe retries + `chat_retryable` auth stamps; `entitlement_denied.jsonl`
- Auth proxy bridge for Chromium `user:pass` proxies; browser recycle modes; account slot retry
- `scripts/remint_expired_and_sync_authdir.py` for inventory remint without starting registration
- Offline tests for one-click dirs/priority, live-gate, and chat entitlement

### Fixed

- `account_slot_retry=0` no longer coerced to 3; no outer worker × slot multiplicative alias burn
- Mint browser no longer double-applies `--proxy-server`
- Proxy bridge failure hard-fails (no silent direct)
- Inventory-only remote inject no longer counts as one-click success when live was targeted
- Models-only 200 no longer counts as usable free Build when chat probe is on
- CI `py_compile` covers `proxy_bridge.py` and remint script

### Security

- Runtime mail/auth assets remain gitignored (`mail_credentials.txt`, `mail_assets/`)

## [1.1.3] - 2026-07-12

### Fixed

- Hard resource/config failures (Hotmail alias exhaustion, missing mail credentials, unconfigured providers) now **stop the whole batch immediately** instead of empty-loop retries
- `FatalRegisterError` + `_fatal_stop` event: workers exit without further account retries; process exit code `2`

### Added

- `classify_email_stage_failure` returns `fatal` for unrecoverable markers
- Offline tests for fatal classification and stop wiring

## [1.1.2] - 2026-07-12

### Added

- Startup cleanup of orphan Drission Chromes reparented to init/launchd (PPID=1)
- `tab_pool.cleanup_orphan_drission_chromes` / `TabPool.cleanup_orphans` with protect list
- Offline tests for Drission Chrome cmdline matching and dry-run cleanup

### Notes

- Success path still **reuses** the register browser (`clear_session`); cleanup only targets crashed leftovers, not the live worker Chrome.

## [1.1.1] - 2026-07-12

### Fixed

- SSO leading-dash normalize now applies at mint core, protocol extract/set, GUI ledger write, and shared `format_account_line`
- `existing_cpa_emails` / skip-existing match Hotmail plus-aliases against sanitized CPA filenames
- Official backfill script routes through `cpa_export` (remote inject + hooks); `--local-only` / `--no-remote` opt out
- Config booleans in `cpa_export` use `_config_bool` so string `"false"` is not truthy
- CI syntax-checks `grok_register_ttk.py`

### Added

- `format_account_line`, `email_match_keys`, `email_in_existing` helpers
- Offline tests for cookie extract normalize, plus-alias skip keys, `_config_bool`

## [1.1.0] - 2026-07-12

### Added

- Public project packaging for open-source release
- `LICENSE` (MIT), `DISCLAIMER.md`, `SECURITY.md`, `CONTRIBUTING.md`
- GitHub Actions CI (syntax + offline tests)
- Local account backup helpers (`account_backup.py`, `scripts/backup_registered_accounts.py`)
- Optional remote CPA auth inject after successful OIDC mint
- Hotmail/Outlook Office REST code fetch with IMAP fallback
- Protocol-first CPA OIDC mint (`cpa_xai/protocol_mint.py`) with browser fallback

### Changed

- README rebranded as **Grok 注册机** with full setup / ops docs
- Live Hotmail REST test gated behind `GROK_REGISTER_LIVE=1`
- Project package name aligned to `grok-register`

### Security

- Runtime secrets remain gitignored (`config.json`, accounts, CPA auths, backups, logs)

## [1.0.0] - 2026-07-11

### Added

- Chromium + DrissionPage registration core
- Hotmail four-field credential pool and plus-alias registration
- CPA export hook and `cpa_xai` OIDC tooling
- CLI (`register_cli.py`) and GUI (`grok_register_ttk.py`) entrypoints
