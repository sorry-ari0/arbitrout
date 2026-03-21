"""Tests for Kyle's lambda estimator and trade callback infrastructure."""
import time
import pytest
from unittest.mock import MagicMock


class TestTradeCallback:
    """Task 1: PolymarketPriceFeed trade callback channel."""

    def test_on_trade_callback_fires_for_trade_event(self):
        """Trade events with size should fire on_trade callbacks."""
        from positions.polymarket_ws import PolymarketPriceFeed
        feed = PolymarketPriceFeed()
        received = []
        feed.on_trade(lambda asset_id, price, size, ts, side: received.append(
            (asset_id, price, size, side)
        ))
        feed._handle_message({
            "event_type": "trade",
            "asset_id": "0xabc",
            "price": "0.55",
            "size": "100.0",
            "side": "BUY",
        })
        assert len(received) == 1
        assert received[0] == ("0xabc", 0.55, 100.0, "buy")

    def test_on_trade_skipped_without_size(self):
        """Trade events missing size should NOT fire on_trade callbacks."""
        from positions.polymarket_ws import PolymarketPriceFeed
        feed = PolymarketPriceFeed()
        received = []
        feed.on_trade(lambda asset_id, price, size, ts, side: received.append(1))
        feed._handle_message({
            "event_type": "trade",
            "asset_id": "0xabc",
            "price": "0.55",
        })
        assert len(received) == 0

    def test_on_trade_still_fires_on_price(self):
        """Trade events should still fire on_price callbacks (no regression)."""
        from positions.polymarket_ws import PolymarketPriceFeed
        feed = PolymarketPriceFeed()
        prices = []
        feed.on_price(lambda asset_id, price, ts: prices.append(price))
        feed._handle_message({
            "event_type": "trade",
            "asset_id": "0xabc",
            "price": "0.55",
            "size": "100.0",
        })
        assert len(prices) == 1
        assert prices[0] == 0.55

    def test_on_trade_tries_amount_field(self):
        """Should fall back to 'amount' if 'size' is absent."""
        from positions.polymarket_ws import PolymarketPriceFeed
        feed = PolymarketPriceFeed()
        received = []
        feed.on_trade(lambda asset_id, price, size, ts, side: received.append(size))
        feed._handle_message({
            "event_type": "trade",
            "asset_id": "0xabc",
            "price": "0.60",
            "amount": "50.0",
        })
        assert len(received) == 1
        assert received[0] == 50.0

    def test_on_trade_normalizes_side(self):
        """Side should be normalized to lowercase."""
        from positions.polymarket_ws import PolymarketPriceFeed
        feed = PolymarketPriceFeed()
        received = []
        feed.on_trade(lambda asset_id, price, size, ts, side: received.append(side))
        feed._handle_message({
            "event_type": "trade",
            "asset_id": "0xabc",
            "price": "0.55",
            "size": "10",
            "side": "SELL",
        })
        assert received[0] == "sell"

    def test_on_trade_defaults_side_to_unknown(self):
        """Missing side field should default to 'unknown'."""
        from positions.polymarket_ws import PolymarketPriceFeed
        feed = PolymarketPriceFeed()
        received = []
        feed.on_trade(lambda asset_id, price, size, ts, side: received.append(side))
        feed._handle_message({
            "event_type": "trade",
            "asset_id": "0xabc",
            "price": "0.55",
            "size": "10",
        })
        assert received[0] == "unknown"


