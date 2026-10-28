"""Read-only market discovery and low/high reversal screen for GLaw training.

This module never signs, swaps, transfers, or sends transactions. Every result is a
research/shadow classification; a "shadow entry" is not a live order instruction.
"""
import asyncio
import datetime as dt
import math
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Query

from .risk import summarize

router = APIRouter()
EVM_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")
TOPIC_RUSH_URL = (
    "https://web3.binance.com/bapi/defi/v2/public/wallet-direct/"
    "buw/wallet/market/token/social-rush/rank/list/ai"
)
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


def _evaluate(token, dynamic_result, risk, candle_result):
    market = dynamic_result.get("data") if dynamic_result.get("available") else {}
    market = market if isinstance(market, dict) else {}
    risk = risk if isinstance(risk, dict) else {"status": "insufficient_evidence", "risk": "unknown"}
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
    if buy_tax is not None and sell_tax is not None and max(buy_tax, sell_tax) <= 0.05:
        score += 5
        reasons.append("buy_and_sell_tax_known_and_at_most_5pct")
    else:
        reasons.append("buy_or_sell_tax_unknown_or_above_5pct")

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
    if token.get("symbol_contract_count", 1) > 1:
        hard_blocks.append("ambiguous_duplicate_symbol")
    if top10 is None or top10 > 35:
        hard_blocks.append("holder_concentration_unknown_or_over_35pct")
    if dev_pct is None or dev_pct > 5:
        hard_blocks.append("developer_concentration_unknown_or_over_5pct")
    if buy_tax is None or sell_tax is None or max(buy_tax, sell_tax) > 0.05:
        hard_blocks.append("tax_not_verified_under_5pct")
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
            "buy_tax_fraction": buy_tax,
            "sell_tax_fraction": sell_tax,
            "target_market_cap_if_1000x_usd": target_cap,
        },
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
                raise HTTPException(502, "Binance topic feed returned a provider error")
            candidates, unique_contract_count = _extract_candidates(
                feed, DEFAULT_MIN_MARKET_CAP, DEFAULT_MAX_MARKET_CAP, DEFAULT_MIN_LIQUIDITY,
            )
            selected = candidates[:limit]

            async def inspect(candidate):
                address = candidate["contract_address"]
                dynamic, risk, candles = await asyncio.gather(
                    _dynamic(client, chain_id, address),
                    _risk(client, chain_id, address),
                    _candles(client, address),
                )
                return _evaluate(candidate, dynamic, risk, candles)

            evaluated = await asyncio.gather(*(inspect(candidate) for candidate in selected))
    except HTTPException:
        raise
    except (httpx.TimeoutException, asyncio.TimeoutError):
        raise HTTPException(504, "market screen upstream timed out") from None
    except (httpx.HTTPError, ValueError, TypeError):
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
        "source": "Binance Web3 rising-topic feed + market dynamic + candles; existing ORACL3 normalized risk preflight",
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
