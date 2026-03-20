# Arbitrout Performance Fix — Three-Phase Rollout

**Date:** 2026-03-19
**Problem:** 31 closed trades, 9.7% win rate, -$160.89 P&L. System makes directional bets labeled as arbitrage. Zero real arbitrage profits. Fees = 65% of losses.

## Phase 1: BTC 5-Min Directional Sniper

### What
New module `src/positions/btc_sniper.py` that trades Polymarket's 5-minute BTC up/down markets using real-time Binance price data to predict direction before the window closes.

### Why
Research shows bots making $150K+ with 85-98% win rates on these markets. The edge: at T-10 seconds before close, ~85% of BTC direction is determined from spot price, but Polymarket odds lag. Maker orders = 0% fee + daily USDC rebates.

### How
1. Stream BTC trades from Binance WebSocket (`wss://stream.binance.com:9443/ws/btcusdt@trade`)
2. Track 5-min windows (Unix timestamps divisible by 300)
3. At T-10s, compute composite signal:
   - Window delta (weight 5-7): current BTC vs window open. >0.10% move = max weight
   - Micro momentum (weight 2): direction of last two 1-min candle closes
   - Tick trend (weight 2): micro-trend from 2-second price samples
   - Confidence = min(abs(score) / 7.0, 1.0)
4. If confidence > 30%, place maker limit order at $0.90-0.95 on winning side
5. FOK market order fallback if maker doesn't fill by T-5s
6. Auto-claim after resolution

### Market mechanics
- Slug: deterministic from window timestamp
- Token resolution: CLOB REST `GET /markets/{conditionId}` → tokens[0]=UP, tokens[1]=DOWN
- Fees: 0% maker, up to 1.56% taker on 5-min crypto. Maker rebates = 20% of taker pool daily.
- Resolution: compare 1-min candle open (window start) vs close (window end) from Binance

### Position sizing
- Safe mode: 25% of sniper bankroll per trade (default)
- Aggressive mode: all gains above initial investment
- Min bet: $1.00 USDC
- Bankroll allocation: $500 initial, isolated from other strategies

### Dependencies
- `websockets` package for Binance stream
- Existing `py_clob_client` for CLOB orders
- Existing `PolymarketExecutor` for token resolution

### Files
- NEW: `src/positions/btc_sniper.py` — main sniper loop + signal computation
- EDIT: `src/server.py` — add sniper startup to lifespan
- EDIT: `src/positions/wallet_config.py` — add sniper bankroll allocation

---

## Phase 2: Market Making

### What
New module `src/positions/market_maker.py` that provides liquidity on both sides of Polymarket crypto markets, profiting from bid-ask spread + maker rebates.

### Why
Market making is the best risk-adjusted strategy (70-80% win rate, 2-5% monthly). 0% maker fees + daily USDC rebates. Lower variance than directional trading.

### How
1. Select target markets: high-volume crypto markets where maker rebates are active (BTC 1H, 4H, Daily, Weekly)
2. Every 5-10 seconds, fetch current orderbook
3. Calculate fair price from Binance spot feed (shared with sniper)
4. Place maker limit orders on BOTH sides:
   - YES bid at `fair_price - half_spread`
   - NO bid at `(1 - fair_price) - half_spread`
   - Combined cost < $1.00 = guaranteed profit when both fill
5. Cancel-replace when price moves beyond staleness threshold (>3s or >0.5% move)
6. Inventory management: track net YES/NO exposure, widen spread on overweight side

