# Project: Arbitrout (Prediction Market Arbitrage + Auto Trading)
Status: ACTIVE
Phase: BUILD
Last Updated: 2026-03-18
Repo: https://github.com/sorry-ari0/arbitrout.git
Branch: feat/derivative-position-manager

## Overview
Arbitrout is a prediction market trading system with three core capabilities:
1. **Cross-platform arbitrage scanner** — finds price discrepancies across Polymarket, PredictIt, Limitless, Kalshi, and more
2. **Autonomous paper trading** — auto trader scans Polymarket for crypto markets, opens positions with risk management
3. **Insider/whale tracker** — monitors top Polymarket traders and uses their positions as trading signals

Integrated into the Lobsterminal financial terminal as a switchable tab. Backend is Python FastAPI on port 8500.

## System Interaction Map

### Dependency Wiring (server.py lifespan init)

```
server.py creates all subsystems and injects dependencies:

  AdapterRegistry ──────────────────────> ArbitrageScanner ·····> AutoTrader
  (10 platform adapters)                  (60s scan loop)         (optional
                                                                   opp source)

  Real Executors ──> PaperExecutor ──────> PositionManager
  (polymarket,       (simulated $,         (CRUD, execute,
   kalshi, etc.)      real prices,          persist, P&L)
                      maker/taker fees)        |
                                               |──────> ExitEngine (30s loop)
                                               |──────> AutoTrader (5m loop)
                                               |──────> InsiderTracker (15m loop)

  TradeJournal <─── PositionManager        (records on close)
  AIAdvisor    <─── ExitEngine             (reviews non-safety triggers)
  InsiderTracker ──> AutoTrader            (signal boost for scoring)
```

### Runtime Data Flow

```
+---------------------------------------------------------------------+
|                      ARBITRAGE PIPELINE                              |
|  (independent — feeds dashboard + optional input to auto trader)     |
|                                                                      |
|  10 Adapters ──fetch──> NormalizedEvents                             |
|       |                      |                                       |
|       |              entity extraction                               |
|       |              (crypto tickers, names,                         |
|       |               countries, key terms)                          |
|       |                      |                                       |
|       |              two-phase matching                              |
|       |              + Union-Find clustering                         |
|       |                      |                                       |
|       |               MatchedEvents                                  |
|       |              (same event, 2+ platforms)                      |
|       |                      |                                       |
|       |              spread calculation                              |
|       |              spread = 1.0 - (best_yes + best_no)             |
|       |                      |                                       |
|       |              ArbitrageOpportunity                            |
|       |              (sorted by profit_pct)                          |
|       |                      |                                       |
|       +--- price feed ------+-----> /api/arbitrage/* endpoints       |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|                     DERIVATIVE POSITION PIPELINE                     |
|  (auto trader opens → exit engine monitors → journal records)        |
|                                                                      |
|  AutoTrader (every 5m)                                               |
|       |                                                              |
|       |  1. Check scanner for cross-platform arb opportunities       |
|       |  2. Fallback: scan Polymarket Gamma API for crypto markets   |
|       |  3. Query InsiderTracker for whale signal boost              |
|       |  4. Score: profit * crypto * expiry * volume * insider       |
|       |  5. Filter: net profit > 3% after 2% round-trip fees        |
|       |                                                              |
|       +---> PositionManager.execute_package()                        |
|                  |                                                   |
|                  +---> PaperExecutor.buy() per leg                   |
|                  |         (real price lookup, simulated money)       |
|                  +---> positions.json (atomic write)                 |
|                                                                      |
|  ExitEngine (every 30s)                                              |
|       |                                                              |
|       |  For each open package:                                      |
|       |  1. Fetch current prices (PaperExecutor → real API)          |
|       |  2. Update P&L (deduct 1% estimated sell fees)               |
|       |  3. Evaluate 18 heuristic triggers                           |
|       |  4. Route triggers:                                          |
|       |                                                              |
|       |     SAFETY OVERRIDES (immediate, no AI):                     |
|       |       #7  spread_inversion (YES+NO > 1.0)                    |
|       |       #10 time_24h (expiry < 24 hours)                       |
|       |       #11 time_6h  (expiry < 6 hours)                        |
|       |           |                                                  |
|       |           +---> PositionManager.exit_leg() immediately       |
|       |                                                              |
|       |     NON-SAFETY (batched to AI):                              |
|       |       #1 target_hit, #2 trailing_stop, #4 stop_loss...       |
|       |           |                                                  |
|       |           +---> AIAdvisor.review_proposals()                 |
|       |                    |                                         |
|       |                    +-- APPROVE --> exit_leg()                 |
|       |                    +-- MODIFY  --> adjust rule params         |
|       |                    +-- REJECT  --> skip (log reason)          |
|       |                    +-- FAIL    --> escalation alert           |
|       |                                                              |
|       +---> TradeJournal.record_close() (when all legs closed)       |
|                  (fees, P&L, trigger, execution log)                 |
|                                                                      |
|  InsiderTracker (every 15m)                                          |
|       |                                                              |
|       |  1. Fetch Polymarket leaderboard (OVERALL + CRYPTO)          |
|       |  2. Flag traders with >$10K lifetime PNL                     |
|       |  3. Fetch positions for top 20 wallets                       |
|       |  4. Compare vs previous scan → detect movements              |
|       |       mass entry (2+ insiders same market)                   |
|       |       size increase (>50% growth)                            |
|       |  5. Track per-wallet accuracy on resolved markets            |
|       |                                                              |
|       +---> AutoTrader reads signals to boost trade scores           |
|             signal = 0.2*count + 0.3*value + 0.2*suspicious          |
|                      + 0.3*accuracy                                  |
+---------------------------------------------------------------------+
```

