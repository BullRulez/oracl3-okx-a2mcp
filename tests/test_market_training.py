from fastapi.testclient import TestClient

from app.main import app
from app.market_training import _evaluate, _extract_candidates, _technical_frame, _progression_ladder, _ema_series, _normalize_candle_rows, _items_from_payload, _skill_item_summary

client = TestClient(app)

RISK_OK = {
    "status": "ok",
    "risk": "no_flag_detected",
    "flags": [],
    "evidence_coverage": {
        "observed_risk_fields": [
            "is_honeypot", "is_blacklisted", "hidden_owner", "selfdestruct",
            "is_proxy", "cannot_sell_all", "transfer_pausable", "is_mintable",
            "owner_change_balance", "can_take_back_ownership",
            "slippage_modifiable", "personal_slippage_modifiable", "trading_cooldown",
        ],
        "missing_risk_fields": [],
    },
    "evidence": {"buy_tax": "0.01", "sell_tax": "0.01"},
}


def _token(address, symbol="TEST"):
    return {
        "contractAddress": address,
        "symbol": symbol,
        "marketCap": "20000",
        "liquidity": "12000",
        "priceChange24h": "-20",
        "holders": 200,
        "netInflow": "100",
        "netInflow1h": "10",
        "count5m": 5,
        "uniqueTrader5m": 4,
        "volumeBuy": "500",
        "volumeSell": "300",
        "devHoldingPercent": "1",
        "createTime": 1793140000000,
        "previewLink": {"x": ["https://x.com/example"]},
        "protocol": 2002,
    }


def _candidate(address="0x1111111111111111111111111111111111111111"):
    return {
        "chain_id": "56",
        "contract_address": address,
        "symbol": "TEST",
        "topic": "Test topic",
        "topic_link": None,
        "topic_tags": [],
        "protocol": 2002,
        "market_cap_feed": 20000,
        "liquidity_feed": 12000,
        "price_change_24h_feed": -20,
        "net_inflow_feed": 100,
        "net_inflow_1h_feed": 10,
        "holders_feed": 200,
        "dev_holding_pct_feed": 1,
        "trades_5m_feed": 5,
        "unique_traders_5m_feed": 4,
        "volume_buy_feed": 500,
        "volume_sell_feed": 300,
        "source_created_ms": 1793140000000,
        "x_links": ["https://x.com/example"],
        "duplicate_symbols_in_feed": 1,
        "symbol_contract_count": 1,
    }


def _market():
    return {
        "available": True,
        "data": {
            "price": "0.0001",
            "marketCap": "20000",
            "liquidity": "12000",
            "holders": "200",
            "top10HoldersPercentage": "20",
            "holdersDevPercent": "1",
            "percentChange5m": "2",
            "percentChange1h": "4",
            "percentChange24h": "-20",
            "volume5mBuy": "100",
            "volume5mSell": "20",
            "volume1hBuy": "500",
            "volume1hSell": "200",
            "count5m": "5",
            "priceHigh24h": "0.00015",
            "priceLow24h": "0.00005",
        },
    }


def _binance_audit():
    return {
        "available": True, "status": "ok", "risk_assessment_confirmed": True,
        "risk_level": 1, "risk_level_enum": "LOW",
        "buy_tax_raw": 0.01, "sell_tax_raw": 0.01,
        "verified_contract": True, "hard_risk_hits": [], "risk_items": [],
    }


def _binance_token_info():
    return {"available": True, "buy_tax_pct": 1, "sell_tax_pct": 1, "symbol": "TEST"}


def _candles():
    return {
        "available": True,
        "source_timestamp": "2026-10-28T01:00:00Z",
        "candles": [
            {"open": 0.00007, "high": 0.00008, "low": 0.00006,
             "close": 0.00007, "volume": 300, "timestamp_ms": 1793145600000, "trades": 12},
            {"open": 0.00007, "high": 0.00009, "low": 0.000065,
             "close": 0.000085, "volume": 400, "timestamp_ms": 1793149200000, "trades": 15},
        ],
    }


def test_rejects_non_bsc_chain_without_external_call():
    response = client.get("/v1/market-screen/1")
    assert response.status_code == 400
    assert "supports BSC" in response.json()["detail"]


