# Insider Tracking Improvements Plan

## Current System Evaluation

### What exists (Polymarket only)
- `insider_tracker.py` (688 lines) monitors top 100 Polymarket traders via Data API
- Fetches leaderboard (OVERALL + CRYPTO, ALL + MONTH time periods)
- Tracks positions for top 20 flagged wallets every 15 minutes
- Classifies wallets: conviction (>15% ROI), market maker (<5% ROI + >$100M vol), unknown
- 8 hardcoded high-conviction wallets (Theo4, Fredi9999, etc.) get 5x signal weight
- 4 known market makers excluded from directional signals
- Detects movements: mass entry (2+), size increases (>50%), whale convergence (3+)
- Signal strength: weighted composite of count (15%), value (25%), conviction (35%), accuracy (25%)
- Accuracy tracking per wallet after market resolution
- Auto trader uses signals as score multiplier (up to 4-5x boost)

### What's working
- Architecture is solid — separation of tracking from trading decisions
- Conviction/MM classification prevents market maker noise from polluting signals
- Convergence detection is a strong signal type
- Accuracy tracking provides feedback loop for wallet quality

### Gaps identified
1. **Polymarket-only** — no Kalshi whale tracking at all
2. **Stale watchlist** — 8 hardcoded wallets from 03/20, no auto-refresh from leaderboard changes
3. **Only top 20 wallets tracked** — misses mid-tier conviction traders entering new markets
4. **No real-time detection** — 15-min polling means we see movements after they happen, not as they happen
5. **No cross-platform convergence** — if Polymarket whales AND Kalshi volume spike on same event, we don't connect them
6. **Position exit tracking is weak** — we detect entries well but don't alert when conviction traders EXIT a market (bearish signal)
7. **No volume-relative sizing** — a $5K position on a $50K market is very different from $5K on a $5M market
8. **Watchlist refresh requires manual intervention** — `update_watchlist()` exists but nothing calls it automatically

---

## Research Findings

### Polymarket Data API — unused capabilities
We currently use only leaderboard + positions. The Data API has much more:

- **`GET /holders?market={cid}`** — Top 20 holders per market, with wallet + amount. **Not used at all.** Could instantly tell us which whales hold positions on markets we're evaluating, without querying each wallet individually.
- **`GET /activity?user={addr}&type=TRADE`** — Real-time activity per wallet, filterable by type (TRADE, SPLIT, MERGE, REDEEM). Has `start`/`end` timestamps for windowed queries. Better than polling `/positions` for detecting *when* a trade happened.
- **`GET /trades?filterType=CASH&filterAmount=10000`** — Server-side whale trade filtering! Returns trades above a $ threshold across ALL markets. We could find whale-sized trades without querying individual wallets.
- **`GET /closed-positions?user={addr}`** — Closed position history with realized PnL. Useful for tracking how whales exited, not just current positions.
- **Leaderboard supports 10 categories** — OVERALL, POLITICS, SPORTS, CRYPTO, CULTURE, MENTIONS, WEATHER, ECONOMICS, TECH, FINANCE. We only query OVERALL + CRYPTO, missing politics/economics specialists.
- **Rate limits generous** — 1000 req/10s general, 150/10s for positions, 200/10s for trades. We use ~6 req/scan.
- **All endpoints are free, no auth needed.**

### Kalshi API capabilities
- **No trader identity** on any endpoint — trades show size/price/side/time but never WHO
- **No leaderboard API** — leaderboard exists on site but is opt-in and not exposed via API
- **Public trade feed**: `GET /markets/trades` returns every trade with `count_fp` (size in dollars), `taker_side`, `yes_price_dollars`, `created_time`
- **Public orderbook**: `GET /markets/{ticker}/orderbook` shows aggregated price levels
- **WebSocket**: `trade` channel streams real-time trades (requires auth to connect)
- **Volume data**: `volume_fp`, `volume_24h_fp`, `open_interest_fp` per market
- **No size filtering server-side** — must paginate all trades and filter client-side

### Kalshi whale detection strategy
Since Kalshi has no identity data, we must use **anonymous behavioral signals**:
1. Large individual trades (>$1K, >$5K, >$10K thresholds)
2. Volume spikes relative to market history
3. Orderbook imbalance (heavy one-sided depth)
4. Rapid sequential trades (possible iceberg orders being filled)

### Third-party options
- **Oddpool API** ($30/mo Pro): Pre-filtered whale feeds, `min_trade_size` param, cross-platform, REST + WebSocket
- **FORCASTR**: Tiered whale alerts, claims 60-65% whale win rate
- Free tools exist but lack APIs (dashboard-only)

---

## Improvement Plan

### Task 1: Auto-refresh watchlist + expand leaderboard categories

**Files:** `src/positions/insider_tracker.py`

**Problem:** Watchlist is static. Only OVERALL + CRYPTO categories queried — missing top politics/economics traders.

**Changes:**

