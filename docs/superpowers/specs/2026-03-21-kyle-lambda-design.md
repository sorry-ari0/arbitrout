# Kyle's Lambda Integration — Design Spec

## Goal

Add a real-time adverse selection signal (Kyle's lambda) to the auto-trader's scoring pipeline. Uses trade flow data from the existing Polymarket CLOB WebSocket to estimate price impact per unit volume, then adjusts opportunity scores based on whether informed flow agrees or disagrees with our arb direction.

## Problem Summary

The auto-trader scores opportunities using spread, expiry, crypto bias, favorite/longshot, insider signals, and cross-platform disagreement — but has no real-time microstructure signal. Some high-spread opportunities exist because informed traders are actively moving the price (adverse selection), not because the market is inefficient. Without detecting this, the auto-trader enters trades where the spread is closing against us.

Kyle's lambda (λ) measures price impact: λ = Δp / Q. High λ means each unit of trade volume moves the price more, indicating informed trading. A sudden λ spike relative to baseline signals "smart money just arrived."

## Architecture

New standalone module `kyle_lambda.py` following the same pattern as `insider_tracker.py` — a stateless-between-cycles estimator with a clear API, wired into the auto-trader's scoring loop via a setter method.

Data flows: `PolymarketPriceFeed` → trade callback → `KyleLambdaEstimator` (buffers trades, computes λ) → `AutoTrader._scan_and_trade()` scoring loop (multiplier on score).

**Concurrency model:** Single-threaded asyncio. The WS receive loop and the scoring loop run on the same event loop and never overlap. `collections.deque` operations are atomic in CPython. No locks needed.

## Data Capture

### Trade Callback on PolymarketPriceFeed

The existing `_handle_message()` in `polymarket_ws.py` (line 167) receives `trade` events but only extracts price. A new `on_trade()` callback channel is added alongside the existing `on_price()` channel.

**In `polymarket_ws.py`:**
- Add `_on_trade_callbacks: list` to `__init__()`
- Add `on_trade(callback)` registration method — callback signature: `(asset_id, price, size, timestamp, side)`
- In `_handle_message()`, when `event_type == "trade"`, also extract `size`/`amount` fields and fire trade callbacks
- If size is not present in the message, skip the trade callback (can't compute λ without volume)

**Trade size field availability:** The Polymarket CLOB WebSocket `trade` event includes a `size` field (trade quantity). The implementation must try the following fields in order: `size`, `amount`, `quantity`. If none are present or parseable as a non-zero float, skip the trade callback. This is a best-effort approach — if the field is systematically absent, the estimator gracefully degrades (returns neutral multiplier due to insufficient data).

**Trade side field:** The `side` field (`"BUY"`/`"SELL"`) may or may not be present. Fallback inference:
1. If `side` field exists, use it directly (normalize to `"buy"`/`"sell"`)
2. Else compare this trade's price to the previous trade's price for the same asset: price increase → `"buy"`, decrease → `"sell"`, no change → `"buy"` (tie-break toward aggressor)

This tick-rule inference is a standard microstructure heuristic (Lee & Ready, 1991). It is imperfect when multiple trades execute at the same price, but is adequate for λ estimation where we need aggregate flow direction, not per-trade precision.

### Trade Buffer in KyleLambdaEstimator

- Per-market `collections.deque(maxlen=5000)` — bounded memory, sized to hold ~2hrs of trades for active markets (Polymarket top markets see ~30-50 trades/min at peak)
- Each record: `(timestamp, price, size, side)` tuple
- Lazy pruning: on each `get_lambda_signal()` call, discard entries older than 2 hours from the left of the deque
- `on_trade(asset_id, price, size, timestamp, side)` — the callback registered with the price feed

**Key mapping:** The trade buffer is keyed by `asset_id` (the token ID from the WebSocket). The `get_lambda_signal()` method accepts either an `asset_id` directly or a `condition_id` — if the key isn't found in the buffer, it tries prefix-matching (`condition_id` is often the prefix of `asset_id` before a `:` separator). This matches how `polymarket_ws.py` already handles the relationship (see line 408 in `server.py`: `cid = aid.split(":")[0]`).

**Deque maxlen vs time pruning:** In low-volume markets, entries may span more than 2 hours before hitting maxlen — time-based pruning handles this. In high-volume markets, maxlen silently drops the oldest entries, which may truncate the 2hr window. This is acceptable: high-volume markets will still have sufficient trades for both windows (the long window needs only 30 trades), and the maxlen prevents unbounded memory growth.

## Lambda Computation

### Regression Model

Kyle's model: Δp_t = λ · Q_t + ε_t

- Δp_t = price change between consecutive trades
- Q_t = signed trade volume (positive for buys, negative for sells)
- λ estimated via univariate OLS: λ = Σ(Q_t · Δp_t) / Σ(Q_t²)

No numpy dependency — univariate OLS is sum-of-products / sum-of-squares.

**Division-by-zero guard:** If Σ(Q_t²) == 0 (all trades have zero volume — shouldn't happen given the size filter, but defensive), return None.

### Dual Rolling Windows

| Window | Duration | Min Trades | Purpose |
|--------|----------|-----------|---------|
| Short | 15 min (900s) | 10 | Detect "something just happened" spikes |
| Long | 2 hr (7200s) | 30 | Structural baseline for the market |

`_compute_lambda(trades, window_seconds)` returns `(lambda_value, n_trades)` or `(None, n)` if below minimum trade threshold.

### Lambda Interpretation

- λ > 0: trades move prices (informed trading present)
- Higher λ = more price impact = more adverse selection risk
- λ ≈ 0: noise trading dominant, trades don't move prices
- Short-term λ spike above long-term baseline = informed trader just arrived
- Spike detection: `lambda_ratio = short_λ / long_λ`
- **Guard:** If `long_λ <= 0`, treat as neutral (no baseline to compare against)

## Directional Signal & Multiplier

### Signal Construction

`get_lambda_signal(market_id, our_direction)` where `our_direction` is `"YES"` or `"NO"`.

1. Compute short-window and long-window λ
2. If either is None (insufficient data), return neutral (multiplier = 1.0)
3. If long_λ ≤ 0, return neutral (no meaningful baseline)
4. Compute `lambda_ratio = short_λ / long_λ`
5. If ratio ≤ 1.5, no spike → return neutral (multiplier = 1.0)
6. Determine flow direction from short-window net signed volume: Σ(size) for buys - Σ(size) for sells. Positive → `"YES"`, negative → `"NO"`, |net| < 10% of total volume → `"MIXED"`
7. Compare flow direction to `our_direction`

### Multiplier Table

| Condition | Multiplier Range | Reasoning |
|-----------|-----------------|-----------|
| Insufficient data (either window) | 1.0 | Neutral pass-through |
| No spike (ratio ≤ 1.5) | 1.0 | No unusual activity |
| Long λ ≤ 0 | 1.0 | No meaningful baseline |
| Spike + flow agrees with our direction | 1.15 – 1.5 | Smart money confirms our trade |
| Spike + flow opposes our direction | 0.4 – 0.8 | Smart money says we're wrong |
| Spike + flow direction unclear (mixed) | 0.85 | Caution — activity but unreadable |

**Linear interpolation formula:**
```python
t = min((lambda_ratio - 1.5) / 1.5, 1.0)  # 0.0 at ratio=1.5, 1.0 at ratio>=3.0
if agrees:
    multiplier = 1.15 + t * 0.35   # 1.15 → 1.5
elif opposes:
    multiplier = 0.8 - t * 0.4     # 0.8 → 0.4
else:  # mixed
    multiplier = 0.85
```

### Return Value

```python
{
    "multiplier": float,           # 0.4 – 1.5
    "short_lambda": float | None,  # raw 15min λ
    "long_lambda": float | None,   # raw 2hr λ
    "lambda_ratio": float | None,  # short/long
    "flow_direction": "YES" | "NO" | "MIXED",
    "agrees_with_arb": bool | None,
    "n_trades_short": int,
    "n_trades_long": int,
    "sufficient_data": bool,
}
```

## Integration Points

### Startup Wiring (server.py)

In `server.py`, `AutoTrader` is created at line 333 and `PolymarketPriceFeed` is created later at line 401. Following the established setter pattern (`set_political_analyzer()` at line 70, `set_weather_scanner()` at line 74):

1. Add `set_kyle_estimator(estimator)` setter method to `AutoTrader` (stores as `self.kyle_estimator`, default `None`)
2. Create `KyleLambdaEstimator()` in the Polymarket WS block (after line 413, inside the existing try/except)
3. Register: `_poly_ws.on_trade(kyle_estimator.on_trade)`
4. Inject: `_auto_trader.set_kyle_estimator(kyle_estimator)`

No new API endpoints. λ stats can be included in the `PolymarketPriceFeed.get_stats()` dict or logged to the decision logger. The `/api/health` endpoint (line 643) already exists for system status.

### Scoring (auto_trader.py, inside `_scan_and_trade()` ~line 418)

Insert after cross-platform disagreement boost (lines 418-423), before `MIN_SPREAD_PCT` check (line 425):

```python
# Kyle's lambda: adverse selection / informed flow signal
if self.kyle_estimator:
    # For cross-platform arbs, we buy YES on buy_yes_platform — so our
    # direction for the Polymarket side depends on which side we're buying there.
    # buy_yes_platform is where we buy YES, buy_no_platform is where we buy NO.
    poly_platform = opp.get("buy_yes_platform", "")
    if poly_platform == "polymarket":
        our_direction = "YES"
    elif opp.get("buy_no_platform", "") == "polymarket":
        our_direction = "NO"
    else:
        our_direction = "YES"  # fallback for non-Polymarket (λ will return neutral anyway)
    kyle_signal = self.kyle_estimator.get_lambda_signal(market_id, our_direction)
    if kyle_signal:
        score *= kyle_signal["multiplier"]
        opp["kyle_signal"] = kyle_signal
```

**Direction logic explained:** In a cross-platform arb, `buy_yes_platform` is the platform where we buy YES and `buy_no_platform` is where we buy NO. Since Kyle's λ uses Polymarket trade flow, our direction on Polymarket is YES if `buy_yes_platform == "polymarket"`, and NO if `buy_no_platform == "polymarket"`. For pure prediction bets, the direction comes from the side chosen later in the function — but λ is most valuable for cross-platform arbs where the Polymarket side is clear at scoring time.

### Interaction with Insider Tracker

Complementary, not redundant:
- **Insider tracker:** Data API, 15-min lag, identifies *who* is trading (known wallets)
- **Kyle's λ:** WebSocket, real-time, detects *that* informed trading is happening (anonymous flow)
- Both multipliers stack. Conviction trader + λ spike in same direction = very strong signal. Conviction trader + opposing λ flow = caution.

### Eval Logging

The `kyle_signal` dict is attached to the opportunity and flows through the existing `dlog` eval logger for later analysis. No changes to the eval logger itself.

### Exit Engine

No changes. λ is an entry-time signal only.

## Testing

**New file: `tests/test_kyle_lambda.py`**

1. **Regression math:** Known trades with analytically calculable λ → verify both windows return correct values
2. **Insufficient data:** <10 trades in short window → `sufficient_data=False`, `multiplier=1.0`
3. **Spike detection:** Short-window λ = 3× long-window → `lambda_ratio ≈ 3.0`, multiplier adjusts
4. **Directional agreement:** Spike + net buy flow + `our_direction="YES"` → multiplier > 1.0; same spike + `"NO"` → multiplier < 1.0
5. **Neutral cases:** No spike → multiplier = 1.0 regardless of direction
6. **Buffer bounds:** Insert more than maxlen trades → deque stays bounded; 2hr+ trades pruned
7. **Integration:** Wire to mock price feed, fire trade callbacks, verify full return structure
8. **Division-by-zero:** All zero-volume trades → returns neutral
9. **Long λ ≤ 0:** Returns neutral multiplier
10. **Side inference:** Verify tick-rule fallback produces correct signed volume

No changes to existing tests — λ is additive to the scoring pipeline.

## What This Does NOT Include

- No changes to exit engine or position management
- No new API endpoints
- No new dependencies (OLS is sum-of-products, no numpy needed)
- No changes to the arb scanner or opportunity conversion
- No Kalshi/Limitless WebSocket integration (Polymarket only — where we have the data)
- No historical λ backfill or persistence (in-memory only, rebuilds from live trades on restart)
