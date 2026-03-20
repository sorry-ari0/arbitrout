"""Tests for MarketMaker — preemptive cancel, token merge accounting, circuit breaker, discovery."""
import asyncio
import time

import pytest

from positions.price_feed import BinancePriceFeed, AssetState
from positions.market_maker import (
    MarketMaker, MarketState, MMStats,
    ADVERSE_MOVE_THRESHOLD, PRICE_DIVERGENCE_HALT,
    MAX_INVENTORY_IMBALANCE, TARGET_SPREAD_LIQUID,
    MERGE_MIN_MATCHED_SHARES,
)


def _make_market(condition_id="cond_abc", asset="BTC", **kwargs):
    """Helper to create a MarketState with defaults."""
    defaults = dict(
        slug="test-market",
        title="Will BTC go up?",
        expiry="2026-12-31T00:00:00Z",
        asset=asset,
    )
    defaults.update(kwargs)
    return MarketState(condition_id=condition_id, **defaults)


# ── Preemptive cancel logic ──────────────────────────────────────

class TestPreemptiveCancel:
    def _setup_mm(self, asset="BTC"):
        feed = BinancePriceFeed(assets=[asset])
        mm = MarketMaker(feed, total_capital=1000.0)
        return feed, mm

    def test_cancel_yes_on_price_drop(self):
        """YES order should be canceled when price drops >0.3%."""
        feed, mm = self._setup_mm()
        market = _make_market(
            yes_order_id="order_yes_1",
            no_order_id=None,  # NO not filled → YES is exposed
            quote_ref_price=100000.0,
        )
        mm._markets["cond_abc"] = market

        # Price drops 0.4% (below -0.3% threshold)
        new_price = 100000.0 * (1 - 0.004)  # $99,600
        mm._preemptive_cancel_check("BTC", new_price, time.time())

        assert not mm._pending_cancels.empty()
        item = mm._pending_cancels.get_nowait()
        assert item == ("cond_abc", "YES", "order_yes_1")
        # Order ID should be cleared immediately
        assert market.yes_order_id is None
        assert mm.stats.preemptive_cancels == 1

    def test_cancel_no_on_price_rise(self):
        """NO order should be canceled when price rises >0.3%."""
        feed, mm = self._setup_mm()
        market = _make_market(
            yes_order_id=None,  # YES not filled → NO is exposed
            no_order_id="order_no_1",
            quote_ref_price=100000.0,
        )
        mm._markets["cond_abc"] = market

        # Price rises 0.4%
        new_price = 100000.0 * (1 + 0.004)
        mm._preemptive_cancel_check("BTC", new_price, time.time())

        item = mm._pending_cancels.get_nowait()
        assert item == ("cond_abc", "NO", "order_no_1")
        assert market.no_order_id is None

    def test_no_cancel_below_threshold(self):
        """No cancel when price moves less than 0.3%."""
        feed, mm = self._setup_mm()
        market = _make_market(
            yes_order_id="order_yes_1",
            no_order_id=None,
            quote_ref_price=100000.0,
        )
        mm._markets["cond_abc"] = market

        # Price drops only 0.1% (below threshold)
        new_price = 100000.0 * (1 - 0.001)
        mm._preemptive_cancel_check("BTC", new_price, time.time())

        assert mm._pending_cancels.empty()
        assert market.yes_order_id == "order_yes_1"  # Not cleared

    def test_no_cancel_when_both_sides_active(self):
        """No cancel when both YES and NO orders are active (not exposed)."""
        feed, mm = self._setup_mm()
        market = _make_market(
            yes_order_id="order_yes_1",
            no_order_id="order_no_1",  # Both active
            quote_ref_price=100000.0,
        )
        mm._markets["cond_abc"] = market

        # Large price drop
        new_price = 100000.0 * 0.99
        mm._preemptive_cancel_check("BTC", new_price, time.time())
        assert mm._pending_cancels.empty()

    def test_no_duplicate_cancel(self):
        """After cancel is queued and order_id cleared, subsequent ticks should not re-queue."""
        feed, mm = self._setup_mm()
        market = _make_market(
            yes_order_id="order_yes_1",
            no_order_id=None,
            quote_ref_price=100000.0,
        )
        mm._markets["cond_abc"] = market

        adverse_price = 100000.0 * (1 - 0.004)

        # First tick triggers cancel
        mm._preemptive_cancel_check("BTC", adverse_price, time.time())
        assert mm.stats.preemptive_cancels == 1

        # Second tick should NOT trigger again (order_id already cleared)
        mm._preemptive_cancel_check("BTC", adverse_price * 0.999, time.time())
        assert mm.stats.preemptive_cancels == 1
        assert mm._pending_cancels.qsize() == 1  # Still just 1

    def test_ignores_other_asset(self):
        """Preemptive cancel only fires for the matching asset."""
        feed, mm = self._setup_mm()
        market = _make_market(
            asset="BTC",
            yes_order_id="order_yes_1",
            no_order_id=None,
            quote_ref_price=100000.0,
        )
        mm._markets["cond_abc"] = market

        # ETH price move should not affect BTC market
        mm._preemptive_cancel_check("ETH", 2000.0, time.time())
        assert mm._pending_cancels.empty()

    def test_skips_zero_ref_price(self):
        """Skip markets where quote_ref_price is not yet set."""
        feed, mm = self._setup_mm()
        market = _make_market(
            yes_order_id="order_yes_1",
            no_order_id=None,
            quote_ref_price=0.0,
        )
        mm._markets["cond_abc"] = market
        mm._preemptive_cancel_check("BTC", 50000.0, time.time())
        assert mm._pending_cancels.empty()


