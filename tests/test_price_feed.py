"""Tests for BinancePriceFeed — signal computation, multi-asset state, on_tick callbacks."""
import asyncio
import time
from collections import deque

import pytest

from positions.price_feed import (
    BinancePriceFeed, Candle, WindowState, SniperSignal, AssetState,
    SUPPORTED_ASSETS,
)


# ── Multi-asset init ─────────────────────────────────────────────

class TestMultiAssetInit:
    def test_single_asset_default(self):
        feed = BinancePriceFeed()
        assert feed.active_assets == ["BTC"]
        assert "BTC" in feed._state

    def test_multi_asset_init(self):
        feed = BinancePriceFeed(assets=["BTC", "ETH", "SOL"])
        assert feed.active_assets == ["BTC", "ETH", "SOL"]
        assert len(feed._state) == 3
        for sym in ["BTC", "ETH", "SOL"]:
            assert isinstance(feed._state[sym], AssetState)

    def test_case_insensitive(self):
        feed = BinancePriceFeed(assets=["btc", "Eth"])
        assert feed.active_assets == ["BTC", "ETH"]


# ── Backward-compatible BTC properties ───────────────────────────

class TestBackwardCompat:
    def test_price_returns_btc(self):
        feed = BinancePriceFeed(assets=["BTC", "ETH"])
        feed._state["BTC"].price = 100000.0
        feed._state["ETH"].price = 3500.0
        assert feed.price == 100000.0

    def test_is_stale_checks_btc(self):
        feed = BinancePriceFeed()
        assert feed.is_stale is True  # No data yet

        feed._state["BTC"].price_time = time.time()
        assert feed.is_stale is False

    def test_candles_returns_btc(self):
        feed = BinancePriceFeed(assets=["BTC", "ETH"])
        c = Candle(open_time=1.0, open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0, closed=True)
        feed._state["BTC"].candles.append(c)
        assert len(feed.candles) == 1
        assert feed.candles[0].close == 100.5


# ── Per-asset access ─────────────────────────────────────────────

class TestPerAssetAccess:
    def test_get_price(self):
        feed = BinancePriceFeed(assets=["BTC", "ETH"])
        feed._state["ETH"].price = 3500.0
        assert feed.get_price("ETH") == 3500.0
        assert feed.get_price("XRP") == 0.0  # Not tracked

    def test_is_asset_stale(self):
        feed = BinancePriceFeed(assets=["BTC", "ETH"])
        feed._state["BTC"].price_time = time.time()
        feed._state["ETH"].price_time = time.time() - 10
        assert feed.is_asset_stale("BTC") is False
        assert feed.is_asset_stale("ETH") is True
        assert feed.is_asset_stale("DOGE") is True  # Not tracked

    def test_get_asset_returns_none_for_unknown(self):
        feed = BinancePriceFeed()
        assert feed.get_asset("DOGE") is None


# ── on_tick callbacks ────────────────────────────────────────────

class TestOnTickCallbacks:
    def test_register_and_fire(self):
        feed = BinancePriceFeed()
        calls = []
        feed.on_tick(lambda a, p, t: calls.append((a, p, t)))

        feed._process_tick("BTC", 100000.0, 1.0)
        assert len(calls) == 1
        assert calls[0] == ("BTC", 100000.0, 1.0)

    def test_remove_callback(self):
        feed = BinancePriceFeed()
        calls = []
        cb = lambda a, p, t: calls.append(1)
        feed.on_tick(cb)
        feed._process_tick("BTC", 100000.0, 1.0)
        assert len(calls) == 1

        feed.remove_on_tick(cb)
        feed._process_tick("BTC", 100001.0, 2.0)
        assert len(calls) == 1  # No new call

    def test_callback_exception_does_not_crash(self):
        feed = BinancePriceFeed()
        feed.on_tick(lambda a, p, t: 1 / 0)  # ZeroDivisionError
        # Should not raise
        feed._process_tick("BTC", 100000.0, 1.0)
        assert feed._state["BTC"].price == 100000.0

    def test_multiple_callbacks(self):
        feed = BinancePriceFeed()
        calls_a, calls_b = [], []
        feed.on_tick(lambda a, p, t: calls_a.append(p))
        feed.on_tick(lambda a, p, t: calls_b.append(p))
        feed._process_tick("BTC", 50000.0, 1.0)
        assert len(calls_a) == 1
        assert len(calls_b) == 1