### How Arbitrage Evaluation Works

The arbitrage scanner finds risk-free profit opportunities by comparing the same event across platforms:

```
Example: "Will BTC exceed $100K by June 2026?"

  Polymarket:  YES = $0.42    NO = $0.58    (total = $1.00)
  PredictIt:   YES = $0.55    NO = $0.48    (total = $1.03)

  Best YES: Polymarket $0.42
  Best NO:  PredictIt  $0.48
  Total cost: $0.90 per pair

  Spread = 1.0 - (0.42 + 0.48) = 0.10  →  10% guaranteed profit
  One side MUST resolve to $1.00, so you always collect $1.00 for $0.90 spent.
```

The matcher finds these by:
1. Extracting entities from titles (crypto: BTC + $100K + "above", names, countries)
2. Quick filter: must share a key entity (same ticker, same person + context, etc.)
3. Detailed scoring: weighted overlap across all entity types (0.0-1.0)
4. Threshold: score >= 0.45 to cluster events together
5. Union-Find groups all matching pairs into clusters
6. For each multi-platform cluster: find cheapest YES and cheapest NO on different platforms

### How Derivative Position Evaluation Works

Derivative packages are synthetic positions — grouped bets with automated risk management:

```
Package: "Auto: Will ETH hit $5000?"
  Strategy: pure_prediction
  Legs:
    - YES @ polymarket  entry=$0.35  qty=142.86  cost=$50
    - NO  @ polymarket  entry=$0.65  qty=76.92   cost=$50
  Exit Rules:
    - target_profit: exit all if P&L >= +15%
    - stop_loss:     exit all if P&L <= -10%
    - trailing_stop: exit if drawdown from peak >= 8%

  P&L Calculation (every 30s):
    current_value   = sum(qty * current_price) for open legs
    estimated_fees  = current_value * 1% (conservative sell-side)
    net_value       = current_value - estimated_fees
    unrealized_pnl  = net_value - total_cost
    pnl_pct         = unrealized_pnl / total_cost * 100

  ITM/OTM per leg:
    YES leg: ITM if current_price > entry_price, else OTM
    NO  leg: ITM if current_price < entry_price, else OTM
  Package level: ITM if unrealized_pnl > 0, OTM if < 0
```

The exit engine evaluates 18 triggers organized in 6 categories:

```
PROFIT TAKING (1-3):     target hit, trailing stop, partial profit
LOSS PREVENTION (4-6):   stop loss, new ATH (tighten trail), correlation break
SPREAD / ARB (7-9):      spread inversion*, spread compression, volume dry-up
TIME (10-12):            time <24h*, time <6h*, time decay (3-7 days)
VOLATILITY (13-15):      vol spike (>15% move), vol crush, negative drift
PLATFORM (16-18):        platform error (3+ consecutive), liquidity gap, fee spike

* = SAFETY OVERRIDE — executes immediately, bypasses AI
```

