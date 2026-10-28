"""Read-only market discovery and low/high reversal screen for GLaw training.

This module never signs, swaps, transfers, or sends transactions. Every result is a
research/shadow classification; a "shadow entry" is not a live order instruction.
"""
import asyncio
import datetime as dt
import logging
import math
import os
import re
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Query

from .risk import summarize

router = APIRouter()
logger = logging.getLogger("glaw_market_screen")
EVM_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")
TOPIC_RUSH_URL = (
    "https://web3.binance.com/bapi/defi/v2/public/wallet-direct/"
    "buw/wallet/market/token/social-rush/rank/list/ai"
)
BINANCE_AUDIT_URL = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit"
TOKEN_SEARCH_URL = "https://web3.binance.com/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search/ai"
DYNAMIC_URL = (
    "https://web3.binance.com/bapi/defi/v4/public/wallet-direct/"
    "buw/wallet/market/token/dynamic/info/ai"
)
KLINE_URL = "https://dquery.sintral.io/u-kline/v1/k-line/candles"
ORACL3_BASE_URL = os.getenv("ORACL3_BASE_URL", "https://amx-oracl3-token-preflight.onrender.com").rstrip("/")
RISK_SEMAPHORE = asyncio.Semaphore(2)
HTTP_TIMEOUT_SECONDS = 8.0
DEFAULT_MIN_MARKET_CAP = 5_000.0
DEFAULT_MAX_MARKET_CAP = 250_000.0
DEFAULT_MIN_LIQUIDITY = 5_000.0


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _positive(value):
    result = _number(value)
    return result if result is not None and result > 0 else None


def _known_tax_fraction(value):
    """GoPlus tax values observed by this service are decimal fractions (0.03 = 3%)."""
    result = _number(value)
    if result is None or result < 0 or result > 1:
        return None
    return result


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def _get_json(client, url, params=None, headers=None, timeout=None):
    response = await client.get(url, params=params, headers=headers, timeout=timeout)
    if response.status_code >= 400:
        logger.warning("UPSTREAM_HTTP_ERROR host_path=%s status=%s", url.split("?", 1)[0], response.status_code)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("upstream returned a non-object JSON response")
    return payload


async def _dynamic(client, chain_id, address):
    try:
        payload = await _get_json(
            client, DYNAMIC_URL,
            params={"chainId": chain_id, "contractAddress": address},
        )
        if str(payload.get("code")) != "000000" or not isinstance(payload.get("data"), dict):
            return {"available": False, "error": "binance_market_data_unavailable"}
        return {"available": True, "data": payload["data"]}
    except (httpx.HTTPError, ValueError, TypeError):
        return {"available": False, "error": "binance_market_data_request_failed"}


async def _candles(client, address):
    try:
        payload = await _get_json(
            client, KLINE_URL,
            params={"platform": "bsc", "address": address, "interval": "1h", "limit": 8},
        )
        candles = payload.get("data")
        if not isinstance(candles, list):
            return {"available": False, "error": "candle_data_unavailable"}
        normalized = []
        for candle in candles:
            if not isinstance(candle, list) or len(candle) < 7:
                continue
            values = [_number(candle[i]) for i in range(6)]
            if any(v is None for v in values):
                continue
            normalized.append({
                "open": values[0], "high": values[1], "low": values[2],
                "close": values[3], "volume": values[4],
                "timestamp_ms": int(values[5]), "trades": int(candle[6]),
            })
        return {
            "available": True, "candles": normalized,
            "source_timestamp": (payload.get("status") or {}).get("timestamp"),
        }
    except (httpx.HTTPError, ValueError, TypeError):
        return {"available": False, "error": "candle_data_request_failed"}


async def _risk(client, chain_id, address):
    # Reuse the existing Oracle-owned normalized risk endpoint. Do not create a
    # second GoPlus credential path or silently downgrade missing data to safe.
    try:
        async with RISK_SEMAPHORE:
            payload = await _get_json(
                client,
                f"{ORACL3_BASE_URL}/v1/token-preflight/{chain_id}/{address}",
                timeout=httpx.Timeout(15.0, connect=5.0),
            )
        if not isinstance(payload, dict):
            raise ValueError("ORACL3 returned a non-object response")
        return payload
    except (httpx.HTTPError, ValueError, TypeError):
        return {
            "status": "insufficient_evidence", "risk": "unknown", "flags": [],
            "evidence_coverage": {"observed_risk_fields": [], "missing_risk_fields": []},
            "reason": "oracle_preflight_unavailable",
        }


