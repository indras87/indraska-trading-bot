#!/usr/bin/env bash
# generate_signal.sh — research/signal side. NEVER sends orders.
# Writes signals/latest_signal.json.
#
# Provider auto-detected:
#   - ZAI_API_KEY set -> Z.ai GLM (OpenAI-compatible)
#   - else            -> MOCK signal (testnet-safe)
#
# Flags: --mock, --provider zai|vibe|mock, --symbol SYMBOL, --action BUY|SELL|HOLD

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load vibe-trading/.env if present (ZAI keys). Safe no-op otherwise.
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
fi

# Pick python: project venv if available, else system python3.
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="python3"
fi

cd "$REPO_ROOT"
exec "$PY" "$SCRIPT_DIR/generate_signal.py" "$@"