The AI advisor (Claude) reviews non-safety triggers with full context:
- Package state (legs, prices, P&L, rules with bounds)
- Returns per-rule verdicts: APPROVE (execute), MODIFY (adjust within bounds), REJECT (skip)
- Rate limited to 10 calls/min, runs sync API in thread executor

### Auto Trader Scoring Formula

```
For each crypto market opportunity:

  raw_profit  = ((1.0 - favored_price) / favored_price) * 100
  net_profit  = raw_profit - 2%  (round-trip fee estimate)

  score = net_profit
  if crypto keyword in title:     score *= 2.0
  if expiry <= 30 days:           score *= 1.5
  if expiry <= 7 days:            score *= 2.0
  if volume > 10,000:             score *= 1.2
  if insider signal exists:
    score *= (1.0 + signal_strength * 2.0)
    if suspicious insiders:       score *= 1.5

  ENTER if: score >= 3.0 AND net_profit >= 3%
  SIZE: min($200, remaining_budget/2), floor $25
  LIMITS: 10 concurrent, $2000 total exposure
```

### Insider Signal Strength Formula

```
signal_strength = 0.2 * normalized_insider_count
                + 0.3 * normalized_total_value
                + 0.2 * normalized_suspicious_count
                + 0.3 * accuracy_score

  insider_count:     number of flagged wallets holding this market
  total_value:       combined USD value of insider positions
  suspicious_count:  insiders with win rate > 80%
  accuracy_score:    weighted average of per-wallet accuracy
                     (only wallets with 3+ resolved markets count)
```

## Architecture

### Backend Stack
- **Framework:** Python FastAPI + uvicorn
- **Server:** `src/server.py` — main app, lifespan init, all subsystem wiring
- **Port:** 8500 (local only)
- **Auto-scan:** Background tasks: arbitrage (60s), exit engine (30s), auto trader (5m), insider tracker (15m)
- **GPU:** Intel Arc 140V (~7GB VRAM) — one 8B model at a time for Ollama

---

## Subsystem 1: Arbitrage Scanner

### Files
| File | Purpose |
|------|---------|
| `src/arbitrage_engine.py` | Cross-platform spread detection, price feed computation, scan orchestrator |
| `src/arbitrage_router.py` | FastAPI router at `/api/arbitrage/` — endpoints + WebSocket |
| `src/event_matcher.py` | Entity-based event matching across platforms using two-phase algorithm |
| `src/adapters/models.py` | Shared dataclasses: `NormalizedEvent`, `MatchedEvent`, `ArbitrageOpportunity` |
| `src/adapters/base.py` | Abstract `BaseAdapter` — rate limiting, caching, error handling, shared HTTP client |
| `src/adapters/registry.py` | `AdapterRegistry` — registers adapters, concurrent `fetch_all()`, status reporting |
| `src/adapters/polymarket.py` | Polymarket Gamma API adapter |
| `src/adapters/predictit.py` | PredictIt API adapter |
| `src/adapters/kalshi.py` | Kalshi API adapter (needs API key) |
| `src/adapters/limitless.py` | Limitless API adapter |
| `src/adapters/robinhood.py` | Robinhood scraper adapter |
| `src/adapters/coinbase.py` | Coinbase scraper adapter |
| `src/adapters/crypto_spot.py` | CoinGecko spot price adapter for crypto |
| `src/adapters/commodities.py` | Commodities adapter |
| `src/adapters/opinion_labs.py` | Opinion Labs API adapter (needs API key) |

### How Arbitrage Detection Works

**Step 1: Fetch events from all platforms**
- `AdapterRegistry.fetch_all()` calls every adapter concurrently via `asyncio.gather()`
- Each adapter inherits `BaseAdapter` which handles rate limiting (min 1s between requests), HTTP client management, caching (returns stale cache on error), and status tracking
- All events normalized to `NormalizedEvent` dataclass: platform, event_id, title, category, yes_price (0-1), no_price (0-1), volume, expiry, url

**Step 2: Match events across platforms (event_matcher.py)**
- Two-phase matching with Union-Find clustering:
  - **Phase 0:** Manual links (highest priority) — user can manually link events via `/api/arbitrage/link`
  - **Phase 1:** Quick filter — events must share a key entity to be candidates (same crypto ticker, shared person name + 2+ context terms, shared quoted term, shared country + 3+ key terms, or 5+ overlapping key terms)
  - **Phase 2:** Detailed entity overlap scoring on candidates that pass Phase 1