### Parameters
- Target spread: 2-4% for liquid markets, 5-8% for volatile
- Max inventory imbalance: 70/30 (one side can't exceed 70%)
- Max capital per market: $500
- Total allocation: $1,000 across 2-4 markets
- Quote refresh: every 5-10 seconds

### Risk controls
- Auto-withdraw from markets <2 hours to expiry
- Price circuit breaker: halt if external feed diverges >5% from Polymarket mid
- Max loss per market: hard cap at capital allocation
- Resolution risk: close all positions before market resolves

### Files
- NEW: `src/positions/market_maker.py` — market making loop + inventory management
- NEW: `src/positions/price_feed.py` — shared Binance WebSocket price feed (used by both sniper and MM)
- EDIT: `src/server.py` — add market maker startup
- EDIT: `src/positions/wallet_config.py` — add MM bankroll allocation

---

## Phase 3: Fix Existing Arb/Synth System

### 3a. Fix Cross-Platform Arb Execution

**Problem:** `_arb_to_opportunity()` uses `event_id` but platforms use different IDs for the same event. When both legs resolve to the same platform, trade falls through to `pure_prediction`.

**Fix:**
- In `_arb_to_opportunity()`: iterate ALL markets in the matched event, not just matching by platform name. Use `market_id` field directly.
- Add validation: reject same-platform arb (YES+NO on same platform = guaranteed loss after fees)
- Add per-platform-pair minimum spread: Poly+Kalshi needs 3.5% min, Poly-only is invalid for arb
- Log when cross-platform matches are found but can't be executed (missing market IDs, insufficient spread)

**Files:** `src/positions/auto_trader.py` (lines 715-755, `_arb_to_opportunity`)

### 3b. Fix Synthetic Derivatives

**Problem:** Requires `is_cross_platform AND is_synthetic` — both conditions rarely true together.

**Fix:**
- Allow same-platform synthetics: "BTC above $90K" YES + "BTC above $100K" NO on Polymarket = valid bull spread
- Change strategy assignment (line 516-521): `is_synthetic` alone → `"synthetic_derivative"` strategy, remove cross-platform requirement
- Add bull/bear spread construction in `arbitrage_engine.py`:
  - Bull spread: YES(lower strike) + NO(higher strike)
  - Bear spread: NO(lower strike) + YES(higher strike)
- Fix exit rules for synthetics: synthetics should be held to resolution (not trailed/stopped like directional bets)

**Files:** `src/positions/auto_trader.py` (lines 509-572), `src/arbitrage_engine.py`

### 3c. Multi-Outcome Arbitrage (new)

**What:** For events with 3+ outcomes (elections, sports champions), buy all outcomes when sum < $1.00.

**How:**
- Add `_scan_multi_outcome()` to `arbitrage_engine.py`
- Fetch grouped events from Polymarket Gamma API `events` endpoint (returns all markets under one event)
- Sum all YES prices. If `sum < (1.0 - fee_threshold)`, it's an arb opportunity
- Create package with one leg per outcome, sized proportionally
- Use maker orders for 0% entry

**Files:** `src/arbitrage_engine.py` (new method), `src/positions/auto_trader.py` (new handler)

---

---

## Phase 4: Arbigab-Inspired Improvements

**Date:** 2026-03-19
**Source:** Analysis of Arbigab trading bot (gabagool22.com/how-it-works)

### 4a. Multi-Asset Price Feed

**What:** Extend price feed from BTC-only to BTC, ETH, SOL, XRP via Binance combined WebSocket stream.

**How:**
- Single combined stream URL: `wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade/...`
- Per-asset `AssetState` dataclass: separate price, candles, window, tick samples
- Backward-compatible: `.price` still returns BTC price for existing code
- Configurable via `SNIPER_ASSETS` env var (comma-separated)
- Market maker auto-detects asset type from market title

**Files:** EDIT `price_feed.py`, EDIT `btc_sniper.py`, EDIT `server.py`

### 4b. Event-Driven Evaluation

**What:** Replace 2-second polling interval with per-tick evaluation using WebSocket callbacks.

**Why:** Arbigab evaluates on every Binance aggTrade tick. Fixed 2s intervals miss up to 2 seconds of price data and react late to spikes. Event-driven provides sub-100ms signal evaluation.

**How:**
- `on_tick(callback)` method on price feed: registers synchronous callbacks
- Callbacks fire inline in WebSocket handler for minimum latency
- Sniper uses `asyncio.Event` + `wait_for(timeout=2.0)`: wakes instantly on tick, falls back to 2s polling if no ticks arrive
- Market maker's preemptive cancel uses same `on_tick` mechanism

**Files:** EDIT `price_feed.py`, EDIT `btc_sniper.py`

### 4c. Preemptive Cancel (Market Maker)

**What:** Cancel exposed market maker orders BEFORE adverse Binance price moves cause toxic fills.

**Why:** The 8-second quote refresh is too slow. If BTC drops 0.3% in 2 seconds, our YES bid is now mispriced. Arbigab monitors every tick and cancels instantly.

**How:**
- Market maker registers `_preemptive_cancel_check` as `on_tick` callback
- On each tick: for each market, check if price moved >0.3% against exposed orders (YES without NO, or vice versa)
- If threshold breached: queue `(condition_id, side)` for cancellation
- Async loop drains cancel queue at start of each iteration
- Tracks `preemptive_cancels` count in stats
- `quote_ref_price` recorded on each quote update for delta calculation

**Files:** EDIT `market_maker.py`

### 4d. On-Chain Token Merging

**What:** When both YES+NO sides fill, merge matched tokens back to $1.00 USDC via CTF contract instead of waiting for market resolution.

**Why:** Arbigab's ProxyWallet Factory merges tokens on-chain for instant profit realization. Benefits:
- Frees capital immediately for next trade cycle
- Eliminates resolution timing risk
- Realizes profit in USDC, not binary tokens

**How:**
- After detecting matched fills (YES shares > 0 AND NO shares > 0), calculate matched quantity
- If matched >= 1.0 shares: call `clob.merge_positions(condition_id, amount_wei)` via CTF
- Deduct merged shares from inventory, add profit to realized P&L
- Return merged USDC to `total_capital` for reuse
- Falls back gracefully: if merge fails, tokens remain as inventory until resolution

**Files:** EDIT `market_maker.py`

---

## Bankroll Allocation

| Strategy | Allocation | Risk Profile |
|----------|------------|--------------|
| Crypto 5-Min Sniper (BTC/ETH/SOL/XRP) | $500 | High frequency, high win rate |
| Market Making (preemptive cancel + merge) | $1,000 | Low risk, steady returns |
| Cross-Platform Arb | $300 | Risk-free when executed |
| Synthetics/Multi-Outcome | $200 | Low frequency, guaranteed |
| Total | $2,000 | |

## Shared Infrastructure

### Price Feed (`price_feed.py`)
Single Binance combined WebSocket stream shared between sniper and market maker. Provides:
- Real-time spot prices for BTC, ETH, SOL, XRP (configurable)
- Per-asset 1-minute candle history (last 10 candles)
- Per-asset window open price tracking for 5-min markets
- Micro momentum and tick trend signals per asset
- Event-driven `on_tick` callbacks for preemptive cancel and instant evaluation

### Integration with Existing Systems
- All strategies use existing `PositionManager` for CRUD
- All positions monitored by existing `ExitEngine`
- All trades recorded in existing `TradeJournal`
- Sniper and MM positions tagged with strategy type for separate P&L tracking