def test_discovery_deduplicates_contracts_but_tracks_symbol_collisions():
    payload = {
        "data": [
            {"name": {"topicNameEn": "One"}, "tokenList": [
                _token("0x1111111111111111111111111111111111111111"),
                _token("0x2222222222222222222222222222222222222222"),
                _token("0x1111111111111111111111111111111111111111"),
            ]},
        ],
    }
    candidates, unique_count = _extract_candidates(payload, 5000, 250000, 5000)
    assert unique_count == 2
    assert len(candidates) == 2
    assert all(item["symbol_contract_count"] == 2 for item in candidates)


def test_complete_evidence_and_reversal_yields_shadow_not_live_trade():
    result = _evaluate(_candidate(), _market(), RISK_OK, _binance_audit(), _binance_token_info(), _candles())
    assert result["disposition"] == "SHADOW_ENTRY_CONDITION_MET"
    assert result["score_is_probability"] is False
    assert result["risk_evidence"]["missing_fields"] == []


def test_symbol_collision_blocks_shadow_entry():
    token = _candidate()
    token["symbol_contract_count"] = 3
    result = _evaluate(token, _market(), RISK_OK, _binance_audit(), _binance_token_info(), _candles())
    assert result["disposition"] == "REJECT_FROM_SHADOW_ENTRY"
    assert "ambiguous_duplicate_symbol" in result["hard_blocks"]


def test_incomplete_security_evidence_blocks_shadow_entry():
    risk = {
        **RISK_OK,
        "evidence_coverage": {
            "observed_risk_fields": ["is_honeypot"],
            "missing_risk_fields": ["cannot_sell_all"],
        },
    }
    result = _evaluate(_candidate(), _market(), risk, _binance_audit(), _binance_token_info(), _candles())
    assert result["disposition"] == "REJECT_FROM_SHADOW_ENTRY"
    assert "security_evidence_incomplete" in result["hard_blocks"]


def test_unknown_binance_audit_blocks_shadow_entry():
    result = _evaluate(
        _candidate(), _market(), RISK_OK,
        {"available": False, "status": "unknown"},
        _binance_token_info(),
        _candles(),
    )
    assert result["disposition"] == "REJECT_FROM_SHADOW_ENTRY"
    assert "binance_audit_unassessed_medium_high_or_flagged" in result["hard_blocks"]


def test_disagreeing_tax_sources_block_shadow_entry():
    audit = {**_binance_audit(), "buy_tax_raw": 0.09}
    result = _evaluate(_candidate(), _market(), RISK_OK, audit, _binance_token_info(), _candles())
    assert result["disposition"] == "REJECT_FROM_SHADOW_ENTRY"
    assert "tax_sources_disagree" in result["hard_blocks"]


def test_zero_risk_level_without_enum_is_unassessed_and_blocked():
    audit = {
        "available": True, "status": "unassessed", "risk_assessment_confirmed": False,
        "risk_level": 0, "risk_level_enum": None,
        "buy_tax_raw": 0.01, "sell_tax_raw": 0.01, "hard_risk_hits": [],
    }
    result = _evaluate(_candidate(), _market(), RISK_OK, audit, _binance_token_info(), _candles())
    assert result["disposition"] == "REJECT_FROM_SHADOW_ENTRY"
    assert "binance_audit_unassessed_medium_high_or_flagged" in result["hard_blocks"]


def test_tax_units_are_only_cross_checked_per_contract():
    audit = _binance_audit()
    result = _evaluate(_candidate(), _market(), RISK_OK, audit, _binance_token_info(), _candles())
    assert result["market"]["tax_cross_check"]["per_contract_low_tax_cross_check_pass"] is True
    assert result["market"]["tax_cross_check"]["numerically_agree_within_0_002_after_endpoint_specific_normalization"] is True


def _synthetic_candles(count=70, interval_ms=300000, now_ms=1_000_000_000_000):
    start = now_ms - count * interval_ms - 1000
    rows = []
    for i in range(count):
        close = 1.0 + i * 0.01
        rows.append({
            "open": close - 0.004,
            "high": close + 0.01,
            "low": close - 0.01,
            "close": close,
            "volume": 100.0 + i,
            "timestamp_ms": start + i * interval_ms,
            "trades": 10 + i,
        })
    return rows


