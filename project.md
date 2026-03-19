# Project: Arbitrout (Prediction Market Arbitrage + Auto Trading)
Status: ACTIVE
Phase: BUILD
Last Updated: 2026-03-18
Repo: https://github.com/sorry-ari0/arbitrout.git
Branch: feat/derivative-position-manager

## Overview
Arbitrout is a prediction market trading system with three core capabilities:
1. **Cross-platform arbitrage scanner** — finds price discrepancies across Polymarket, PredictIt, Limitless, Kalshi, and more
2. **Autonomous paper trading** — auto trader scans ALL platforms (9 adapters, 10 executors) for opportunities, opens directional bets and cross-platform arb with risk management
3. **Insider/whale tracker** — monitors top Polymarket traders and uses their positions as trading signals
4. **AI news scanner** — monitors RSS feeds, uses AI to match headlines to prediction markets, executes trades on breaking news before markets react

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

  TradeJournal <─── PositionManager        (records on close)
  AIAdvisor    <─── ExitEngine             (reviews non-safety triggers)
  NewsAI       <─── NewsScanner            (headline scan + deep article analysis)
  InsiderTracker ──> AutoTrader            (signal boost for scoring)
  NewsScanner  ──> AutoTrader              (queued opportunities via asyncio.Lock)
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

  SKIP if: yes_price > 0.92 or < 0.08  (near-resolved, no edge)
  SKIP if: 0.42 < yes_price < 0.58     (near-50/50, no conviction)
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

  ENTER if: score >= 3.0 AND net_profit >= 3%
  STRATEGY: directional bet (one side) on same-platform, arb (both sides) on cross-platform
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
- **Auto-scan:** Background tasks: arbitrage (60s), exit engine (60s), auto trader (5m), insider tracker (15m), news scanner (150s)
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
| `src/positions/exit_engine.py` | 60s scan loop, 18 heuristic triggers, safety overrides, AI routing |
| `src/positions/auto_trader.py` | Autonomous paper trader — scans Polymarket Gamma API for crypto markets |
| `src/positions/trade_journal.py` | Records completed trades with P&L, fees, exit triggers, performance analytics |
| `src/positions/insider_tracker.py` | Whale/insider monitor — leaderboard, positions, accuracy tracking, movement alerts |
| `src/positions/ai_advisor.py` | Multi-provider AI advisor for exit decisions (Groq/Gemini/OpenRouter/Anthropic) |
| `src/positions/news_scanner.py` | AI news scanner — RSS feed monitor, two-pass AI pipeline, trade execution |
| `src/positions/news_ai.py` | Multi-provider LLM analysis for headline scanning and deep article review |
| `src/positions/decision_log.py` | JSONL decision logger — records buys, skips, triggers, AI verdicts, exits, news signals |
| `src/positions/wallet_config.py` | Paper/live mode config, .env loading, key validation, safe config API |

### How Derivative Packages Work

**Package structure:**
- A "package" is a grouped position with one or more "legs" (individual market bets) and "exit rules"
- Strategy types: `spot_plus_hedge`, `cross_platform_arb`, `pure_prediction`, `news_driven`
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

**Non-safety trigger routing (with rate limiting: max 3 AI reviews per tick, 2s spacing):**
- If AI advisor available → batch non-safety triggers to AI for review (Groq → Gemini → OpenRouter chain)
- AI returns: APPROVE (execute), MODIFY (adjust rule params within bounds), REJECT (skip with reason)
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
- Builds structured prompt with: package context (legs, P&L, rules), triggered proposals
- Expects response format: `<rule_id>: APPROVE | MODIFY <value> | REJECT <reason>`
- Parses response into verdicts dict
- On failure: logs warning, tries next provider. All fail → returns empty dict (caller auto-executes)
- API keys configured in `.env`: `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`

### How the Auto Trader Works

1. Every 5 minutes, scans ALL platforms via ArbitrageScanner (not just Polymarket)
2. **Three opportunity sources:**
   - Cross-platform arbitrage (highest priority, 3x score boost): from `scanner.get_opportunities()`
   - Single-platform directional bets: from ALL platform events via `scanner.get_events()`, filtered to tradeable platforms only (those with registered executors)
   - Queued news signals (2x score boost): from NewsScanner via `add_news_opportunity()`
3. Fallback: direct Polymarket Gamma API scan if scanner fails entirely
4. **ITM/OTM filter:** skips entries with side price > $0.85 (tiny upside) or < $0.15 (lottery tickets)
5. **Filters:** skips near-resolved (>0.92 or <0.08), near-50/50 (0.42-0.58), and <2 day expiry
6. Calculates profit potential AFTER estimated round-trip fees: `raw_profit = ((1.0 - favored_price) / favored_price) * 100`, then `net_profit = raw_profit - 2%` round-trip fees
7. Scores each market: `profit_pct * crypto_boost(2x) * expiry_boost(2x if 3-14d, 1.5x if 14-30d) * volume_boost(1.5x if >100K, 1.2x if >10K) * conviction_boost(1.5x if >0.3) * insider_boost`
8. Insider signal boost: if insiders have positions, `score *= (1 + strength * 2)`. Suspicious insiders add extra 1.5x
9. Requires minimum 3% net spread after fees to enter
10. **Directional bets (same platform):** picks ONE side based on conviction — cheaper side = higher upside. Buying both YES and NO on the same platform locks in the spread minus fees = guaranteed loss
11. **Cross-platform arb:** buys both sides on different platforms (spread capture)
12. **Cooldown:** 30-minute re-entry cooldown after exiting a market
13. Exit rules tuned for directional bets: target profit (25%), stop loss (-20%), trailing stop (12%, bounds 5-30%)
14. Position limits: $200 max per trade, $5 min, 10 concurrent positions, $2000 total exposure
15. **Decision logging:** all buys, skips (with reason), and failures logged to `decision_log.jsonl`

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
- **Buys use maker rate** (limit orders, 0% for Polymarket), **sells always use taker rate** (2% for Polymarket)
- `sell(asset_id, quantity, last_known_price=0)` — tries real price first, falls back to `last_known_price` (from exit engine's last scan), then entry price
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
- Auto trader deducts 2% round-trip from profit assessment before entering
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
3. Tests in `tests/` (48 passing, ignore test_arbitrage.py)
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
| Kalshi | Needs API key (401) | 0 |
| Robinhood | Scraping returns 0 | 0 |
| Coinbase | Scraping returns 0 | 0 |
| CryptoSpot | Working (CoinGecko) | ~45 |
| Opinion Labs | Needs API key (401) | 0 |

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

## Current Status (2026-03-18)
- Paper trading: 10 open positions, $1,837 invested, +$38 unrealized, +$422 realized P&L
- 32 closed packages, 12% win rate (improving — previous 28/32 were breakeven from same-platform both-sides bug)
- AI advisor active via Groq (Llama 3.3 70B), ~500ms per review
- Insider tracker: 100 traders monitored, 139 markets with signals
- Auto trader: event-driven (wakes within seconds of each 60s arb scan), 5-min safety-net fallback, directional bets, respecting $2K exposure limit, $5 min trade size
- Exit engine: 60s interval, 18 heuristic triggers, max 3 AI reviews/tick with 2s spacing
- News scanner: 150s interval, 8 RSS feeds, two-pass AI pipeline (headline scan → deep analysis), breaking trades execute immediately
- Decision logging: all buys, skips, trigger fires, AI verdicts, news signals logged to `decision_log.jsonl`
- Arbitrage scanner: 1010 events from 8 adapters
- **Recent changes (2026-03-18):**
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
