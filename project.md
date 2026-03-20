# Project: Arbitrout (Prediction Market Arbitrage + Auto Trading)
Status: ACTIVE
Phase: BUILD
Last Updated: 2026-03-19
Repo: https://github.com/sorry-ari0/arbitrout.git
Branch: main (feature/trading-perf-overhaul pending merge)

## Overview
Arbitrout is a prediction market trading system with eight core capabilities:
1. **Cross-platform arbitrage scanner** — finds price discrepancies across Polymarket, PredictIt, Limitless, Kalshi, and more
2. **Autonomous paper trading** — auto trader scans ALL platforms (9 adapters, 10 executors) for opportunities, opens directional bets and cross-platform arb with risk management
3. **Insider/whale tracker** — monitors top Polymarket traders and uses their positions as trading signals
4. **AI news scanner** — monitors RSS feeds, uses AI to match headlines to prediction markets, executes trades on breaking news before markets react
5. **Political synthetic derivatives** — rule-based classification + LLM-driven multi-leg strategy generation for political prediction markets, with cross-platform correlation detection and fee-adjusted expected value analysis
6. **BTC 5-min directional sniper** — streams real-time BTC price from Binance WebSocket, computes composite directional signal (window delta + micro momentum + tick trend) at T-10s before 5-min market close, places maker limit orders (0% fee + USDC rebates) on the winning side. Research-validated: 85%+ win rate on these markets.
7. **Market maker** — provides dual-sided liquidity on Polymarket crypto markets by placing maker limit orders on both YES and NO sides (combined cost < $1.00 = guaranteed profit). Inventory management with 70/30 imbalance limits, auto-withdrawal before resolution.
8. **Multi-outcome arbitrage** — scans Polymarket grouped events (3+ outcomes) where sum of all YES prices < $1.00, buys all outcomes for guaranteed profit at resolution

Integrated into the Lobsterminal financial terminal as a switchable tab. Backend is Python FastAPI on port 8500.

## System Interaction Map

### Dependency Wiring (server.py lifespan init)

```
server.py creates all subsystems and injects dependencies:

  AdapterRegistry ──────────────────────> ArbitrageScanner ·····> AutoTrader
  (8 platform adapters)                   (60s scan loop)         (multi-platform
                                                                   opp source)

  Real Executors ──> PaperExecutor ──────> PositionManager
  (10 executors:     (simulated $,          (CRUD, execute,
   polymarket,        real prices,           persist, P&L)
   kalshi, coinbase,  maker/taker fees)
   predictit,
   limitless,
   opinion_labs,
   robinhood,
   crypto_spot,
   kraken)        |
                                               |──────> ExitEngine (60s loop)
                                               |──────> AutoTrader (5m loop)
                                               |──────> InsiderTracker (15m loop)
                                               |──────> NewsScanner (150s loop)

  TradeJournal <─── PositionManager        (records on close, tracks exit_order_type)
  AIAdvisor    <─── ExitEngine             (reviews non-safety triggers, receives news context)
  NewsAI       <─── NewsScanner            (headline scan + deep article analysis)
  NewsScanner  ──> ExitEngine              (headline cache for news-validated exits)
  InsiderTracker ──> AutoTrader            (signal boost for scoring)
  NewsScanner  ──> AutoTrader              (queued opportunities via asyncio.Lock)
  CalibrationEngine <── EvalLogger + TradeJournal  (24h reports, /api/derivatives/calibration)

  ProbabilityModel <── ArbitrageScanner    (matched events → consensus prices)
  ProbabilityModel ──> AutoTrader          (1.3x boost on cross-platform disagreement)

  PoliticalAnalyzer ──> AutoTrader         (political synthetic opportunities)
  PoliticalAnalyzer <── ArbitrageScanner   (event feed for political filtering)
  PoliticalAnalyzer <── AIAdvisor          (LLM strategy generation)
  PoliticalAnalyzer <── DecisionLogger     (political analysis logging)

  EvalLogger   <─── AutoTrader             (logs all entered/skipped opportunities)
  EvalLogger   <─── PoliticalAnalyzer      (logs political opportunities)

  BinancePriceFeed ──> BtcSniper           (real-time BTC price stream)
  BinancePriceFeed ──> MarketMaker         (fair price for quote calculation)
  BtcSniper    ──> PositionManager         (creates btc_sniper packages)
  MarketMaker  ──> PolymarketExecutor      (maker limit orders via CLOB)
  ArbitrageScanner ──> multi_outcome scan  (grouped events with 3+ outcomes)
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
|       |  5. Filter: net profit > 12% after 2% round-trip fees       |
|       |                                                              |
|       +---> PositionManager.execute_package()                        |
|                  |                                                   |
|                  +---> PaperExecutor.buy_limit() per leg              |
|                  |         (0% maker fee, real price, simulated $)    |
|                  +---> positions.json (atomic write)                 |
|                                                                      |
|  ExitEngine (every 60s)                                              |
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
|                                                                      |
|  NewsScanner (every 150s)                                            |
|       |                                                              |
|       |  1. Fetch 8 RSS feeds (crypto, macro, finance) concurrently  |
|       |  2. Dedup: SHA-256 hash (24h) + >80% word overlap (30m)      |
|       |  3. Pass 1: AI scans headlines vs 200 Polymarket markets     |
|       |     (Groq → Gemini → OpenRouter chain)                       |
|       |  4. Pass 2: Fetch full article (httpx → Scrapling fallback)  |
|       |     + deep AI analysis for trade/no-trade decision           |
|       |  5. Route by urgency:                                        |
|       |     BREAKING (HIGH + confidence>=8) → execute immediately    |
|       |     NORMAL (confidence>=7) → queue to AutoTrader             |
|       |                                                              |
|       +---> PositionManager.execute_package() (breaking trades)      |
|       +---> AutoTrader.add_news_opportunity() (queued trades)        |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|                 POLITICAL SYNTHETIC DERIVATIVE PIPELINE              |
|  (15-min cycle: classify → cluster → analyze → score → trade)       |
|                                                                      |
|  PoliticalAnalyzer (every 15m)                                       |
|       |                                                              |
|       |  1. Fetch events from ArbitrageScanner                       |
|       |  2. Filter to "politics" category                            |
|       |  3. Classify contracts (6 types: margin_bracket,             |
|       |     vote_share, matchup, party_outcome,                      |
|       |     candidate_win, yes_no_binary)                            |
|       |  4. Cluster by normalized race+state (fuzzy matching)        |
|       |     ("TX Senate" = "Texas Senate" = "Senate TX")             |
|       |  5. For top 10 clusters (by contract count):                 |
|       |     a. Check SHA-256 keyed LRU cache (15m TTL, 3% shift)    |
|       |     b. Detect relationships (6 types with score multipliers):|
|       |        mispriced_correlation(3.0x), candidate_party(2.5x),   |
|       |        margin_decomposition(2.0x), conditional_hedge(1.5x),  |
|       |        bracket_spread(1.5x), matchup_arbitrage(2.0x)         |
|       |     c. Build 2-4 leg combinations (greedy from best pair)    |
|       |     d. LLM generates strategy (JSON schema, fee-adjusted)    |
|       |     e. Validate: EV>=3%, win_prob>=50%, max_loss>=-60%       |
|       |     f. Convert to PoliticalOpportunity with platform fees    |
|       |                                                              |
|       +---> AutoTrader (merged with arb + news opportunities)        |
|       +---> /api/political/* endpoints                               |
|       +---> EvalLogger (all entered + skipped)                       |
+---------------------------------------------------------------------+

+---------------------------------------------------------------------+
|                    UNIVERSAL EVAL LOGGER                             |
|  (records ALL opportunities for hindsight analysis)                  |
|                                                                      |
|  EvalLogger (append-only JSONL)                                      |
|       |                                                              |
|       |  1. log_opportunity(): records every opportunity              |
|       |     - strategy_type, action (entered/skipped),               |
|       |       action_reason, markets, score, prices                  |
|       |  2. backfill_outcome(): hourly loop checks resolved markets  |
|       |     - adds actual_pnl, resolution_price, hypothetical_pnl   |
|       |  3. get_missed_opportunities(): finds profitable skips       |
|       |  4. get_calibration(): per-reason correct-skip rate          |
|       |                                                              |
|       +---> /api/eval/* endpoints                                    |
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
  Strategy: pure_prediction (directional bet)
  Legs:
    - YES @ polymarket  entry=$0.35  qty=285.71  cost=$100
  Exit Rules:
    - target_profit: exit all if P&L >= +25%
    - stop_loss:     exit all if P&L <= -20%
    - trailing_stop: exit if drawdown from peak >= 12%

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

The exit engine evaluates 21 triggers organized in 7 categories:

```
PROFIT TAKING (1-3):     target hit, trailing stop, partial profit
LOSS PREVENTION (4-6):   stop loss, new ATH (tighten trail), correlation break
SPREAD / ARB (7-9):      spread inversion*, spread compression, volume dry-up
TIME (10-12):            time <24h, time <6h, time decay (3-7 days)
VOLATILITY (13-15):      vol spike (>15% move), vol crush, negative drift
PLATFORM (16-18):        platform error (3+ consecutive), liquidity gap, fee spike
RESEARCH (19-20):        stale position, longshot decay
POLITICAL (21):          political event resolved* (leg price <=0.01 or >=0.99)

