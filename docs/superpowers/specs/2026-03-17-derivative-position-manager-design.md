# Derivative Position Manager & Auto-Exit System

**Date:** 2026-03-17
**Status:** Approved (rev 2 -- all review issues resolved)
**Author:** Claude Opus 4.6

## Overview

A system for executing, tracking, and automatically managing synthetic derivative positions that combine crypto spot buys, prediction market contracts, and stock recommendations into hedged packages. The system monitors positions in real-time, applies AI-managed exit rules within user-set guardrails, and provides a dashboard showing ITM/OTM status per-leg and per-package.

## Architecture: Layered Engine

```
UI (arbitrout.js positions dashboard)
  -> API (src/positions/position_router.py)
    -> Position Manager (src/positions/position_manager.py) -- package CRUD, balance, rollback
    -> Exit Engine (src/positions/exit_engine.py) -- 30s loop, 18 heuristic triggers
    -> AI Advisor (src/positions/ai_advisor.py) -- heuristic proposals -> Claude API review
    -> Platform Executors (src/execution/*.py) -- one per platform, buy+sell
    -> Position Store (data/positions.json -> SQLite when >20 packages)
```

**Package split (issue #4):** Business logic (position_manager, exit_engine, ai_advisor, position_router) lives in `src/positions/`. Platform-specific execution code stays in `src/execution/`. This prevents circular imports and clarifies responsibilities.

## Data Model

### Package (top-level synthetic derivative)

```python
{
    "id": "pkg_abc123",
    "name": "BTC Volatility Hedge",
    "strategy_type": "spot_plus_hedge",  # see Strategy Types below
    "status": "open",  # open | partially_closed | closed | rollback_failed
    "created_at": "2026-03-17T12:00:00Z",
    "total_cost": 10.00,
    "current_value": 12.40,
    "unrealized_pnl": 2.40,
    "unrealized_pnl_pct": 24.0,
    "itm_status": "ITM",  # ITM | OTM | ATM
    "legs": [...],
    "exit_rules": [...],
    "ai_strategy": {...},
    "execution_log": [...]
}
```

**Strategy Types (issue #17):**
- `spot_plus_hedge` -- real crypto on exchange + prediction market NO as downside protection. Heuristics focus on hedge ratio balance and crypto volatility.
- `cross_platform_arb` -- buy YES on one prediction market + buy NO on another for the same event. Heuristics focus on spread maintenance and simultaneous exit.
- `pure_prediction` -- single-platform or single-direction prediction market bet. Heuristics focus on theta decay and price triggers.

Strategy type determines which heuristic triggers are relevant (e.g., spread_collapse only applies to cross_platform_arb).

### Leg (one side of the trade on one platform)

```python
{
    "leg_id": "leg_001",
    "platform": "coinbase_spot",  # see Platform IDs below
    "type": "spot_buy",  # spot_buy | spot_sell | prediction_yes | prediction_no | stock_advisory
    "asset_id": "BTC",   # CoinGecko ID, CLOB token_id, Kalshi ticker, etc.
    "asset_label": "BTC spot",
    "entry_price": 97000.0,
    "current_price": 99500.0,
    "quantity": 0.0000515,
    "cost": 5.00,
    "current_value": 5.13,
    "expiry": "2026-07-01",  # ISO date or "ongoing" (issue #13)
    "leg_status": "ITM",  # ITM | OTM | ATM
    "status": "open"  # open | closed | exit_failed | rollback_failed
}
```

**Platform IDs (issue #2, #3):**
- `polymarket` -- Polymarket CLOB (prediction markets)
- `kalshi` -- Kalshi exchange (prediction markets)
- `predictit` -- PredictIt (prediction markets)
- `coinbase_spot` -- Coinbase Advanced Trade API (spot crypto buy/sell). **Distinct from** the existing `CoinbaseAdapter` in `src/adapters/coinbase.py`, which fetches prediction market events. The executor uses the Coinbase Advanced Trade REST API, not the prediction market adapter.
- `robinhood` -- Robinhood (stock price fetching + advisory only). Uses Scrapling-based price scraping matching the existing `RobinhoodAdapter` pattern. **No official Robinhood API key exists for retail.** Auth via the same Scrapling session used by the adapter.

**Leg types:**
- `spot_buy` / `spot_sell` -- real crypto on Coinbase Advanced Trade (auto-executed)
- `prediction_yes` / `prediction_no` -- prediction market contracts (auto-executed)
- `stock_advisory` -- recommended stock trade, manually executed by user. User inputs entry price + quantity after execution. System tracks P&L. Never auto-sold.

**ITM/OTM calculation:**
- Per-leg: current_value > cost = ITM, equal = ATM, less = OTM
- Per-package: sum of all leg current_values vs sum of all leg costs

### Exit Rules

```python
{
    "rule_id": "rule_001",
    "type": "trailing_stop",  # trailing_stop | time_exit | price_trigger | spread_collapse
    "params": {
        # trailing_stop
        "bound_min": 5,      # % - user guardrail minimum
        "bound_max": 25,     # % - user guardrail maximum
        "current": 12,       # % - AI-managed current value
        "peak_value": 13.10, # tracked peak package value

        # time_exit
        "hours_before_expiry": 24,

        # price_trigger
        "leg_id": "leg_002",
        "direction": "above",  # above | below
        "price": 0.85,

        # spread_collapse
        "min_spread": 2.0    # % - exit if arb spread drops below this
    },
    "active": true
}
```

### AI Strategy State

```python
{
    "last_evaluated": "2026-03-17T12:05:00Z",
    "recommendation": "Hold -- volatility stable, trailing stop at 12%",
    "adjustments_made": [
        {
            "time": "2026-03-17T12:05:00Z",
            "rule_id": "rule_001",
            "old_value": 15,
            "new_value": 12,
            "reason": "Volatility increased 1.8x -- tightening trailing stop",
            "llm_verdict": "APPROVE"
        }
    ]
}
```

### Execution Log Entry

```python
{
    "time": "2026-03-17T12:00:00Z",
    "action": "buy",  # buy | sell
    "leg_id": "leg_001",
    "platform": "polymarket",
    "price": 0.60,
    "quantity": 8.33,
    "amount_usd": 5.00,
    "tx_id": "0xabc...",
    "trigger": "manual",  # manual | trailing_stop | time_exit | price_trigger | spread_collapse | ai_recommendation
    "fees": 0.00
}
```

## Platform Executors

**The existing `polymarket_executor.py` (22-line stub) will be fully rewritten.** It has no sell logic, no base class, wrong method signatures, and is synchronous. The rewrite is a complete replacement, not a refactoring.

All executors implement `BaseExecutor`:

```python
class BaseExecutor(ABC):
    """Base class for all platform executors. All methods are async."""

    @abstractmethod
    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult: ...

    @abstractmethod
    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult: ...

    @abstractmethod
    async def get_balance(self) -> BalanceResult: ...

    @abstractmethod
    async def get_positions(self) -> list[PositionInfo]: ...

    @abstractmethod
    async def get_current_price(self, asset_id: str) -> float: ...

    def is_configured(self) -> bool:
        """Check if required env vars are set."""
        ...
```

**ExecutionResult** dataclass: `success: bool, tx_id: str | None, filled_price: float, filled_quantity: float, fees: float, error: str | None`

| File | Platform | Auth | Executes | Notes |
|------|----------|------|----------|-------|
| polymarket_executor.py | Polymarket | EIP-712 wallet, Polygon | YES/NO buy+sell via CLOB | **Full rewrite of existing stub.** Sell requires posting sell-side limit/market orders to CLOB. Prefer limit (maker) orders for zero fees + rebates. Market orders for urgent exits only. Dynamic taker fee up to ~1.56% at 50% probability. ~200 lines (buy + sell + CLOB sell-side logic). |
| kalshi_executor.py | Kalshi | RSA keypair | YES/NO buy+sell | 0% fees currently. Prices in cents (1-99). Sells by posting opposing order. |
| coinbase_spot_executor.py | Coinbase Advanced Trade | API key + secret | Spot crypto buy/sell | **Unrelated to existing CoinbaseAdapter** (which fetches prediction market events). Uses `coinbase-advanced-py` for spot trading. Market orders for immediate fill. |
| predictit_executor.py | PredictIt | Session auth | YES/NO buy+sell | 850 share limit per contract. Position manager must validate quantity against this cap before execution (issue #12). |
| robinhood_advisor.py | Robinhood | Scrapling session | **No execution** | Uses same scraping approach as existing `RobinhoodAdapter`. Has `recommend()` and `get_current_price()` but NOT `buy()`/`sell()`. Does NOT inherit BaseExecutor. |

**Env vars:** `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER_ADDRESS`, `KALSHI_API_KEY`, `KALSHI_RSA_PRIVATE_KEY`, `COINBASE_ADV_API_KEY`, `COINBASE_ADV_API_SECRET`, `PREDICTIT_SESSION`, `ANTHROPIC_API_KEY`

Note: No `ROBINHOOD_API_KEY` -- Robinhood uses Scrapling scraping, not an API key.

## Exit Engine

`src/positions/exit_engine.py` -- runs a background loop every 30 seconds. Realistic estimate: **~400-450 lines** (issue #16) including error handling per trigger.

### Loop Logic

```
every 30s:
  for each open package:
    fetch current prices for all legs (via executor.get_current_price or scraping)
    update current_value, pnl, itm_status on each leg + package
    save state (atomic write)
    broadcast position_update via WebSocket
    for each exit_rule:
      if triggered:
        if safety override -> execute immediately
        else -> queue for AI review (heuristic -> Claude API -> execute/reject)
    if errors: continue to next package (never halt loop for one failure)
```

### 18 Heuristic Triggers (6 categories)

**Price & Value (5):**
1. Volatility spike (>2x 7-day avg stddev) -> tighten trailing stop
2. Volatility collapse (<0.5x avg) -> loosen trailing stop
3. Flash crash (any leg drops >15% in <5 min) -> emergency escalate, freeze auto-exits
4. Sustained drift (P&L negative 6+ consecutive checks) -> recommend full exit
5. New ATH on package value -> update peak for trailing stop

**Spread & Arbitrage (3):**
6. Spread narrowing (>50% from entry) -> recommend full exit
7. Spread inversion (negative) -> SAFETY: immediate full exit, bypass LLM
8. Counter-party spike (opposing side >20% move) -> tighten trailing stop to bound_min

**Time-Based (4):**
9. <48h to expiry -> tighten trailing stop to bound_min
10. <24h to expiry -> SAFETY: auto-exit all legs, bypass LLM
11. <6h to expiry -> SAFETY: force-exit at market price
12. Weekend/holiday approaching + thin spread -> pre-alert, recommend exit

**Leg Divergence (4):**
13. One leg >80% profit -> recommend partial exit
14. One leg >50% loss, other flat -> recommend closing losing leg
15. Both legs profitable -> recommend full exit (windfall)
16. Both legs moving same direction (hedge failing) -> escalate

**Liquidity & Platform (2):**
17. Platform API errors 3+ consecutive -> alert, pause exits for that platform
18. Platform outage detected -> freeze affected legs, alert

**Volume & Market (bonus, evaluated when data available):**
- Volume spike >5x average -> trigger LLM review
- Contract near resolution (>$0.95 or <$0.05) -> recommend selling near-certain leg
- New related contract appears -> LLM evaluates restructuring

**Safety Overrides (bypass LLM, execute immediately):**
- Spread inversion (trigger 7)
- <24h to expiry (trigger 10)
- <6h to expiry (trigger 11)
- Platform reports position liquidated/settled

**Strategy type filtering:** Not all triggers apply to all strategy types. `spread_collapse` and `spread_narrowing` only apply to `cross_platform_arb`. `hedge_failing` (trigger 16) only applies to `spot_plus_hedge`. `theta_decay` triggers apply to any package with prediction market legs.

## AI Advisor

`src/positions/ai_advisor.py` -- two-stage system. Realistic estimate: **~250-300 lines** (issue #16).

### Flow

```
heuristic detects signal -> produces structured proposal
  -> build context: package state, price history, volatility, time to expiry
  -> call Claude API with structured prompt
  -> Claude returns: APPROVE / MODIFY / REJECT
  -> APPROVE or MODIFY (within bounds): apply change, log, broadcast
  -> MODIFY (outside bounds): create escalation alert for user
  -> REJECT: log reason, no action
  -> API timeout (>10s): safety rules execute anyway, non-safety hold
```

### Claude API Usage (issue #14)

- Import: `from anthropic import Anthropic` (PyPI package `anthropic` is correct for Python)
- **Batching:** All proposals for a single package in one 30s cycle are batched into a single Claude API call. This means at most 1 API call per package per 30s cycle, not 1 per rule.
- **Rate limit:** Max 10 Claude API calls per minute across all packages. If more proposals queue, they wait for next cycle.
- **Cost estimate:** ~500 input tokens per call (package context + proposal). At $15/M input tokens, 10 calls/min = $0.45/hour worst case, typically much less since proposals only fire on signal detection.

### Claude Prompt Structure

```
You are reviewing a trading strategy adjustment for a synthetic derivative package.

Package: {name, strategy_type, legs with entry/current prices, P&L, time to expiry}
Proposed changes (may be multiple): [{rule_type, old_value, new_value, heuristic_reasoning}]
Guardrail bounds per rule: [{rule_id, min, max}]
Recent price history: [last 10 price points per leg]

For each proposed change, respond with exactly one of:
- APPROVE -- apply as proposed
- MODIFY <value> -- apply with your adjustment (must be within bounds)
- REJECT <reason> -- no change

Format: one line per proposal, e.g.:
rule_001: APPROVE
rule_002: MODIFY 8
```

### Strategy Defaults (from March 2026 research)

- **Position sizing:** Quarter Kelly (25% of Kelly-recommended). User-adjustable [10-50%].
- **Max single package risk:** 5% of total portfolio.
- **Hedge ratio:** 50% default. AI adjustable [30-70%].
- **Partial exit ladder:** recommend selling at 1:1 R:R (cover risk), 2:1 R:R (take 30-40%), trail remainder.
- **Fee awareness:** reject opportunities where spread < combined platform fees. Prefer maker orders (Polymarket: zero fees + rebate). Kalshi currently 0% fees.
- **Theta awareness:** start recommending partial exits at 72h to expiry. Contracts >$0.90 with <48h = near-certain, sell opposite leg.
- **Speed focus:** target structural/synthetic arbs (minutes-days), not latency arbs (seconds).
- **PredictIt cap (issue #12):** position_manager validates quantity <= 850 shares before executing on PredictIt. If Quarter Kelly recommends more, cap at 850.

## API Endpoints

All under `/api/derivatives/` (issue #5 -- avoids conflict with existing `/api/positions` stock portfolio routes).

All endpoints require `Depends(verify_api_key)` authentication (issue #10).

### Packages
| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/packages` | List all packages (filterable by status) |
| GET | `/packages/{id}` | Full detail: legs, rules, P&L, AI log |
| POST | `/packages` | Create package (legs + rules + guardrails) |
| PATCH | `/packages/{id}` | Update name, guardrail bounds |
| DELETE | `/packages/{id}` | Force-close all legs, archive |

### Manual Actions
| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/packages/{id}/exit` | Manual full exit |
| POST | `/packages/{id}/exit-leg/{leg_id}` | Manual single leg exit |
| POST | `/packages/{id}/confirm-stock` | Confirm stock advisory executed (entry_price, quantity) |

### Exit Rules
| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/packages/{id}/rules` | List active rules |
| POST | `/packages/{id}/rules` | Add rule |
| PATCH | `/packages/{id}/rules/{rule_id}` | Adjust params/bounds |
| DELETE | `/packages/{id}/rules/{rule_id}` | Remove rule |

### Dashboard
| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/dashboard` | Aggregate: total invested, value, P&L, package counts |
| GET | `/dashboard/alerts` | Pending escalations, notifications, recommendations |
| POST | `/dashboard/alerts/{id}/approve` | Approve escalation |
| POST | `/dashboard/alerts/{id}/reject` | Reject escalation |

### Platform Status
| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/balances` | Balance per configured platform |
| GET | `/config` | Which platforms have API keys set |

### WebSocket
| Endpoint | Messages |
|----------|----------|
| WS `/api/derivatives/ws` (issue #6) | position_update, package_created, package_closed, leg_closed, ai_adjustment, escalation, exit_executed, recommendation |

## Frontend: Position Dashboard

New section in arbitrout.js with three areas:

### Portfolio Overview Bar
Total invested, current value, P&L ($ and %), open/closed counts, per-platform balances.

### Package Cards
Each open package as a card showing:
- Package name, ITM/OTM badge (green/red/yellow), P&L
- Each leg with entry vs current price, quantity, individual ITM/OTM, expiry countdown
- Active exit rules with current AI-managed values and guardrail bounds
- Latest AI recommendation text
- Action buttons: Sell Leg, Exit All, Edit Rules
- Stock advisory legs show "Confirm Execution" instead of "Sell"

### Alerts Panel
- Escalations (need user response): approve/reject buttons
- Executed exits (informational): trigger reason, P&L realized
- Stock recommendations: "Sell X shares SPY" with "Mark as Done" button

### Package Creation Flow
From opportunity list "Execute" button:
1. Select amount ($10, $25, $50, custom)
2. Preview: package structure, legs, platforms, fee breakdown, expected P&L range
3. Set guardrails (or accept defaults: trailing 5-25%, time exit 24h, quarter Kelly)
4. Confirm -> execute both legs, create package, start monitoring

## Data Flow

### Creation
```
POST /api/derivatives/packages -> verify_api_key
  -> validate: platforms configured? sufficient balance?
  -> validate: PredictIt quantity <= 850 shares if applicable
  -> quarter Kelly sizing, fee estimation
  -> concurrent executor.buy() for each auto-executed leg
  -> stock_advisory legs: create as "open" with entry_price=0 (user confirms later)
  -> both succeed: save package, broadcast package_created
  -> one fails: attempt sell successful leg (rollback)
    -> rollback succeeds: return error with "rolled back" status
    -> rollback also fails: save package as status="rollback_failed", alert user (issue #8)
```

### Monitoring (every 30s)
```
exit_engine loop -> fetch prices -> update P&L -> save state
  -> broadcast position_update -> evaluate rules (filtered by strategy_type)
  -> triggers fire: safety=immediate, else=batch proposals per package->Claude->execute
```

### Selling
```
exit triggered -> for each leg:
  stock_advisory: create recommendation alert, skip auto-sell
  prediction market: limit order first (maker, zero fee on Polymarket)
    -> if no fill in 30s, downgrade to market order (taker fee accepted)
  crypto spot: market order on Coinbase Advanced Trade
  -> update leg/package status, save, broadcast
```

### Persistence (issue #9)
- `data/positions.json` -- all packages, atomic writes via write-to-temp + os.replace()
- `data/execution_log.json` -- append-only audit trail
- Loaded into memory on server start, written on every state change
- **os.replace() limitation on Windows NTFS:** not fully atomic when target exists. Acceptable for single-user local system with 30s write interval. Under concurrent access or >20 packages, migrate to SQLite (single-file DB, true atomic writes, WAL mode for concurrent reads).
- **Migration path:** when package count exceeds 20, log a warning recommending SQLite migration. The position_manager's load/save interface is abstracted so swapping JSON for SQLite requires changing only the persistence layer.

## File Structure

```
src/positions/              # NEW package -- business logic
  __init__.py
  position_manager.py       (~200 lines)  -- package CRUD, balance checks, rollback
  exit_engine.py            (~400-450 lines) -- 30s loop, 18 triggers, exit execution
  ai_advisor.py             (~250-300 lines) -- heuristic proposals, Claude batching, guardrails
  position_router.py        (~200 lines)  -- FastAPI router + WebSocket
  wallet_config.py          (~50 lines)   -- env var loading, platform availability

src/execution/              # EXISTING package -- platform-specific executors
  __init__.py               # needs creation
  base_executor.py          (~80 lines)   -- ABC + ExecutionResult/BalanceResult dataclasses
  polymarket_executor.py    (~200 lines)  -- FULL REWRITE of existing 22-line stub
  kalshi_executor.py        (~150 lines)  -- RSA auth, buy+sell
  coinbase_spot_executor.py (~130 lines)  -- Coinbase Advanced Trade (NOT the prediction adapter)
  predictit_executor.py     (~120 lines)  -- session auth, 850-share cap validation
  robinhood_advisor.py      (~80 lines)   -- Scrapling price fetch + recommend()

data/
  positions.json            (runtime, created on first package)
  execution_log.json        (runtime, created on first trade)
```

**Total: ~1,860-2,060 lines across 13 files** (revised estimates, issue #16)

### New Dependencies
- `py-clob-client` -- Polymarket CLOB API (buy + sell side)
- `kalshi-python` -- Kalshi REST API
- `coinbase-advanced-py` -- Coinbase Advanced Trade API (spot crypto, NOT prediction markets)
- `anthropic` -- Claude API for AI advisor (`from anthropic import Anthropic`)

### Integration Points
- `server.py`:
  - Import and include `position_router` with prefix `/api/derivatives`
  - Start exit_engine background task in lifespan
  - Add `"PATCH"` to CORS allowed methods (issue #15)
  - Apply `Depends(verify_api_key)` to all derivative endpoints
- `arbitrout.js`: add positions dashboard tab, package cards, alerts, execute button
- No changes to existing arbitrage_engine, adapters, or scanner

## Error Handling
- Every executor call: try/except, returns typed `ExecutionResult` (success/failure with error reason)
- Partial execution rollback: attempt to sell successful leg. If rollback also fails, mark package `rollback_failed` and alert user immediately (issue #8)
- Atomic file writes: write temp file -> os.replace() (acceptable for single-user, see persistence notes)
- Exit engine: continues processing other packages if one fails (never halt the loop)
- Claude API timeout (>10s): safety rules execute anyway, non-safety proposals hold until next cycle
- Claude API batching: max 1 call per package per cycle, max 10 calls/min total (issue #14)
- Platform outage (3+ consecutive errors): freeze platform exits, alert user
- Stock advisory legs: never auto-sold, recommendation only
- PredictIt 850-share cap: validated at creation time, rejected if exceeded (issue #12)