- Entity extraction from titles:
  - **Crypto:** Ticker normalization (30+ aliases: "bitcoin"→BTC, "ethereum"→ETH, etc.), price targets, direction (above/below)
  - **Names:** Capitalized proper nouns (3+ chars), excluding common words and countries
  - **Countries:** 30+ country/nationality patterns
  - **Quoted terms:** Terms in quotes
  - **Key terms:** Non-stopword terms (3+ chars)
- Entity overlap scoring (0.0-1.0): crypto ticker match (3pts), names (2pts), countries (1pt), quoted terms (2pts), key terms (2pts, proportional)
- Match threshold: 0.45 score required
- Additional filters: same-platform events never match, category must be compatible, expiry must be within 7 days
- PredictIt titles cleaned ("Market: Contract" → "Market")
- Polymarket interval markets ("Up or Down - 9:55PM-10:00PM") only match other interval markets
- Output: `MatchedEvent` objects with canonical title, category, all platform markets grouped

**Step 3: Find arbitrage (arbitrage_engine.py)**
- For each `MatchedEvent` with markets on 2+ platforms:
  - `best_yes` = cheapest YES price across platforms
  - `best_no` = cheapest NO price across platforms
  - Must be on different platforms (otherwise tries second-best)
  - `spread = 1.0 - (best_yes + best_no)`
  - If spread > 0 → arbitrage exists, `profit_pct = spread * 100`
- Also computes allocation percentages: how much capital to put in YES vs NO
- Results sorted by profit_pct descending

**Step 4: Price feed (arbitrage_engine.py)**
- `compute_feed()` tracks price changes between scans using `_previous_prices` dict (thread-safe with `threading.Lock`)
- Returns recent changes sorted by absolute change, capped at 50 items
- Each feed item: platform, event_id, title, yes_price, previous, change, change_pct, timestamp

**Step 5: Cache to disk**
- Latest events saved to `src/data/arbitrage/cache.json` for offline viewing
- Saved/bookmarked markets stored in `src/data/arbitrage/saved_markets.json`
- Manual links stored in `src/data/arbitrage/manual_links.json`

### Arbitrage API Endpoints (`/api/arbitrage/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/opportunities` | Current arbitrage spreads, sorted by profit % |
| GET | `/events` | All matched events across platforms |
| GET | `/feed` | Recent odds/price changes |
| GET | `/platforms` | Platform adapter status (up/down/error/cached count) |
| POST | `/scan` | Manually trigger a full scan cycle |
| POST | `/link` | Manually link events across platforms (body: `{event_ids: [...]}`) |
| DELETE | `/link/{link_id}` | Remove a manual link |
| GET | `/saved` | Get bookmarked markets |
| POST | `/saved` | Bookmark a matched event |
| DELETE | `/saved/{match_id}` | Remove a bookmark |
| WS | `/ws` | Real-time WebSocket — sends init state, pushes scan results. Client actions: `scan`, `ping` |

### Data Models
```
NormalizedEvent:
  platform, event_id, title, category, yes_price (0-1), no_price (0-1),
  volume, expiry, url, last_updated

MatchedEvent:
  match_id, canonical_title, category, expiry, markets[], match_type (auto|manual)
  platform_count (computed property)

ArbitrageOpportunity:
  matched_event, buy_yes_platform, buy_yes_price, buy_no_platform, buy_no_price,
  spread, profit_pct, combined_volume, yes_allocation_pct, no_allocation_pct
```

---

## Subsystem 2: Derivative Position System

### Files
| File | Purpose |
|------|---------|
| `src/positions/position_manager.py` | CRUD, persistence, execution, rollback for derivative packages |
| `src/positions/position_router.py` | FastAPI router at `/api/derivatives/` — all position endpoints + WebSocket |
| `src/positions/exit_engine.py` | 30s scan loop, 18 heuristic triggers, safety overrides |
| `src/positions/auto_trader.py` | Autonomous paper trader — scans Polymarket Gamma API for crypto markets |
| `src/positions/trade_journal.py` | Records completed trades with P&L, fees, exit triggers, performance analytics |
| `src/positions/insider_tracker.py` | Whale/insider monitor — leaderboard, positions, accuracy tracking, movement alerts |
| `src/positions/ai_advisor.py` | Claude AI advisor for exit decisions (requires ANTHROPIC_API_KEY) |
| `src/positions/wallet_config.py` | Paper/live mode config, .env loading, key validation, safe config API |

### How Derivative Packages Work

