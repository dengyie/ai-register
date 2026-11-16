#!/usr/bin/env bash
# z.ai (ZCode Coding Plan) register runner (in-process via register_core).
# Usage:
#   ./providers/zai/run-register.sh [count]
# Env:
#   ZAI_PYTHON        preferred interpreter (must have slidex w/ aliyun_captcha_js
#                     + playwright headed-capable); auto-detected otherwise
#   ZAI_DOMAIN        mailbox domain (default dengyie.tech)
#   ZAI_HANDLE        fixed handle (default: auto-generate unique)
#   ZAI_ATTEMPTS      fresh-session retries (default 3)
#   ONEMAIL_ADMIN_KEY / ZAI_ADMIN_KEY   one-mail unified admin API key (required)
#   ZAI_MAIL_API      one-mail origin (default https://mail-api.mangoqwq.cc.cd)
#   ZAI_HUB_URL/ZAI_HUB_KEY               hub (for uniqueness preflight + verify)
#   ZAI_SINK          pipeline jsonl path (default providers/zai/output/pipeline.jsonl)
set -euo pipefail

COUNT="${COUNT:-${1:-1}}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

# --- interpreter resolution: slidex must carry the aliyun_captcha_js provider ---
pick_py() {
  if [[ -n "${ZAI_PYTHON:-}" && -x "${ZAI_PYTHON}" ]]; then
    echo "${ZAI_PYTHON}"; return
  fi
  if [[ -x "$ROOT/.venv/bin/python" ]] \
     && "$ROOT/.venv/bin/python" -c "import slidex.providers as p, importlib; importlib.import_module('slidex.providers.aliyun_captcha_js')" >/dev/null 2>&1; then
    echo "$ROOT/.venv/bin/python"; return
  fi
  for cand in "$HOME/project/slidex/.venv/bin/python" "$HOME/slidex/.venv/bin/python"; do
    if [[ -x "$cand" ]] && "$cand" -c "import importlib; importlib.import_module('slidex.providers.aliyun_captcha_js')" >/dev/null 2>&1; then
      echo "$cand"; return
    fi
  done
  echo ""
}
PY="$(pick_py)"
if [[ -z "$PY" ]]; then
  echo "[zai] FATAL: no interpreter with slidex aliyun_captcha_js provider." >&2
  echo "       set ZAI_PYTHON=... (slidex>=0.11 w/ aliyun_captcha_js + playwright, headed-capable)" >&2
  exit 2
fi
echo "[zai] interpreter: $PY" >&2

export ZAI_DOMAIN="${ZAI_DOMAIN:-${ZAI_EMAIL_DOMAIN:-dengyie.tech}}"
export ZAI_ATTEMPTS="${ZAI_ATTEMPTS:-3}"
: "${ONEMAIL_ADMIN_KEY:?set ONEMAIL_ADMIN_KEY (one-mail x-admin-auth)}"
: "${ZAI_HUB_KEY:?set ZAI_HUB_KEY (hub admin key: naming preflight + pool verify)}"
export ZAI_MAIL_API="${ZAI_MAIL_API:-${ONEMAIL_API_BASE:-https://mail-api.mangoqwq.cc.cd}}"
export ZAI_HUB_URL="${ZAI_HUB_URL:-https://zcode.mangoqwq.com}"

SINK="${ZAI_SINK:-$ROOT/providers/zai/output/pipeline.jsonl}"
mkdir -p "$(dirname "$SINK")"

echo "[zai] COUNT=$COUNT domain=$ZAI_DOMAIN attempts=$ZAI_ATTEMPTS mail=$ZAI_MAIL_API" >&2

# pipeline 按 provider 名自动解析 verifier（zai → zai_hub 池核验门禁）
ARGS=(
  -m register_core run
  -p zai
  -n "$COUNT"
  --sink "$SINK"
  --timeout "$(( ZAI_ATTEMPTS * 600 + 300 ))"
)

exec "$PY" "${ARGS[@]}"
