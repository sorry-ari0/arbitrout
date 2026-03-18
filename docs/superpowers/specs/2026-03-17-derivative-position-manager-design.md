# Derivative Position Manager & Auto-Exit System

**Date:** 2026-03-17
**Status:** Approved
**Author:** Claude Opus 4.6

## Overview

A system for executing, tracking, and automatically managing synthetic derivative positions that combine crypto spot buys, prediction market contracts, and stock recommendations into hedged packages. The system monitors positions in real-time, applies AI-managed exit rules within user-set guardrails, and provides a dashboard showing ITM/OTM status per-leg and per-package.

## Architecture: Layered Engine

```
UI (arbitrout.js positions dashboard)
  -> API (position_router.py)
    -> Position Manager (position_manager.py) -- package CRUD, balance, rollback
    -> Exit Engine (exit_engine.py) -- 30s loop, 18 heuristic triggers
    -> AI Advisor (ai_advisor.py) -- heuristic proposals -> Claude API review
    -> Platform Executors (execution/*.py) -- one per platform
    -> Position Store (data/positions.json) -- persistent state
```

## Data Model

### Package (top-level synthetic derivative)

```python
{
    "id": "pkg_abc123",
    "name": "BTC Volatility Hedge",
    "strategy_type": "spot_plus_hedge | cross_platform_arb | pure_prediction",
    "status": "open | partially_closed | closed",
    "created_at": "2026-03-17T12:00:00Z",
    "total_cost": 10.00,
    "current_value": 12.40,
    "unrealized_pnl": 2.40,
    "unrealized_pnl_pct": 24.0,
    "itm_status": "ITM | OTM | ATM",
    "legs": [...],
    "exit_rules": [...],
    "ai_strategy": {...},
    "execution_log": [...]
}
```

### Leg (one side of the trade on one platform)

```python
{
    "leg_id": "leg_001",
    "platform": "coinbase | polymarket | kalshi | predictit | robinhood",
    "type": "spot_buy | spot_sell | prediction_yes | prediction_no | stock_advisory",
    "asset_id": "BTC | token_id | ticker",
    "asset_label": "BTC spot" | "NO 'BTC>100k Jul'",
    "entry_price": 0.60,
    "current_price": 0.72,
    "quantity": 8.33,
    "cost": 5.00,
    "current_value": 6.00,
    "leg_status": "ITM | OTM | ATM",
    "status": "open | closed | exit_failed"
}
```

**Leg types:**
- `spot_buy` / `spot_sell` -- real crypto on Coinbase (auto-executed)
- `prediction_yes` / `prediction_no` -- prediction market contracts (auto-executed)
- `stock_advisory` -- recommended stock trade, manually executed by user. User inputs entry price + quantity after execution. System tracks P&L. Never auto-sold.

**ITM/OTM calculation:**
- Per-leg: current_value > cost = ITM, equal = ATM, less = OTM
- Per-package: sum of all leg current_values vs sum of all leg costs

### Exit Rules

```python
{
    "rule_id": "rule_001",
    "type": "trailing_stop | time_exit | price_trigger | spread_collapse",
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
        "direction": "above | below",
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
    "action": "buy | sell",
    "leg_id": "leg_001",
    "platform": "polymarket",
    "price": 0.60,
    "quantity": 8.33,
    "amount_usd": 5.00,
    "tx_id": "0xabc...",
    "trigger": "manual | trailing_stop | time_exit | price_trigger | spread_collapse | ai_recommendation",
    "fees": 0.00
}
```

## Platform Executors

All executors implement `BaseExecutor`:

```python
class BaseExecutor(ABC):
    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult
    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult
    async def get_balance(self) -> BalanceResult
    async def get_positions(self) -> list[PositionInfo]
    async def get_current_price(self, asset_id: str) -> float
    def is_configured(self) -> bool
```

| File | Platform | Auth | Executes | Notes |
|------|----------|------|----------|-------|
| polymarket_executor.py | Polymarket | EIP-712 wallet, Polygon | YES/NO contracts via CLOB | Prefer limit (maker) orders for zero fees + rebates. Market orders for urgent exits only. Dynamic taker fee up to ~1.56% at 50% probability. |
| kalshi_executor.py | Kalshi | RSA keypair | YES/NO contracts | 0% fees currently. Prices in cents (1-99). |
| coinbase_executor.py | Coinbase | API key + secret | Spot crypto | Market orders for immediate fill. |
| predictit_executor.py | PredictIt | Session auth | YES/NO shares | 850 share limit per contract. |
| robinhood_advisor.py | Robinhood | API key (read-only) | **No execution** | Price fetching + recommendations only. Has `recommend()` instead of `buy()`/`sell()`. |

**Env vars:** `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER_ADDRESS`, `KALSHI_API_KEY`, `KALSHI_RSA_PRIVATE_KEY`, `COINBASE_API_KEY`, `COINBASE_API_SECRET`, `PREDICTIT_SESSION`, `ROBINHOOD_API_KEY`, `ANTHROPIC_API_KEY`

