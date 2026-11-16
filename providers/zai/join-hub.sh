#!/usr/bin/env bash
# z.ai → zcode-hub OAuth 四步直投（操作员显式命令——绝不自动写生产池）。
# Usage:
#   ./providers/zai/join-hub.sh <token-file> [label] [--verify-only <label>]
# Env:
#   ZAI_HUB_URL (default https://zcode.mangoqwq.com)
#   ZAI_HUB_KEY (required: hub admin key)
#   ZAI_DOMAIN  (default dengyie.tech, label 自动推导用)
#   ZAI_PYTHON  interpreter override (default repo .venv)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

TOKEN_FILE="${1:-}"
LABEL="${2:-}"
MODE="join"
if [[ "${1:-}" == "--verify-only" ]]; then
  MODE="verify"; LABEL="${2:-}"; TOKEN_FILE=""
fi
if [[ "$MODE" == "join" && ( -z "$TOKEN_FILE" || ! -f "$TOKEN_FILE" ) ]]; then
  echo "usage: $0 <token-file> [label]   |   $0 --verify-only <label>" >&2
  exit 2
fi
if [[ "$MODE" == "verify" && -z "$LABEL" ]]; then
  echo "usage: $0 --verify-only <label>" >&2
  exit 2
fi
if [[ "$MODE" == "join" && -z "$LABEL" ]]; then
  LABEL="$("$ROOT/.venv/bin/python" - "$TOKEN_FILE" <<'PY'
import json, sys, base64
tok = open(sys.argv[1]).read().strip()
payload = tok.split(".")[1]
data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
email = str(data.get("email") or "")
local = email.split("@")[0]
domain = email.split("@")[1] if "@" in email else "dengyie.tech"
print(f"{local}-{domain.replace('.', '')}")
PY
)"
  echo "[zai] label 自动推导: $LABEL" >&2
fi

: "${ZAI_HUB_KEY:?set ZAI_HUB_KEY (hub admin key)}"
export ZAI_HUB_URL="${ZAI_HUB_URL:-https://zcode.mangoqwq.com}"

exec "$ROOT/.venv/bin/python" - "$MODE" "$TOKEN_FILE" "$LABEL" <<'PY'
import json, os, sys
from providers.zai.protocol.hub import hub_join
from register_core.verify.zai_hub import pool_verify_account

mode, token_file, label = sys.argv[1], sys.argv[2], sys.argv[3]
hub_url = os.environ["ZAI_HUB_URL"]
hub_key = os.environ["ZAI_HUB_KEY"]

def log(msg):
    print(f"[zai-hub] {msg}", flush=True)

if mode == "verify":
    ok, detail = pool_verify_account(label, hub_url=hub_url, hub_key=hub_key, log=log)
else:
    token = open(os.path.expanduser(token_file)).read().strip()
    ok, detail = hub_join(token, label, hub_url=hub_url, hub_key=hub_key, log=log)
    if ok:
        ok, detail = pool_verify_account(label, hub_url=hub_url, hub_key=hub_key, log=log)

print(f"RESULT: {ok} ({detail})")
sys.exit(0 if ok else 1)
PY
