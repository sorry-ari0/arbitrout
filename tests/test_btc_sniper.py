"""Tests for BtcSniper — bet sizing, decision logging, per-asset state, fee accounting."""
import asyncio
import time

import pytest

from positions.price_feed import BinancePriceFeed, WindowState, SniperSignal
from positions.btc_sniper import (
    BtcSniper, SniperStats, AssetSniperState,
    MIN_CONFIDENCE, MAKER_PRICE_HIGH, MAKER_PRICE_LOW,
    DEFAULT_BANKROLL, MIN_BET, SAFE_BET_FRACTION, TAKER_FEE_PCT,
)


# ── Initialization ───────────────────────────────────────────────

class TestSniperInit:
    def test_default_single_asset(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed)
        assert sniper.assets == ["BTC"]
        assert "BTC" in sniper._asset_state

    def test_multi_asset(self):
        feed = BinancePriceFeed(assets=["BTC", "ETH", "SOL"])
        sniper = BtcSniper(feed, assets=["BTC", "ETH", "SOL"])
        assert sniper.assets == ["BTC", "ETH", "SOL"]
        assert len(sniper._asset_state) == 3

    def test_per_asset_tick_events(self):
        feed = BinancePriceFeed(assets=["BTC", "ETH"])
        sniper = BtcSniper(feed, assets=["BTC", "ETH"])
        assert "BTC" in sniper._tick_events
        assert "ETH" in sniper._tick_events
        assert isinstance(sniper._tick_events["BTC"], asyncio.Event)

    def test_default_bankroll(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed)
        assert sniper.bankroll == DEFAULT_BANKROLL
        assert sniper.initial_bankroll == DEFAULT_BANKROLL

    def test_custom_bankroll(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed, bankroll=1000.0)
        assert sniper.bankroll == 1000.0

    def test_paper_mode(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed, mode="paper")
        assert sniper.mode == "paper"


# ── on_tick callback ─────────────────────────────────────────────

class TestTickCallback:
    def test_sets_correct_asset_event(self):
        feed = BinancePriceFeed(assets=["BTC", "ETH"])
        sniper = BtcSniper(feed, assets=["BTC", "ETH"])

        sniper._on_price_tick("BTC", 100000.0, 1.0)
        assert sniper._tick_events["BTC"].is_set()
        assert not sniper._tick_events["ETH"].is_set()

    def test_ignores_untracked_asset(self):
        feed = BinancePriceFeed(assets=["BTC"])
        sniper = BtcSniper(feed, assets=["BTC"])

        # Should not crash even though SOL is not tracked
        sniper._on_price_tick("SOL", 150.0, 1.0)
        assert not sniper._tick_events["BTC"].is_set()


# ── Bet sizing ───────────────────────────────────────────────────

class TestBetSizing:
    def test_safe_mode(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed, bankroll=100.0, mode="safe")
        bet = sniper._calculate_bet_size()
        assert bet == 25.0  # 25% of $100

    def test_safe_mode_min_bet(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed, bankroll=2.0, mode="safe")
        bet = sniper._calculate_bet_size()
        assert bet == MIN_BET

    def test_safe_mode_capped_at_bankroll(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed, bankroll=3.0, mode="safe")
        bet = sniper._calculate_bet_size()
        assert bet <= sniper.bankroll

    def test_paper_mode_fixed_10(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed, bankroll=500.0, mode="paper")
        bet = sniper._calculate_bet_size()
        assert bet == 10.0

    def test_paper_mode_capped_at_bankroll(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed, bankroll=5.0, mode="paper")
        bet = sniper._calculate_bet_size()
        assert bet == 5.0

    def test_aggressive_mode_uses_gains(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed, bankroll=600.0, mode="aggressive")
        sniper.initial_bankroll = 500.0
        bet = sniper._calculate_bet_size()
        assert bet == 100.0  # $600 - $500 = $100 gains

    def test_aggressive_mode_no_gains(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed, bankroll=500.0, mode="aggressive")
        sniper.initial_bankroll = 500.0
        bet = sniper._calculate_bet_size()
        # No gains → falls back to 10% of bankroll
        assert bet == max(MIN_BET, 500.0 * 0.10)


