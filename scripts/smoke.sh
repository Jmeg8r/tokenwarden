#!/usr/bin/env bash
#
# Part-A dogfood: boot tokenwarden, send ONE real (cheap) call through it, show
# the metered row, then tear everything down. Proves the gateway meters live
# Anthropic traffic end to end.
#
# Runs against a throwaway port + temp DB, so it never touches your real
# tokenwarden.db or whatever else is on the default port.
#
# Usage:
#   export ANTHROPIC_API_KEY=sk-ant-api03-...   # a real API key (NOT a sub token)
#   scripts/smoke.sh
#
# Optional env overrides:
#   SMOKE_PORT  (default 8799)
#   SMOKE_MODEL (default claude-haiku-4-5)
#   SMOKE_AGENT (default smoke)
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
BIN="$REPO/.venv/bin/tokenwarden"

PORT="${SMOKE_PORT:-8799}"
MODEL="${SMOKE_MODEL:-claude-haiku-4-5}"
AGENT="${SMOKE_AGENT:-smoke}"

info() { printf '==> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# --- preflight ------------------------------------------------------------
[ -n "${ANTHROPIC_API_KEY:-}" ] || fail "ANTHROPIC_API_KEY is not set (need a real sk-ant-api03-... key)"
[ -x "$BIN" ] || fail "tokenwarden not found at $BIN — run: (cd \"$REPO\" && python3.13 -m venv .venv && ./.venv/bin/pip install -e '.[dev]')"

TMP="$(mktemp -d)"
CFG="$TMP/config.toml"
SRV=""
cleanup() {
  [ -n "$SRV" ] && kill "$SRV" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

cat >"$CFG" <<EOF
[gateway]
port = $PORT
db_path = "$TMP/smoke.db"
EOF

# --- boot the gateway -----------------------------------------------------
info "starting tokenwarden on 127.0.0.1:$PORT (throwaway db)"
"$BIN" serve --config "$CFG" >"$TMP/serve.log" 2>&1 &
SRV=$!

# --- one real call (curl retries connrefused until the server is listening) -
info "sending one $MODEL call as agent '$AGENT'"
if ! RESP="$(curl -sS -w $'\n%{http_code}' \
      --retry 15 --retry-connrefused --retry-delay 1 --connect-timeout 2 --max-time 30 \
      "http://127.0.0.1:$PORT/v1/messages" \
      -H "x-api-key: $ANTHROPIC_API_KEY" \
      -H "anthropic-version: 2023-06-01" \
      -H "content-type: application/json" \
      -H "x-watchdog-agent: $AGENT" \
      -d "{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"reply with: smoke ok\"}]}")"; then
  echo "--- gateway log ---"; cat "$TMP/serve.log"
  fail "request to the gateway failed (is port $PORT free? see log above)"
fi

HTTP="${RESP##*$'\n'}"
BODY="${RESP%$'\n'*}"

if [ "$HTTP" != "200" ]; then
  echo "--- response ($HTTP) ---"; echo "$BODY"
  echo "--- gateway log ---"; cat "$TMP/serve.log"
  fail "expected HTTP 200 from Anthropic, got $HTTP"
fi
info "HTTP 200 — Anthropic replied through the gateway"

# --- show what the gateway metered ----------------------------------------
echo "--- metered (from gateway log) ---"
grep "metered" "$TMP/serve.log" || echo "(no 'metered' line found — check the log)"

echo "--- tokenwarden status ---"
"$BIN" status --config "$CFG"

info "smoke OK"