# ── _process_tick ────────────────────────────────────────────────

class TestProcessTick:
    def test_updates_price_and_time(self):
        feed = BinancePriceFeed()
        before = time.time()
        feed._process_tick("BTC", 99000.0, 1.0)
        assert feed._state["BTC"].price == 99000.0
        assert feed._state["BTC"].price_time >= before

    def test_updates_window_state(self):
        feed = BinancePriceFeed()
        feed._process_tick("BTC", 100000.0, 1.0)
        window = feed.get_current_window("BTC")
        assert window is not None
        assert window.current_price == 100000.0

        feed._process_tick("BTC", 100500.0, 2.0)
        assert window.high == 100500.0
        assert window.tick_count == 2

    def test_ignores_zero_price(self):
        feed = BinancePriceFeed()
        feed._process_tick("BTC", 0.0, 1.0)
        assert feed._state["BTC"].price == 0.0

    def test_ignores_unknown_asset(self):
        feed = BinancePriceFeed(assets=["BTC"])
        feed._process_tick("DOGE", 1.0, 1.0)
        assert "DOGE" not in feed._state

    def test_tick_sampling_every_2_seconds(self):
        feed = BinancePriceFeed()
        feed._state["BTC"].last_sample_time = time.time() - 3  # 3s ago
        feed._process_tick("BTC", 100000.0, 1.0)
        assert len(feed._state["BTC"].tick_samples) == 1

        # Immediate second tick — should NOT add sample
        feed._process_tick("BTC", 100001.0, 2.0)
        assert len(feed._state["BTC"].tick_samples) == 1


# ── Window management ────────────────────────────────────────────

class TestWindowManagement:
    def test_creates_window(self):
        feed = BinancePriceFeed()
        feed._state["BTC"].price = 100000.0
        window = feed.get_current_window("BTC")
        assert window is not None
        assert window.open_price == 100000.0

    def test_no_window_without_price(self):
        feed = BinancePriceFeed()
        assert feed.get_current_window("BTC") is None

    def test_window_for_unknown_asset(self):
        feed = BinancePriceFeed(assets=["BTC"])
        assert feed.get_current_window("ETH") is None

    def test_seconds_until_window_close(self):
        feed = BinancePriceFeed()
        remaining = feed.seconds_until_window_close()
        assert 0 <= remaining <= 300

    def test_window_slug_btc(self):
        feed = BinancePriceFeed()
        slug = feed.current_window_slug("BTC")
        assert slug.startswith("btc-updown-5m-")

    def test_window_slug_eth(self):
        feed = BinancePriceFeed(assets=["ETH"])
        slug = feed.current_window_slug("ETH")
        assert slug.startswith("eth-updown-5m-")


# ── Signal computation ───────────────────────────────────────────

