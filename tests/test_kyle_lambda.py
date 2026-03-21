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
        est._trades["0xtest"] = []
        for i in range(20):
            est._trades["0xtest"].append((now - 800 + i * 40, 0.50 + i * 0.001, 0.0, "buy"))
        result = est._compute_lambda("0xtest", 900)
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
