"""Unit tests for the arbitrage engine."""
import sys
from pathlib import Path

# Add src to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from adapters.models import NormalizedEvent, MatchedEvent, ArbitrageOpportunity
from arbitrage_engine import find_arbitrage, compute_feed


def _make_event(platform: str, event_id: str, title: str, yes: float, no: float, volume: int = 1000) -> NormalizedEvent:
    """Helper to create a NormalizedEvent."""
    return NormalizedEvent(
        platform=platform,
        event_id=event_id,
        title=title,
        category="crypto",
        yes_price=yes,
        no_price=no,
        volume=volume,
        expiry="2026-12-31",
        url=f"https://{platform}.com/{event_id}",
    )


def _make_matched(events: list[NormalizedEvent], title: str = "Test Event") -> MatchedEvent:
    """Helper to create a MatchedEvent from a list of events."""
    return MatchedEvent(
        match_id="test-match-1",
        canonical_title=title,
        category="crypto",
        expiry="2026-12-31",
        markets=events,
    )


class TestFindArbitrage:
    """Tests for find_arbitrage()."""

    def test_basic_arbitrage_spread(self):
        """Two events with yes=0.40 and no=0.55 should produce profit=5%."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.65)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55)
        matched = _make_matched([ev_a, ev_b])

        opps = find_arbitrage([matched])
        assert len(opps) == 1
        opp = opps[0]
        # Best YES = 0.40 (polymarket), Best NO = 0.55 (kalshi)
        # spread = 1.0 - (0.40 + 0.55) = 0.05
        assert opp.buy_yes_price == 0.40
        assert opp.buy_no_price == 0.55
        assert abs(opp.spread - 0.05) < 0.001
        assert abs(opp.profit_pct - 5.0) < 0.1

    def test_same_platform_excluded(self):
        """Same-platform pairs should not produce arbitrage."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.55)
        # Only one platform
        matched = _make_matched([ev_a])

        opps = find_arbitrage([matched])
        assert len(opps) == 0

    def test_same_platform_best_prices_uses_other(self):
        """When best YES and best NO are on the same platform, use other platform."""
        # Platform A has the best YES AND best NO — must look at platform B
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.30, no=0.40)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.45, no=0.50)
        matched = _make_matched([ev_a, ev_b])

        opps = find_arbitrage([matched])
        assert len(opps) == 1
        opp = opps[0]
        # Can't use polymarket for both — should pick cross-platform pair
        assert opp.buy_yes_platform != opp.buy_no_platform

    def test_no_arbitrage_when_spread_negative(self):
        """No opportunity when yes + no > 1.0."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.60, no=0.70)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.55, no=0.65)
        matched = _make_matched([ev_a, ev_b])

        opps = find_arbitrage([matched], min_spread=0.0)
        # Spread = 1.0 - (0.55 + 0.65) = -0.20, no profit
        # But find_arbitrage includes negative spreads if min_spread=0
        # The real check: profit_pct < 0
        for opp in opps:
            assert opp.profit_pct < 0

    def test_min_spread_filter(self):
        """Opportunities below min_spread should be filtered."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.65)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55)
        matched = _make_matched([ev_a, ev_b])

        # spread = 0.05, so min_spread=0.10 should filter it out
        opps = find_arbitrage([matched], min_spread=0.10)
        assert len(opps) == 0

        # min_spread=0.01 should include it
        opps = find_arbitrage([matched], min_spread=0.01)
        assert len(opps) == 1

    def test_min_volume_filter(self):
        """Opportunities with low volume should be filtered."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.65, volume=500)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55, volume=300)
        matched = _make_matched([ev_a, ev_b])

        # Combined volume = 800, filter at 1000
        opps = find_arbitrage([matched], min_volume=1000)
        assert len(opps) == 0

        # Filter at 500
        opps = find_arbitrage([matched], min_volume=500)
        assert len(opps) == 1

    def test_allocation_percentages(self):
        """Trade ratio should return correct allocation percentages."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.70)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55)
        matched = _make_matched([ev_a, ev_b])

        opps = find_arbitrage([matched])
        assert len(opps) == 1
        opp = opps[0]

        # yes_allocation = no_price / (yes + no) * 100
        # no_allocation = yes_price / (yes + no) * 100
        total = opp.buy_yes_price + opp.buy_no_price
        expected_yes_alloc = round((opp.buy_no_price / total) * 100, 1)
        expected_no_alloc = round((opp.buy_yes_price / total) * 100, 1)
        assert abs(opp.yes_allocation_pct - expected_yes_alloc) < 0.01
        assert abs(opp.no_allocation_pct - expected_no_alloc) < 0.01
        # Should sum to ~100%
        assert abs(opp.yes_allocation_pct + opp.no_allocation_pct - 100.0) < 0.1

    def test_sorted_by_profit_descending(self):
        """Multiple opportunities should be sorted by profit % descending."""
        ev1a = _make_event("polymarket", "p1", "Event A", yes=0.40, no=0.65)
        ev1b = _make_event("kalshi", "k1", "Event A", yes=0.50, no=0.55)
        match1 = _make_matched([ev1a, ev1b], "Event A")
        match1.match_id = "m1"

        ev2a = _make_event("polymarket", "p2", "Event B", yes=0.30, no=0.65)
        ev2b = _make_event("kalshi", "k2", "Event B", yes=0.45, no=0.50)
        match2 = _make_matched([ev2a, ev2b], "Event B")
        match2.match_id = "m2"

        opps = find_arbitrage([match1, match2])
        assert len(opps) == 2
        assert opps[0].profit_pct >= opps[1].profit_pct

    def test_three_platforms(self):
        """Arbitrage with three platforms should pick the best cross-platform pair."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.70)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55)
        ev_c = _make_event("predictit", "pi1", "BTC > 100k", yes=0.45, no=0.60)
        matched = _make_matched([ev_a, ev_b, ev_c])

        opps = find_arbitrage([matched])
        assert len(opps) == 1
        opp = opps[0]
        # Best YES = 0.40 (polymarket), Best NO = 0.55 (kalshi)
        assert opp.buy_yes_platform == "polymarket"
        assert opp.buy_no_platform == "kalshi"

    def test_pure_arb_ignores_dead_best_quote_and_uses_tradeable_pair(self):
        """Scanner should not anchor on a 0c quote with no volume when a live pair exists."""
        dead_yes = _make_event("predictit", "pi1", "BTC > 100k", yes=0.0, no=1.0, volume=0)
        live_yes = _make_event("polymarket", "p1", "BTC > 100k", yes=0.35, no=0.70, volume=500)
        live_no = _make_event("kalshi", "k1", "BTC > 100k", yes=0.55, no=0.55, volume=700)
        matched = _make_matched([dead_yes, live_yes, live_no])

        opps = find_arbitrage([matched])
        assert len(opps) == 1
        opp = opps[0]
        assert opp.buy_yes_platform == "polymarket"
        assert opp.buy_no_platform == "kalshi"
        assert opp.combined_volume == 1200

    def test_pure_arb_requires_actionable_liquidity_on_selected_legs(self):
        """No opportunity should be emitted when the only apparent spread uses dead legs."""
        dead_yes = _make_event("predictit", "pi1", "BTC > 100k", yes=0.0, no=1.0, volume=0)
        dead_no = _make_event("kalshi", "k1", "BTC > 100k", yes=0.70, no=0.40, volume=0)
        matched = _make_matched([dead_yes, dead_no])

        opps = find_arbitrage([matched])
        assert len(opps) == 0


class TestComputeFeed:
    """Tests for compute_feed()."""

    def test_first_scan_no_changes(self):
        """First scan should produce no feed items (no previous prices)."""
        from arbitrage_engine import _previous_prices
        _previous_prices.clear()

        events = [_make_event("polymarket", "p1", "BTC", yes=0.50, no=0.50)]
        feed = compute_feed(events)
        assert len(feed) == 0

    def test_price_change_detected(self):
        """Price changes between scans should appear in feed."""
        from arbitrage_engine import _previous_prices
        _previous_prices.clear()

        events1 = [_make_event("polymarket", "p1", "BTC > 100k", yes=0.50, no=0.50)]
        compute_feed(events1)

        events2 = [_make_event("polymarket", "p1", "BTC > 100k", yes=0.55, no=0.45)]
        feed = compute_feed(events2)
        assert len(feed) == 1
        assert feed[0]["change"] == pytest.approx(0.05, abs=0.001)

    def test_no_change_no_feed(self):
        """Same price should produce no feed items."""
        from arbitrage_engine import _previous_prices
        _previous_prices.clear()

        events = [_make_event("polymarket", "p1", "BTC", yes=0.50, no=0.50)]
        compute_feed(events)
        feed = compute_feed(events)
        assert len(feed) == 0


class TestArbitrageOpportunity:
    """Tests for ArbitrageOpportunity data class."""

    def test_to_dict(self):
        """to_dict should include all key fields."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.65)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55)
        matched = _make_matched([ev_a, ev_b])

        opps = find_arbitrage([matched])
        d = opps[0].to_dict()

        assert "matched_event" in d
        assert "buy_yes_platform" in d
        assert "buy_yes_price" in d
        assert "buy_no_platform" in d
        assert "buy_no_price" in d
        assert "spread" in d
        assert "profit_pct" in d
        assert "yes_allocation_pct" in d
        assert "no_allocation_pct" in d
        assert "net_profit_pct" in d
        assert "confidence" in d

    def test_to_dict_confidence_is_valid(self):
        """confidence should be one of the valid levels."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.65)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        d = opps[0].to_dict()
        assert d["confidence"] in ("high", "medium", "low", "very_low")


class TestFeeAdjustedProfit:
    """Tests for fee-aware arbitrage filtering."""

    def test_small_spread_with_predictit_filtered_out(self):
        """0.6% spread with PredictIt NO at 99c should be filtered (fees > profit)."""
        ev_a = _make_event("polymarket", "p1", "Peru Election: Forsyth", yes=0.004, no=0.996)
        ev_b = _make_event("predictit", "pi1", "Peru Election: Forsyth", yes=0.01, no=0.99)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        # After PM 2% taker + PI 10% profit tax, this is a loss
        assert len(opps) == 0

    def test_large_spread_survives_fees(self):
        """16.8% spread with Polymarket+Limitless should survive after fees."""
        ev_a = _make_event("polymarket", "p1", "Corners 7+", yes=0.536, no=0.464)
        ev_b = _make_event("limitless", "l1", "Corners 7+", yes=0.704, no=0.296)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        assert len(opps) == 1
        opp = opps[0]
        # Net profit should be less than gross but still positive
        assert opp.net_profit_pct > 0
        assert opp.net_profit_pct < opp.profit_pct

    def test_predictit_profit_tax_modeled(self):
        """PredictIt 10% profit tax + 5% withdrawal should reduce net payout."""
        # Use 15% gross spread (PI YES=0.30 + Kalshi NO=0.55 = 0.85)
        ev_a = _make_event("predictit", "pi1", "Event X", yes=0.30, no=0.70)
        ev_b = _make_event("kalshi", "k1", "Event X", yes=0.60, no=0.55)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        # Should exist but net < gross due to PI profit tax + withdrawal fee
        assert len(opps) >= 1
        assert opps[0].net_profit_pct < opps[0].profit_pct

    def test_kalshi_1pct_spread_with_predictit_filtered(self):
        """1% spread Kalshi+PredictIt should be negative after fees."""
        ev_a = _make_event("predictit", "pi1", "Cabinet: Loeffler", yes=0.01, no=0.99)
        ev_b = _make_event("kalshi", "k1", "Cabinet: Loeffler", yes=0.99, no=0.98)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        assert len(opps) == 0  # Filtered out

    def test_2pct_spread_with_predictit_no_filtered(self):
        """2.3% spread with PredictIt NO at 95c should be filtered."""
        ev_a = _make_event("polymarket", "p1", "2028 Dem: Pritzker", yes=0.027, no=0.973)
        ev_b = _make_event("predictit", "pi1", "2028 Dem: Pritzker", yes=0.06, no=0.95)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        # 2.3% gross - PI profit tax on 95c NO = loss
        assert len(opps) == 0

    def test_small_crypto_spread_filtered_by_polymarket_fee_curve(self):
        """Crypto arbs should use the same taker fee curve as paper execution."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.50, no=0.50)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.55, no=0.49)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        assert len(opps) == 0