async def _token_search_tax(client, chain_id, address):
    """Fetch Binance's market-search tax fields by exact contract address."""
    try:
        payload = await _get_json(
            client, TOKEN_SEARCH_URL,
            params={"keyword": address, "chainIds": chain_id},
            timeout=httpx.Timeout(10.0, connect=4.0),
            headers={"User-Agent": "binance-web3/2.0 (Skill)"},
        )
        if str(payload.get("code")) != "000000" or not isinstance(payload.get("data"), list):
            return {"available": False, "reason": "token_search_unavailable"}
        matches = [
            item for item in payload["data"]
            if isinstance(item, dict)
            and str(item.get("chainId")) == str(chain_id)
            and str(item.get("contractAddress") or "").lower() == address.lower()
        ]
        if len(matches) != 1:
            return {
                "available": False,
                "reason": "exact_contract_search_not_unique",
                "match_count": len(matches),
            }
        item = matches[0]
        buy_pct = _number(item.get("taxRateBuy"))
        sell_pct = _number(item.get("taxRateSell"))
        return {
            "available": True,
            "chain_id": str(chain_id),
            "contract_address": address,
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "buy_tax_pct": buy_pct,
            "sell_tax_pct": sell_pct,
            "risk_level_search": item.get("riskLevel"),
            "source": "Binance Web3 token search",
        }
    except (httpx.HTTPError, ValueError, TypeError):
        return {"available": False, "reason": "token_search_request_failed"}


async def _binance_audit(client, chain_id, address):
    """Independent read-only Binance token audit; never submits a wallet action."""
    try:
        response = await client.post(
            BINANCE_AUDIT_URL,
            json={
                "binanceChainId": chain_id,
                "contractAddress": address,
                "requestId": str(uuid.uuid4()),
            },
            headers={
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "binance-web3/1.4 (Skill)",
                "source": "agent",
            },
            timeout=httpx.Timeout(10.0, connect=4.0),
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or str(payload.get("code")) != "000000"
                or not isinstance(data, dict) or data.get("hasResult") is not True
                or data.get("isSupported") is not True):
            return {"available": False, "status": "unknown", "reason": "binance_audit_data_unavailable"}
        extra = data.get("extraInfo") if isinstance(data.get("extraInfo"), dict) else {}
        risk_items = data.get("riskItems") if isinstance(data.get("riskItems"), list) else []
        details = []
        for item in risk_items:
            if not isinstance(item, dict):
                continue
            for detail in item.get("details") or []:
                if isinstance(detail, dict):
                    details.append({
                        "category": item.get("id"),
                        "title": detail.get("title"),
                        "risk_type": detail.get("riskType"),
                        "is_hit": detail.get("isHit"),
                    })
        level = _number(data.get("riskLevel"))
        risk_enum = str(data.get("riskLevelEnum") or "").strip().upper() or None
        hard_hits = [
            item for item in details
            if item.get("is_hit") is True and str(item.get("risk_type") or "").upper() == "RISK"
        ]
        assessed = risk_enum in {"LOW", "MEDIUM", "HIGH"} and level is not None
        return {
            "available": True,
            "status": "ok" if assessed else "unassessed",
            "risk_assessment_confirmed": assessed,
            "risk_level": int(level) if level is not None else None,
            "risk_level_enum": risk_enum,
            # Preserve provider values exactly. Binance's skill docs call these
            # percentages, while live samples numerically match GoPlus fractions.
            # Cross-check raw values against ORACL3; do not silently scale them.
            "buy_tax_raw": _number(extra.get("buyTax")),
            "sell_tax_raw": _number(extra.get("sellTax")),
            "tax_unit_semantics_verified": False,
            "verified_contract": extra.get("isVerified"),
            "hard_risk_hits": hard_hits,
            "risk_items": details,
        }
    except (httpx.HTTPError, ValueError, TypeError):
        return {"available": False, "status": "unknown", "reason": "binance_audit_request_failed"}


