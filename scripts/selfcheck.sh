#!/usr/bin/env sh
set -eu
BASE="${1:?usage: selfcheck.sh https://your-public-endpoint}"
BASE="${BASE%/}"
PYTHON="${PYTHON:-python3}"
CHAIN_ID="${CHAIN_ID:-1}"
CONTRACT_ADDRESS="${CONTRACT_ADDRESS:-0x0000000000000000000000000000000000000000}"
CHECK_DIR=$(mktemp -d)
trap 'rm -rf "$CHECK_DIR"' EXIT HUP INT TERM

curl -fsS --connect-timeout 10 --max-time 70 "$BASE/health" -o "$CHECK_DIR/health.json"
ENABLED=$("$PYTHON" -c 'import json,sys; h=json.load(open(sys.argv[1])); assert h.get("status")=="ok" and isinstance(h.get("x402_enabled"),bool); print(str(h["x402_enabled"]).lower())' "$CHECK_DIR/health.json")
STATUS=$(curl -sS --connect-timeout 5 --max-time 25 -D "$CHECK_DIR/headers" -o "$CHECK_DIR/body" -w '%{http_code}' "$BASE/v1/token-preflight/$CHAIN_ID/$CONTRACT_ADDRESS")

"$PYTHON" - "$CHECK_DIR" "$STATUS" "$ENABLED" <<'PY'
import json
import pathlib
import sys

root, status, enabled = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "true"
health = json.loads((root / "health.json").read_text())
if enabled:
    headers = (root / "headers").read_text().lower()
    assert status == "402" and "\npayment-required:" in headers, "x402 unpaid boundary failed"
    result = "HTTP 402 challenge verified; paid replay and settlement remain separate checks"
else:
    assert status == "200", f"functional request failed: HTTP {status}"
    body = json.loads((root / "body").read_text())
    assert body.get("risk") in {"high", "caution", "no_flag_detected", "unknown"}, "invalid functional response"
    assert body.get("source") == "GoPlus Token Security API", "missing source attribution"
    result = f"HTTP 200 evidence response verified ({body['risk']}); payment middleware disabled"
print(f"PASS version={health.get('version')} — {result}")
PY