**1a. Expand leaderboard categories:**
- Add POLITICS, ECONOMICS, FINANCE to `_fetch_leaderboard()` category list
- These overlap heavily with our tradeable markets (elections, policy, economic indicators)
- Still use ALL + MONTH time periods for each
- Rate impact: +6 requests per scan (3 new categories × 2 time periods) = negligible

**1b. Auto-promote conviction wallets:**
- At end of `_fetch_leaderboard()`, after classifying wallets, auto-promote wallets that meet all criteria:
  - PNL > $1M
  - ROI > 20%
  - On leaderboard for 2+ consecutive scans (not a one-scan fluke)
  - Not already in KNOWN_MARKET_MAKERS
- Track `_consecutive_scans: dict[wallet, int]` — increment each scan if wallet appears, reset if absent
- Auto-promote to conviction watchlist with `signal_weight = 4.0` (below manual 5.0)
- Auto-demote wallets that fall off leaderboard for 10+ consecutive scans (but never demote hardcoded ones)
- Log promotions/demotions

**Why:** The prediction market landscape shifts rapidly. Traders who were top 10 last month may be inactive now, while new dominant players emerge. And our best arbitrage opportunities are in politics/economics — we need to track the specialists.

---

### Task 2: Kalshi anonymous whale detector

**Files:**
- Create: `src/positions/kalshi_whale_tracker.py`
- Modify: `src/server.py` (wire into startup)
- Modify: `src/positions/auto_trader.py` (consume signals)

**Problem:** Zero Kalshi whale intelligence currently.

**Design:** Since Kalshi has no trader identity, build an **anonymous behavioral signal** system:

```
KalshiWhaleTracker
├── _poll_large_trades()      # GET /markets/trades, filter by size
├── _detect_volume_spikes()   # Compare 24h vol to 7-day baseline
├── _detect_orderbook_tilt()  # Imbalanced depth = directional pressure
├── get_whale_signal(ticker)  # Composite signal for auto_trader
└── _loop()                   # Background polling every 5 minutes
```

**Trade size monitoring:**
- Poll `GET /markets/trades` for markets we're actively watching (from adapter cache)
- Filter trades where `count_fp >= 1000` ($1K+)
- Track per-market: large trade count, total large-trade volume, net direction (taker_side), timestamps
- Rolling 1-hour window

**Volume spike detection:**
- Store rolling 7-day average of `volume_24h_fp` per market (from adapter fetches)
- Flag when current 24h volume exceeds 2x the 7-day average
- Stronger signal when spike + large trades align

**Orderbook tilt:**
- When fetching orderbook (already done in kalshi adapter), compute bid/ask imbalance
- If YES depth > 3x NO depth (or vice versa), flag directional pressure
- Weight by depth within 10% of midpoint (ignore far-away orders)

**Signal output format (matches Polymarket insider signal shape):**
```python
{
    "has_signal": bool,
    "signal_type": "anonymous_whale",  # distinguishes from Polymarket identity-based
    "large_trade_count": int,          # trades > $1K in last hour
    "large_trade_volume": float,       # total $ of large trades
    "net_direction": "YES"|"NO"|"MIXED",
    "volume_spike_ratio": float,       # current_24h / 7d_avg
    "orderbook_tilt": float,           # -1 (heavy NO) to +1 (heavy YES)
    "signal_strength": float,          # 0-1 composite
}
```

**Signal strength formula:**
```
strength = 0.40 * trade_score + 0.35 * volume_score + 0.25 * orderbook_score

trade_score = min(large_trade_count / 5, 1.0) * direction_consistency
volume_score = min(volume_spike_ratio / 3.0, 1.0) if spike_ratio > 1.5 else 0
orderbook_score = min(abs(orderbook_tilt) / 0.5, 1.0) if abs(tilt) > 0.2 else 0
```

**Persistence:** `kalshi_whale_signals.json` — rolling window data, baselines

---

### Task 3: Cross-platform convergence detector

**Files:**
- Modify: `src/positions/insider_tracker.py`
- Modify: `src/positions/auto_trader.py`

**Problem:** If Polymarket conviction traders enter a market AND Kalshi sees whale-sized trades on the same event, that's a much stronger signal than either alone. Currently no connection between them.

**Changes:**
- Add method `get_cross_platform_signal(polymarket_cid, kalshi_ticker)` to InsiderTracker
- During auto_trader scanning, for matched cross-platform events:
  1. Get Polymarket insider signal (existing)
  2. Get Kalshi whale signal (new from Task 2)
  3. If both have signals with same direction → boost combined strength by 1.5x
  4. If both have signals but opposite direction → reduce to 0 (conflicting signals = stay out)
- Log cross-platform convergence events to decision log

**Why:** Cross-platform agreement is the strongest possible whale signal — independent groups of large traders seeing the same edge.

---

### Task 4: Insider exit detection

**Files:** `src/positions/insider_tracker.py`

**Problem:** We detect entries but don't alert on exits. If Theo4 exits a YES position, that's bearish for our YES position on the same market.