**Package structure:**
- A "package" is a grouped position with one or more "legs" (individual market bets) and "exit rules"
- Strategy types: `spot_plus_hedge`, `cross_platform_arb`, `pure_prediction`
- Each package tracks: status (open/closed/partial_exit/rollback), total_cost, current_value, peak_value, unrealized_pnl, itm_status (ITM/OTM/ATM), execution_log, ai_strategy

**Leg structure:**
- Each leg: platform, type (prediction_yes/prediction_no/spot_buy), asset_id, entry_price, current_price, quantity (derived: cost/price), cost, current_value, expiry, status, buy_fees, sell_fees, tx_id

**Exit rule structure:**
- Each rule: type, params (with bounds for AI modification), active flag
- Types: `target_profit` (target_pct), `stop_loss` (stop_pct), `trailing_stop` (current, bound_min, bound_max), `partial_profit` (threshold_pct)

**Execution flow:**
1. `PositionManager.execute_package(pkg)` acquires async lock
2. For each leg, calls `executor.buy(asset_id, amount_usd)` on the platform executor
3. Records tx_id, filled_price, filled_quantity, buy_fees from `ExecutionResult`
4. On any leg failure → automatic rollback: sells all previously executed legs
5. Robinhood legs get "advisory" status (no execution, user must trade manually)
6. Package saved to `positions.json` via atomic write (write to .tmp, `os.replace`)

**P&L calculation (`update_pnl`):**
- Current value = sum(quantity * current_price) for open legs
- Estimated sell fees = 1% of current value (conservative: sometimes limit, sometimes market)
- Net value = current_value - estimated_sell_fees
- Unrealized P&L = net_value - total_cost
- Peak value tracked for trailing stop calculations
- Per-leg ITM/OTM: YES legs ITM when price > entry, NO legs ITM when price < entry

**Rollback on failure:**
- If any leg execution fails, all previously bought legs are sold back
- Rollback status tracked per leg: "rolled_back" or "rollback_failed"
- Package marked as "rollback" status

### How the Exit Engine Works

The exit engine runs a 30-second scan loop evaluating all open packages.

**Each tick:**
1. Fetch current prices for all open legs via platform executors
2. Recalculate P&L (fee-aware)
3. Track negative streak counter
4. Evaluate all 18 heuristic triggers
5. Route fired triggers: safety overrides execute immediately, others go to AI advisor

**18 Heuristic Triggers:**

| ID | Name | Category | Action | Safety? |
|----|------|----------|--------|---------|
| 1 | target_hit | Profit Taking | full_exit | No |
| 2 | trailing_stop | Profit Taking | full_exit | No |
| 3 | partial_profit | Profit Taking | partial_exit | No |
| 4 | stop_loss | Loss Prevention | full_exit | No |
| 5 | new_ath | Loss Prevention | tighten_trail | No |
| 6 | correlation_break | Loss Prevention | review | No |
| 7 | spread_inversion | Spread/Arb | immediate_exit | **YES** |
| 8 | spread_compression | Spread/Arb | review | No |
| 9 | volume_dry | Spread/Arb | review | No (placeholder) |
| 10 | time_24h | Time | immediate_exit | **YES** |
| 11 | time_6h | Time | immediate_exit | **YES** |
| 12 | time_decay | Time | review | No |
| 13 | vol_spike | Volatility | review | No |
| 14 | vol_crush | Volatility | review | No (placeholder) |
| 15 | negative_drift | Volatility | review | No |
| 16 | platform_error | Platform | review | No |
| 17 | liquidity_gap | Platform | review | No (placeholder) |
| 18 | fee_spike | Platform | review | No (placeholder) |

**Safety overrides (triggers 7, 10, 11):**
- Execute immediately — no AI review, no escalation
- Spread inversion: YES + NO prices > 1.0 (guaranteed loss if held)
- Time <24h / <6h: expiry approaching, must exit regardless of P&L

**Non-safety trigger routing:**
- If AI advisor available → batch all non-safety triggers to Claude for review
- AI returns: APPROVE (execute), MODIFY (adjust rule params within bounds), REJECT (skip)
- If AI fails or unavailable → escalate as alerts for human review

### How the AI Advisor Works (`ai_advisor.py`)

