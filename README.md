# ORACL3 Token Preflight

A small FastAPI service that turns external token-security evidence into a consistent response for agent integrations. Built by Ulrich du Plessis / AMilliMATRiX.

**Inspect the implementation:** [live API docs](https://amx-oracl3-token-preflight.onrender.com/docs) · [request boundary](app/main.py) · [classification rules](app/risk.py) · [failure and evidence tests](tests)

## What the software does

`GET /v1/token-preflight/{chain_id}/{contract_address}` validates an EVM address, reads GoPlus Token Security, and returns structured flags, evidence coverage, and source attribution. It makes no transaction or trading recommendation.

The implementation separates three outcomes:

- Valid evidence produces a deterministic classification: `high`, `caution`, or `no_flag_detected`.
- Missing records, blank flags, or metadata without risk evidence produce `unknown`. A missing flag never becomes proof of safety.
- Provider errors, malformed responses, and network failures produce controlled HTTP 502/504 errors. External requests have a 15-second total budget; diagnostic messages and credentials are not reflected to callers.

## Run and verify

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

In another terminal:

```sh
sh scripts/selfcheck.sh http://127.0.0.1:8080
```

The tests exercise input validation, real HTTPX mock transports, upstream timeouts and cancellation, malformed provider payloads, case-insensitive addresses, and incomplete risk evidence. They do not establish external marketplace acceptance or payment.

`/` opens the existing API docs. `/health` reports the application version and whether the x402 middleware is enabled. The self-check verifies both health and the actual endpoint; a healthy process alone does not pass it.

## x402 integration boundary

The official OKX Python x402 middleware is enabled only when all seller bindings exist: `PAY_TO_ADDRESS`, `OKX_API_KEY`, `OKX_SECRET_KEY`, and `OKX_PASSPHRASE`. Secrets belong in the runtime environment. Optional GoPlus authentication uses `GOPLUS_BEARER_TOKEN`.

The configured default is X Layer testnet (`eip155:1952`) at `$0.01/call`. When enabled, an unpaid endpoint request must return HTTP 402 with a `PAYMENT-REQUIRED` challenge. Paid replay, settlement, OKX registration, and marketplace listing each require their own evidence. Middleware configuration alone proves none of those outcomes.

## Deployment limits

The existing public demo uses Render's free web service. After idle time it can require roughly a minute to start; it is not an always-on production service. For the fastest inspection, the public source and tests are available without waiting for the API. See [deployment checks](DEPLOYMENT_CHECKLIST.md) for the current route and remaining verification.