**Changes in `_detect_movements()`:**
- Add detection: wallets that were in `_prev_positions` but absent from current `_insider_positions` for a market
- Add "insider_exit" alert type with wallet info, previous position size, and direction
- If a conviction trader exits, flag for the exit engine (potential early exit trigger)
- Add `get_exit_signals(condition_id)` method that returns recent exit activity for a market

**Changes in exit engine integration:**
- During exit evaluation, check `insider_tracker.get_exit_signals(cid)`
- If conviction trader exited same-side position within last 2 scans → log warning, don't auto-sell but flag for review

**Why:** Smart money exiting is as informative as smart money entering. We currently ignore this signal.

---

### Task 5: Track more wallets with tiered polling

**Files:** `src/positions/insider_tracker.py`

**Problem:** Only top 20 wallets by PNL get position tracking. A trader ranked #40 with 60% accuracy and $500K PNL is more valuable than #5 with $2M PNL but 48% accuracy.

**Changes:**
- Tier 1 (every scan, top 10): conviction watchlist wallets + top accuracy wallets
- Tier 2 (every 2nd scan, next 15): high-PNL wallets
- Tier 3 (every 4th scan, next 25): remaining flagged wallets
- Sort by `signal_weight * accuracy_bonus` instead of raw PNL
  - `accuracy_bonus = accuracy if total >= 3 else 0.5`
- Track `_scan_count` to implement tiered rotation
- Net effect: 50 unique wallets tracked per hour instead of 20

**Rate limit math:**
- Tier 1: 10 wallets × 4 scans/hour = 40 requests
- Tier 2: 15 wallets × 2 scans/hour = 30 requests
- Tier 3: 25 wallets × 1 scan/hour = 25 requests
- Total: 95 requests/hour = ~1.6/min (well within limits)

---

### Task 6: Use /holders and /trades endpoints for market-level whale detection

**Files:** `src/positions/insider_tracker.py`

**Problem:** We query wallets → find their positions. But we should ALSO query markets → find their whales. The `/holders` endpoint gives us the top 20 holders for any market — instantly. And `/trades?filterAmount=10000` finds whale-sized trades across ALL markets without querying individual wallets.

**Changes:**

**6a. Market-level whale lookup via `/holders`:**
- Add method `_fetch_market_holders(client, condition_id)` → `GET /holders?market={cid}&limit=20`
- Call this for markets in our opportunity pipeline (from auto_trader's current scan results)
- Cross-reference returned wallets against `_flagged_wallets` — if a known conviction trader holds this market, that's a signal even if we didn't query that wallet's positions this scan
- Cache results per market (10-min TTL, same as existing cache)
- Rate impact: ~10-20 requests per scan for active opportunity markets

**6b. Global whale trade scanning via `/trades`:**
- Add method `_scan_whale_trades(client)` → `GET /trades?filterType=CASH&filterAmount=5000&limit=100`
- Run once per scan cycle — returns recent $5K+ trades across ALL markets
- Cross-reference with our matched events — if a whale trade lands on a market we're watching, flag it
- Track whale trade velocity: if 3+ large trades on same market within 1 hour = convergence signal
- No wallet-level tracking needed — this catches anonymous whale activity on Polymarket too

**Why:** `/holders` is the most efficient way to check if whales are in a specific market. `/trades` with filterAmount catches whale activity we'd otherwise miss because we only track 20 wallets.

---

### Task 7: Position-relative sizing signals

**Files:** `src/positions/insider_tracker.py`

**Problem:** A $5K insider position means very different things depending on market size. On a $50K market it's 10% (huge). On a $5M market it's 0.1% (noise).

**Changes in `get_insider_signal()`:**
- Accept optional `market_volume` parameter
- Compute `position_concentration = total_insider_value / market_volume` when available
- If concentration > 5%: boost signal strength by +0.15
- If concentration > 10%: boost by +0.25
- Pass market volume from auto_trader (already available in opportunity data)

---

## Implementation Order

**Phase 1 — Polymarket improvements (no new modules):**
1. **Task 1** (watchlist auto-refresh + leaderboard categories) — Quick win
2. **Task 4** (exit detection) — Purely additive to movement detection
3. **Task 5** (tiered polling) — Better wallet coverage
4. **Task 6** (/holders + /trades whale scanning) — Biggest Polymarket improvement
5. **Task 7** (position-relative sizing) — Small signal quality boost

**Phase 2 — Kalshi expansion:**
6. **Task 2** (Kalshi anonymous whale tracker) — New module
7. **Task 3** (cross-platform convergence) — Depends on Task 2

Phase 1 tasks are independent and can be done in parallel.
Phase 2 tasks are sequential (Task 3 depends on Task 2).

## What NOT to do

- **Don't pay for Oddpool/FORCASTR** — the free Kalshi public API gives us enough for anonymous whale detection
- **Don't add WebSocket connections to Kalshi** — REST polling every 5 min is sufficient for our scan interval and avoids connection management complexity
- **Don't auto-trade from insider signals alone** — keep signals as score multipliers, not standalone triggers
- **Don't remove hardcoded watchlist** — it's a good safety net. Auto-refresh adds to it, never replaces it
