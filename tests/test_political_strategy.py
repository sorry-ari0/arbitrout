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
        assert "Polymarket=2%" in prompt
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
        s = self._make_strategy(win_probability=0.40)
        assert validate_strategy(s) is False

    def test_extreme_loss_rejected(self):
        s = self._make_strategy(max_loss_pct=-70.0)
        assert validate_strategy(s) is False

    def test_low_ev_rejected(self):
        s = self._make_strategy(expected_value_pct=2.0)
        assert validate_strategy(s) is False

    def test_low_confidence_rejected(self):
        s = self._make_strategy(confidence="low")
        assert validate_strategy(s) is False