def _extract_candidates(payload, min_cap, max_cap, min_liquidity):
    topics = payload.get("data")
    if not isinstance(topics, list):
        raise HTTPException(502, "Binance topic feed returned an invalid data shape")
    deduped = {}
    symbol_addresses = {}
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        tokens = topic.get("tokenList")
        if not isinstance(tokens, list):
            continue
        for token in tokens:
            if not isinstance(token, dict):
                continue
            address = token.get("contractAddress")
            symbol = str(token.get("symbol") or "").strip()
            if not isinstance(address, str) or not EVM_ADDRESS.fullmatch(address) or not symbol:
                continue
            key = address.lower()
            if key not in deduped:
                deduped[key] = {
                    "chain_id": "56",
                    "contract_address": address,
                    "symbol": symbol,
                    "topic": (topic.get("name") or {}).get("topicNameEn"),
                    "topic_link": topic.get("topicLink") or None,
                    "topic_tags": topic.get("topicTags") or [],
                    "protocol": token.get("protocol"),
                    "market_cap_feed": _number(token.get("marketCap")),
                    "liquidity_feed": _number(token.get("liquidity")),
                    "price_change_24h_feed": _number(token.get("priceChange24h")),
                    "net_inflow_feed": _number(token.get("netInflow")),
                    "net_inflow_1h_feed": _number(token.get("netInflow1h")),
                    "holders_feed": _number(token.get("holders")),
                    "dev_holding_pct_feed": _number(token.get("devHoldingPercent")),
                    "trades_5m_feed": _number(token.get("count5m")),
                    "unique_traders_5m_feed": _number(token.get("uniqueTrader5m")),
                    "volume_buy_feed": _number(token.get("volumeBuy")),
                    "volume_sell_feed": _number(token.get("volumeSell")),
                    "source_created_ms": _number(token.get("createTime")),
                    "x_links": ((token.get("previewLink") or {}).get("x") or []),
                    "duplicate_symbols_in_feed": 1,
                }
            else:
                deduped[key]["duplicate_symbols_in_feed"] += 0
            symbol_addresses.setdefault(symbol.casefold(), set()).add(key)

    candidates = []
    for token in deduped.values():
        token["symbol_contract_count"] = len(symbol_addresses.get(token["symbol"].casefold(), set()))
        cap = token["market_cap_feed"]
        liquidity = token["liquidity_feed"]
        change = token["price_change_24h_feed"]
        holders = token["holders_feed"]
        if cap is None or liquidity is None or change is None or holders is None:
            continue
        if not (min_cap <= cap <= max_cap and liquidity >= min_liquidity):
            continue
        if not (-85.0 <= change <= -5.0 and holders >= 25):
            continue
        candidates.append(token)

    # Prefer lower-cap, sufficiently liquid assets with recent trading and improving
    # one-hour inflow. This is discovery ordering, not an investment probability.
    candidates.sort(key=lambda t: (
        -(t["net_inflow_1h_feed"] if t["net_inflow_1h_feed"] is not None else -1e12),
        -(t["trades_5m_feed"] if t["trades_5m_feed"] is not None else 0),
        -(t["liquidity_feed"] or 0),
        t["market_cap_feed"] or float("inf"),
    ))
    return candidates, len(deduped)


