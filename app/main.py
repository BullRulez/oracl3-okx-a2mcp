import os
import re
import httpx
from fastapi import FastAPI, HTTPException

from .risk import summarize

app = FastAPI(title="AMilliMATRiX ORACL3 Token Preflight", version="0.1.0")

ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"

@app.get("/health")
async def health():
    return {"status": "ok", "service": "ORACL3 Token Preflight", "version": "0.1.0"}

@app.get("/v1/token-preflight/{chain_id}/{contract_address}")
async def token_preflight(chain_id: str, contract_address: str):
    if not chain_id.isdigit():
        raise HTTPException(400, "chain_id must be numeric")
    if not ADDRESS.match(contract_address):
        raise HTTPException(400, "contract_address must be a 20-byte EVM address")

    headers = {"accept": "application/json"}
    token = os.getenv("GOPLUS_BEARER_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=12.0) as client:
        r = await client.get(
            GOPLUS.format(chain_id=chain_id),
            params={"contract_addresses": contract_address},
            headers=headers,
        )
    if r.status_code != 200:
        raise HTTPException(502, f"security upstream returned HTTP {r.status_code}")
    data = r.json()
    out = summarize(data, contract_address)
    out.update({
        "chain_id": chain_id,
        "contract_address": contract_address,
        "source": "GoPlus Token Security API",
    })
    return out

def configure_x402(app):
    """Attach official OKX x402 middleware only when all required seller env vars exist."""
    required = ["PAY_TO_ADDRESS", "OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"]
    if not all(os.getenv(k) for k in required):
        return False
    from x402.http import OKXAuthConfig, OKXFacilitatorClient, OKXFacilitatorConfig, PaymentOption
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    from x402.http.types import RouteConfig
    from x402.mechanisms.evm.exact.server import ExactEvmScheme
    from x402.server import x402ResourceServer

    network = os.getenv("OKX_X402_NETWORK", "eip155:1952")  # testnet first
    facilitator = OKXFacilitatorClient(OKXFacilitatorConfig(
        auth=OKXAuthConfig(
            api_key=os.environ["OKX_API_KEY"],
            secret_key=os.environ["OKX_SECRET_KEY"],
            passphrase=os.environ["OKX_PASSPHRASE"],
        ),
        base_url=os.getenv("OKX_BASE_URL", "https://web3.okx.com"),
        sync_settle=True,
    ))
    server = x402ResourceServer(facilitator)
    server.register(network, ExactEvmScheme())
    routes = {
        "GET /v1/token-preflight/{chain_id}/{contract_address}": RouteConfig(
            accepts=[PaymentOption(
                scheme="exact",
                price=os.getenv("ORACL3_PRICE", "$0.01"),
                network=network,
                pay_to=os.environ["PAY_TO_ADDRESS"],
                max_timeout_seconds=300,
            )],
            description="ORACL3 token-security evidence preflight",
            mime_type="application/json",
        )
    }
    app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)
    return True

X402_ENABLED = configure_x402(app)