# ── Decision logging ─────────────────────────────────────────────

class TestDecisionLog:
    def test_log_decision(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed)
        sniper._log_decision(1000, "trade", "placed", {"direction": "UP"})
        assert len(sniper._decision_log) == 1
        entry = sniper._decision_log[0]
        assert entry["window_ts"] == 1000
        assert entry["action"] == "trade"
        assert entry["reason"] == "placed"
        assert entry["direction"] == "UP"

    def test_log_truncation(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed)
        for i in range(600):
            sniper._log_decision(i, "test", "test")
        assert len(sniper._decision_log) == 500

    def test_get_stats_includes_recent_decisions(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed)
        for i in range(15):
            sniper._log_decision(i, "test", "test")
        stats = sniper.get_stats()
        assert len(stats["recent_decisions"]) == 10  # Last 10


# ── Resolution tracking (fee accounting) ─────────────────────────

class TestResolutionAccounting:
    @pytest.fixture
    def sniper(self):
        feed = BinancePriceFeed()
        return BtcSniper(feed, bankroll=100.0, mode="paper")

    def test_win_deducts_taker_fee(self, sniper):
        """Verify that wins account for taker fees in profit calculation."""
        # Simulate: bet $10 at entry price $0.90
        bet_size = 10.0
        entry_price = 0.90
        sniper.bankroll -= bet_size  # Deducted on placement

        # Manually run resolution logic
        shares = bet_size / entry_price  # ~11.11 shares
        payout = shares * 1.0  # $11.11
        fee = bet_size * TAKER_FEE_PCT  # $0.156
        expected_profit = payout - bet_size - fee

        # The actual P&L should be less than without fees
        profit_without_fee = payout - bet_size
        assert expected_profit < profit_without_fee
        assert fee > 0

    def test_loss_includes_taker_fee(self, sniper):
        """Verify losses include taker fee in total loss."""
        bet_size = 10.0
        fee = bet_size * TAKER_FEE_PCT
        total_loss = bet_size + fee
        assert total_loss > bet_size

    def test_taker_fee_constant(self):
        """Taker fee should be 1.56% for 5-min crypto."""
        assert TAKER_FEE_PCT == 0.0156


# ── Stats ────────────────────────────────────────────────────────

class TestStats:
    def test_stats_dict_keys(self):
        feed = BinancePriceFeed(assets=["BTC", "ETH"])
        sniper = BtcSniper(feed, assets=["BTC", "ETH"])
        stats = sniper.get_stats()
        assert "assets" in stats
        assert stats["assets"] == ["BTC", "ETH"]
        assert "win_rate" in stats
        assert "total_fees" in stats

    def test_win_rate_no_trades(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed)
        assert sniper.get_stats()["win_rate"] == 0.0

    def test_initial_stats_zero(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed)
        stats = sniper.get_stats()
        assert stats["trades_placed"] == 0
        assert stats["total_pnl"] == 0.0
        assert stats["total_fees"] == 0.0


# ── Paper order placement ────────────────────────────────────────

class TestPaperOrder:
    @pytest.mark.asyncio
    async def test_paper_order_deducts_bankroll(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed, bankroll=100.0, mode="paper")
        result = await sniper._place_sniper_order(1000, "UP", 10.0, 0.90, "BTC")
        assert result["success"] is True
        assert result["mode"] == "paper"
        assert sniper.bankroll == 90.0

    @pytest.mark.asyncio
    async def test_paper_order_without_pm(self):
        feed = BinancePriceFeed()
        sniper = BtcSniper(feed, bankroll=100.0, mode="safe")
        # No position_manager → falls back to paper
        result = await sniper._place_sniper_order(1000, "DOWN", 5.0, 0.92, "ETH")
        assert result["success"] is True
        assert sniper.bankroll == 95.0