## Exit Engine

`exit_engine.py` -- runs a background loop every 30 seconds.

### Loop Logic

```
every 30s:
  for each open package:
    fetch current prices for all legs
    update current_value, pnl, itm_status on each leg + package
    save state
    broadcast position_update via WebSocket
    for each exit_rule:
      if triggered:
        if safety override -> execute immediately
        else -> queue for AI review (heuristic -> Claude API -> execute/reject)
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

## AI Advisor

Two-stage system: heuristics detect signals fast, Claude API reviews before execution.

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

### Claude Prompt Structure

```
You are reviewing a trading strategy adjustment for a synthetic derivative package.

Package: {name, strategy_type, legs with entry/current prices, P&L, time to expiry}
Proposed change: {rule_type, old_value, new_value}
Heuristic reasoning: {signal detected, magnitude, recent price data}
Guardrail bounds: {min, max for this rule}

Respond with exactly one of:
- APPROVE -- apply as proposed
- MODIFY <value> -- apply with your adjustment (must be within bounds [{min}, {max}])
- REJECT <reason> -- no change
```

### Strategy Defaults (from March 2026 research)

- **Position sizing:** Quarter Kelly (25% of Kelly-recommended). User-adjustable [10-50%].
- **Max single package risk:** 5% of total portfolio.
- **Hedge ratio:** 50% default. AI adjustable [30-70%].
- **Partial exit ladder:** recommend selling at 1:1 R:R (cover risk), 2:1 R:R (take 30-40%), trail remainder.
- **Fee awareness:** reject opportunities where spread < combined platform fees. Prefer maker orders (Polymarket: zero fees + rebate). Kalshi currently 0% fees.
- **Theta awareness:** start recommending partial exits at 72h to expiry. Contracts >$0.90 with <48h = near-certain, sell opposite leg.
- **Speed focus:** target structural/synthetic arbs (minutes-days), not latency arbs (seconds).

## API Endpoints

All under `/api/positions/`:

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
| WS `/ws` | position_update, package_created, package_closed, leg_closed, ai_adjustment, escalation, exit_executed, recommendation |

## Frontend: Position Dashboard

New section in arbitrout.js with three areas:

### Portfolio Overview Bar
Total invested, current value, P&L ($ and %), open/closed counts, per-platform balances.

### Package Cards
Each open package as a card showing:
- Package name, ITM/OTM badge (green/red/yellow), P&L
- Each leg with entry vs current price, quantity, individual ITM/OTM
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
POST /packages -> validate platforms + balances -> quarter Kelly sizing
  -> concurrent executor.buy() for each leg
  -> both succeed: save package, broadcast package_created
  -> one fails: sell successful leg (rollback), return error
```

### Monitoring (every 30s)
```
exit_engine loop -> fetch prices -> update P&L -> save state
  -> broadcast position_update -> evaluate rules
  -> triggers fire: safety=immediate, else=heuristic->Claude->execute
```

### Selling
```
exit triggered -> for each leg:
  stock_advisory: create recommendation, skip
  prediction market: limit order first, market order after 30s if no fill
  crypto spot: market order
  -> update leg/package status, save, broadcast
```

### Persistence
- `data/positions.json` -- all packages, atomic writes (temp -> rename)
- `data/execution_log.json` -- append-only audit trail
- Loaded into memory on server start, written on every state change

## File Structure

```
src/execution/
  __init__.py
  base_executor.py          (~60 lines)
  polymarket_executor.py    (~150 lines)
  kalshi_executor.py        (~140 lines)
  coinbase_executor.py      (~120 lines)
  predictit_executor.py     (~100 lines)
  robinhood_advisor.py      (~80 lines)
  position_manager.py       (~200 lines)
  exit_engine.py            (~250 lines)
  ai_advisor.py             (~200 lines)
  position_router.py        (~180 lines)
  wallet_config.py          (~50 lines)

data/
  positions.json            (runtime)
  execution_log.json        (runtime)
```

**Total: ~1,530 lines across 12 files**

### New Dependencies
- `py-clob-client` -- Polymarket CLOB API
- `kalshi-python` -- Kalshi REST API
- `coinbase-advanced-py` -- Coinbase Advanced Trade API
- `anthropic` -- Claude API for AI advisor

### Integration Points
- `server.py`: import position_router, start exit_engine in lifespan
- `arbitrout.js`: add positions dashboard tab, package cards, alerts, execute button
- No changes to existing arbitrage_engine, adapters, or scanner

## Error Handling
- Every executor call: try/except, typed result (success/failure with reason)
- Partial execution: rollback (sell successful leg)
- Atomic file writes: temp file -> rename
- Exit engine: continues other packages if one fails
- Claude API timeout (>10s): safety rules execute, non-safety hold
- Platform outage (3+ consecutive errors): freeze platform exits, alert user
- Stock advisory legs: never auto-sold, recommendation only
