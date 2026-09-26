# ORACL3 Token Preflight — deployment/ASP checklist

State boundary: this package is deployment-ready source, **not evidence of deployment, OKX registration, marketplace listing, or payment**.

## Public HTTPS deployment
1. Deploy this directory to a public HTTPS runtime. `api/index.py` + `vercel.json` provide a Vercel ASGI adapter; `Dockerfile` remains the portable container route.
2. Bind secrets only in provider environment variables: `PAY_TO_ADDRESS`, `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE`. Optional: `GOPLUS_BEARER_TOKEN`.
3. Keep `OKX_X402_NETWORK=eip155:1952` for testnet verification. Do not switch to `eip155:196` until paid replay is verified.
4. Run `scripts/selfcheck.sh https://<public-host>`.
5. Paid endpoint without a payment header must return HTTP 402 and a `PAYMENT-REQUIRED` challenge when x402 bindings are active.
6. Execute a real testnet paid replay and preserve the request/response evidence before mainnet.

## OKX.AI ASP registration payload
- Type: A2MCP
- Name: ORACL3 Token Preflight
- Description: Read-only EVM token-security evidence preflight for autonomous agents. Returns concise machine-readable risk flags without making trading recommendations.
- Price: $0.01/call
- Endpoint: `https://<public-host>/v1/token-preflight/{chain_id}/{contract_address}`

## External bindings still required
- Public HTTPS deployment target
- Receiving EVM address
- OKX Developer Portal seller credentials
- OKX Agent Identity / Agentic Wallet login for ASP registration/listing

These are provider/account bindings, not missing application logic.
