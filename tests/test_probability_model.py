"""Tests for consensus probability model."""
import pytest
from positions.probability_model import ProbabilityModel


class TestProbabilityModel:
    def test_consensus_from_two_markets(self):
        model = ProbabilityModel()
        model.update_from_matched_events([{
            "canonical_title": "BTC > 100k",
            "markets": [
                {"platform": "polymarket", "yes_price": 0.60, "volume": 100000},
                {"platform": "kalshi", "yes_price": 0.55, "volume": 50000},
            ]
        }])
        c = model.get_consensus("BTC > 100k")
        assert c is not None
        # Volume-weighted: (0.60*100k + 0.55*50k) / 150k = 0.5833
        assert 0.58 < c["consensus_yes"] < 0.59
        assert c["platform_count"] == 2

    def test_deviation_detection(self):
        model = ProbabilityModel()
        model.update_from_matched_events([{
            "canonical_title": "Test Event",
            "markets": [
                {"platform": "polymarket", "yes_price": 0.70, "volume": 100000},
                {"platform": "kalshi", "yes_price": 0.45, "volume": 100000},
            ]
        }])
        assert model.has_deviation("Test Event", threshold=0.10)

    def test_no_consensus_for_single_market(self):
        model = ProbabilityModel()
        model.update_from_matched_events([{
            "canonical_title": "Solo",
            "markets": [{"platform": "polymarket", "yes_price": 0.60, "volume": 100}]
        }])
        assert model.get_consensus("Solo") is None

    def test_no_deviation_when_prices_agree(self):
        model = ProbabilityModel()
        model.update_from_matched_events([{
            "canonical_title": "Agreement",
            "markets": [
                {"platform": "polymarket", "yes_price": 0.60, "volume": 100000},
                {"platform": "kalshi", "yes_price": 0.62, "volume": 100000},
            ]
        }])
        assert not model.has_deviation("Agreement", threshold=0.10)