def _evaluate(token, dynamic_result, risk, binance_audit, candle_result):
    market = dynamic_result.get("data") if dynamic_result.get("available") else {}
    market = market if isinstance(market, dict) else {}
    risk = risk if isinstance(risk, dict) else {"status": "insufficient_evidence", "risk": "unknown"}
    binance_audit = binance_audit if isinstance(binance_audit, dict) else {"available": False, "status": "unknown"}
    binance_token_info = binance_token_info if isinstance(binance_token_info, dict) else {"available": False}
    evidence = risk.get("evidence") or {}
    coverage = risk.get("evidence_coverage") or {}
    missing_risk = coverage.get("missing_risk_fields") or []

    market_cap = _positive(market.get("marketCap")) or token["market_cap_feed"]
    liquidity = _positive(market.get("liquidity")) or token["liquidity_feed"]
    change_5m = _number(market.get("percentChange5m"))
    change_1h = _number(market.get("percentChange1h"))
    change_24h = _number(market.get("percentChange24h"))
    if change_24h is None:
        change_24h = token["price_change_24h_feed"]

    buy_5m = _number(market.get("volume5mBuy"))
    sell_5m = _number(market.get("volume5mSell"))
    buy_1h = _number(market.get("volume1hBuy"))
    sell_1h = _number(market.get("volume1hSell"))
    trades_5m = _number(market.get("count5m"))
    if trades_5m is None:
        trades_5m = token["trades_5m_feed"]

    top10 = _number(market.get("top10HoldersPercentage"))
    dev_pct = _number(market.get("holdersDevPercent"))
    if dev_pct is None:
        dev_pct = token["dev_holding_pct_feed"]
    buy_tax = _known_tax_fraction(evidence.get("buy_tax"))
    sell_tax = _known_tax_fraction(evidence.get("sell_tax"))
    binance_buy_tax_raw = _number(binance_audit.get("buy_tax_raw"))
    binance_sell_tax_raw = _number(binance_audit.get("sell_tax_raw"))
    search_buy_tax_pct = _number(binance_token_info.get("buy_tax_pct"))
    search_sell_tax_pct = _number(binance_token_info.get("sell_tax_pct"))
    # Binance token-search taxRate fields use percentage points (e.g. 3 = 3%).
    # Live audit extraInfo fields numerically match GoPlus fractions (e.g. 0.03 = 3%).
    # Normalize market-search fields only, and cross-check all values for this exact contract.
    search_buy_tax_fraction = search_buy_tax_pct / 100.0 if search_buy_tax_pct is not None and 0 <= search_buy_tax_pct <= 100 else None
    search_sell_tax_fraction = search_sell_tax_pct / 100.0 if search_sell_tax_pct is not None and 0 <= search_sell_tax_pct <= 100 else None
    tax_values = [buy_tax, sell_tax, binance_buy_tax_raw, binance_sell_tax_raw, search_buy_tax_fraction, search_sell_tax_fraction]
    tax_source_values_available = all(v is not None and 0 <= v <= 1 for v in tax_values)
    tax_sources_disagree = False
    if tax_source_values_available:
        buy_group = [buy_tax, binance_buy_tax_raw, search_buy_tax_fraction]
        sell_group = [sell_tax, binance_sell_tax_raw, search_sell_tax_fraction]
        tax_sources_disagree = (
            max(buy_group) - min(buy_group) > 0.002
            or max(sell_group) - min(sell_group) > 0.002
        )
    tax_unit_semantics_verified = bool(
        tax_source_values_available
        and all(v <= 0.05 for v in tax_values)
        and not tax_sources_disagree
        and binance_token_info.get("available") is True
    )
    price = _positive(market.get("price"))
    high_24h = _positive(market.get("priceHigh24h"))
    low_24h = _positive(market.get("priceLow24h"))
    range_position = None
    if price is not None and high_24h is not None and low_24h is not None and high_24h > low_24h:
        range_position = max(0.0, min(1.0, (price - low_24h) / (high_24h - low_24h)))

    candles = candle_result.get("candles") if candle_result.get("available") else []
    prev = candles[-2] if len(candles) >= 2 else None
    last = candles[-1] if candles else None
    candle_reversal = bool(
        prev and last and last["close"] > prev["close"] and last["low"] >= prev["low"]
    )
    buy_pressure_5m = buy_5m is not None and sell_5m is not None and buy_5m > sell_5m
    buy_pressure_1h = buy_1h is not None and sell_1h is not None and buy_1h > sell_1h
    fresh_buying = bool(
        (trades_5m or 0) >= 3 and buy_pressure_5m and buy_pressure_1h
        and change_5m is not None and change_5m > 0
        and change_1h is not None and change_1h > 0
        and candle_reversal
    )

    score = 0
    reasons = []
    if market_cap is not None and 5_000 <= market_cap <= 250_000:
        score += 15
        reasons.append("market_cap_in_microcap_research_band")
    else:
        reasons.append("market_cap_outside_microcap_research_band")
    if liquidity is not None and liquidity >= 10_000:
        score += 15
        reasons.append("liquidity_at_least_10000_usd")
    elif liquidity is not None and liquidity >= DEFAULT_MIN_LIQUIDITY:
        score += 8
        reasons.append("liquidity_at_least_5000_usd")
    else:
        reasons.append("insufficient_or_unknown_liquidity")
    if change_24h is not None and -60 <= change_24h <= -5:
        score += 10
        reasons.append("negative_24h_retracement_not_extreme")
    elif change_24h is not None and -85 <= change_24h < -60:
        score += 3
        reasons.append("extreme_24h_drawdown")
    else:
        reasons.append("no_qualifying_negative_24h_setup")
    if top10 is not None and top10 <= 20:
        score += 15
        reasons.append("top10_holder_concentration_at_most_20pct")
    elif top10 is not None and top10 <= 35:
        score += 10
        reasons.append("top10_holder_concentration_at_most_35pct")
    elif top10 is not None:
        reasons.append("holder_concentration_high")
    else:
        reasons.append("holder_concentration_unknown")
    if dev_pct is not None and dev_pct <= 5:
        score += 10
        reasons.append("developer_holding_at_most_5pct")
    else:
        reasons.append("developer_holding_unknown_or_high")
    if buy_pressure_5m:
        score += 5
        reasons.append("5m_buy_volume_exceeds_sell_volume")
    else:
        reasons.append("no_5m_buy_pressure_confirmation")
    if buy_pressure_1h:
        score += 5
        reasons.append("1h_buy_volume_exceeds_sell_volume")
    else:
        reasons.append("no_1h_buy_pressure_confirmation")
    if candle_reversal and fresh_buying:
        score += 10
        reasons.append("low_high_reversal_confirmed_by_candles_and_flow")
    else:
        reasons.append("low_high_reversal_not_confirmed")
    if risk.get("status") == "ok" and risk.get("risk") == "no_flag_detected" and not missing_risk:
        score += 20
        reasons.append("goplus_no_flag_with_complete_risk_field_coverage")
    elif risk.get("risk") == "high":
        reasons.append("security_provider_reports_high_risk")
    elif missing_risk:
        reasons.append("security_risk_coverage_incomplete")
    else:
        reasons.append("security_evidence_unknown_or_caution")
    if tax_unit_semantics_verified:
        score += 5
        reasons.append("per_contract_tax_values_cross_check_between_oracl3_and_binance")
    else:
        reasons.append("tax_missing_high_unit_unverified_or_not_cross_validated")
    if (binance_audit.get("available") and binance_audit.get("risk_assessment_confirmed")
            and binance_audit.get("risk_level_enum") == "LOW"
            and not binance_audit.get("hard_risk_hits")):
        score += 10
        reasons.append("binance_audit_explicit_low_assessment_no_safety_guarantee")
    else:
        reasons.append("binance_audit_unassessed_or_medium_high_or_flagged")

    if token.get("symbol_contract_count", 1) > 1:
        score -= 20
        reasons.append("symbol_collision_multiple_contracts_in_feed")
    score = max(0, min(100, score))

    hard_blocks = []
    if risk.get("risk") == "high":
        hard_blocks.append("security_high_risk")
    elif risk.get("risk") != "no_flag_detected":
        hard_blocks.append("security_caution_or_unknown")
    if risk.get("status") != "ok" or missing_risk:
        hard_blocks.append("security_evidence_incomplete")
    if (not binance_audit.get("available") or not binance_audit.get("risk_assessment_confirmed")
            or binance_audit.get("risk_level_enum") != "LOW"
            or binance_audit.get("risk_level") is None or binance_audit.get("risk_level") > 1
            or binance_audit.get("hard_risk_hits")):
        hard_blocks.append("binance_audit_unassessed_medium_high_or_flagged")
    if tax_sources_disagree:
        hard_blocks.append("tax_sources_disagree")
    if token.get("symbol_contract_count", 1) > 1:
        hard_blocks.append("ambiguous_duplicate_symbol")
    if top10 is None or top10 > 35:
        hard_blocks.append("holder_concentration_unknown_or_over_35pct")
    if dev_pct is None or dev_pct > 5:
        hard_blocks.append("developer_concentration_unknown_or_over_5pct")
    if not tax_unit_semantics_verified:
        hard_blocks.append("tax_missing_high_unit_unverified_or_not_cross_verified_under_5pct")
    if market_cap is None or not (5_000 <= market_cap <= 250_000):
        hard_blocks.append("market_cap_outside_research_band")
    if liquidity is None or liquidity < DEFAULT_MIN_LIQUIDITY:
        hard_blocks.append("liquidity_below_minimum")
    if change_24h is None or not (-85 <= change_24h <= -5):
        hard_blocks.append("not_in_negative_24h_research_band")

    if hard_blocks:
        disposition = "REJECT_FROM_SHADOW_ENTRY"
    elif fresh_buying:
        disposition = "SHADOW_ENTRY_CONDITION_MET"
    else:
        disposition = "WATCH_FOR_REVERSAL"

    target_cap = market_cap * 1000 if market_cap is not None else None
    return {
        "chain_id": token["chain_id"],
        "contract_address": token["contract_address"],
        "symbol": token["symbol"],
        "topic": token["topic"],
        "topic_link": token["topic_link"],
        "x_links": token["x_links"],
        "protocol": token["protocol"],
        "disposition": disposition,
        "heuristic_score_0_to_100": score,
        "score_is_probability": False,
        "hard_blocks": hard_blocks,
        "reason_codes": reasons,
        "market": {
            "price_usd": price,
            "market_cap_usd": market_cap,
            "liquidity_usd": liquidity,
            "holders": _number(market.get("holders")) or token["holders_feed"],
            "top10_holders_pct": top10,
            "developer_holding_pct": dev_pct,
            "price_change_5m_pct": change_5m,
            "price_change_1h_pct": change_1h,
            "price_change_24h_pct": change_24h,
            "price_24h_low_usd": low_24h,
            "price_24h_high_usd": high_24h,
            "position_in_24h_range_0_to_1": range_position,
            "volume_5m_buy_usd": buy_5m,
            "volume_5m_sell_usd": sell_5m,
            "volume_1h_buy_usd": buy_1h,
            "volume_1h_sell_usd": sell_1h,
            "trades_5m": trades_5m,
            "symbol_contract_count_in_feed": token["symbol_contract_count"],
            "buy_tax_fraction_oracl3": buy_tax,
            "sell_tax_fraction_oracl3": sell_tax,
            "buy_tax_raw_binance": binance_buy_tax_raw,
            "sell_tax_raw_binance": binance_sell_tax_raw,
            "tax_cross_check": {
                "oracl3_buy_tax_fraction": buy_tax,
                "oracl3_sell_tax_fraction": sell_tax,
                "binance_audit_buy_tax_raw": binance_buy_tax_raw,
                "binance_audit_sell_tax_raw": binance_sell_tax_raw,
                "binance_market_search_buy_tax_pct": search_buy_tax_pct,
                "binance_market_search_sell_tax_pct": search_sell_tax_pct,
                "numerically_agree_within_0_002_after_endpoint_specific_normalization": not tax_sources_disagree if tax_source_values_available else False,
                "per_contract_low_tax_cross_check_pass": tax_unit_semantics_verified,
                "note": "Token search percentage points are divided by 100; audit raw values are retained and cross-checked against ORACL3 fractions, never silently rescaled."
            },
            "tax_sources_disagree": tax_sources_disagree,
            "target_market_cap_if_1000x_usd": target_cap,
        },
        "binance_audit": binance_audit,
        "binance_token_info": binance_token_info,
        "risk_evidence": {
            "status": risk.get("status"),
            "risk": risk.get("risk"),
            "flags": risk.get("flags", []),
            "observed_fields": coverage.get("observed_risk_fields", []),
            "missing_fields": missing_risk,
            "source": "GoPlus Token Security API",
        },
        "candles": {
            "source_timestamp": candle_result.get("source_timestamp"),
            "last_two": candles[-2:] if len(candles) >= 2 else candles,
            "low_high_reversal_confirmed": candle_reversal,
        },
        "data_freshness": {
            "retrieved_at_utc": _utc_now(),
            "candle_source_timestamp": candle_result.get("source_timestamp"),
            "market_snapshot_source_timestamp": "not provided by upstream; retrieve_at is not exchange event time",
        },
    }


