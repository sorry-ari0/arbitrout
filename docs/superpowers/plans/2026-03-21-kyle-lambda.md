# Kyle's Lambda Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a real-time adverse selection signal (Kyle's λ) to the auto-trader scoring pipeline, using Polymarket CLOB WebSocket trade flow with dual rolling windows and directional multiplier.

**Architecture:** New `kyle_lambda.py` module receives trade callbacks from the existing `PolymarketPriceFeed`, buffers trades per market, computes λ via OLS on 15min and 2hr windows, and exposes a `get_lambda_signal()` method returning a directional multiplier (0.4–1.5). Wired into `AutoTrader` via setter pattern, inserted into the scoring loop after cross-platform disagreement boost.

**Tech Stack:** Python 3.11+, pytest, no new dependencies (OLS is sum-of-products).

**Spec:** `docs/superpowers/specs/2026-03-21-kyle-lambda-design.md`

**Spec deviation:** `_compute_lambda()` returns `None` (not `(None, n)` as the spec says) when data is insufficient. This is simpler — the caller just checks `if result is None`. The n_trades count is still available in the signal return dict.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/positions/kyle_lambda.py` | Create | Trade buffer, λ computation, directional signal |
| `src/positions/polymarket_ws.py` | Modify (lines 28-37, 72-74, 167-194) | Add `on_trade` callback channel |
| `src/positions/auto_trader.py` | Modify (lines 67-68, 418-424) | Setter + scoring integration |
| `src/server.py` | Modify (lines 398-416) | Startup wiring |
| `tests/test_kyle_lambda.py` | Create | All λ tests |

---

### Task 1: Trade Callback Channel on PolymarketPriceFeed

**Files:**
- Modify: `src/positions/polymarket_ws.py:28-37` (init), `72-74` (on_price pattern), `167-194` (message handler)
- Test: `tests/test_kyle_lambda.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_kyle_lambda.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/test_kyle_lambda.py::TestTradeCallback -v`
Expected: FAIL — `PolymarketPriceFeed` has no `on_trade` method.

- [ ] **Step 3: Implement the trade callback channel**

In `src/positions/polymarket_ws.py`, make three changes:

**3a. Add `_on_trade_callbacks` to `__init__()` (after line 37):**

```python
        self._on_price_callbacks: list = []
        self._on_trade_callbacks: list = []  # NEW: trade-level callbacks
```

**3b. Add `on_trade()` method (after the `on_price` method, after line 74):**

```python
    def on_trade(self, callback):
        """Register callback for trade events: callback(asset_id, price, size, timestamp, side)."""
        self._on_trade_callbacks.append(callback)
```

**3c. Extend `_handle_message()` to fire trade callbacks (after line 194, inside the `if asset_id and price` block):**

Replace the existing block at lines 185-194:

```python
            if asset_id and price is not None and 0 < price < 1:
                self._prices[asset_id] = price
                self._updated_at[asset_id] = time.time()

                # Fire price callbacks
                for cb in self._on_price_callbacks:
                    try:
                        cb(asset_id, price, time.time())
                    except Exception:
                        pass

                # Fire trade callbacks (only for trade events with size)
                if event_type == "trade" and self._on_trade_callbacks:
                    size = None
                    for size_field in ("size", "amount", "quantity"):
                        val = msg.get(size_field)
                        if val is not None:
                            try:
                                size = float(val)
                                if size > 0:
                                    break
                                size = None
                            except (ValueError, TypeError):
                                pass
                    if size is not None:
                        side_raw = msg.get("side", "unknown")
                        side = side_raw.lower() if isinstance(side_raw, str) else "unknown"
                        now = time.time()
                        for cb in self._on_trade_callbacks:
                            try:
                                cb(asset_id, price, size, now, side)
                            except Exception:
                                pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/test_kyle_lambda.py::TestTradeCallback -v`
Expected: ALL 6 tests PASS.

- [ ] **Step 5: Run existing tests to verify no regression**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/ -v`
Expected: ALL existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/positions/polymarket_ws.py tests/test_kyle_lambda.py
git commit -m "feat: add on_trade callback channel to PolymarketPriceFeed

