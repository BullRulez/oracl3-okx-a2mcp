#!/usr/bin/env sh
set -eu
BASE="${1:?usage: selfcheck.sh https://your-public-endpoint}"
CHAIN_ID="${CHAIN_ID:-1}"
CONTRACT_ADDRESS="${CONTRACT_ADDRESS:-0x0000000000000000000000000000000000000000}"
printf '%s\n' '== health =='
curl -fsS -i "$BASE/health"
printf '\n%s\n' '== paid route, no payment header: expect HTTP 402 + PAYMENT-REQUIRED when seller bindings are enabled =='
curl -sS -i "$BASE/v1/token-preflight/$CHAIN_ID/$CONTRACT_ADDRESS" | sed -n '1,30p'