- Uses Claude API (default model: `claude-sonnet-4-20250514`, configurable via `CLAUDE_MODEL` env var)
- Rate limited: max 10 calls/minute (sliding window)
- Lazy-inits Anthropic client from `ANTHROPIC_API_KEY`
- Builds structured prompt with: package context (legs, P&L, rules), triggered proposals
- Expects response format: `<rule_id>: APPROVE | MODIFY <value> | REJECT <reason>`
- Parses response into verdicts dict
- Runs sync API call in `run_in_executor` to avoid blocking event loop
- On failure: raises exception, exit engine catches and creates escalation alert

### How the Auto Trader Works

1. Every 5 minutes, scans Polymarket Gamma API for crypto prediction markets
2. Fetches 200 markets in bulk (`GET gamma-api.polymarket.com/markets?closed=false&limit=100&order=volume`), filters by crypto keywords (btc, eth, sol, xrp, doge, etc.)
3. Parses `outcomePrices` (JSON string like `'["0.475","0.525"]'`) → YES/NO prices
4. Skips near-resolved markets (>0.95 or <0.05)
5. Calculates profit potential AFTER estimated round-trip fees: `raw_profit = ((1.0 - favored_price) / favored_price) * 100`, then `net_profit = raw_profit - 2%` round-trip fees
6. Scores each market: `profit_pct * crypto_boost(2x) * expiry_boost(1.5x if ≤30d, 2x if ≤7d) * volume_boost(1.2x if >10K) * insider_boost`
7. Insider signal boost: if insiders have positions, `score *= (1 + strength * 2)`. Suspicious insiders add extra 1.5x
8. Requires minimum 3% net spread after fees to enter
9. Creates packages with YES and NO legs, exit rules: target profit (15%), stop loss (-10%), trailing stop (8%, bounds 3-20%)
10. Position limits: $200 max per trade, $25 min, 10 concurrent positions, $2000 total exposure

### How the Insider Tracker Works

1. Every 15 minutes, fetches Polymarket leaderboard from Data API (`data-api.polymarket.com/v1/leaderboard`)
2. Fetches both OVERALL and CRYPTO categories
3. Flags traders with >$10K lifetime PNL, marks as "suspicious" if win rate >80%
4. Fetches current positions for top 20 flagged wallets via `/positions` endpoint
5. Compares current vs previous scan positions to detect movements:
   - **Mass entry:** 2+ insiders enter same market → alert
   - **Size increase:** Position grows >50% → alert
6. Auto-triggers on significant movements ($5K+ value or suspicious wallets)
7. Signal strength formula: `0.2*insider_count + 0.3*total_value + 0.2*suspicious_count + 0.3*accuracy_score`
8. Accuracy tracking: when markets resolve, `record_resolution()` compares insider direction vs outcome
9. Per-wallet accuracy: min 3 resolved markets before contributing to signal strength
10. Data persisted to `insider_signals.json`

### Derivative API Endpoints (`/api/derivatives/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/packages` | List packages (filter: `?status=open\|closed`) |
| POST | `/packages` | Create new package with legs and exit rules |
| GET | `/packages/{id}` | Get specific package |
| PATCH | `/packages/{id}` | Update package (name, exit_rules, ai_strategy) |
| DELETE | `/packages/{id}` | Force close all legs and delete |
| POST | `/packages/{id}/exit` | Full exit — close all open legs |
| POST | `/packages/{id}/exit-leg/{leg_id}` | Exit single leg |
| POST | `/packages/{id}/confirm-stock` | Confirm advisory legs (Robinhood) |
| GET | `/packages/{id}/rules` | List exit rules |
| POST | `/packages/{id}/rules` | Add exit rule |
| PATCH | `/packages/{id}/rules/{rule_id}` | Update rule params/active |
| DELETE | `/packages/{id}/rules/{rule_id}` | Delete exit rule |
| GET | `/dashboard` | Portfolio stats (open/closed count, P&L, win rate, pending alerts) |
| GET | `/dashboard/alerts` | Pending escalation alerts |
| POST | `/dashboard/alerts/{id}/approve` | Approve alert → execute exit |
| POST | `/dashboard/alerts/{id}/reject` | Reject alert |
| GET | `/balances` | Executor balances (paper + real per platform) |
| GET | `/config` | System config (paper mode, platforms, AI enabled, exit engine running) |
| GET | `/journal` | Trade journal entries (`?limit=20`) |
| GET | `/journal/performance` | Aggregate P&L, win rate, fees, by strategy/trigger (`?mode=&strategy=`) |
| GET | `/auto-trader` | Auto trader stats (running, trades opened/skipped, exposure) |
| GET | `/insiders` | Insider tracker stats + top flagged wallets |
| GET | `/insiders/signals` | Insider signals for markets (`?condition_id=`) |
| GET | `/insiders/alerts` | Recent movement alerts (last 20) |
| GET | `/insiders/accuracy` | Per-wallet accuracy (min 3 resolved, sorted by accuracy) |
| WS | `/ws` | Real-time WebSocket for position updates |

