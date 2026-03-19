"""Political synthetic derivative cache.

SHA-256 keyed LRU cache with configurable TTL and price-shift invalidation.
Used to avoid redundant LLM calls for strategy generation when the same
set of contracts is re-evaluated and prices haven't moved significantly.
"""
import hashlib
import time
from collections import OrderedDict

PRICE_SHIFT_THRESHOLD = 0.03  # 3% — invalidate cache if any price shifts more


class PoliticalCache:
    """LRU cache with TTL and price-shift invalidation for political analysis."""

    def __init__(self, ttl_seconds: int = 900, max_entries: int = 200):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: OrderedDict[str, dict] = OrderedDict()

    # ------------------------------------------------------------------
    # Key generation
    # ------------------------------------------------------------------
    @staticmethod
    def _make_key(contract_ids: list[str]) -> str:
        """SHA-256 hash of sorted, comma-joined contract IDs, truncated to 16 hex chars."""
        joined = ",".join(sorted(contract_ids))
        return hashlib.sha256(joined.encode()).hexdigest()[:16]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get(self, contract_ids: list[str], current_prices: dict[str, float]) -> dict | None:
        """Retrieve cached data if still valid.

        Returns None (and deletes entry) if:
          - key not found
          - TTL expired
          - any price shifted > PRICE_SHIFT_THRESHOLD from cached price
        Otherwise returns cached data and promotes entry to most-recently-used.
        """
        key = self._make_key(contract_ids)
        entry = self._store.get(key)
        if entry is None:
            return None

        # TTL check
        if (time.monotonic() - entry["created_at"]) > self._ttl:
            del self._store[key]
            return None

        # Price-shift check
        cached_prices: dict[str, float] = entry["prices"]
        for cid, cached_price in cached_prices.items():
            current = current_prices.get(cid)
            if current is None:
                continue
            if cached_price == 0:
                # Avoid division by zero; any non-zero current price is a shift
                if current != 0:
                    del self._store[key]
                    return None
                continue
            shift = abs(current - cached_price) / cached_price
            if shift > PRICE_SHIFT_THRESHOLD:
                del self._store[key]
                return None

        # Valid hit — promote to most-recently-used
        self._store.move_to_end(key)
        return entry["data"]

    def set(self, contract_ids: list[str], data: dict, prices: dict[str, float]) -> None:
        """Store data with associated prices and timestamp.

        Evicts oldest entries when cache exceeds max_entries.
        """
        key = self._make_key(contract_ids)
        self._store[key] = {
            "data": data,
            "prices": dict(prices),
            "created_at": time.monotonic(),
        }
        self._store.move_to_end(key)

        # Evict oldest entries if over capacity
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def clear(self) -> None:
        """Remove all entries."""
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