@router.get("/v1/market-screen/{chain_id}")
async def market_screen(
    chain_id: str,
    start_amount_usd: float = Query(default=0.40, gt=0, le=1_000_000),
    target_multiple: int = Query(default=1000, ge=2, le=1_000_000),
    limit: int = Query(default=8, ge=1, le=10),
):
    """Return a read-only BSC meme screen and $-to-target arithmetic; never executes a trade."""
    if chain_id != "56":
        raise HTTPException(400, "this candidate endpoint currently supports BSC chain_id 56 only")
    retrieved_at = _utc_now()
    headers = {
        "accept": "application/json",
        "User-Agent": "AMX-GLaw-Market-Screen/0.1",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=4.0),
                                     headers=headers) as client:
            feed = await _get_json(
                client, TOPIC_RUSH_URL,
                params={
                    "chainId": chain_id, "rankType": 20, "sort": 20,
                    "netInflowMin": 100, "tokenSizeMin": 2,
                },
            )
            if str(feed.get("code")) != "000000":
                logger.warning("BINANCE_TOPIC_FEED_NON_SUCCESS code=%s message=%s", feed.get("code"), feed.get("message"))
                raise HTTPException(502, f"Binance topic feed provider code {feed.get('code')}")
            candidates, unique_contract_count = _extract_candidates(
                feed, DEFAULT_MIN_MARKET_CAP, DEFAULT_MAX_MARKET_CAP, DEFAULT_MIN_LIQUIDITY,
            )
            selected = candidates[:limit]

            async def inspect(candidate):
                address = candidate["contract_address"]
                dynamic, risk, binance_audit, binance_token_info, candles = await asyncio.gather(
                    _dynamic(client, chain_id, address),
                    _risk(client, chain_id, address),
                    _binance_audit(client, chain_id, address),
                    _token_search_tax(client, chain_id, address),
                    _candles(client, address),
                )
                try:
                    return _evaluate(candidate, dynamic, risk, binance_audit, binance_token_info, candles)
                except Exception:
                    logger.exception("CANDIDATE_EVALUATION_FAILED address=%s", address)
                    raise

            evaluated = await asyncio.gather(*(inspect(candidate) for candidate in selected))
    except HTTPException as exc:
        logger.warning("MARKET_SCREEN_HTTP_EXCEPTION status=%s detail=%s", exc.status_code, exc.detail)
        raise
    except (httpx.TimeoutException, asyncio.TimeoutError):
        logger.exception("MARKET_SCREEN_TIMEOUT")
        raise HTTPException(504, "market screen upstream timed out") from None
    except (httpx.HTTPError, ValueError, TypeError):
        logger.exception("MARKET_SCREEN_UPSTREAM_OR_PROCESSING_FAILURE")
        raise HTTPException(502, "market screen upstream request failed") from None

    dispositions = {}
    for candidate in evaluated:
        name = candidate["disposition"]
        dispositions[name] = dispositions.get(name, 0) + 1

    target_value = start_amount_usd * target_multiple
    return {
        "status": "read_only_screen_complete",
        "service": "ORACL3 / GLaw Market Training Candidate",
        "retrieved_at_utc": retrieved_at,
        "chain_id": chain_id,
        "source": "Binance Web3 rising-topic feed + market dynamic + candles + Binance token-audit; existing ORACL3 normalized risk preflight",
        "candidate_universe": {
            "topic_count": len(feed.get("data") or []),
            "unique_contracts_scanned": unique_contract_count,
            "candidates_matching_initial_liquidity_cap_drawdown_filter": len(candidates),
            "candidates_fully_inspected": len(evaluated),
            "dispositions": dispositions,
            "initial_filters": {
                "market_cap_usd_min": DEFAULT_MIN_MARKET_CAP,
                "market_cap_usd_max": DEFAULT_MAX_MARKET_CAP,
                "liquidity_usd_min": DEFAULT_MIN_LIQUIDITY,
                "price_change_24h_pct_min": -85,
                "price_change_24h_pct_max": -5,
                "holder_count_min": 25,
            },
        },
        "capital_math": {
            "starting_amount_usd": round(start_amount_usd, 8),
            "target_multiple": target_multiple,
            "target_gross_value_usd": round(target_value, 8),
            "gross_profit_before_costs_usd": round(target_value - start_amount_usd, 8),
            "required_return_pct": round((target_multiple - 1) * 100, 4),
            "interpretation": "Arithmetic only, not a forecast or probability. Excludes gas, swap fees, slippage, taxes, failed transactions, and exit liquidity.",
        },
        "candidates": evaluated,
        "decision_rule": {
            "low_high_logic": "A negative daily return is only a discovery condition. Shadow-entry status additionally requires complete security evidence, low holder concentration, known low taxes, no same-chain symbol collision, active buy pressure on 5m and 1h, positive short-term price change, and higher lows/higher closes in the last two hourly candles.",
            "trading_enabled": False,
            "live_order_submitted": False,
            "score_is_probability": False,
        },
        "disclaimer": "Read-only training evidence; no result is a live buy instruction or proof of safety.",
    }