* = SAFETY OVERRIDE — executes immediately, bypasses AI

NOTE: time_24h (#10) and time_6h (#11) are NO LONGER safety overrides.
Prediction markets move most in final hours — early exits destroyed value
($38.88 in losses from premature time-based exits). Now soft review triggers.

HOLD PERIOD: All packages have _min_hold_until (24h from entry). During hold,
only safety overrides + target_hit + stop_loss + political_event_resolved fire.
All other soft triggers (trailing_stop, negative_drift, etc.) are suppressed.
Research: <24h holds underperform by 18%.
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

  SKIP if: yes_price > 0.92 or < 0.08  (near-resolved, no edge)
  SKIP if: 0.42 < yes_price < 0.58     (near-50/50, no conviction)
  SKIP if: hours_to_expiry < 1           (short-duration, bot-dominated)
  SKIP if: days_to_expiry <= 2          (time_24h safety would close immediately)

  score = net_profit
  if crypto keyword in title:     score *= 2.0
  if 3 <= expiry <= 14 days:      score *= 2.0  (sweet spot)
  if 14 < expiry <= 30 days:      score *= 1.5
  if volume > 100,000:            score *= 1.5
  if volume > 10,000:             score *= 1.2
  if conviction > 0.3:            score *= 1.5
  if conviction > 0.2:            score *= 1.2
  if insider signal exists:
    score *= (1.0 + signal_strength * 2.0)
    if suspicious insiders:       score *= 1.5

  # Favorite-longshot bias (research: longshots lose ~40%, favorites ~5%)
  if favored >= 0.80:     score *= 2.5   # strong favorite
  elif favored >= 0.70:   score *= 1.8   # moderate favorite
  elif favored <= 0.20:   score *= 0.2   # severe longshot penalty
  elif favored <= 0.30:   score *= 0.5   # longshot penalty

  # Cross-platform disagreement boost (probability model)
  if platforms disagree >10%:  score *= 1.3

  ENTER if: score >= 12.0 AND net_profit >= 12% (MIN_SPREAD_PCT)
  STRATEGY: directional bet (one side) on same-platform, arb (both sides) on cross-platform
  SIZE: variable Kelly — 1/4 for favorites (>=0.70), 1/5 mid-range, 1/8 for longshots (<=0.30)
  LIMITS: 7 concurrent, $1400 auto exposure, 3 trades/day cap
  ORDERS: limit orders (GTC) for entries = 0% maker fee on Polymarket
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
- **Auto-scan:** Background tasks: arbitrage (60s), exit engine (60s), auto trader (5m), insider tracker (15m), news scanner (150s), political analyzer (15m), eval backfill (1h)
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
| `src/adapters/kalshi.py` | Kalshi API adapter (public events+orderbook, optional auth) |
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
  - **Acronyms:** Party names, tickers, PredictIt contract identifiers (e.g., MSZP, TISZA, KDNP, Fidesz). Used to reject false matches where both events have unique non-overlapping acronyms.
  - **Quoted terms:** Terms in quotes
  - **Key terms:** Non-stopword terms (3+ chars)
- **Acronym conflict check:** If both events have unique acronyms with no overlap (e.g., MSZP vs TISZA), the match is rejected even if other entities overlap. Prevents false cross-platform matches on multi-contract political markets.
- Entity overlap scoring (0.0-1.0): crypto ticker match (3pts), names (2pts), countries (1pt), quoted terms (2pts), key terms (2pts, proportional)
- **Price ratio filter:** Same-direction crypto markets (both "above" or both "below") must have price ratio >= 0.98 (tight, prevents false matches between $90K and $100K targets). Range markets keep 0.90 threshold.
- **Expiry date parsing:** Supports ISO `%Y-%m-%d` and Limitless formats (`%b %d, %Y`, `%B %d, %Y`)
- Match threshold: 0.45 score required
- Additional filters: same-platform events never match, category must be compatible, expiry must be within 7 days
- **Mega-cluster splitting (Phase 2.5):** After Union-Find clustering, crypto clusters with >5% internal price divergence are split into sub-groups by price proximity. Prevents unrelated price targets (e.g., BTC $90K and $150K) from merging into one cluster.
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
- `_previous_prices` stores `(price, timestamp)` tuples with 24-hour TTL pruning and 5K entry cap to prevent memory leaks
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
  spread, profit_pct, combined_volume, buy_yes_event_id, buy_no_event_id,
  yes_allocation_pct, no_allocation_pct, is_synthetic, synthetic_info
```

---

## Subsystem 2: Derivative Position System

### Files
| File | Purpose |
|------|---------|
| `src/positions/position_manager.py` | CRUD, persistence, execution, rollback for derivative packages |
| `src/positions/position_router.py` | FastAPI router at `/api/derivatives/` — all position endpoints + WebSocket |
| `src/positions/exit_engine.py` | 60s scan loop, 21 heuristic triggers, safety overrides, AI routing, limit order exits, news-validated decisions |
| `src/positions/auto_trader.py` | Autonomous paper trader — scans all platforms, limit orders, 3/day cap, 24h hold, variable Kelly, favorite-longshot bias |
| `src/positions/probability_model.py` | Consensus probability aggregator — volume-weighted prices across platforms, deviation detection |
| `src/positions/trade_journal.py` | Records completed trades with P&L, fees, exit triggers, exit_order_type, performance analytics, hold duration analysis |
| `src/positions/calibration.py` | CalibrationEngine — 24h reports on entry/exit thresholds, hold duration, fee analysis, limit fill rates |
| `src/positions/insider_tracker.py` | Whale/insider monitor — leaderboard, positions, accuracy tracking, movement alerts |
| `src/positions/ai_advisor.py` | Multi-provider AI advisor for exit decisions (Groq/Gemini/OpenRouter/Anthropic) |
| `src/positions/news_scanner.py` | AI news scanner — RSS feed monitor, two-pass AI pipeline, trade execution, headline cache for exit engine |
| `src/positions/news_ai.py` | Multi-provider LLM analysis for headline scanning and deep article review |
| `src/positions/decision_log.py` | JSONL decision logger — records buys, skips, triggers, AI verdicts, exits, news signals |
| `src/positions/wallet_config.py` | Paper/live mode config, .env loading, key validation, safe config API |
| `src/positions/price_feed.py` | Multi-asset Binance WebSocket (BTC/ETH/SOL/XRP) — real-time prices, per-asset candles, window tracking, event-driven on_tick callbacks |
| `src/positions/btc_sniper.py` | Multi-asset 5-min directional sniper — event-driven evaluation, composite signal at T-10s, maker orders |
| `src/positions/market_maker.py` | Dual-sided liquidity — preemptive cancel on adverse ticks, on-chain token merging, multi-asset discovery |

### How Derivative Packages Work

**Package structure:**
- A "package" is a grouped position with one or more "legs" (individual market bets) and "exit rules"
- Strategy types: `spot_plus_hedge`, `cross_platform_arb`, `pure_prediction`, `news_driven`, `synthetic_derivative`, `political_synthetic`, `btc_sniper`, `multi_outcome_arb`, `market_making`
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

The exit engine runs a 60-second scan loop evaluating all open packages.

**Each tick:**
1. Resolve any pending limit orders from previous tick (check status → finalize or FOK-fallback after 60s timeout)
2. Fetch current prices for all open legs via platform executors
3. Recalculate P&L (fee-aware)
4. Track negative streak counter
5. Skip packages with pending limit orders (treated as "exiting")
6. Evaluate all 18 heuristic triggers
7. Route fired triggers: safety overrides execute immediately (cancel pending limit orders first), others go to AI advisor
8. AI-approved exits use limit orders (0% maker fee) except stop_loss which always uses FOK
9. AI prompt includes recent news headlines from news_scanner — no negative news → strong REJECT bias for trailing_stop, negative_drift, time_decay

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
| 10 | time_24h | Time | review | No (was safety, demoted — early exits destroyed value) |
| 11 | time_6h | Time | review | No (was safety, demoted — early exits destroyed value) |
| 12 | time_decay | Time | review | No |
| 13 | vol_spike | Volatility | review | No |
| 14 | vol_crush | Volatility | review | No (placeholder) |
| 15 | negative_drift | Volatility | review | No |
| 16 | platform_error | Platform | review | No |
| 17 | liquidity_gap | Platform | review | No (placeholder) |
| 18 | fee_spike | Platform | review | No (placeholder) |
| 19 | stale_position | Time | review | No |
| 20 | longshot_decay | Research | review | No |
| 21 | political_event_resolved | Political | immediate_exit | **YES** |

**Safety overrides (triggers 7, 21):**
- Execute immediately — no AI review, no escalation
- Spread inversion (#7): YES + NO prices > 1.0 (guaranteed loss if held)
- Political event resolved (#21): any leg price <= 0.01 or >= 0.99 (market has resolved, exit immediately)

**Non-safety trigger routing (batched AI review):**
- All non-safety triggers from ALL packages are collected per tick, then sent in a SINGLE batched AI call (reduces Groq/Gemini 429 rate limit hits)
- Batched prompt uses `[PKG:id]` markers so AI can address each package separately
- Provider chain: Groq → Gemini → OpenRouter (Anthropic first in live mode)
- AI returns per-package: APPROVE (execute), MODIFY (adjust rule params within bounds), REJECT (skip with reason)
- AI prompt includes paper trading performance data (17 auto exits, 0 wins, -$143) to bias toward REJECT
- AI prompt is nuanced per trigger type:
  - `trailing_stop`: REJECT unless drawdown >25% from peak AND position open >24 hours
  - `time_decay`: REJECT almost always. APPROVE only if P&L < -20% AND <12 hours left
  - `negative_drift`: REJECT unless loss >15% sustained over many ticks
- If AI fails or unavailable → auto-execute mechanical triggers (target_hit, stop_loss, trailing_stop)
- Noisy triggers (vol_spike, new_ath, spread_compression) suppressed without AI — not actionable
- Escalation triggers (correlation_break, time_decay, negative_drift, platform_error) create alerts
- All decisions logged to `decision_log.jsonl` for later review

### How the AI Advisor Works (`ai_advisor.py`)

- **Multi-provider chain** — tries providers in priority order, falls back on failure:
  - **Live trading:** Anthropic (Claude Sonnet 4) → Groq (Llama 3.3 70B) → Gemini 2.0 Flash → OpenRouter (Llama 3.1 70B)
  - **Paper trading:** Groq → Gemini → OpenRouter (skips Anthropic to save costs)
- Rate limited: max 10 calls/minute (sliding window)
- Mode-aware: `AIAdvisor(paper_mode=True)` selects the provider chain
- Three API styles: OpenAI-compatible (Groq, OpenRouter), Gemini REST, Anthropic Messages
- All calls via `httpx.AsyncClient` (30s timeout)
- **Batched review:** `_build_batched_prompt()` combines multiple packages into a single prompt with `[PKG:id]` markers; `_parse_batched_response()` splits response by markers into per-package verdicts
- Expects response format: `[PKG:id] <rule_id>: APPROVE | MODIFY <value> | REJECT <reason>`
- Conservative guidance: time_decay rejects unless deeply negative or near expiry; negative_drift rejects unless sustained large loss
- On failure: logs warning, tries next provider. All fail → returns empty dict (caller auto-executes)
- API keys configured in `.env`: `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`

### How the Auto Trader Works

1. Every 5 minutes, scans ALL platforms via ArbitrageScanner (not just Polymarket)
2. **Four opportunity sources:**
   - Cross-platform arbitrage (highest priority, 3x score boost): from `scanner.get_opportunities()`
   - Single-platform directional bets: from ALL platform events via `scanner.get_events()`, filtered to tradeable platforms only (those with registered executors)
   - Queued news signals (2x score boost): from NewsScanner via `add_news_opportunity()`
   - Political synthetic derivatives: from PoliticalAnalyzer, scored by `EV * confidence * cross_platform_boost(1.5x)`
3. Fallback: direct Polymarket Gamma API scan if scanner fails entirely
4. **ITM/OTM filter:** skips entries with side price > $0.85 (tiny upside) or < $0.15 (lottery tickets)
5. **Filters:** skips near-resolved (>0.92 or <0.08), near-50/50 (0.42-0.58), <2 day expiry, AND <1 hour expiry (short-duration markets with dynamic fees up to 3.15%, dominated by sub-100ms bots)
6. Calculates profit potential AFTER estimated round-trip fees: `raw_profit = ((1.0 - favored_price) / favored_price) * 100`, then `net_profit = raw_profit - 2%` round-trip fees
7. Scores each market: `profit_pct * crypto_boost(2x) * expiry_boost(2x if 3-14d, 1.5x if 14-30d) * volume_boost(1.5x if >100K, 1.2x if >10K) * conviction_boost(1.5x if >0.3) * insider_boost * favorite_longshot_bias * cross_platform_disagreement_boost(1.3x if >10% deviation)`
8. **Favorite-longshot bias (research-validated):** Strong favorites (>=0.80) get 2.5x boost, moderate (>=0.70) 1.8x. Severe longshots (<=0.20) get 0.2x penalty, mild (<=0.30) 0.5x. Longshots lose ~40%, favorites ~5%.
9. Insider signal boost: if insiders have positions, `score *= (1 + strength * 2)`. Suspicious insiders add extra 1.5x
10. **Probability model:** Volume-weighted consensus across platforms. Boosts score 1.3x when platforms disagree >10% (informational edge).
11. Requires minimum 12% gross spread to enter (ensures ~10% net margin after 2% round-trip fees — raised from 8% to reduce churn)
12. **Limit orders for entry:** All entries use GTC limit orders (0% maker fee on Polymarket) instead of FOK market orders (2% taker fee). Saves ~$108 per $5.7K deployed.
13. **Directional bets (same platform):** picks ONE side based on conviction — cheaper side = higher upside. Buying both YES and NO on the same platform locks in the spread minus fees = guaranteed loss
14. **Cross-platform arb:** buys both sides on different platforms (spread capture)
15. **Cooldown:** 48-hour re-entry cooldown after exiting a market (raised from 24h)
16. **Daily trade cap:** Max 3 new trades per calendar day (counter resets at midnight)
17. Exit rules tuned from 31-trade paper data: target profit (50%), stop loss (-40%), trailing stop (35%, bounds 15-50%)
18. **Variable Kelly sizing:** 1/4 Kelly for favorites (>=0.70), 1/5 for mid-range, 1/8 for longshots (<=0.30). Reduces risk on uncertain positions.
19. **24h minimum hold period:** All new packages get `_min_hold_until` timestamp. During hold, soft triggers (trailing_stop, negative_drift, time_decay, stale_position) are suppressed. Safety overrides (spread_inversion) and mechanical exits (target_hit, stop_loss) still fire. Research: <24h holds underperform by 18%.
20. Position limits: $200 max per trade, $5 min, 7 concurrent positions (3 reserved for news), $1400 auto exposure + $600 news
21. **Market loss limit:** Block re-entry after 2 losses on the same market (prevents churning)
16. **Decision logging:** all buys, skips (with reason), and failures logged to `decision_log.jsonl`

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
| GET | `/calibration` | Calibration report (entry/exit thresholds, hold duration, fee analysis, limit fill rate) |
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
| `src/execution/limitless_executor.py` | Limitless Exchange — public API price lookups, paper-only trading |
| `src/execution/opinion_labs_executor.py` | Opinion Labs — REST API with optional API key, paper-only trading |
| `src/execution/robinhood_executor.py` | Robinhood — paper-only (no public price API, uses fallback_price) |
| `src/execution/crypto_spot_executor.py` | CCXT-based crypto spot — 7-exchange priority chain (Kraken→Coinbase→Binance→Bybit→OKX→KuCoin→Bitget), synthetic probability markets |
| `src/execution/kraken_cli.py` | Kraken CLI via WSL — Rust binary wrapper for spot trading + MCP server |

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
  - Limitless: 0.5% maker / 1% taker
  - Opinion Labs: 1% maker / 2% taker
  - Robinhood: 0% / 0% (commission-free)
  - Crypto Spot: 0% / 0% (synthetic, no real fees)
  - Kraken: 0.16% maker / 0.26% taker
- **Buys use maker rate** (limit orders, 0% for Polymarket), **sells use maker rate for limit exits** (0% for Polymarket) **or taker rate for FOK exits** (2% for Polymarket)
- `sell_limit(asset_id, quantity, price)` — limit sell at specified price using maker fee (0% for Polymarket). Used by exit engine for non-emergency exits.
- `sell(asset_id, quantity, last_known_price=0)` — FOK market sell, tries real price first, falls back to `last_known_price` (from exit engine's last scan), then entry price
- `check_order_status(order_id)` — returns order fill status (paper mode: always "filled" immediately)
- `cancel_order(order_id)` — cancels a pending limit order (paper mode: no-op returns True)
- `get_current_price(asset_id)` — returns real price or 0 (does NOT fall back to entry price, so stale prices don't mask failures)
- Tracks positions, fills at real market prices

### CryptoSpot Executor (CCXT)
- Uses CCXT library (v4.5.44) for universal exchange access (113 exchanges)
- Exchange priority chain: Kraken, Coinbase, Binance, Bybit, OKX, KuCoin, Bitget (first with API keys wins)
- Two asset_id formats: direct spot (BTC/USDT) and synthetic probability (crypto-btc-100000:YES)
- Price fallback chain: configured exchange, CCXT public Kraken API, CoinGecko
- Synthetic probability: Black-Scholes log-normal model converts spot prices into implied probabilities
- is_configured() = True always (public API price lookups work without keys)
- Trading requires API keys set in .env (e.g., KRAKEN_API_KEY, KRAKEN_API_SECRET)

### Kraken CLI Executor
- Wraps Kraken CLI (Rust binary, v0.2.0) installed in WSL Ubuntu
- CLI handles auth, nonce management, HMAC signing internally
- Uses asyncio.create_subprocess_exec with explicit argument lists (no shell injection)
- Pair mapping: BTC/USD to XBTUSD (Kraken native format), 10+ pairs mapped
- Output: NDJSON parsed to Python dicts
- MCP server: kraken mcp -s market,paper exposes 19 tools (10 market data, 9 paper trading) via .mcp.json
- Public endpoints (ticker, orderbook, trades) work without API keys
- Real trading requires Kraken API keys configured in WSL kraken CLI config

### Polymarket Price Lookup
- Uses Gamma API: GET gamma-api.polymarket.com/markets?condition_id={id}
- NOT /markets/{id} (returns 422 for conditionIds)
- outcomePrices is a JSON string that must be parsed: '["0.475","0.525"]'
- YES price = parsed[0], NO price = 1.0 - YES price
- Asset IDs formatted as {conditionId}:YES or {conditionId}:NO

---

## Subsystem 5: Political Synthetic Derivatives

### Files
| File | Purpose |
|------|---------|
| `src/political/__init__.py` | Package init |
| `src/political/models.py` | 7 dataclasses: PoliticalContractInfo, PoliticalCluster, SyntheticLeg, Scenario, PoliticalSyntheticStrategy, PoliticalLeg, PoliticalOpportunity + PLATFORM_FEES dict |
| `src/political/classifier.py` | Rule-based contract classification — 6 types in priority order via regex |
| `src/political/clustering.py` | Groups contracts by normalized race+state with fuzzy matching |
| `src/political/cache.py` | SHA-256 keyed LRU cache — 15-min TTL, 3% price-shift invalidation, 200-entry max |
| `src/political/relationships.py` | Pairwise relationship detection (6 types), greedy leg combination builder (2-4 legs) |
| `src/political/strategy.py` | LLM prompt builder, response parser, strategy validator |
| `src/political/analyzer.py` | 15-min async loop orchestrator — event filtering, clustering, LLM analysis, opportunity output |
| `src/political/router.py` | FastAPI router at `/api/political/` — clusters, strategies, eval endpoints |

### How Political Synthetic Analysis Works

**Step 1: Contract Classification (`classifier.py`)**
- Classifies each political event into one of 6 types (priority order):
  1. `margin_bracket` — "win by 5-10 points" (regex: margin, spread, points)
  2. `vote_share` — "get more than 55% of vote" (regex: percent, share, threshold)
  3. `matchup` — "Cruz vs Talarico" (regex: vs, versus, head-to-head)
  4. `party_outcome` — "Democrats win Senate" (regex: democrat, republican, party)
  5. `candidate_win` — "Cruz to win TX Senate" (regex: win, elected, victory)
  6. `yes_no_binary` — fallback for all other political contracts
- Extracts: candidates, party, race, state, threshold, direction

**Step 2: Clustering (`clustering.py`)**
- `_normalize_race()`: lowercase → replace state names with abbreviations → remove filler words → sort tokens → join with "-"
- Groups contracts with identical normalized key
- Minimum 2 contracts per cluster (singleton = no synthesis possible)
- Deduplicates events by event_id

**Step 3: Relationship Detection (`relationships.py`)**
- Pairwise comparison of all contracts in a cluster
- 6 relationship types with score multipliers:
  - `mispriced_correlation` (3.0x) — same candidate, same outcome, different prices cross-platform
  - `candidate_party_link` (2.5x) — same candidate on same party
  - `margin_decomposition` (2.0x) — margin_bracket pairs that decompose a range
  - `conditional_hedge` (1.5x) — opposing sides create natural hedges
  - `bracket_spread` (1.5x) — adjacent bracket pairs
  - `matchup_arbitrage` (2.0x) — matchup contracts with arbitrage potential
- `build_leg_combinations()`: starts from highest-scored pair, greedily extends to 4 legs max
- Minimum relationship score: 1.5

**Step 4: LLM Strategy Generation (`strategy.py`)**
- `build_cluster_prompt()`: constructs prompt with contracts, relationships, platform fee rates, JSON schema
- `parse_strategy_response()`: strips code fences/trailing commas, parses JSON, maps contract indices to event_ids
- `validate_strategy()`: win_probability >= 0.50, max_loss >= -60%, expected_value >= 3%, confidence != "low"

**Step 5: Opportunity Conversion (`analyzer.py`)**
- Converts validated `PoliticalSyntheticStrategy` to `PoliticalOpportunity`
- Calculates weighted platform fees from `PLATFORM_FEES` dict
- Net EV = gross EV - weighted fees
- `PoliticalOpportunity.to_dict()` produces auto-trader-compatible format

**Step 6: Auto Trader Integration (`auto_trader.py`)**
- Political opportunities merged into scoring pipeline after news opportunities
- Scored by: `net_EV * confidence * cross_platform_boost(1.5x)`
- Multi-leg execution: weight-based capital allocation across legs
- Custom exit rules: target_profit (EV/2%), stop_loss (-max_loss/2%), trailing_stop

### Political API Endpoints (`/api/political/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/clusters` | Current political clusters with contract counts |
| GET | `/strategies` | All generated strategies with scores |
| GET | `/strategies/{cluster_id}` | Analyze specific cluster (triggers fresh LLM analysis) |
| POST | `/analyze` | Force a full analysis cycle |
| GET | `/eval` | Eval summary for political strategies |
| GET | `/eval/missed` | Missed political opportunities |

### Political Data Models
```
PoliticalContractInfo:
  event, contract_type (6 types), candidates[], party, race, state,
  threshold, direction

PoliticalCluster:
  cluster_id, race, state, contracts[], matched_events[]

PoliticalSyntheticStrategy:
  cluster_id, strategy_name, legs[] (SyntheticLeg), scenarios[] (Scenario),
  expected_value_pct, win_probability, max_loss_pct, confidence (str|float), reasoning

PoliticalOpportunity:
  cluster_id, strategy, legs[] (PoliticalLeg), total_fee_pct,
  net_expected_value_pct, platforms[], created_at
  → to_dict() produces auto-trader-compatible format
```

---

## Subsystem 6: Universal Eval Logger

### Files
| File | Purpose |
|------|---------|
| `src/eval_logger.py` | Append-only JSONL logger — records all opportunities (entered + skipped), backfill outcomes |
| `src/eval_router.py` | FastAPI router at `/api/eval/` — summary, missed, calibration, details |

### How the Eval Logger Works
- `log_opportunity()`: records every opportunity with strategy_type, opportunity_id, action (entered/skipped), action_reason, markets, score, prices
- `backfill_outcome()`: hourly background loop adds resolution data (actual_pnl, resolution_price, hypothetical_pnl for skips)
- `get_summary()`: counts by strategy_type and action
- `get_missed_opportunities()`: finds skipped entries where hypothetical P&L was positive
- `get_calibration()`: per action_reason, calculates correct-skip rate
- `get_details(opportunity_id)`: merged opportunity + backfill data lookup
- `get_unresolved_skips()`: skipped entries awaiting backfill

### Eval API Endpoints (`/api/eval/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/summary` | Strategy-type breakdown of entered vs skipped |
| GET | `/missed` | Profitable opportunities that were skipped |
| GET | `/calibration` | Per-reason correct-skip rates |
| GET | `/details/{opportunity_id}` | Full detail for a single opportunity |

---

## Fee Model
| Platform | Maker (limit) | Taker (market) | Our Strategy |
|----------|---------------|----------------|--------------|
| Polymarket | 0% | ~2% | Limit orders (0% entry), taker exit worst case |
| Kalshi | 0.5% | 1% | — |
| Coinbase | 0.4% | 0.6% | — |
| PredictIt | 5% | 5% | — |
| Limitless | 0.5% | 1% | — |
| Opinion Labs | 1% | 2% | — |
| Robinhood | 0% | 0% | Commission-free |
| Crypto Spot | 0% | 0% | Synthetic (no real fees) |
| Kraken | 0.16% | 0.26% | CLI via WSL |

- Paper executor uses separate buy/sell fee rates: buys at maker rate, sells always at taker rate
- P&L always calculated AFTER fees (estimated 1% sell-side for unrealized)
- Auto trader deducts 2% round-trip from profit assessment before entering (0% maker entry + ~2% taker exit = 2% total)
- Trade journal records buy_fees, sell_fees, total_fees per completed trade
- Trade journal recalculates exit value from per-leg exit data (not stale pkg current_value)

## Data Persistence
| File | Location | Purpose |
|------|----------|---------|
| `positions.json` | `src/data/positions/` | All packages, legs, alerts (atomic write via .tmp + os.replace) |
| `trade_journal.json` | `src/data/positions/` | Completed trade history with fees, P&L, exit triggers |
| `insider_signals.json` | `src/data/positions/` | Insider positions, accuracy scores, movement alerts |
| `decision_log.jsonl` | `src/data/positions/` | All trading decisions: buys, skips, triggers, AI verdicts, exits, news signals |
| `news_cache.json` | `src/data/positions/` | News scanner state: seen headline hashes (24h), daily trade counts, market cooldowns |
| `cache.json` | `src/data/arbitrage/` | Latest fetched events from all platforms |
| `saved_markets.json` | `src/data/arbitrage/` | User-bookmarked matched events |
| `manual_links.json` | `src/data/arbitrage/` | User-created manual event links |
| `eval_log.jsonl` | `src/data/eval/` | Universal eval log — all opportunities (entered + skipped) with backfilled P&L |
| `political_cache.json` | `src/data/arbitrage/` | Political synthetic LLM analysis cache (15-min TTL, 200-entry LRU) |

## Frontend
| File | Purpose |
|------|---------|
| `src/static/js/app.js` | Lobsterminal frontend (~2300 lines) |
| `src/static/js/arbitrout.js` | Arbitrout frontend (~960 lines) |
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
3. Tests in `tests/` (130 passing, ignore test_arbitrage.py)
4. Never push directly to main

## Running
```bash
cd ~/.openclaw/workspace/projects/arbitrout/src
python -m uvicorn server:app --host 127.0.0.1 --port 8500 --log-level info
```

## Platform Status

### Adapters (data feeds)
| Platform | Status | Events |
|----------|--------|--------|
| Polymarket | Working | ~100 |
| PredictIt | Working | ~865 |
| Limitless | Working (500 errors sometimes) | ~25 |
| Kalshi | Working (public API, no auth needed) | ~54 |
| Robinhood | Scraping returns 0 | 0 |
| Coinbase | Scraping returns 0 | 0 |
| CryptoSpot | Working (CoinGecko) | ~45 |
| Opinion Labs | Disabled (US-restricted) | 0 |

### Executors (trading)
| Platform | Key | Status | Notes |
|----------|-----|--------|-------|
| polymarket | polymarket | Working | CLOB + Gamma API |
| kalshi | kalshi | Needs API key | REST API |
| coinbase_spot + coinbase | coinbase_spot, coinbase | Needs API key | Dual-registered (adapter uses "coinbase") |
| predictit | predictit | Needs session cookie | No official API |
| limitless | limitless | Working (paper) | Public API price lookups |
| opinion_labs | opinion_labs | Working (paper) | Optional API key for full access |
| robinhood | robinhood | Working (paper) | No public price API (uses fallback_price) |
| crypto_spot | crypto_spot | Working (paper) | CCXT, public Kraken + CoinGecko prices |
| kraken | kraken | Working (paper) | CLI via WSL, 19 MCP tools |

## Current Status (2026-03-19)
- Paper trading: 10 open positions, ~$1,800 invested, -$142.73 realized P&L (3 wins / 26 losses, 10.3% win rate)
- Root cause of losses: AI advisor was blanket-approving time_decay/negative_drift exits → all 7 time_decay exits lost money. Fixed with nuanced AI prompt.
- AI advisor active via Groq (Llama 3.3 70B), batched reviews (~1 call per tick instead of 8)
- Insider tracker: 100 traders monitored, 139 markets with signals
- Auto trader: event-driven, MIN_SPREAD_PCT raised to 12% (ensures 10% net margin), limit orders (0% maker fee), 3/day cap, 48h cooldown, 24h hold period, variable Kelly sizing, favorite-longshot bias (2.5x/0.2x), probability model (1.3x cross-platform disagreement boost)
- Exit engine: 60s interval, 21 heuristic triggers, batched AI reviews (single LLM call for all packages per tick)
- News scanner: 150s interval, 14 RSS feeds, two-pass AI pipeline (headline scan → deep analysis), breaking trades execute immediately
- Decision logging: all buys, skips, trigger fires, AI verdicts, news signals logged to `decision_log.jsonl`
- Arbitrage scanner: ~1170 events from 8 adapters
- **Political synthetic derivatives:** 15-min scan cycle, 6-type classifier, fuzzy clustering, 6 relationship types, LLM strategy generation with fee-adjusted EV, trigger #21 (political_event_resolved), integrated with auto trader
- **Universal eval logger:** JSONL logging of all opportunities (entered + skipped), hourly backfill loop, missed opportunity analysis, calibration tracking
- Spec: `docs/specs/2026-03-19-political-synthetic-analysis-design.md`
- Plan: `docs/plans/2026-03-19-political-synthetic-analysis.md`
- **Recent changes (2026-03-20) — Exit Optimization (limit exits, news-validated exits, calibration loop):**
  - **Limit order exits:** `sell_limit()` rewritten in PaperExecutor to use 0% maker fee (was delegating to `sell()` with 2% taker). Added `check_order_status()` and `cancel_order()` stubs. Position manager gains `use_limit=True` param on `exit_leg()`, async pending order pattern (place limit → release lock → resolve next tick → FOK fallback after 60s). Two-phase locking: status check outside lock, finalization inside lock. Safety overrides cancel pending orders. stop_loss always FOK.
  - **News-validated exits:** News scanner caches all matched headlines in `_matched_headlines` dict (before confidence gate, 500-entry cap, 48h prune). Exit engine collects headlines per package, injects into AI advisor prompt as `RECENT NEWS` section. No negative news → strong REJECT bias for trailing_stop, negative_drift, time_decay.
  - **Calibration loop:** New `CalibrationEngine` reads eval_logger + trade_journal. Generates reports with entry calibration (correct_skip_rate per reason), exit calibration (win_rate per trigger), hold duration analysis (5 time buckets), fee analysis (limit fill rate, fee drag). 24h background task saves to `data/calibration/`. API: `GET /api/derivatives/calibration`.
  - **Trade journal:** Records `exit_order_type` ("limit_filled", "fok_direct", etc.) per trade. New `get_performance_by_hold_duration()` method.
  - **Test coverage:** 284 total tests (was 271).
  - Spec: `docs/superpowers/specs/2026-03-19-exit-optimization-design.md`
  - Plan: `docs/superpowers/plans/2026-03-19-exit-optimization.md`
- **Previous changes (2026-03-19) — Trading Performance Overhaul (branch: feature/trading-perf-overhaul):**
  - **Limit orders (Task 1):** Added `buy_limit`/`sell_limit`/`check_order_status`/`cancel_order` to BaseExecutor (defaults), PolymarketExecutor (GTC orders via OrderArgs + OrderType.GTC), and PaperExecutor (0% maker fee simulation). Position manager routes to `buy_limit` when `pkg["_use_limit_orders"]` is set. All auto-trader entries now use limit orders. Saves ~$108 per $5.7K deployed.
  - **Churn reduction (Task 2):** `MIN_SPREAD_PCT` 8%→12%, `MAX_NEW_TRADES_PER_DAY=3` (daily counter resets on date change), `MARKET_COOLDOWN_SECONDS` 24h→48h (172800s). `get_stats()` now reports `trades_today` and `max_trades_per_day`.
  - **24h hold period (Task 3):** All packages get `_min_hold_until = time.time() + 86400`. Exit engine suppresses soft triggers (trailing_stop, negative_drift, time_decay, stale_position, longshot_decay, spread_compression, vol_spike, correlation_break, platform_error) during hold. Safety overrides + target_hit + stop_loss + political_event_resolved still fire.
  - **Short-duration filter (Task 4):** `MIN_HOURS_TO_EXPIRY=1.0`. Upgraded expiry parsing to `datetime.fromisoformat()` with hour precision (fallback to date-only). Filter applied in all 3 scoring pipelines (_scan_and_trade, _events_to_opportunities, _scan_polymarket).
  - **Favorite-longshot scoring (Task 5):** Multipliers strengthened: favorites 1.8x→2.5x (>=0.80), 1.4x→1.8x (>=0.70); longshots 0.4x→0.2x (<=0.20), 0.7x→0.5x (<=0.30). Variable Kelly: 1/4 favorites, 1/5 mid-range, 1/8 longshots. Applied consistently across all 3 scoring pipelines.
  - **Probability model (Task 6):** New `probability_model.py` — volume-weighted consensus across platforms. Flags >10% deviations. Auto-trader scores 1.3x boost when platforms disagree. Updated in `_auto_scan_loop` from matched events. Wired into server.py.
  - **Test coverage:** 32 new tests (271 total), covering limit orders, churn reduction, hold period, short-duration filter, scoring, probability model.
- **Previous changes (2026-03-19):**
  - **Kalshi adapter rewrite:** Public API now uses events→markets→orderbook pipeline (no auth needed). Returns ~54 properly-priced events. Old approach returned 600 useless multi-leg parlays.
  - **Coinbase dedup:** Coinbase prediction markets ARE Kalshi markets. `_fetch_via_kalshi()` returns empty when no `COINBASE_ADV_API_KEY` to avoid duplicate events.
  - **Opinion Labs disabled:** US-restricted platform. Adapter only registered when `OPINION_LABS_API_KEY` is set.
  - **OpenRouter auto-disable:** AI advisor tracks `_disabled_providers` dict. On 402/401 errors, provider disabled for 1 hour (prevents spam retries).
  - **OPPORTUNITIES 0 fix:** Relaxed `_passes_quick_filter` from requiring 2+ context terms to: 2+ shared names OR 1 shared name + 1 context term.
  - **Zero-price market filter:** `find_arbitrage()` skips events where both yes_price and no_price are 0 (closed/no liquidity).
  - **Finnhub API key added:** `FINNHUB_API_KEY` configured in `.env` for quote/history fallback.
  - **False opportunity fixes:**
    - Hungarian MSZP election: added acronym extraction (`_extract_acronyms()`) to entity matcher. Extracts party names, tickers, PredictIt contract identifiers. Rejects matches where both events have unique non-overlapping acronyms (e.g., MSZP vs TISZA).
    - BTC $74K-$76K range synthetic: added `_build_range_synthetic_info()` with proper 4-scenario analysis for range markets. Sorts boundaries into zones, calculates per-zone payoff, rejects if win_count <= loss_count or loss_prob > 60%.
  - **Batched AI exit reviews:** Exit engine now collects ALL non-safety triggers across ALL packages per tick, sends single batched prompt with `[PKG:id]` markers. Reduces Groq/Gemini 429 rate limit hits (was: 8 separate calls → now: 1).
  - **Conservative AI prompt rewrite:** time_decay: REJECT if 3+ days left and P&L > -5%. negative_drift: REJECT if loss < 5%. Stops blanket-approving exits that were causing 90% loss rate.
  - **MIN_SPREAD_PCT raised 3%→5%:** Ensures minimum 3% net margin after 2% round-trip fees.
  - **Memory leak fix:** `_previous_prices` dict in arbitrage_engine.py now stores `(price, timestamp)` tuples with 24h TTL pruning and 5K entry cap.
  - **Bare except cleanup:** Fixed bare `except:` → `except Exception as e:` with logging in kalshi_executor.py, predictit_executor.py, coinbase_spot_executor.py.
  - **Exit engine trigger count:** 18 → 21 heuristic triggers (added stale_position #19, longshot_decay #20, political_event_resolved #21). Time_24h (#10) and time_6h (#11) demoted from safety overrides to soft review triggers.
- **Previous changes (2026-03-18):**
  - **Arbitrage display fixes (4):**
    - Tightened crypto price ratio from 0.90 to 0.98 for same-direction matches (prevents false matches: $90K vs $100K)
    - Limitless expiry date parsing (supports `%b %d, %Y` and `%B %d, %Y` formats)
    - Mega-cluster splitting (Phase 2.5): crypto clusters with >5% internal price divergence get split
    - ACTION column matches by `event_id` (unique) instead of `platform` name (can have duplicates)
    - Added `buy_yes_event_id` and `buy_no_event_id` to ArbitrageOpportunity model
  - **Full stock universe (9,920 tickers):**
    - SEC EDGAR `company_tickers.json` provides all US tickers; 135 HKEX Hang Seng constituents hardcoded
    - Universe browser UI: WATCHLIST/UNIVERSE toggle, search, exchange filter, HKEX checkbox, pagination (50/page)
    - `.HK` ticker support: `_SYMBOL_RE` updated for dotted suffixes
    - Full universe screener: background thread fetches fundamentals in chunks of 500, progressive cache saves
    - New endpoints: `/api/research/universe`, `/api/research/universe/quotes`, `/api/generate-asset/universe-status`, `/api/generate-asset/universe-refresh`
  - **Real data fallback chains (no mock data):**
    - Quotes: yfinance → Yahoo v8 Chart API → Finnhub → Scrapling (Google Finance) → stale cache
    - History: yfinance → Yahoo v8 Chart API → Finnhub → disk cache (7-day TTL at `data/history_cache/`)
    - Backtest: yfinance → Yahoo v8 Chart API → Finnhub → Alpha Vantage → Scrapling Yahoo CSV (5 real sources)
    - Screener: FMP → SP500 cache → Full universe cache → MOCK_UNIVERSE
  - **Multi-platform executors:** Added 5 new executors: Limitless, Opinion Labs, Robinhood, CryptoSpot (CCXT), Kraken CLI. Server now registers 10 executors total.
  - **Multi-platform auto trader:** Uses `scanner.get_opportunities()` for cross-platform arb + `scanner.get_events()` for all-platform directional bets.
  - **CCXT crypto executor:** 7-exchange priority chain, public price fallback via Kraken/CoinGecko, synthetic probability markets.
  - **Kraken CLI:** Rust binary in WSL Ubuntu, 19 MCP tools, `.mcp.json` configured for Claude Code.
  - **Directional betting:** Auto trader buys ONE side (cheaper = higher upside). Cross-platform arb still buys both.
  - **Multi-provider AI advisor:** Groq → Gemini → OpenRouter chain for paper (Anthropic first for live).
  - **News scanner:** AI-powered RSS feed monitor with two-pass pipeline. 14 RSS feeds. Breaking news executes immediately.
  - **Event-driven auto trader:** Auto trader now wakes within seconds of each 60s arb scan via `asyncio.Event` notification (was: independent 5-min polling loop). Reads cached scanner results instead of triggering redundant scans. Trade execution latency dropped from ~5 minutes to ~1-2 seconds.
  - **Market feed fix:** WebSocket `init` and `scan_result` messages now handled by frontend. Auto-scan loop broadcasts feed + opportunities to all WS clients after each scan.
  - **Synthetic derivative validation:** Direction-aware scenario analysis (accounts for above/below/between/dip). Rejects invalid synthetics: same-direction doubling (both legs bet same move), loss probability >40%, strike gap >10%. Added "dip/sink/crash/plunge/decline" to below-direction keywords. **Range synthetics** (direction="between") now use dedicated 4-scenario analysis (`_build_range_synthetic_info`): sorts all boundaries into zones, calculates per-zone payoff for both range and directional legs, rejects if win_count <= loss_count or loss probability > 60%.
  - **PredictIt contract matching fix:** Stopped stripping contract names from "Market: Contract" titles. Previously "Who will win: Fidesz" and "Who will win: MSZP" both became "Who will win" and false-matched external events.
  - **Decision logging:** All trading decisions to JSONL.
  - Split paper executor fee model: buy=maker, sell=taker
  - Alert deduplication, exit_value fix, dashboard P&L fix
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
6. **Crypto Spot via CCXT (optional — first exchange with keys wins):**
   - `KRAKEN_API_KEY` + `KRAKEN_API_SECRET` — Kraken (priority 1)
   - `COINBASE_ADV_API_KEY` + `COINBASE_ADV_API_SECRET` — Coinbase (priority 2)
   - `BINANCE_API_KEY` + `BINANCE_API_SECRET` — Binance (priority 3)
   - `BYBIT_API_KEY` + `BYBIT_API_SECRET` — Bybit (priority 4)
   - `OKX_API_KEY` + `OKX_API_SECRET` + `OKX_PASSPHRASE` — OKX (priority 5)
   - `KUCOIN_API_KEY` + `KUCOIN_API_SECRET` + `KUCOIN_PASSPHRASE` — KuCoin (priority 6)
   - `BITGET_API_KEY` + `BITGET_API_SECRET` + `BITGET_PASSPHRASE` — Bitget (priority 7)
7. **Kraken CLI (optional):** Requires kraken CLI installed in WSL Ubuntu with API keys configured via `kraken auth`
8. **Opinion Labs (optional):** `OPINION_LABS_API_KEY` — API key for full access
9. **AI Advisor (optional — at least one key recommended):**
   - `ANTHROPIC_API_KEY` — Anthropic (Claude Sonnet 4, used first in live mode)
   - `GROQ_API_KEY` — Groq (Llama 3.3 70B, free tier)
   - `GEMINI_API_KEY` — Google Gemini (2.0 Flash, free tier)
   - `OPENROUTER_API_KEY` — OpenRouter (Llama 3.1 70B, pay-per-use)
10. **News Scanner AI (optional — separate keys prevent rate limit contention):**
   - `NEWS_ANTHROPIC_API_KEY`, `NEWS_GROQ_API_KEY`, `NEWS_GEMINI_API_KEY`, `NEWS_OPENROUTER_API_KEY`
   - Falls back to the base AI advisor keys above if not set

Run `GET /api/derivatives/config` to verify which platforms show as "configured" vs "missing".
The system validates: paper mode flag, platform keys, .env file permissions, .gitignore coverage.

## MCP Integration
- `.mcp.json` at project root configures Kraken MCP server for Claude Code
- Exposes 19 tools: market data (ticker, orderbook, OHLC, trades, spreads) + paper trading (init, buy, sell, balance, history)
- Start: `wsl -d Ubuntu -- bash -c 'source $HOME/.cargo/env && kraken mcp -s market,paper'`
- Dangerous tools (real orders, withdrawals) require `--allow-dangerous` flag and `"acknowledged": true` in args
- To enable all services: change args to `["-s", "all"]` in `.mcp.json`

---

## Subsystem 4: Lobsterminal (Stock Terminal)

### Files
| File | Purpose |
|------|---------|
| `src/server.py` | Main backend — quotes, history, watchlist, portfolio, Dexter fundamentals, WebSocket |
| `src/static/js/app.js` | Main frontend — market table, chart, watchlist, screener, portfolio, universe browser |
| `src/static/index.html` | HTML shell with 6 panes + universe controls |
| `src/static/css/terminal.css` | Terminal styles |
| `src/swarm_engine.py` | AI stock screener — natural language → structured rules → filtered tickers |
| `src/backtest_engine.py` | Backtesting engine — multi-source historical data, metrics, scoring |
| `src/stock_universe.py` | SEC EDGAR universe — 9,920 US tickers + 135 HKEX Hang Seng constituents |
| `src/portfolio_manager.py` | Portfolio CRUD, weight optimization, deployment |
| `src/strategy_engine.py` | Strategy templates, research-based trading strategies |

### Stock Universe (`stock_universe.py`)
- **SEC EDGAR source:** Downloads `company_tickers.json` from SEC (~9,920 US tickers with CIK, name, exchange)
- **HKEX:** 135 hardcoded Hang Seng Index constituents (e.g., `0700.HK` Tencent, `9988.HK` Alibaba)
- **`get_universe(exchange, include_hk)`** — filter by exchange (ALL/NASDAQ/NYSE/AMEX), optionally include HKEX
- **Ticker validation:** `_SYMBOL_RE` updated to `^[A-Z]{1,5}(\.[A-Z]{1,4})?$` for `.HK` tickers

### Universe Browser (Frontend)
- **WATCHLIST/UNIVERSE toggle** in Pane 1 header — switches between curated watchlist and full universe
- **Universe controls bar:** search input (400ms debounce), exchange dropdown (All US/NASDAQ/NYSE), +HKEX checkbox, total counter, LOAD MORE button
- **Pagination:** fetches 50 tickers per page from `/api/research/universe`, then quotes from `/api/research/universe/quotes`
- **`universeState` object:** tracks mode, tickers, quotes, offset, pageSize, total, loading state

### Data Fallback Architecture

All data fetching uses multi-source fallback chains with persistent disk caching. No mock/fake data — every chain ends with a real data source or cached real data.

**Quote Fallback Chain (server.py):**
```
yfinance → Yahoo v8 Chart API → Finnhub /api/v1/quote → Scrapling (Google Finance) → stale cache → mock (last resort)
```

**History Fallback Chain (server.py):**
```
yfinance → Yahoo v8 Chart API → Finnhub /api/v1/stock/candle → disk cache (7-day TTL) → mock
```
- Persistent history cache at `data/history_cache/` — auto-populated on every successful fetch, 7-day TTL

**Backtest Fallback Chain (backtest_engine.py):**
```
yfinance → Yahoo v8 Chart API → Finnhub candles → Alpha Vantage TIME_SERIES_DAILY → Scrapling Yahoo CSV
```
- 5 real data sources, no mock fallback — if all fail, backtest returns error

**Screener Fallback Chain (swarm_engine.py):**
```
FMP Screener API → SP500 fundamentals cache → Full universe cache (9,920 tickers) → MOCK_UNIVERSE
```
- Full universe cache at `data/universe_fundamentals.json` — background thread fetches fundamentals in chunks of 500
- Progressive: cache saves after each chunk, so partial data available during fetch

**Key API sources:**
| Source | Endpoint | Used For |
|--------|----------|----------|
| yfinance | Python library | Quotes, history (primary) |
| Yahoo v8 Chart | `query1.finance.yahoo.com/v8/finance/chart/` | Quotes, history (bypasses yfinance rate limiter) |
| Finnhub | `finnhub.io/api/v1/` | Quotes (`/quote`), history (`/stock/candle`) |
| Alpha Vantage | `alphavantage.co/query` | History (`TIME_SERIES_DAILY_ADJUSTED`) |
| Scrapling | Web scraper | Google Finance quotes, Yahoo Finance CSV history |
| FMP | `financialmodelingprep.com/api/v3/` | Screener, fundamentals |

### Lobsterminal API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/research/quotes` | Real-time quotes (6-source fallback) |
| GET | `/api/research/history` | Historical OHLCV (5-source fallback + disk cache) |
| GET | `/api/research/universe` | Paginated ticker list from SEC EDGAR (offset, limit, search, exchange) |
| GET | `/api/research/universe/quotes` | Quotes for a page of tickers (50 max) |
| GET | `/api/generate-asset/universe-status` | Universe cache status (count, age, fetching?) |
| POST | `/api/generate-asset/universe-refresh` | Trigger background universe fundamentals fetch |
| POST | `/api/generate-asset/backtest` | Run backtest (5-source historical data) |
| POST | `/api/generate-asset/screen` | AI stock screener (natural language → filtered tickers) |
| GET | `/api/research/watchlist` | Get watchlist |
| POST | `/api/research/watchlist` | Add to watchlist |
| DELETE | `/api/research/watchlist` | Remove from watchlist |
| GET | `/api/research/news` | RSS news aggregation |
| WS | `/ws/prices` | Real-time price WebSocket |
