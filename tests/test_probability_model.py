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
            "category": "crypto",
            "expiry": "2026-12-31",
            "markets": [
                {"platform": "polymarket", "yes_price": 0.60, "volume": 100000},
                {"platform": "kalshi", "yes_price": 0.62, "volume": 100000},
            ]
        }])
        assert not model.has_deviation("Agreement", threshold=0.10)

    def test_calibration_signal_shrinks_toward_consensus(self):
        model = ProbabilityModel()
        model.update_from_matched_events([{
            "canonical_title": "BTC > 100k",
            "category": "crypto",
            "expiry": "2026-12-31",
            "markets": [
                {"platform": "polymarket", "yes_price": 0.40, "volume": 200000},
                {"platform": "kalshi", "yes_price": 0.60, "volume": 200000},
                {"platform": "limitless", "yes_price": 0.62, "volume": 50000},
            ]
        }])
        signal = model.get_calibration_signal(
            title="BTC > 100k",
            platform="polymarket",
            raw_yes=0.40,
            category="crypto",
            days_to_expiry=5,
            volume=200000,
        )
        assert signal is not None
        assert signal["calibrated_yes"] > 0.40
        assert signal["preferred_side"] in {"YES", "NO"}
        assert signal["calibrated_edge_pct"] > 0

    def test_generate_report_returns_bucket_summaries(self):
        model = ProbabilityModel()
        model.update_from_matched_events([{
            "canonical_title": "ETH > 4k",
            "category": "crypto",
            "expiry": "2026-04-02",
            "markets": [
                {"platform": "polymarket", "yes_price": 0.30, "volume": 100000},
                {"platform": "kalshi", "yes_price": 0.48, "volume": 120000},
            ]
        }])
        report = model.generate_report()
        assert report["tracked_events"] == 1
        assert report["tracked_buckets"] >= 2
        assert report["top_unstable_buckets"]
        assert report["largest_live_disagreements"][0]["title"] == "ETH > 4k"