---

## Subsystem 3: Execution Layer

### Files
| File | Purpose |
|------|---------|
| `src/execution/base_executor.py` | Abstract `BaseExecutor` ABC + shared dataclasses |
| `src/execution/paper_executor.py` | Wraps real executors — real prices, simulated money, maker/taker fee simulation |
| `src/execution/polymarket_executor.py` | Polymarket CLOB client + Gamma API price lookup |
| `src/execution/kalshi_executor.py` | Kalshi trading API |
| `src/execution/coinbase_spot_executor.py` | Coinbase spot trading |
| `src/execution/predictit_executor.py` | PredictIt share trading |
| `src/execution/robinhood_advisor.py` | Advisory-only (no execution, user trades manually) |

### Executor Interface
All executors implement `BaseExecutor`:
```
buy(asset_id, amount_usd) → ExecutionResult(success, tx_id, filled_price, filled_quantity, fees, error)
sell(asset_id, quantity) → ExecutionResult
get_balance() → BalanceResult(available, total)
get_positions() → list[PositionInfo(asset_id, quantity, avg_entry_price, current_price, unrealized_pnl)]
get_current_price(asset_id) → float
is_configured() → bool
```

### Paper Executor
- Wraps a real executor for price lookups but simulates money
- Starting balance: $10,000 (configurable via `PAPER_STARTING_BALANCE` env var)
- Separate maker/taker fee rates (split buy vs sell):
  - Polymarket: 0% maker / 2% taker
  - Kalshi: 0.5% maker / 1% taker
  - Coinbase: 0.4% maker / 0.6% taker
  - PredictIt: 5% / 5%