class TestLambdaComputation:
    """Task 2: Trade buffer and OLS lambda computation."""

    def test_compute_lambda_known_values(self):
        """Known linear relationship: price moves 0.01 per unit volume."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        price = 0.50
        for i in range(20):
            price += 0.01
            est.on_trade(f"0xtest", price, 10.0, now - 800 + i * 40, "buy")
        result = est._compute_lambda("0xtest", 900)
        assert result is not None
        lam, n = result
        assert abs(lam - 0.001) < 0.0001
        assert n == 20

    def test_compute_lambda_insufficient_data(self):
        """Fewer than min trades should return None."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        for i in range(5):
            est.on_trade("0xtest", 0.50 + i * 0.01, 10.0, now - 100 + i * 10, "buy")
        result = est._compute_lambda("0xtest", 900)
        assert result is None

    def test_compute_lambda_sell_side(self):
        """Sells should produce negative signed volume, still computable."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        price = 0.60
        for i in range(15):
            price -= 0.005
            est.on_trade("0xtest", price, 8.0, now - 700 + i * 40, "sell")
        result = est._compute_lambda("0xtest", 900)
        assert result is not None
        lam, n = result
        assert lam > 0

    def test_buffer_maxlen_bounded(self):
        """Buffer should not exceed maxlen."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        for i in range(6000):
            est.on_trade("0xtest", 0.50, 1.0, now - 6000 + i, "buy")
        assert len(est._trades["0xtest"]) <= 5000

    def test_time_pruning(self):
        """Old trades (>2hr) should be pruned on access."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        for i in range(20):
            est.on_trade("0xtest", 0.50 + i * 0.001, 5.0, now - 10800 + i, "buy")
        for i in range(15):
            est.on_trade("0xtest", 0.55 + i * 0.001, 5.0, now - 300 + i * 10, "buy")
        result = est._compute_lambda("0xtest", 7200)
        assert result is not None
        _, n = result
        assert n == 15

    def test_zero_volume_returns_none(self):
        """All zero-volume trades should return None (division by zero guard)."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        for i in range(20):
            est.on_trade("0xtest", 0.50 + i * 0.001, 0.0, now - 800 + i * 40, "buy")
        result = est._compute_lambda("0xtest", 900)
        assert result is None

    def test_long_window_requires_30_trades(self):
        """Long window with 10-29 trades should return None (needs 30 min)."""
        from positions.kyle_lambda import KyleLambdaEstimator, LONG_WINDOW_SECONDS, MIN_TRADES_LONG
        est = KyleLambdaEstimator()
        now = time.time()
        for i in range(20):
            est.on_trade("0xtest", 0.50 + i * 0.001, 10.0, now - 3600 + i * 60, "buy")
        result = est._compute_lambda("0xtest", LONG_WINDOW_SECONDS, MIN_TRADES_LONG)
        assert result is None

    def test_unknown_market_returns_none(self):
        """Querying a market with no trades should return None."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        result = est._compute_lambda("0xunknown", 900)
        assert result is None

    def test_side_inference_produces_correct_lambda(self):
        """Unknown-side trades with rising prices should infer buys → positive λ."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est_known = KyleLambdaEstimator()
        est_unknown = KyleLambdaEstimator()
        now = time.time()
        price = 0.50
        for i in range(20):
            price += 0.01
            ts = now - 800 + i * 40
            est_known.on_trade("0xtest", price, 10.0, ts, "buy")
            est_unknown.on_trade("0xtest", price, 10.0, ts, "unknown")
        known_result = est_known._compute_lambda("0xtest", 900)
        unknown_result = est_unknown._compute_lambda("0xtest", 900)
        assert known_result is not None
        assert unknown_result is not None
        known_lam, _ = known_result
        unknown_lam, _ = unknown_result
        assert abs(known_lam - unknown_lam) < 0.0001


