from fastapi.testclient import TestClient

from app.main import app
from app.market_training import _evaluate, _extract_candidates

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
    result = _evaluate(_candidate(), _market(), RISK_OK, _candles())
    assert result["disposition"] == "SHADOW_ENTRY_CONDITION_MET"
    assert result["score_is_probability"] is False
    assert result["risk_evidence"]["missing_fields"] == []


def test_symbol_collision_blocks_shadow_entry():
    token = _candidate()
    token["symbol_contract_count"] = 3
    result = _evaluate(token, _market(), RISK_OK, _candles())
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
    result = _evaluate(_candidate(), _market(), risk, _candles())
    assert result["disposition"] == "REJECT_FROM_SHADOW_ENTRY"
    assert "security_evidence_incomplete" in result["hard_blocks"]
