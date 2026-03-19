"""Tests for political LRU cache with TTL and price-shift invalidation (Task 5)."""
import sys
import time
from pathlib import Path
from unittest.mock import patch

# Add src to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from political.cache import PoliticalCache, PRICE_SHIFT_THRESHOLD


class TestPoliticalCache:
    """Tests for PoliticalCache."""

    def test_set_and_get(self):
        """Basic store and retrieve returns cached data."""
        cache = PoliticalCache(ttl_seconds=60)
        ids = ["contract-a", "contract-b"]
        prices = {"contract-a": 0.55, "contract-b": 0.45}
        data = {"strategy": "hedge", "ev": 5.2}

        cache.set(ids, data, prices)
        result = cache.get(ids, prices)

        assert result == data
        assert len(cache) == 1

    def test_ttl_expiry(self):
        """Cache entry with ttl_seconds=0 expires immediately."""
        cache = PoliticalCache(ttl_seconds=0)
        ids = ["contract-a"]
        prices = {"contract-a": 0.50}
        data = {"strategy": "momentum"}

        cache.set(ids, data, prices)
        # Even with ttl=0, monotonic time will have advanced
        result = cache.get(ids, prices)

        assert result is None
        assert len(cache) == 0  # expired entry was deleted

    def test_price_shift_invalidation(self):
        """Price change >3% invalidates the cache entry."""
        cache = PoliticalCache(ttl_seconds=600)
        ids = ["contract-a"]
        original_price = 0.50
        prices = {"contract-a": original_price}
        data = {"strategy": "arb"}

        cache.set(ids, data, prices)

        # Shift price by more than 3% (4% shift)
        shifted_prices = {"contract-a": original_price * 1.04}
        result = cache.get(ids, shifted_prices)

        assert result is None
        assert len(cache) == 0  # invalidated entry was deleted

    def test_price_within_threshold(self):
        """Price change <=3% keeps the cache entry valid."""
        cache = PoliticalCache(ttl_seconds=600)
        ids = ["contract-a"]
        original_price = 0.50
        prices = {"contract-a": original_price}
        data = {"strategy": "spread"}

        cache.set(ids, data, prices)

        # Shift price by exactly 2% (within threshold)
        shifted_prices = {"contract-a": original_price * 1.02}
        result = cache.get(ids, shifted_prices)

        assert result == data
        assert len(cache) == 1

    def test_lru_eviction(self):
        """When max_entries exceeded, oldest entry is evicted."""
        cache = PoliticalCache(ttl_seconds=600, max_entries=3)

        for i in range(4):
            ids = [f"contract-{i}"]
            cache.set(ids, {"idx": i}, {f"contract-{i}": 0.50})

        # Should have 3 entries (0 was evicted)
        assert len(cache) == 3

        # Oldest (contract-0) should be gone
        result = cache.get(["contract-0"], {"contract-0": 0.50})
        assert result is None

        # Newest three should still be present
        for i in [1, 2, 3]:
            result = cache.get([f"contract-{i}"], {f"contract-{i}": 0.50})
            assert result == {"idx": i}

    def test_cache_key_order_independent(self):
        """["id1","id2"] produces the same cache key as ["id2","id1"]."""
        cache = PoliticalCache(ttl_seconds=600)
        prices = {"id1": 0.50, "id2": 0.50}
        data = {"strategy": "pair"}

        # Store with one order
        cache.set(["id1", "id2"], data, prices)

        # Retrieve with reversed order
        result = cache.get(["id2", "id1"], prices)

        assert result == data
        assert len(cache) == 1

    def test_clear(self):
        """clear() removes all entries."""
        cache = PoliticalCache()
        cache.set(["a"], {"x": 1}, {"a": 0.5})
        cache.set(["b"], {"y": 2}, {"b": 0.6})
        assert len(cache) == 2

        cache.clear()
        assert len(cache) == 0

    def test_price_shift_threshold_constant(self):
        """PRICE_SHIFT_THRESHOLD is 0.03 (3%)."""
        assert PRICE_SHIFT_THRESHOLD == 0.03
