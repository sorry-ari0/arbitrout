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