class TestDirectionalSignal:
    """Task 3: get_lambda_signal directional multiplier."""

    def _make_estimator_with_spike(self, flow_side="buy"):
        """Create an estimator with a clear λ spike in the short window.

        Long window: gentle price movement (low λ).
        Short window: aggressive price movement (high λ).
        """
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()

        # Long-window background: 2 hours of gentle trades (low λ)
        price = 0.50
        for i in range(50):
            price += 0.0005  # tiny moves
            est.on_trade("0xmarket", price, 10.0,
                         now - 7000 + i * 120, flow_side)

        # Short-window spike: 15 min of aggressive trades (high λ)
        for i in range(20):
            price += 0.005  # big moves (10x the background rate)
            est.on_trade("0xmarket", price, 10.0,
                         now - 800 + i * 30, flow_side)

        return est

    def test_spike_agrees_boosts(self):
        """λ spike with flow matching our direction → multiplier > 1.0."""
        est = self._make_estimator_with_spike(flow_side="buy")
        signal = est.get_lambda_signal("0xmarket", "YES")
        assert signal["sufficient_data"] is True
        assert signal["multiplier"] > 1.0
        assert signal["agrees_with_arb"] is True

    def test_spike_opposes_discounts(self):
        """λ spike with flow opposing our direction → multiplier < 1.0."""
        est = self._make_estimator_with_spike(flow_side="buy")
        # Flow is buying (YES direction), but we want NO
        signal = est.get_lambda_signal("0xmarket", "NO")
        assert signal["sufficient_data"] is True
        assert signal["multiplier"] < 1.0
        assert signal["agrees_with_arb"] is False

    def test_no_spike_neutral(self):
        """No λ spike → multiplier = 1.0 regardless of direction."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        # Uniform trades — no spike
        price = 0.50
        for i in range(60):
            price += 0.001
            est.on_trade("0xmarket", price, 10.0, now - 7000 + i * 100, "buy")

        signal = est.get_lambda_signal("0xmarket", "YES")
        assert signal["multiplier"] == 1.0

    def test_insufficient_data_neutral(self):
        """Not enough trades → neutral multiplier."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        for i in range(5):
            est.on_trade("0xmarket", 0.50 + i * 0.01, 10.0, now - 100 + i * 10, "buy")
        signal = est.get_lambda_signal("0xmarket", "YES")
        assert signal["multiplier"] == 1.0
        assert signal["sufficient_data"] is False

    def test_unknown_market_neutral(self):
        """Unknown market → neutral."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        signal = est.get_lambda_signal("0xnonexistent", "YES")
        assert signal["multiplier"] == 1.0
        assert signal["sufficient_data"] is False

    def test_multiplier_bounds(self):
        """Multiplier should be within [0.4, 1.5]."""
        est = self._make_estimator_with_spike(flow_side="buy")
        agree_signal = est.get_lambda_signal("0xmarket", "YES")
        oppose_signal = est.get_lambda_signal("0xmarket", "NO")
        assert 1.0 <= agree_signal["multiplier"] <= 1.5
        assert 0.4 <= oppose_signal["multiplier"] <= 1.0

    def test_long_lambda_zero_neutral(self):
        """Long λ ≤ 0 → neutral (no meaningful baseline)."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        # Trades where price doesn't move (λ ≈ 0)
        for i in range(60):
            est.on_trade("0xmarket", 0.50, 10.0, now - 7000 + i * 100, "buy")
        signal = est.get_lambda_signal("0xmarket", "YES")
        assert signal["multiplier"] == 1.0

    def test_prefix_match_condition_id(self):
        """Should find trades via condition_id prefix match."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        # Trades stored under full asset_id
        for i in range(40):
            est.on_trade("0xcond123:YES", 0.50 + i * 0.002, 10.0,
                         now - 7000 + i * 150, "buy")
        for i in range(15):
            est.on_trade("0xcond123:YES", 0.58 + i * 0.005, 10.0,
                         now - 800 + i * 40, "buy")

        # Query using condition_id prefix
        signal = est.get_lambda_signal("0xcond123", "YES")
        assert signal["n_trades_short"] > 0 or signal["n_trades_long"] > 0

    def test_signal_return_structure(self):
        """Verify all expected fields are present in the return dict."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        signal = est.get_lambda_signal("0xtest", "YES")
        expected_keys = {
            "multiplier", "short_lambda", "long_lambda", "lambda_ratio",
            "flow_direction", "agrees_with_arb", "n_trades_short",
            "n_trades_long", "sufficient_data",
        }
        assert set(signal.keys()) == expected_keys