class TestSniperSignal:
    def _setup_feed(self, asset="BTC", open_price=100000.0, current_price=100100.0):
        """Helper to create a feed with window state for signal computation."""
        feed = BinancePriceFeed(assets=[asset])
        state = feed._state[asset]
        state.price = current_price
        state.price_time = time.time()

        now = time.time()
        window_ts = int(now) - (int(now) % 300)
        state.window = WindowState(
            window_ts=window_ts,
            open_price=open_price,
            current_price=current_price,
            high=max(open_price, current_price),
            low=min(open_price, current_price),
        )
        return feed

    def test_returns_none_without_data(self):
        feed = BinancePriceFeed()
        assert feed.compute_sniper_signal("BTC") is None

    def test_returns_none_for_unknown_asset(self):
        feed = BinancePriceFeed(assets=["BTC"])
        assert feed.compute_sniper_signal("DOGE") is None

    def test_up_signal_on_price_increase(self):
        # 0.1% increase → should be UP with decent confidence
        feed = self._setup_feed(open_price=100000.0, current_price=100100.0)
        signal = feed.compute_sniper_signal("BTC")
        assert signal is not None
        assert signal.direction == "UP"
        assert signal.confidence > 0
        assert signal.window_delta_pct > 0
        assert signal.asset == "BTC"

    def test_down_signal_on_price_decrease(self):
        feed = self._setup_feed(open_price=100000.0, current_price=99900.0)
        signal = feed.compute_sniper_signal("BTC")
        assert signal is not None
        assert signal.direction == "DOWN"
        assert signal.window_delta_pct < 0

    def test_zero_confidence_on_flat_price(self):
        feed = self._setup_feed(open_price=100000.0, current_price=100000.0)
        signal = feed.compute_sniper_signal("BTC")
        assert signal is not None
        assert signal.confidence == 0.0

    def test_high_confidence_on_large_move(self):
        # 0.15% move → max delta weight (7.0)
        feed = self._setup_feed(open_price=100000.0, current_price=100150.0)
        signal = feed.compute_sniper_signal("BTC")
        assert signal.confidence >= 0.9

    def test_micro_momentum_bullish(self):
        feed = self._setup_feed(open_price=100000.0, current_price=100050.0)
        state = feed._state["BTC"]
        # Add two bullish closed candles
        state.candles.append(Candle(1.0, 100.0, 102.0, 99.0, 101.0, 10.0, True))
        state.candles.append(Candle(2.0, 101.0, 103.0, 100.0, 102.0, 10.0, True))
        signal = feed.compute_sniper_signal("BTC")
        assert "micro_momentum" in signal.components
        assert signal.components["micro_momentum"] == 2.0  # Both bullish

    def test_micro_momentum_bearish(self):
        feed = self._setup_feed(open_price=100000.0, current_price=99950.0)
        state = feed._state["BTC"]
        state.candles.append(Candle(1.0, 102.0, 102.0, 99.0, 100.0, 10.0, True))
        state.candles.append(Candle(2.0, 101.0, 101.0, 98.0, 99.0, 10.0, True))
        signal = feed.compute_sniper_signal("BTC")
        assert signal.components["micro_momentum"] == -2.0

    def test_tick_trend_signal(self):
        feed = self._setup_feed(open_price=100000.0, current_price=100050.0)
        state = feed._state["BTC"]
        # Add tick samples showing upward trend (>0.01% over window)
        base_time = time.time() - 10
        state.tick_samples.append((base_time, 100000.0))
        state.tick_samples.append((base_time + 2, 100005.0))
        state.tick_samples.append((base_time + 4, 100015.0))
        signal = feed.compute_sniper_signal("BTC")
        assert "tick_trend" in signal.components
        assert signal.components["tick_trend"] > 0

    def test_signal_includes_asset_field(self):
        feed = self._setup_feed(asset="ETH", open_price=3500.0, current_price=3510.0)
        signal = feed.compute_sniper_signal("ETH")
        assert signal.asset == "ETH"


# ── Combined stream URL ─────────────────────────────────────────

class TestStreamURL:
    def test_single_asset_url(self):
        feed = BinancePriceFeed(assets=["BTC"])
        url = feed._build_combined_stream_url("trade")
        assert "btcusdt@trade" in url
        assert "stream?streams=" not in url  # Single stream, not combined

    def test_multi_asset_combined_url(self):
        feed = BinancePriceFeed(assets=["BTC", "ETH"])
        url = feed._build_combined_stream_url("trade")
        assert "stream?streams=" in url
        assert "btcusdt@trade" in url
        assert "ethusdt@trade" in url

    def test_kline_url(self):
        feed = BinancePriceFeed(assets=["BTC"])
        url = feed._build_combined_stream_url("kline_1m")
        assert "btcusdt@kline_1m" in url


# ── Symbol resolution ────────────────────────────────────────────

class TestSymbolResolution:
    def test_resolve_btcusdt(self):
        feed = BinancePriceFeed()
        assert feed._resolve_asset_from_symbol("BTCUSDT") == "BTC"

    def test_resolve_ethusdt(self):
        feed = BinancePriceFeed(assets=["ETH"])
        assert feed._resolve_asset_from_symbol("ETHUSDT") == "ETH"

    def test_resolve_unknown(self):
        feed = BinancePriceFeed()
        assert feed._resolve_asset_from_symbol("DOGEUSDT") is None

    def test_case_insensitive(self):
        feed = BinancePriceFeed()
        assert feed._resolve_asset_from_symbol("btcusdt") == "BTC"
