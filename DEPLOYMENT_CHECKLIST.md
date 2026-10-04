# ORACL3 deployment and verification

Use the existing service at `https://amx-oracl3-token-preflight.onrender.com`. Its Render service is `srv-davt3mh42hec73e2usvg`, backed by this repository's `main` branch. Do not create a replacement service to repair an existing deployment.

## Release checks

1. Install `requirements-dev.txt` and run `python -m pytest -q`.
2. Refresh `MANIFEST.sha256.json` when tracked package files change.
3. Commit verified changes. Confirm the provider deployed that exact commit; a successful push alone is not a deployment receipt.
4. Run `sh scripts/selfcheck.sh https://amx-oracl3-token-preflight.onrender.com`. Health has a 70-second cold-start budget; the warmed functional request has a 25-second budget.
5. Inspect `/docs` and confirm invalid input returns HTTP 400. Confirm missing provider evidence returns `unknown`, and upstream failure returns a controlled 502/504 rather than a fabricated risk assessment.

`Dockerfile` remains a portable deployment option; `api/index.py` and `vercel.json` are retained adapters, not evidence of a second live deployment.

## Payment and registration checks

1. Inspect `/health`: `x402_enabled` reports whether all seller environment bindings enabled the middleware.
2. If enabled, an unpaid request must return HTTP 402 and `PAYMENT-REQUIRED`; the self-check validates this boundary.
3. Preserve a real testnet paid replay before claiming payment enforcement or settlement. Keep `OKX_X402_NETWORK=eip155:1952` until verified.
4. Record OKX Agent Identity / Agentic Wallet registration and listing evidence separately. Proposed name: `ORACL3 Token Preflight`; proposed price: `$0.01/call`; type: `A2MCP`.

Never publish seller secrets or substitute an enabled setting for transaction evidence. No acceptance, listing, invoice, or payment is implied by this checklist.

## Availability

Render free services can idle after 15 minutes and require roughly a minute to restart. Source and local tests remain inspectable during a cold start. Do not claim an always-on service or add an artificial keep-alive loop as an availability fix.
