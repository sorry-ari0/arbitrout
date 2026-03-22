"""Tests for political LLM strategy prompt building and response parsing."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import json
import pytest
from political.strategy import build_cluster_prompt, parse_strategy_response, validate_strategy
from political.models import (
    PoliticalContractInfo, PoliticalCluster,
    PoliticalSyntheticStrategy, SyntheticLeg, Scenario,
)
from adapters.models import NormalizedEvent


def _make_cluster():
    events = [
        NormalizedEvent("polymarket", "e1", "Talarico wins TX Senate", "politics",
                        0.62, 0.38, 1000, "2026-11-03", "https://poly.com/e1"),
        NormalizedEvent("kalshi", "e2", "Democrat wins TX Senate", "politics",
                        0.55, 0.45, 800, "2026-11-03", "https://kalshi.com/e2"),
        NormalizedEvent("polymarket", "e3", "Talarico wins by >5%", "politics",
                        0.38, 0.62, 500, "2026-11-03", "https://poly.com/e3"),
    ]
    contracts = [
        PoliticalContractInfo(events[0], "candidate_win", ["Talarico"], "dem", "TX Senate", "TX", None, None),
        PoliticalContractInfo(events[1], "party_outcome", [], "dem", "TX Senate", "TX", None, None),
        PoliticalContractInfo(events[2], "margin_bracket", ["Talarico"], None, None, "TX", 5.0, "above"),
    ]
    return PoliticalCluster("tx-senate-2026", "TX Senate", "TX", contracts, ["match1"])


class TestBuildPrompt:
    def test_prompt_includes_contracts(self):
        cluster = _make_cluster()
        rels = [{"type": "candidate_party_link", "pair": (0, 1), "score": 2.5,
                 "details": "Price gap 7%"}]
        prompt = build_cluster_prompt(cluster, rels)
        assert "Talarico wins TX Senate" in prompt
        assert "Democrat wins TX Senate" in prompt
        assert "candidate_party_link" in prompt
        assert "Fee rates" in prompt

    def test_prompt_includes_fee_rates(self):
        cluster = _make_cluster()
        prompt = build_cluster_prompt(cluster, [])
        assert "Polymarket=0% (maker)" in prompt
        assert "Kalshi=1.5%" in prompt


class TestParseResponse:
    def test_valid_json_parsed(self):
        cluster = _make_cluster()
        response = json.dumps({
            "strategies": [{
                "strategy_name": "TX Senate Dem Link",
                "legs": [
                    {"contract": 1, "side": "YES", "weight": 0.5},
                    {"contract": 2, "side": "YES", "weight": 0.5},
                ],
                "scenarios": [
                    {"outcome": "Talarico wins", "probability": 0.6, "pnl_pct": 12.5},
                    {"outcome": "Other Dem wins", "probability": 0.1, "pnl_pct": -5.0},
                    {"outcome": "Republican wins", "probability": 0.3, "pnl_pct": -40.0},
                ],
                "expected_value_pct": 8.2,
                "win_probability": 0.65,
                "max_loss_pct": -45.0,
                "confidence": "high",
                "reasoning": "Price gap exploitation",
            }]
        })
        strategies = parse_strategy_response(response, cluster)
        assert len(strategies) == 1
        assert strategies[0].strategy_name == "TX Senate Dem Link"
        assert len(strategies[0].legs) == 2
        assert strategies[0].legs[0].event_id == "e1"

    def test_invalid_json_returns_empty(self):
        cluster = _make_cluster()
        strategies = parse_strategy_response("not json at all", cluster)
        assert strategies == []

    def test_missing_fields_returns_empty(self):
        cluster = _make_cluster()
        response = json.dumps({"strategies": [{"strategy_name": "X"}]})
        strategies = parse_strategy_response(response, cluster)
        assert strategies == []


class TestValidateStrategy:
    def _make_strategy(self, **overrides):
        defaults = {
            "cluster_id": "test",
            "strategy_name": "Test",
            "legs": [SyntheticLeg(1, "e1", "YES", 0.5)],
            "scenarios": [Scenario("Win", 0.6, 10.0)],
            "expected_value_pct": 8.0,
            "win_probability": 0.65,
            "max_loss_pct": -40.0,
            "confidence": "high",
            "reasoning": "test",
        }
        defaults.update(overrides)
        return PoliticalSyntheticStrategy(**defaults)

    def test_valid_strategy_passes(self):
        s = self._make_strategy()
        assert validate_strategy(s) is True

    def test_low_win_probability_rejected(self):
        # Threshold was relaxed from 0.50 to 0.35; use value below new threshold
        s = self._make_strategy(win_probability=0.30)
        assert validate_strategy(s) is False

    def test_extreme_loss_rejected(self):
        s = self._make_strategy(max_loss_pct=-70.0)
        assert validate_strategy(s) is False

    def test_low_ev_rejected(self):
        # EV threshold was relaxed from 3% to 1%; use value below new threshold
        s = self._make_strategy(expected_value_pct=0.5)
        assert validate_strategy(s) is False

    def test_low_confidence_rejected(self):
        s = self._make_strategy(confidence="low")
        assert validate_strategy(s) is False


class TestCryptoPromptExtension:
    """Tests for crypto market context in LLM prompt."""

    def _make_crypto_cluster(self, contracts=None):
        """Build a crypto cluster with 2 BTC contracts."""
        from political.models import PoliticalCluster, PoliticalContractInfo
        from adapters.models import NormalizedEvent

        ev1 = NormalizedEvent(
            platform="polymarket", event_id="btc-150k", title="BTC above $150K",
            category="crypto", yes_price=0.35, no_price=0.65,
            volume=50000, expiry="2026-12-31", url="https://polymarket.com/btc150k",
        )
        ev2 = NormalizedEvent(
            platform="polymarket", event_id="btc-sec", title="SEC classifies BTC as security",
            category="crypto", yes_price=0.15, no_price=0.85,
            volume=30000, expiry="2026-12-31", url="https://polymarket.com/btcsec",
        )
        c1 = PoliticalContractInfo(
            event=ev1, contract_type="crypto_event",
            crypto_asset="BTC", event_category="price_target",
            crypto_direction="positive", crypto_threshold=150000.0,
        )
        c2 = PoliticalContractInfo(
            event=ev2, contract_type="crypto_event",
            crypto_asset="BTC", event_category="regulatory",
            crypto_direction="negative",
        )
        return PoliticalCluster(
            cluster_id="crypto-btc-2026", race=None, state=None,
            contracts=[c1, c2], matched_events=[ev1, ev2],
        )

    def _make_political_cluster(self):
        """Build a political cluster (for comparison)."""
        from political.models import PoliticalCluster, PoliticalContractInfo
        from adapters.models import NormalizedEvent

        ev1 = NormalizedEvent(
            platform="polymarket", event_id="tx-1", title="Talarico wins TX Senate",
            category="politics", yes_price=0.55, no_price=0.45,
            volume=10000, expiry="2026-11-03", url="https://polymarket.com/tx1",
        )
        c1 = PoliticalContractInfo(
            event=ev1, contract_type="candidate_win",
            candidates=["Talarico"], race="TX Senate", state="TX",
        )
        ev2 = NormalizedEvent(
            platform="polymarket", event_id="tx-2", title="Cruz wins TX Senate",
            category="politics", yes_price=0.40, no_price=0.60,
            volume=8000, expiry="2026-11-03", url="https://polymarket.com/tx2",
        )
        c2 = PoliticalContractInfo(
            event=ev2, contract_type="candidate_win",
            candidates=["Cruz"], race="TX Senate", state="TX",
        )
        return PoliticalCluster(
            cluster_id="senate-tx-2026", race="TX Senate", state="TX",
            contracts=[c1, c2], matched_events=[ev1, ev2],
        )

    def test_crypto_cluster_prompt_has_asset_header(self):
        """Crypto cluster prompt uses 'Asset: BTC' instead of 'Race: ...'."""
        cluster = self._make_crypto_cluster()
        prompt = build_cluster_prompt(cluster, [])
        assert "Asset: BTC" in prompt
        assert "Race:" not in prompt

    def test_crypto_cluster_prompt_has_context_block(self):
        """Crypto cluster prompt includes crypto market context section."""
        cluster = self._make_crypto_cluster()
        prompt = build_cluster_prompt(cluster, [])
        assert "Crypto Market Context" in prompt
        assert "Annualized volatility" in prompt
        assert "Regulatory events" in prompt

    def test_political_cluster_no_crypto_context(self):
        """Political cluster prompt does NOT include crypto context."""
        cluster = self._make_political_cluster()
        prompt = build_cluster_prompt(cluster, [])
        assert "Crypto Market Context" not in prompt
        assert "Race: TX Senate" in prompt

    def test_crypto_cluster_prompt_with_spot_prices(self):
        """Crypto prompt shows actual spot prices when provided."""
        cluster = self._make_crypto_cluster()
        prompt = build_cluster_prompt(cluster, [], spot_prices={"BTC": 97450.0})
        assert "$97,450.00" in prompt
        assert "(price unavailable)" not in prompt

    def test_crypto_cluster_prompt_without_spot_prices(self):
        """Crypto prompt shows '(price unavailable)' when spot_prices is None."""
        cluster = self._make_crypto_cluster()
        prompt = build_cluster_prompt(cluster, [])
        assert "(price unavailable)" in prompt

    def test_political_cluster_prompt_unchanged(self):
        """Political cluster prompt still works correctly."""
        cluster = self._make_political_cluster()
        prompt = build_cluster_prompt(cluster, [])
        assert "CLUSTER:senate-tx-2026" in prompt
        assert "Race: TX Senate" in prompt
