HIGH_RISK_TRUE = {
    "is_honeypot": "honeypot",
    "is_blacklisted": "blacklist",
    "hidden_owner": "hidden_owner",
    "selfdestruct": "selfdestruct",
    "is_proxy": "proxy",
    "cannot_sell_all": "cannot_sell_all",
    "transfer_pausable": "transfer_pausable",
}
CAUTION_TRUE = {
    "is_mintable": "mintable",
    "owner_change_balance": "owner_change_balance",
    "can_take_back_ownership": "ownership_reclaim",
    "slippage_modifiable": "slippage_modifiable",
    "personal_slippage_modifiable": "personal_slippage_modifiable",
    "trading_cooldown": "trading_cooldown",
}

def _truthy(v):
    return str(v).strip().lower() in {"1","true","yes"}

def summarize(raw: dict, address: str) -> dict:
    result = raw.get("result") if isinstance(raw, dict) else None
    result = result if isinstance(result, dict) else {}
    item = result.get(address.lower())
    if item is None:
        # Some upstream responses preserve checksum case.
        item = next((v for k,v in result.items()
                     if isinstance(k, str) and k.lower() == address.lower()), None)
    if not isinstance(item, dict) or not item:
        return {
            "status": "insufficient_evidence",
            "risk": "unknown",
            "flags": [],
            "reason": "No token-security record returned for the requested contract.",
        }

    risk_keys = list(HIGH_RISK_TRUE) + list(CAUTION_TRUE)
    observed = [key for key in risk_keys
                if str(item.get(key)).strip().lower() in {"0", "1", "true", "false", "yes", "no"}]
    if not observed:
        return {
            "status": "insufficient_evidence", "risk": "unknown", "flags": [],
            "reason": "Token metadata was returned without usable risk flags.",
        }
    high = [label for key,label in HIGH_RISK_TRUE.items() if _truthy(item.get(key))]
    caution = [label for key,label in CAUTION_TRUE.items() if _truthy(item.get(key))]
    if high:
        risk = "high"
    elif caution:
        risk = "caution"
    else:
        risk = "no_flag_detected"

    return {
        "status": "ok",
        "risk": risk,
        "flags": high + caution,
        "evidence_coverage": {
            "observed_risk_fields": observed,
            "missing_risk_fields": [key for key in risk_keys if key not in observed],
        },
        "evidence": {
            "token_name": item.get("token_name"),
            "token_symbol": item.get("token_symbol"),
            "is_open_source": item.get("is_open_source"),
            "buy_tax": item.get("buy_tax"),
            "sell_tax": item.get("sell_tax"),
            "holder_count": item.get("holder_count"),
        },
        "disclaimer": "Evidence preflight only; absence of a flag is not proof of safety or financial advice.",
    }