# ── Drain pending cancels ────────────────────────────────────────

class TestDrainCancels:
    @pytest.mark.asyncio
    async def test_drain_processes_queue(self):
        feed = BinancePriceFeed()
        mm = MarketMaker(feed)
        mm._markets["cond_abc"] = _make_market()

        mm._pending_cancels.put_nowait(("cond_abc", "YES", "paper_order_1"))
        await mm._drain_pending_cancels()
        assert mm._pending_cancels.empty()

    @pytest.mark.asyncio
    async def test_drain_handles_missing_market(self):
        feed = BinancePriceFeed()
        mm = MarketMaker(feed)
        mm._pending_cancels.put_nowait(("nonexistent", "YES", "order_1"))
        await mm._drain_pending_cancels()
        assert mm._pending_cancels.empty()


# ── Token merge accounting ───────────────────────────────────────

class TestTokenMerge:
    @pytest.mark.asyncio
    async def test_paper_merge_accounting(self):
        """Paper mode merge should correctly update inventory and profit."""
        feed = BinancePriceFeed()
        mm = MarketMaker(feed, total_capital=1000.0)

        market = _make_market()
        # Simulate: bought 10 YES @ $0.45 and 10 NO @ $0.50
        market.yes_shares = 10.0
        market.no_shares = 10.0
        market.yes_cost = 4.50  # 10 * $0.45
        market.no_cost = 5.00   # 10 * $0.50
        mm._markets["cond_abc"] = market

        await mm._merge_matched_tokens(market, 10.0)

        # After merge: 10 shares merged at $1.00 each
        # Profit = 10 * (1.0 - 0.45 - 0.50) = 10 * 0.05 = $0.50
        assert market.yes_shares == 0.0
        assert market.no_shares == 0.0
        assert market.yes_cost == 0.0
        assert market.no_cost == 0.0
        assert abs(market.total_merged_profit - 0.50) < 0.01
        assert mm.stats.merges_completed == 1
        # Capital freed: full $1.00/share = $10.00
        assert mm.total_capital == 1010.0

    @pytest.mark.asyncio
    async def test_partial_merge(self):
        """When YES != NO shares, only matched quantity merges."""
        feed = BinancePriceFeed()
        mm = MarketMaker(feed, total_capital=1000.0)

        market = _make_market()
        market.yes_shares = 15.0
        market.no_shares = 10.0
        market.yes_cost = 6.75   # 15 * $0.45
        market.no_cost = 5.00    # 10 * $0.50
        mm._markets["cond_abc"] = market

        await mm._merge_matched_tokens(market, 10.0)

        # Remaining: 5 YES shares, 0 NO shares
        assert market.yes_shares == 5.0
        assert market.no_shares == 0.0
        # YES cost reduced proportionally: 6.75 - (10 * 0.45) = 2.25
        assert abs(market.yes_cost - 2.25) < 0.01
        assert market.no_cost == 0.0
        assert mm.stats.merges_completed == 1

    @pytest.mark.asyncio
    async def test_merge_updates_global_stats(self):
        feed = BinancePriceFeed()
        mm = MarketMaker(feed, total_capital=500.0)

        market = _make_market()
        market.yes_shares = 5.0
        market.no_shares = 5.0
        market.yes_cost = 2.25  # 5 * $0.45
        market.no_cost = 2.50   # 5 * $0.50
        mm._markets["cond_abc"] = market

        await mm._merge_matched_tokens(market, 5.0)

        assert mm.stats.total_merged_profit > 0
        assert mm.stats.total_pnl > 0
        assert mm.stats.merges_completed == 1


# ── Circuit breaker ──────────────────────────────────────────────

