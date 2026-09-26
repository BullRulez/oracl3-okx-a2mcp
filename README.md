# ORACL3 Token Preflight — OKX.AI A2MCP candidate

A read-only token-security preflight packaged for OKX.AI Agent Service Provider (A2MCP).

## Endpoint
`GET /v1/token-preflight/{chain_id}/{contract_address}`

The service queries GoPlus Token Security and returns a compact evidence classification.
It deliberately uses `no_flag_detected`, never `safe`, because missing flags are not proof of safety.

## x402
The service uses OKX's official Python x402 SDK boundary when seller credentials and
`PAY_TO_ADDRESS` are present. Default network is X Layer testnet (`eip155:1952`).
Switch to mainnet (`eip155:196`) only after testnet verification.

Default price: `$0.01` per successful paid call.

## Required seller bindings
- PAY_TO_ADDRESS
- OKX_API_KEY
- OKX_SECRET_KEY
- OKX_PASSPHRASE

Secrets are environment-only. Do not commit them.

## Run
`uvicorn app.main:app --host 0.0.0.0 --port 8080`

## Registration candidate
Name: `ORACL3 Token Preflight`
Description: `Read-only EVM token-security evidence preflight for autonomous agents. Returns concise machine-readable risk flags without making trading recommendations.`
Price: `$0.01/call`
