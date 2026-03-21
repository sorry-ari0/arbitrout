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