class TestCircuitBreaker:
    def test_trips_on_stale_feed(self):
        feed = BinancePriceFeed(assets=["BTC"])
        mm = MarketMaker(feed)
        market = _make_market()
        mm._markets["cond_abc"] = market
        # Feed is stale by default (no price_time set)
        assert mm._check_circuit_breaker() is True

    def test_no_trip_on_fresh_feed(self):
        feed = BinancePriceFeed(assets=["BTC"])
        feed._state["BTC"].price_time = time.time()
        feed._state["BTC"].price = 100000.0
        mm = MarketMaker(feed)
        market = _make_market(quote_ref_price=100000.0)
        mm._markets["cond_abc"] = market
        assert mm._check_circuit_breaker() is False

    def test_trips_on_large_price_divergence(self):
        """Circuit breaker should trip if Binance moves >5% from quote ref."""
        feed = BinancePriceFeed(assets=["BTC"])
        feed._state["BTC"].price = 106000.0  # 6% above ref
        feed._state["BTC"].price_time = time.time()
        mm = MarketMaker(feed)
        market = _make_market(
            quote_ref_price=100000.0,
            yes_order_price=0.50,
        )
        mm._markets["cond_abc"] = market
        assert mm._check_circuit_breaker() is True

    def test_no_trip_on_small_divergence(self):
        """Small Binance move (2%) should not trip circuit breaker."""
        feed = BinancePriceFeed(assets=["BTC"])
        feed._state["BTC"].price = 102000.0  # 2% above ref
        feed._state["BTC"].price_time = time.time()
        mm = MarketMaker(feed)
        market = _make_market(
            quote_ref_price=100000.0,
            yes_order_price=0.50,
        )
        mm._markets["cond_abc"] = market
        assert mm._check_circuit_breaker() is False

    def test_no_trip_without_ref_price(self):
        """Skip divergence check if quote_ref_price is not set."""
        feed = BinancePriceFeed(assets=["BTC"])
        feed._state["BTC"].price = 200000.0  # Huge move but no ref
        feed._state["BTC"].price_time = time.time()
        mm = MarketMaker(feed)
        market = _make_market(quote_ref_price=0.0, yes_order_price=0.50)
        mm._markets["cond_abc"] = market
        assert mm._check_circuit_breaker() is False


# ── Asset detection ──────────────────────────────────────────────

class TestAssetDetection:
    def test_detect_btc(self):
        feed = BinancePriceFeed(assets=["BTC"])
        mm = MarketMaker(feed)
        assert mm._detect_asset_from_title("Will BTC go above $100K?") == "BTC"
        assert mm._detect_asset_from_title("Bitcoin price prediction") == "BTC"

    def test_detect_eth(self):
        feed = BinancePriceFeed(assets=["BTC", "ETH"])
        mm = MarketMaker(feed)
        assert mm._detect_asset_from_title("Will ETH reach $5000?") == "ETH"
        assert mm._detect_asset_from_title("Ethereum price") == "ETH"

    def test_detect_sol(self):
        feed = BinancePriceFeed(assets=["BTC", "SOL"])
        mm = MarketMaker(feed)
        assert mm._detect_asset_from_title("SOL above $200?") == "SOL"
        assert mm._detect_asset_from_title("Solana prediction") == "SOL"

    def test_returns_none_for_unknown(self):
        feed = BinancePriceFeed(assets=["BTC"])
        mm = MarketMaker(feed)
        assert mm._detect_asset_from_title("Who will win the election?") is None

    def test_returns_none_for_untracked(self):
        """If we track BTC only, ETH title returns None."""
        feed = BinancePriceFeed(assets=["BTC"])
        mm = MarketMaker(feed)
        assert mm._detect_asset_from_title("ETH price") is None


# ── Near resolution ──────────────────────────────────────────────

class TestNearResolution:
    def test_far_from_resolution(self):
        feed = BinancePriceFeed()
        mm = MarketMaker(feed)
        market = _make_market(expiry="2026-12-31T00:00:00Z")
        assert mm._is_near_resolution(market) is False

    def test_no_expiry(self):
        feed = BinancePriceFeed()
        mm = MarketMaker(feed)
        market = _make_market(expiry="")
        assert mm._is_near_resolution(market) is False

    def test_invalid_expiry(self):
        feed = BinancePriceFeed()
        mm = MarketMaker(feed)
        market = _make_market(expiry="not-a-date")
        assert mm._is_near_resolution(market) is False


# ── Stats ────────────────────────────────────────────────────────

class TestMMStats:
    def test_stats_dict_keys(self):
        feed = BinancePriceFeed()
        mm = MarketMaker(feed)
        stats = mm.get_stats()
        required_keys = [
            "total_capital", "total_fills", "total_spread_captured",
            "total_merged_profit", "total_rebates", "total_pnl",
            "markets_active", "quote_updates", "preemptive_cancels",
            "merges_completed", "halted", "markets",
        ]
        for key in required_keys:
            assert key in stats, f"Missing key: {key}"

    def test_market_stats_includes_asset(self):
        feed = BinancePriceFeed()
        mm = MarketMaker(feed)
        market = _make_market(asset="ETH")
        mm._markets["cond_abc"] = market
        stats = mm.get_stats()
        assert stats["markets"]["cond_abc"]["asset"] == "ETH"

    def test_total_allocated(self):
        feed = BinancePriceFeed()
        mm = MarketMaker(feed)
        m1 = _make_market(condition_id="c1")
        m1.yes_cost = 100.0
        m1.no_cost = 50.0
        m2 = _make_market(condition_id="c2")
        m2.yes_cost = 200.0
        m2.no_cost = 75.0
        mm._markets["c1"] = m1
        mm._markets["c2"] = m2
        assert mm._total_allocated() == 425.0