Trade events with size/amount now fire separate callbacks alongside
the existing on_price callbacks. Needed for Kyle's lambda estimator."
```

---

### Task 2: KyleLambdaEstimator — Trade Buffer & Lambda Computation

**Files:**
- Create: `src/positions/kyle_lambda.py`
- Test: `tests/test_kyle_lambda.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_kyle_lambda.py`:

```python
class TestLambdaComputation:
    """Task 2: Trade buffer and OLS lambda computation."""

    def test_compute_lambda_known_values(self):
        """Known linear relationship: price moves 0.01 per unit volume."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()

        # Generate trades where each buy of size 10 moves price by 0.01
        # Δp = 0.01, Q = 10 → λ = 0.01/10 = 0.001
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

        result = est._compute_lambda("0xtest", 900)  # short window, min 10 trades
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
        assert lam > 0  # λ should be positive (price impact exists)

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
        # Add old trades (3 hours ago)
        for i in range(20):
            est.on_trade("0xtest", 0.50 + i * 0.001, 5.0, now - 10800 + i, "buy")
        # Add recent trades (5 min ago)
        for i in range(15):
            est.on_trade("0xtest", 0.55 + i * 0.001, 5.0, now - 300 + i * 10, "buy")

        # Long window should only see recent trades after pruning
        result = est._compute_lambda("0xtest", 7200)
        assert result is not None
        _, n = result
        assert n == 15  # Only recent trades remain

    def test_zero_volume_returns_none(self):
        """All zero-volume trades should return None (division by zero guard)."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        # This shouldn't happen (on_trade filters size > 0) but test the guard
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
        """Unknown-side trades with rising prices should infer buys → positive λ.

        Tick rule: price up → buy, price down → sell. With all prices rising
        and side='unknown', _compute_lambda should infer all buys and produce
        a positive λ matching the known-side result.
        """
        from positions.kyle_lambda import KyleLambdaEstimator
        # Known-side estimator (explicit "buy")
        est_known = KyleLambdaEstimator()
        # Unknown-side estimator (tick-rule inference)
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
        # Both should produce the same λ since tick rule infers all buys
        assert abs(known_lam - unknown_lam) < 0.0001
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/test_kyle_lambda.py::TestLambdaComputation -v`
Expected: FAIL — `positions.kyle_lambda` does not exist.

- [ ] **Step 3: Implement KyleLambdaEstimator**

Create `src/positions/kyle_lambda.py`:

```python
"""Kyle's Lambda Estimator — real-time adverse selection signal from trade flow.

Estimates Kyle's price impact coefficient (λ) using Polymarket CLOB WebSocket
trade data. High λ indicates informed trading; a short-term λ spike relative
to the long-term baseline signals "smart money just arrived."

Uses dual rolling windows (15min short, 2hr long) and produces a directional
multiplier (0.4–1.5) for the auto-trader's scoring pipeline.

Concurrency: single-threaded asyncio. deque operations are atomic in CPython.
No locks needed — the WS receive loop and scoring loop run on the same event loop.
"""
import logging
import time
from collections import defaultdict, deque

logger = logging.getLogger("positions.kyle_lambda")

# Window durations
SHORT_WINDOW_SECONDS = 900     # 15 minutes
LONG_WINDOW_SECONDS = 7200     # 2 hours

# Minimum trades for reliable λ estimate
MIN_TRADES_SHORT = 10
MIN_TRADES_LONG = 30

# Buffer size per market (bounded memory)
MAX_TRADES_PER_MARKET = 5000

# Spike detection
SPIKE_THRESHOLD_LOW = 1.5    # ratio below this = no spike
SPIKE_THRESHOLD_HIGH = 3.0   # ratio above this = full spike

# Multiplier bounds
AGREE_MIN = 1.15
AGREE_MAX = 1.5
OPPOSE_MIN = 0.4
OPPOSE_MAX = 0.8
MIXED_MULTIPLIER = 0.85

# Flow direction threshold: net must be >10% of total to have direction
FLOW_DIRECTION_THRESHOLD = 0.10