class TestConfidenceScoring:
    """Tests for match confidence scoring."""

    def test_huge_spread_filtered_as_false_match(self):
        """91% spread = almost certainly a false match, should be dropped entirely."""
        ev_a = _make_event("polymarket", "p1", "WV Senate: Republican", yes=0.015, no=0.985)
        ev_b = _make_event("predictit", "pi1", "WV Senate: Republican", yes=0.95, no=0.07)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        assert len(opps) == 0  # Filtered out as very_low confidence

    def test_moderate_spread_gets_medium_confidence(self):
        """16% spread gets medium confidence."""
        ev_a = _make_event("polymarket", "p1", "Corners 7+", yes=0.536, no=0.464)
        ev_b = _make_event("limitless", "l1", "Corners 7+", yes=0.704, no=0.296)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        assert len(opps) == 1
        assert opps[0].confidence == "medium"

    def test_small_spread_gets_high_confidence(self):
        """5% spread between two agreeing platforms = high confidence."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.65)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        assert len(opps) == 1
        assert opps[0].confidence == "high"


class TestCrossThresholdSynthetics:
    """Tests for cross-threshold synthetic derivative detection."""

    def test_corners_different_thresholds_detected_as_synthetic(self):
        """7+ corners and 9+ corners should create a synthetic, not be filtered."""
        ev_a = _make_event("polymarket", "p1", "Will Team A have 7 or more total corners?", yes=0.245, no=0.755)
        ev_b = _make_event("limitless", "l1", "Will Team A have 9 or more total corners?", yes=0.805, no=0.196)
        matched = _make_matched([ev_a, ev_b], "Team A corners")
        opps = find_arbitrage([matched])
        assert len(opps) == 1
        opp = opps[0]
        assert opp.is_synthetic
        assert opp.synthetic_info.get("type") == "cross_threshold"
        # All scenarios win (nested thresholds)
        assert opp.synthetic_info.get("loss_conditions", 1) == 0

    def test_corners_guaranteed_profit_all_scenarios(self):
        """Cross-threshold corners bet should win in all scenarios."""
        ev_a = _make_event("polymarket", "p1", "Will Team A have 7 or more total corners?", yes=0.25, no=0.75)
        ev_b = _make_event("limitless", "l1", "Will Team A have 9 or more total corners?", yes=0.80, no=0.20)
        matched = _make_matched([ev_a, ev_b], "Team A corners")
        opps = find_arbitrage([matched])
        assert len(opps) == 1
        opp = opps[0]
        scenarios = opp.synthetic_info.get("scenarios", {})
        # Every scenario should have positive net
        for name, s in scenarios.items():
            assert s["net"] > 0, f"Scenario {name} has negative net: {s['net']}"

    def test_approval_rating_different_thresholds(self):
        """43.5% or higher vs 45% or higher should create a synthetic."""
        ev_a = _make_event("kalshi", "k1", "Approval rating 43.5% or higher", yes=0.09, no=0.89)
        ev_b = _make_event("predictit", "pi1", "Approval rating 45% or higher", yes=0.80, no=0.21)
        matched = _make_matched([ev_a, ev_b], "Approval rating")
        opps = find_arbitrage([matched])
        assert len(opps) == 1
        assert opps[0].is_synthetic

    def test_same_threshold_not_synthetic(self):
        """Same threshold should be pure arb, not synthetic."""
        ev_a = _make_event("polymarket", "p1", "Will Team A have 7 or more corners?", yes=0.40, no=0.65)
        ev_b = _make_event("kalshi", "k1", "Will Team A have 7 or more corners?", yes=0.50, no=0.55)
        matched = _make_matched([ev_a, ev_b], "Team A 7 corners")
        opps = find_arbitrage([matched])
        assert len(opps) == 1
        assert not opps[0].is_synthetic


class TestDeduplication:
    """Tests for opportunity deduplication."""

    def test_duplicate_event_ids_deduped(self):
        """Same event_id pair should only appear once."""
        ev_a = _make_event("polymarket", "p1", "Event X", yes=0.40, no=0.65)
        ev_b = _make_event("kalshi", "k1", "Event X", yes=0.50, no=0.55)
        match1 = _make_matched([ev_a, ev_b], "Event X")
        match1.match_id = "m1"
        match2 = _make_matched([ev_a, ev_b], "Event X duplicate")
        match2.match_id = "m2"
        opps = find_arbitrage([match1, match2])
        assert len(opps) == 1
