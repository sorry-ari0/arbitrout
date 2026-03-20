"""Tests for Polymarket WebSocket price feed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import time
from positions.polymarket_ws import PolymarketPriceFeed, PRICE_STALE_SECONDS


class TestPriceFeed:
    def test_get_price_returns_none_when_not_tracked(self):
        feed = PolymarketPriceFeed()
        assert feed.get_price("unknown_id") is None

    def test_get_price_returns_value_after_update(self):
        feed = PolymarketPriceFeed()
        feed._prices["abc123"] = 0.55
        feed._updated_at["abc123"] = time.time()
        assert feed.get_price("abc123") == 0.55

    def test_get_price_returns_none_when_stale(self):
        feed = PolymarketPriceFeed()
        feed._prices["abc123"] = 0.55
        feed._updated_at["abc123"] = time.time() - PRICE_STALE_SECONDS - 1
        assert feed.get_price("abc123") is None

    def test_subscribe_adds_ids(self):
        feed = PolymarketPriceFeed()
        feed.subscribe(["cid_1", "cid_2"])
        assert "cid_1" in feed._subscribed
        assert "cid_2" in feed._subscribed
        assert feed.tracked_count == 2

    def test_unsubscribe_removes_ids(self):
        feed = PolymarketPriceFeed()
        feed.subscribe(["cid_1", "cid_2"])
        feed.unsubscribe(["cid_1"])
        assert "cid_1" not in feed._subscribed
        assert "cid_2" in feed._subscribed

    def test_handle_price_change_message(self):
        feed = PolymarketPriceFeed()
        feed._handle_message({
            "event_type": "price_change",
            "asset_id": "test_asset",
            "price": "0.65",
        })
        assert feed._prices["test_asset"] == 0.65

    def test_handle_trade_message(self):
        feed = PolymarketPriceFeed()
        feed._handle_message({
            "event_type": "trade",
            "asset_id": "test_asset",
            "price": "0.42",
        })
        assert feed._prices["test_asset"] == 0.42

    def test_ignores_invalid_prices(self):
        feed = PolymarketPriceFeed()
        # Price > 1 should be ignored
        feed._handle_message({
            "event_type": "price_change",
            "asset_id": "test",
            "price": "1.5",
        })
        assert "test" not in feed._prices

        # Price <= 0 should be ignored
        feed._handle_message({
            "event_type": "price_change",
            "asset_id": "test",
            "price": "0",
        })
        assert "test" not in feed._prices

    def test_get_prices_filters_stale(self):
        feed = PolymarketPriceFeed()
        feed._prices["fresh"] = 0.50
        feed._updated_at["fresh"] = time.time()
        feed._prices["stale"] = 0.60
        feed._updated_at["stale"] = time.time() - PRICE_STALE_SECONDS - 10
        prices = feed.get_prices()
        assert "fresh" in prices
        assert "stale" not in prices

    def test_get_stats(self):
        feed = PolymarketPriceFeed()
        feed.subscribe(["a", "b", "c"])
        feed._prices["a"] = 0.50
        feed._updated_at["a"] = time.time()
        stats = feed.get_stats()
        assert stats["tracked"] == 3
        assert stats["cached_prices"] == 1
        assert stats["fresh_prices"] == 1
        assert stats["connected"] is False

    def test_on_price_callback(self):
        feed = PolymarketPriceFeed()
        received = []
        feed.on_price(lambda aid, p, t: received.append((aid, p)))
        feed._handle_message({
            "event_type": "price_change",
            "asset_id": "test",
            "price": "0.75",
        })
        assert len(received) == 1
        assert received[0] == ("test", 0.75)