class KyleLambdaEstimator:
    """Estimates Kyle's λ from real-time Polymarket trade flow.

    Registered as a trade callback on PolymarketPriceFeed.
    Queried by AutoTrader's scoring loop via get_lambda_signal().
    """

    def __init__(self):
        # Per-market trade buffer: asset_id → deque of (timestamp, price, size, side)
        self._trades: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=MAX_TRADES_PER_MARKET)
        )
        # Last known price per market (for side inference)
        self._last_price: dict[str, float] = {}

    def on_trade(self, asset_id: str, price: float, size: float,
                 timestamp: float, side: str):
        """Trade callback — registered with PolymarketPriceFeed.on_trade()."""
        self._trades[asset_id].append((timestamp, price, size, side))
        self._last_price[asset_id] = price

    def _prune_old(self, asset_id: str):
        """Remove trades older than LONG_WINDOW_SECONDS from the left of the deque."""
        trades = self._trades.get(asset_id)
        if not trades:
            return
        cutoff = time.time() - LONG_WINDOW_SECONDS
        while trades and trades[0][0] < cutoff:
            trades.popleft()

    def _compute_lambda(self, asset_id: str, window_seconds: int) -> "tuple[float, int] | None":
        """Compute λ via OLS regression on trades within the window.

        Kyle's model: Δp_t = λ · Q_t + ε_t
        OLS: λ = Σ(Q_t · Δp_t) / Σ(Q_t²)

        Returns (lambda_value, n_trades) or None if insufficient data.
        """
        self._prune_old(asset_id)
        trades = self._trades.get(asset_id)
        if not trades:
            return None

        cutoff = time.time() - window_seconds
        window_trades = [(ts, p, s, side) for ts, p, s, side in trades if ts >= cutoff]

        min_trades = MIN_TRADES_SHORT if window_seconds <= SHORT_WINDOW_SECONDS else MIN_TRADES_LONG
        if len(window_trades) < min_trades:
            return None

        # Build Δp and Q arrays
        sum_qd = 0.0  # Σ(Q_t · Δp_t)
        sum_qq = 0.0  # Σ(Q_t²)

        for i in range(1, len(window_trades)):
            _, prev_price, _, _ = window_trades[i - 1]
            ts, cur_price, size, side = window_trades[i]

            delta_p = cur_price - prev_price

            # Signed volume: positive for buys, negative for sells
            if side in ("buy", "BUY"):
                signed_vol = size
            elif side in ("sell", "SELL"):
                signed_vol = -size
            else:
                # Infer from price movement (tick rule)
                if delta_p > 0:
                    signed_vol = size
                elif delta_p < 0:
                    signed_vol = -size
                else:
                    signed_vol = size  # tie-break toward buy

            sum_qd += signed_vol * delta_p
            sum_qq += signed_vol * signed_vol

        if sum_qq == 0:
            return None

        lambda_val = sum_qd / sum_qq
        return (lambda_val, len(window_trades))

    def _get_flow_direction(self, asset_id: str, window_seconds: int) -> str:
        """Determine net flow direction from trades in the window.

        Returns "YES" (net buying), "NO" (net selling), or "MIXED".
        """
        trades = self._trades.get(asset_id)
        if not trades:
            return "MIXED"

        cutoff = time.time() - window_seconds
        buy_vol = 0.0
        sell_vol = 0.0
        prev_price = None

        for ts, price, size, side in trades:
            if ts < cutoff:
                prev_price = price
                continue

            if side in ("buy", "BUY"):
                buy_vol += size
            elif side in ("sell", "SELL"):
                sell_vol += size
            else:
                # Infer from price movement
                if prev_price is not None:
                    if price > prev_price:
                        buy_vol += size
                    elif price < prev_price:
                        sell_vol += size
                    else:
                        buy_vol += size  # tie-break
                else:
                    buy_vol += size  # no reference, assume buy

            prev_price = price

        total = buy_vol + sell_vol
        if total == 0:
            return "MIXED"

        net = buy_vol - sell_vol
        if abs(net) / total < FLOW_DIRECTION_THRESHOLD:
            return "MIXED"

        return "YES" if net > 0 else "NO"

    def get_lambda_signal(self, market_id: str, our_direction: str) -> dict:
        """Get the λ-based directional multiplier for scoring.

        Args:
            market_id: asset_id or condition_id (prefix-matched)
            our_direction: "YES" or "NO" — which side we'd buy on Polymarket

        Returns:
            dict with 'multiplier' (0.4–1.5) and metadata.
        """
        # Resolve market_id: try exact match, then prefix match
        asset_id = self._resolve_asset_id(market_id)

        neutral = {
            "multiplier": 1.0,
            "short_lambda": None,
            "long_lambda": None,
            "lambda_ratio": None,
            "flow_direction": "MIXED",
            "agrees_with_arb": None,
            "n_trades_short": 0,
            "n_trades_long": 0,
            "sufficient_data": False,
        }

        if asset_id is None:
            return neutral

        # Compute both windows
        short_result = self._compute_lambda(asset_id, SHORT_WINDOW_SECONDS)
        long_result = self._compute_lambda(asset_id, LONG_WINDOW_SECONDS)

        if short_result is None or long_result is None:
            n_short = short_result[1] if short_result else 0
            n_long = long_result[1] if long_result else 0
            neutral["n_trades_short"] = n_short
            neutral["n_trades_long"] = n_long
            return neutral

        short_lambda, n_short = short_result
        long_lambda, n_long = long_result

        # Guard: no meaningful baseline
        if long_lambda <= 0:
            neutral["short_lambda"] = short_lambda
            neutral["long_lambda"] = long_lambda
            neutral["n_trades_short"] = n_short
            neutral["n_trades_long"] = n_long
            neutral["sufficient_data"] = True
            return neutral

        lambda_ratio = short_lambda / long_lambda

        # No spike
        if lambda_ratio <= SPIKE_THRESHOLD_LOW:
            return {
                "multiplier": 1.0,
                "short_lambda": short_lambda,
                "long_lambda": long_lambda,
                "lambda_ratio": lambda_ratio,
                "flow_direction": self._get_flow_direction(asset_id, SHORT_WINDOW_SECONDS),
                "agrees_with_arb": None,
                "n_trades_short": n_short,
                "n_trades_long": n_long,
                "sufficient_data": True,
            }

        # Spike detected — compute directional multiplier
        flow_dir = self._get_flow_direction(asset_id, SHORT_WINDOW_SECONDS)

        # Linear interpolation: t=0 at ratio=1.5, t=1 at ratio>=3.0
        t = min((lambda_ratio - SPIKE_THRESHOLD_LOW) /
                (SPIKE_THRESHOLD_HIGH - SPIKE_THRESHOLD_LOW), 1.0)

        if flow_dir == our_direction:
            agrees = True
            multiplier = AGREE_MIN + t * (AGREE_MAX - AGREE_MIN)
        elif flow_dir == "MIXED":
            agrees = None
            multiplier = MIXED_MULTIPLIER
        else:
            agrees = False
            multiplier = OPPOSE_MAX - t * (OPPOSE_MAX - OPPOSE_MIN)

        logger.info(
            "Kyle λ signal: market=%s ratio=%.2f flow=%s arb=%s mult=%.2f (n=%d/%d)",
            market_id[:20], lambda_ratio, flow_dir, our_direction,
            multiplier, n_short, n_long,
        )

        return {
            "multiplier": round(multiplier, 3),
            "short_lambda": round(short_lambda, 8),
            "long_lambda": round(long_lambda, 8),
            "lambda_ratio": round(lambda_ratio, 3),
            "flow_direction": flow_dir,
            "agrees_with_arb": agrees,
            "n_trades_short": n_short,
            "n_trades_long": n_long,
            "sufficient_data": True,
        }

    def _resolve_asset_id(self, market_id: str) -> "str | None":
        """Resolve a market_id/condition_id to a buffered asset_id.

        Tries exact match first, then prefix match (condition_id is often
        the prefix of asset_id before a ':' separator).
        """
        if market_id in self._trades and self._trades[market_id]:
            return market_id
        # Prefix match: condition_id → asset_id
        for aid in self._trades:
            if aid.startswith(market_id):
                return aid
        return None

    def get_stats(self) -> dict:
        """Return estimator status for diagnostics."""
        total_trades = sum(len(d) for d in self._trades.values())
        return {
            "tracked_markets": len(self._trades),
            "total_buffered_trades": total_trades,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/test_kyle_lambda.py::TestLambdaComputation -v`
Expected: ALL 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/positions/kyle_lambda.py tests/test_kyle_lambda.py
git commit -m "feat: add KyleLambdaEstimator with trade buffer and OLS lambda

Dual rolling windows (15min + 2hr), side inference via tick rule,
bounded per-market deque, division-by-zero guards."
```

---

### Task 3: Directional Signal & Multiplier

**Files:**
- Modify: `src/positions/kyle_lambda.py` (already has `get_lambda_signal` from Task 2)
- Test: `tests/test_kyle_lambda.py`

Note: The core `get_lambda_signal()` is already implemented in Task 2. This task adds comprehensive tests for the directional multiplier logic and spike detection.

- [ ] **Step 1: Write the signal tests**

Append to `tests/test_kyle_lambda.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/test_kyle_lambda.py::TestDirectionalSignal -v`
Expected: ALL 9 tests PASS (implementation already in place from Task 2).

- [ ] **Step 3: Fix any failures**

If any tests fail, adjust the implementation in `kyle_lambda.py` to match the spec behavior. Common issues to check:
- Spike detection thresholds may need tuning if the synthetic trade data produces different ratios than expected
- Flow direction threshold may need adjustment

- [ ] **Step 4: Commit**

```bash
git add tests/test_kyle_lambda.py
git commit -m "test: add directional signal and multiplier tests for Kyle's lambda

Covers spike detection, directional agreement/opposition, bounds,
neutral cases, prefix matching, and return structure."
```

---

### Task 4: AutoTrader Integration — Setter & Scoring

**Files:**
- Modify: `src/positions/auto_trader.py:67-68` (add setter), `418-424` (scoring insertion)
- Test: `tests/test_kyle_lambda.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_kyle_lambda.py`:

```python
class TestAutoTraderIntegration:
    """Task 4: AutoTrader setter and scoring integration."""

    def test_set_kyle_estimator(self):
        """AutoTrader should accept a kyle_estimator via setter."""
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        pm.list_packages = MagicMock(return_value=[])
        pm.executors = {"polymarket": MagicMock(), "kalshi": MagicMock()}
        trader = AutoTrader(pm)
        assert trader.kyle_estimator is None

        mock_est = MagicMock()
        trader.set_kyle_estimator(mock_est)
        assert trader.kyle_estimator is mock_est

    def test_scoring_applies_kyle_multiplier(self):
        """The scoring loop should multiply score by kyle_signal['multiplier'].

        We can't easily run _scan_and_trade() (it's async, needs scanner, etc.)
        so we verify the integration by checking that an opportunity dict gets
        the kyle_signal attached after the scoring code path runs.

        Approach: construct a mock estimator, set it on the trader, then call
        the scoring-relevant code path indirectly by checking that the
        auto_trader code correctly accesses kyle_estimator.get_lambda_signal().
        """
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        pm.list_packages = MagicMock(return_value=[])
        pm.executors = {"polymarket": MagicMock(), "kalshi": MagicMock()}
        trader = AutoTrader(pm)

        mock_est = MagicMock()
        mock_est.get_lambda_signal.return_value = {
            "multiplier": 1.3,
            "short_lambda": 0.005,
            "long_lambda": 0.002,
            "lambda_ratio": 2.5,
            "flow_direction": "YES",
            "agrees_with_arb": True,
            "n_trades_short": 15,
            "n_trades_long": 40,
            "sufficient_data": True,
        }
        trader.set_kyle_estimator(mock_est)

        # Simulate what the scoring loop does:
        # 1. It checks self.kyle_estimator is truthy
        # 2. It determines direction from buy_yes_platform
        # 3. It calls get_lambda_signal(market_id, direction)
        # 4. It multiplies score by the returned multiplier
        opp = {
            "buy_yes_platform": "polymarket",
            "buy_no_platform": "kalshi",
            "buy_yes_market_id": "0xtest_market",
        }
        market_id = opp["buy_yes_market_id"]
        score = 20.0

        # Replicate the exact code that will be inserted:
        if trader.kyle_estimator and market_id:
            poly_platform = opp.get("buy_yes_platform", "")
            if poly_platform == "polymarket":
                kyle_direction = "YES"
            elif opp.get("buy_no_platform", "") == "polymarket":
                kyle_direction = "NO"
            else:
                kyle_direction = "YES"
            kyle_signal = trader.kyle_estimator.get_lambda_signal(market_id, kyle_direction)
            if kyle_signal:
                score *= kyle_signal["multiplier"]
                opp["kyle_signal"] = kyle_signal

        # Verify: score was multiplied by 1.3
        assert score == pytest.approx(26.0)
        # Verify: signal was attached to opportunity
        assert "kyle_signal" in opp
        assert opp["kyle_signal"]["multiplier"] == 1.3
        # Verify: correct direction was passed
        mock_est.get_lambda_signal.assert_called_once_with("0xtest_market", "YES")

    def test_scoring_direction_when_polymarket_is_no_side(self):
        """When Polymarket is the buy_no_platform, direction should be NO."""
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        pm.list_packages = MagicMock(return_value=[])
        pm.executors = {"polymarket": MagicMock(), "kalshi": MagicMock()}
        trader = AutoTrader(pm)

        mock_est = MagicMock()
        mock_est.get_lambda_signal.return_value = {
            "multiplier": 0.6,
            "short_lambda": 0.01,
            "long_lambda": 0.003,
            "lambda_ratio": 3.3,
            "flow_direction": "YES",
            "agrees_with_arb": False,
            "n_trades_short": 20,
            "n_trades_long": 50,
            "sufficient_data": True,
        }
        trader.set_kyle_estimator(mock_est)

        opp = {
            "buy_yes_platform": "kalshi",
            "buy_no_platform": "polymarket",
            "buy_yes_market_id": "0xtest_no",
        }
        market_id = opp["buy_yes_market_id"]
        score = 20.0

        if trader.kyle_estimator and market_id:
            poly_platform = opp.get("buy_yes_platform", "")
            if poly_platform == "polymarket":
                kyle_direction = "YES"
            elif opp.get("buy_no_platform", "") == "polymarket":
                kyle_direction = "NO"
            else:
                kyle_direction = "YES"
            kyle_signal = trader.kyle_estimator.get_lambda_signal(market_id, kyle_direction)
            if kyle_signal:
                score *= kyle_signal["multiplier"]

        assert score == pytest.approx(12.0)  # 20 * 0.6
        mock_est.get_lambda_signal.assert_called_once_with("0xtest_no", "NO")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/test_kyle_lambda.py::TestAutoTraderIntegration -v`
Expected: FAIL — `AutoTrader` has no `kyle_estimator` attribute or `set_kyle_estimator` method. (3 tests fail.)

- [ ] **Step 3: Add setter to AutoTrader**

In `src/positions/auto_trader.py`, add after line 68 (`self._weather_scanner = None`):

```python
        self.kyle_estimator = None
```

Then add after the `set_weather_scanner` method (after line 76):

```python
    def set_kyle_estimator(self, estimator):
        """Set the Kyle's lambda estimator for adverse selection scoring."""
        self.kyle_estimator = estimator
```

- [ ] **Step 4: Add scoring integration**

In `src/positions/auto_trader.py`, insert after line 423 (after the cross-platform disagreement `score *= 1.3` block), before line 425 (`# Skip low-score opportunities`):

```python
            # Kyle's lambda: adverse selection / informed flow signal
            if self.kyle_estimator and market_id:
                # Determine our direction on the Polymarket side
                poly_platform = opp.get("buy_yes_platform", "")
                if poly_platform == "polymarket":
                    kyle_direction = "YES"
                elif opp.get("buy_no_platform", "") == "polymarket":
                    kyle_direction = "NO"
                else:
                    kyle_direction = "YES"  # fallback
                kyle_signal = self.kyle_estimator.get_lambda_signal(market_id, kyle_direction)
                if kyle_signal:
                    score *= kyle_signal["multiplier"]
                    opp["kyle_signal"] = kyle_signal
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/test_kyle_lambda.py::TestAutoTraderIntegration -v`
Expected: ALL 3 tests PASS.

- [ ] **Step 6: Run full test suite**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/ -v`
Expected: ALL tests pass, no regressions.

- [ ] **Step 7: Commit**

```bash
git add src/positions/auto_trader.py tests/test_kyle_lambda.py
git commit -m "feat: integrate Kyle's lambda estimator into auto-trader scoring

Adds set_kyle_estimator() setter and directional multiplier in
_scan_and_trade() scoring loop, after cross-platform disagreement
boost, before MIN_SPREAD_PCT filter."
```

---

### Task 5: Server Startup Wiring

**Files:**
- Modify: `src/server.py:398-416`
- Test: manual (startup verification)

- [ ] **Step 1: Add startup wiring**

In `src/server.py`, inside the Polymarket WS try/except block (lines 398-416), add after line 413 (`_poly_ws.start()`), before line 414 (`logger.info(...)`):

```python
                # Kyle's lambda estimator — adverse selection signal from trade flow
                try:
                    from positions.kyle_lambda import KyleLambdaEstimator
                    _kyle_est = KyleLambdaEstimator()
                    _poly_ws.on_trade(_kyle_est.on_trade)
                    if _auto_trader:
                        _auto_trader.set_kyle_estimator(_kyle_est)
                    logger.info("Kyle lambda estimator started, tracking trade flow")
                except Exception as e:
                    logger.warning("Kyle lambda init failed (non-critical): %s", e)
```

- [ ] **Step 2: Run full test suite**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/ -v`
Expected: ALL tests pass.

- [ ] **Step 3: Verify server starts**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout\src" && timeout 10 python -m uvicorn server:app --host 127.0.0.1 --port 8500 2>&1 | head -30`
Expected: Server starts, log includes "Kyle lambda estimator started".

- [ ] **Step 4: Commit**

```bash
git add src/server.py
git commit -m "feat: wire Kyle lambda estimator into server startup

Creates estimator after PolymarketPriceFeed, registers on_trade
callback, injects into AutoTrader via setter. Non-critical — server
starts even if lambda init fails."
```

---

### Task 6: Final Integration Test & Cleanup

**Files:**
- Test: `tests/test_kyle_lambda.py`

- [ ] **Step 1: Add end-to-end integration test**

Append to `tests/test_kyle_lambda.py`:

```python
class TestEndToEnd:
    """Task 6: Full pipeline integration test."""

    def test_full_pipeline_trade_to_signal(self):
        """Trade callbacks → buffer → lambda → signal: full pipeline."""
        from positions.polymarket_ws import PolymarketPriceFeed
        from positions.kyle_lambda import KyleLambdaEstimator

        feed = PolymarketPriceFeed()
        est = KyleLambdaEstimator()
        feed.on_trade(est.on_trade)

        now = time.time()
        # Simulate 2hr of background trades
        price = 0.50
        for i in range(50):
            price += 0.0005
            feed._handle_message({
                "event_type": "trade",
                "asset_id": "0xfull_test",
                "price": str(round(price, 4)),
                "size": "10.0",
                "side": "BUY",
            })
            # Manually set timestamps (since _handle_message uses time.time())
            # Override the last trade's timestamp to be in the past
            if est._trades.get("0xfull_test"):
                trades = est._trades["0xfull_test"]
                old = trades[-1]
                trades[-1] = (now - 7000 + i * 120, old[1], old[2], old[3])

        # Simulate 15min of spike trades
        for i in range(20):
            price += 0.005
            feed._handle_message({
                "event_type": "trade",
                "asset_id": "0xfull_test",
                "price": str(round(price, 4)),
                "size": "10.0",
                "side": "BUY",
            })
            if est._trades.get("0xfull_test"):
                trades = est._trades["0xfull_test"]
                old = trades[-1]
                trades[-1] = (now - 800 + i * 30, old[1], old[2], old[3])

        signal = est.get_lambda_signal("0xfull_test", "YES")
        # Should have sufficient data
        assert signal["n_trades_short"] > 0
        assert signal["n_trades_long"] > 0
        # Full structure
        assert "multiplier" in signal
        assert "flow_direction" in signal

    def test_get_stats(self):
        """Verify stats method returns expected structure."""
        from positions.kyle_lambda import KyleLambdaEstimator
        est = KyleLambdaEstimator()
        now = time.time()
        est.on_trade("0xa", 0.5, 10.0, now, "buy")
        est.on_trade("0xb", 0.6, 5.0, now, "sell")
        stats = est.get_stats()
        assert stats["tracked_markets"] == 2
        assert stats["total_buffered_trades"] == 2
```

- [ ] **Step 2: Run all tests**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/test_kyle_lambda.py -v`
Expected: ALL tests PASS (~25 tests).

- [ ] **Step 3: Run full suite one final time**

Run: `cd "C:\Users\afoma\.openclaw\workspace\projects\arbitrout" && python -m pytest tests/ -v`
Expected: ALL tests pass, no regressions.

- [ ] **Step 4: Commit**

```bash
git add tests/test_kyle_lambda.py
git commit -m "test: add end-to-end integration test for Kyle's lambda pipeline

Full pipeline: trade callback → buffer → lambda computation →
directional signal. Verifies stats method."
```