def test_ema_uses_full_sma_seed_window():
    assert _ema_series([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_technical_frame_uses_closed_candles_and_marks_bullish_alignment():
    now_ms = 1_000_000_000_000
    interval_ms = 5 * 60 * 1000
    rows = _synthetic_candles(70, interval_ms, now_ms)
    # Add a still-open candle; it must not be included in the indicator calculations.
    rows.append({
        "open": 2.0, "high": 2.1, "low": 1.9, "close": 2.05,
        "volume": 10000, "timestamp_ms": now_ms, "trades": 999,
    })
    frame = _technical_frame({"available": True, "candles": rows}, "5min", now_ms)
    assert frame["closed_candles_used"] == 70
    assert frame["stale"] is False
    assert frame["trend"] == "BULLISH"
    assert frame["technical_bias"] == "BULLISH"
    assert frame["ema_50"] is not None
    assert frame["rsi_14"] is not None
    assert frame["macd_12_26_9"]["histogram"] is not None
    assert frame["atr_14"] is not None


def test_technical_frame_fails_closed_on_stale_or_short_data():
    now_ms = 1_000_000_000_000
    interval_ms = 5 * 60 * 1000
    rows = _synthetic_candles(10, interval_ms, now_ms)
    frame = _technical_frame({"available": True, "candles": rows}, "5min", now_ms)
    assert frame["data_quality"] in {"STALE_OR_UNAVAILABLE", "SHORT_HISTORY"}
    assert frame["technical_bias"] == "INSUFFICIENT_DATA"


def test_progression_ladder_requires_ten_doublings_to_cross_400_gross():
    ladder = _progression_ladder(0.40, 2.0, 12, 400.0)
    assert ladder["minimum_successive_doublings_to_reach_goal"] == 10
    assert ladder["steps"][1]["target_balance_usd"] == 0.8
    assert ladder["steps"][2]["target_balance_usd"] == 1.6
    assert ladder["steps"][3]["target_balance_usd"] == 3.2
    assert ladder["balance_after_goal_step_usd"] == 409.6
    assert ladder["all_values_gross_before_costs"] is True


def test_technical_analysis_rejects_unsupported_chain_without_external_call():
    response = client.get("/v1/technical-analysis/1/0x1111111111111111111111111111111111111111")
    assert response.status_code == 400
    assert "supports BSC" in response.json()["detail"]


def test_daily_candles_with_null_trade_count_keep_valid_ohlcv():
    rows = _normalize_candle_rows([
        [1.0, 1.2, 0.9, 1.1, 1000, 1787961600000, None],
        [1.1, 1.3, 1.0, 1.2, 1200, 1788048000000, 15],
        [None, 1.2, 0.9, 1.1, 1000, 1788134400000, 10],
    ])
    assert len(rows) == 2
    assert rows[0]["close"] == 1.1
    assert rows[0]["trades"] is None
    assert rows[1]["trades"] == 15


def test_topic_rush_skill_feed_flattens_tokens_and_preserves_topic():
    items = _items_from_payload("topic-rush", [
        {
            "name": {"topicNameEn": "Rising meme narrative"},
            "tokenList": [
                {"chainId": "56", "contractAddress": "0x1111111111111111111111111111111111111111", "symbol": "TEST", "marketCap": "10000"},
            ],
        },
    ])
    assert len(items) == 1
    assert items[0]["_topic_name"] == "Rising meme narrative"
    summary = _skill_item_summary(items[0], "56", "topic-rush")
    assert summary["contract_address"] == "0x1111111111111111111111111111111111111111"
    assert summary["topic_name"] == "Rising meme narrative"
    assert summary["buy_tax_raw"] is None
    assert summary["sell_tax_raw"] is None


def test_skill_sweep_rejects_unsupported_chain_without_external_call():
    response = client.get("/v1/skill-sweep/999")
    assert response.status_code == 400
    assert "supported chain_id values" in response.json()["detail"]