- **Buys use maker rate** (limit orders, 0% for Polymarket), **sells always use taker rate** (2% for Polymarket)
- `sell(asset_id, quantity, last_known_price=0)` — tries real price first, falls back to `last_known_price` (from exit engine's last scan), then entry price
- `get_current_price(asset_id)` — returns real price or 0 (does NOT fall back to entry price, so stale prices don't mask failures)
- Tracks positions, fills at real market prices

### Polymarket Price Lookup
- Uses Gamma API: `GET gamma-api.polymarket.com/markets?condition_id={id}`
- NOT `/markets/{id}` (returns 422 for conditionIds)
- `outcomePrices` is a JSON string that must be parsed: `'["0.475","0.525"]'`
- YES price = parsed[0], NO price = 1.0 - YES price
- Asset IDs formatted as `{conditionId}:YES` or `{conditionId}:NO`

---

## Fee Model
| Platform | Maker (limit) | Taker (market) | Our Strategy |
|----------|---------------|----------------|--------------|
| Polymarket | 0% | ~2% | Limit orders (0% entry), taker exit worst case |
| Kalshi | 0.5% | 1% | — |
| Coinbase | 0.4% | 0.6% | — |
| PredictIt | 5% | 5% | — |

- Paper executor uses separate buy/sell fee rates: buys at maker rate, sells always at taker rate
- P&L always calculated AFTER fees (estimated 1% sell-side for unrealized)
- Auto trader deducts 2% round-trip from profit assessment before entering
- Trade journal records buy_fees, sell_fees, total_fees per completed trade
- Trade journal recalculates exit value from per-leg exit data (not stale pkg current_value)

## Data Persistence
| File | Location | Purpose |
|------|----------|---------|
| `positions.json` | `src/data/positions/` | All packages, legs, alerts (atomic write via .tmp + os.replace) |
| `trade_journal.json` | `src/data/positions/` | Completed trade history with fees, P&L, exit triggers |
| `insider_signals.json` | `src/data/positions/` | Insider positions, accuracy scores, movement alerts |
| `cache.json` | `src/data/arbitrage/` | Latest fetched events from all platforms |
| `saved_markets.json` | `src/data/arbitrage/` | User-bookmarked matched events |
| `manual_links.json` | `src/data/arbitrage/` | User-created manual event links |

## Frontend
| File | Purpose |
|------|---------|
| `src/static/js/app.js` | Lobsterminal frontend (~2000 lines) |
| `src/static/js/arbitrout.js` | Arbitrout frontend (533 lines) |
| `src/static/css/terminal.css` | Terminal styles |
| `src/static/css/arbitrout.css` | Arbitrout styles |

### Wallet Config & Security (`wallet_config.py`)
- `load_env_file()` — loads `src/.env` into `os.environ` (setdefault only, never overrides existing env vars)
- `is_paper_mode()` / `get_paper_balance()` — reads `PAPER_TRADING` and `PAPER_STARTING_BALANCE`
- `get_configured_platforms()` — checks which platforms have all required API keys set
- `validate_live_config()` — checks paper mode, platform keys, .env file permissions, .gitignore coverage
- `get_safe_config()` — returns config safe for API exposure: key status shown as `"***set***"` or `"missing"`, never exposes actual values
- `_SENSITIVE_KEYS` — set of keys that must never be logged or exposed
- Called from server.py lifespan to load .env before subsystem init

### Alert Deduplication
- `PositionManager.add_alert()` checks for existing pending alert with same `pkg_id + trigger_name` before creating a new one
- Prevents exit engine (30s loop) from creating thousands of duplicate alerts for the same trigger

## Git Workflow
1. Feature branch: `feat/derivative-position-manager`
2. All changes committed to feature branch
3. Tests in `tests/` (48 passing, ignore test_arbitrage.py)
4. Never push directly to main

## Running
```bash
cd ~/.openclaw/workspace/projects/arbitrout/src
python -m uvicorn server:app --host 127.0.0.1 --port 8500 --log-level info
```

## Platform Status
| Platform | Status | Events |
|----------|--------|--------|
| Polymarket | Working | ~100 |
| PredictIt | Working | ~865 |
| Limitless | Working (500 errors sometimes) | ~25 |
| Kalshi | Needs API key (401) | 0 |
| Robinhood | Scraping returns 0 | 0 |
| Coinbase | Scraping returns 0 | 0 |
| CryptoSpot | Working (CoinGecko) | ~45 |
| Opinion Labs | Needs API key (401) | 0 |

## Current Status (2026-03-18)
- Paper trading: 8 open positions, $1,425 invested, ~$441 unrealized profit (31% ROI)
- Insider tracker: 100 traders monitored, 139 markets with signals
- Auto trader: scanning every 5 min, respecting $2K exposure limit
- Exit engine: reassessing every 30s with 18 heuristic triggers
- Arbitrage scanner: 1010 events from 8 adapters
- **Recent fixes (2026-03-18):**
  - Split paper executor fee model: buy=maker, sell=taker (was using maker for both → 0% exit fees on Polymarket)
  - Added `last_known_price` fallback to sell() — prevents $0 P&L on closed trades when real price unavailable
  - `get_current_price()` returns 0 on failure instead of falling back to entry price — prevents stale price masking
  - Trade journal recalculates exit value from per-leg data instead of stale `pkg["current_value"]`
  - Alert deduplication in `add_alert()` — cleared 1280 stale pending alerts
  - Wallet config: .env loading, key validation, safe config API for live trading prep
  - All 48 tests passing

## Live Trading Requirements
To switch from paper to live trading:

1. **Set `PAPER_TRADING=false`** in `src/.env`
2. **Polymarket (required):**
   - `POLYMARKET_PRIVATE_KEY` — Polygon private key (hex, no 0x prefix)
   - `POLYMARKET_FUNDER_ADDRESS` — funding wallet address
3. **Kalshi (optional):**
   - `KALSHI_API_KEY` — API key from Kalshi dashboard
   - `KALSHI_RSA_PRIVATE_KEY` — RSA private key for API auth
4. **Coinbase Advanced Trade (optional):**
   - `COINBASE_ADV_API_KEY` — API key
   - `COINBASE_ADV_API_SECRET` — API secret
5. **PredictIt (optional):**
   - `PREDICTIT_SESSION` — session cookie (PredictIt has no official API)
6. **AI Advisor (optional):**
   - `ANTHROPIC_API_KEY` — for Claude exit decision review

Run `GET /api/derivatives/config` to verify which platforms show as "configured" vs "missing".
The system validates: paper mode flag, platform keys, .env file permissions, .gitignore coverage.
