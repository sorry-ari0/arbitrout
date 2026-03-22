# Fee Elimination & Exit Strategy Overhaul Design

**Goal:** Eliminate fee drag (currently $57.40 on $25.02 net loss — fees are 2.3x the actual losses) by switching all exits to 0% maker GTC limit orders, removing premature exit triggers, and defaulting to hold-to-resolution for binary prediction markets.

**Architecture:** Bracket-only exits + hold-to-resolution default. Every package gets GTC limit sell brackets at entry. No trailing stops, no AI exits, no mechanical price-based triggers. Safety overrides use limit orders first. Per-category fee model replaces the hardcoded 2% taker assumption.

**Tech Stack:** Python, existing modules (no new files or classes).

---

## 1. Exit Strategy Overhaul

Strip exit logic down to three paths:

| Exit Path | Trigger | Order Type | Fee |
|---|---|---|---|
| Bracket target | Price hits GTC limit sell | Maker | 0% |
| Safety override | Spread inversion, political resolution, expired contract | GTC limit (fallback to FOK for expiry) | 0% (2% fallback) |
| Resolution | Contract settles at $0/$1 | N/A | 0% |

### What gets removed/disabled

- `trailing_stop` removed from `_auto_execute_triggers` mechanical execution list — the one-line change from `("target_hit", "stop_loss", "trailing_stop")` to `("target_hit",)`
- `stop_loss` removed from mechanical execution — no longer auto-executes
- AI exits remain `AI_EXITS_ENABLED=False` (already done, unchanged)
- `time_decay`, `negative_drift` triggers still evaluate for logging/monitoring but never execute

### What stays

- `target_hit` via bracket GTC orders (0% maker fee)
- Safety overrides: `spread_inversion`, `political_event_resolved` (all legs), `expired_contract`
- 24h minimum hold period (already implemented, unchanged)
- Bracket trail adjustment — rolls target up as price rises (already implemented in bracket_manager)
- `evaluate_heuristics()` continues running — triggers are logged/suppressed, not executed

### Rationale

Academic research shows:
- Trailing stops are fundamentally wrong for binary instruments (terminal payoff is $0 or $1, not continuous)
- Traders holding 7+ days outperform short-term traders by 18%
- Hold-to-resolution eliminates exit fees, slippage, and spread costs
- The only valid early exit is when the thesis is broken (safety overrides), not when price moves against you

---

## 2. Per-Category Fee Model

Replace the hardcoded 0% maker / 2% taker assumption with category-aware fee rates.

| Category | Maker Fee | Taker Fee | Source |
|---|---|---|---|
| Politics, entertainment, general events | 0% | 0% | Polymarket docs |
| Crypto (5-min, 15-min, longer) | 0% | 0.20%-1.56% (price curve) | feeRate=0.25, exponent=2 |
| Sports (NCAAB, Serie A, etc.) | 0% | 0.04%-0.44% (price curve) | feeRate=0.0175, exponent=1 |
| Kalshi | 0.5% | 1% | Existing rates (unchanged) |
| Coinbase | 0.4% | 0.6% | Existing rates (unchanged) |

### Polymarket fee curve formula

```
fee = quantity * price * feeRate * (price * (1 - price))^exponent
```

Peak fee at price=0.50, drops to near-zero at extremes ($0.01, $0.99).

### Implementation

Add a `get_taker_fee_rate(category: str, price: float) -> float` function to `paper_executor.py`. The auto_trader already classifies markets via `_detect_category()` — pass category through the package to the executor.

Since we're moving to all-maker exits, taker fees only matter for:
- The rare FOK safety fallback on expired contracts
- Trade selection math (deciding whether spread > fee threshold)

`ROUND_TRIP_FEE_PCT = 0.0` in auto_trader remains correct for maker-only round trips on non-crypto markets. For crypto markets, the auto_trader should compute the actual taker fee cost when evaluating whether a spread is profitable enough.

---

## 3. Bracket Orders for All Packages

### Current state

Only some strategy types get `_use_brackets = True`: `portfolio_no`, `multi_outcome_arb`, `weather_forecast`, `political_synthetic`. Pure prediction and `cross_platform_arb` packages often miss brackets.

### New behavior

Every package gets bracket orders at creation:

1. **Target bracket** — GTC limit sell at `entry_price * (1 + target_pct)`. For a 50% target on $0.60 entry, limit sell sits at $0.90. Fills at 0% maker fee.
2. **No stop bracket** — Stops removed entirely. If thesis is wrong, contract resolves at $0. Better than paying 2% taker to exit early on a binary instrument that might still resolve in our favor.
3. **Trail adjustment** — Existing bracket_manager logic rolls target up as price rises. Unchanged.

### When brackets don't fill

- Short-duration markets (<2 weeks): Hold to resolution. Contract pays $1 or $0.
- Long-duration markets (>1 month): Bracket target sits on the book. If it fills, great. If not, the position resolves naturally.

### Changes

- `auto_trader.py`: Set `_use_brackets = True` on ALL packages in every strategy handler, not just selected ones
- `exit_engine.py`: Already skips heuristic-driven exits for bracketed packages (line 596: `if pkg.get("_brackets"): continue`)
- `position_manager.py`: Ensure `execute_package()` always invokes bracket_manager when `_use_brackets` is set (already does at lines 256-260, just needs all packages to have the flag)

---

## 4. Safety Override Limit Orders

### Current state

Safety overrides execute via `exit_leg()` with FOK (2% taker fee).

### New behavior

Safety overrides try GTC limit first, fall back to FOK only if time-critical:

| Safety Trigger | Order Type | Rationale |
|---|---|---|
| `spread_inversion` | GTC limit, 5min timeout then FOK | Mispricing — resting sell may fill quickly |
| `political_event_resolved` (all legs) | GTC limit, 5min timeout then FOK | Known prices — limit at $0.99/$0.01 fills fast |
| `expired_contract` | FOK direct | Contract about to delist — no time for limit |

### Implementation

Modify safety override loop in `exit_engine.py` (lines 583-593):
- For non-expiry safety triggers: call `exit_leg(use_limit=True)` instead of `exit_leg()`
- Add a timeout mechanism: if pending limit order hasn't filled within 5 minutes, cancel and FOK
- The existing `_place_limit_sell` → `resolve_pending_order` path handles the limit logic
- `expired_contract` continues using FOK (no change)

---

## 5. File Changes

### `exit_engine.py`
- Line 697: Change `("target_hit", "stop_loss", "trailing_stop")` to `("target_hit",)` — removes mechanical trailing_stop and stop_loss execution
- Lines 583-593: Safety overrides pass `use_limit=True` for non-expiry triggers
- Add 5-minute timeout check for pending safety limit orders (new logic in the scan loop)

### `auto_trader.py`
- Every strategy handler: ensure `pkg["_use_brackets"] = True` is set. Several handlers already do this; the remaining ones (pure_prediction at line 885, cross_platform_arb at line 1462) need it added.

### `paper_executor.py`
- Add `get_taker_fee_rate(category: str, price: float) -> float` function implementing the Polymarket fee curve
- Update `sell()` method to accept optional `category` parameter and use category-aware fee rates instead of the flat `self.sell_fee_rate`
- `sell_limit()` unchanged — already returns 0% maker fee

### `position_manager.py`
- Pass market category through to executor sell calls (for the rare FOK fallback)
- No structural changes — `execute_package` already calls bracket_manager when `_use_brackets` is set

### No changes needed
- `bracket_manager.py` — already handles GTC target placement, trail adjustment, and fill resolution correctly
- `trade_journal.py` — records whatever fees come through, no changes
- `news_scanner.py` — uses same position_manager path, benefits automatically
- `btc_sniper.py` — separate crypto system, not affected
- `market_maker.py` — already uses limit orders

---

## 6. Expected Impact

### Fee reduction
- Current: ~$3.59/trade average fee ($57.40 / 16 trades)
- Expected: ~$0/trade for political/entertainment markets, <$0.10/trade for crypto at price extremes
- **$57.40 saved** if applied retroactively to the 16 journal trades

### Win rate improvement
- Current: 13.3% (2/15 wins, excluding flat)
- Expected: Higher — positions that were stopped out at 30 minutes (#12-16) can now resolve at $1 if the prediction was correct
- Trades #3 and #4 (the only wins) were held 15.6h and resolved naturally — confirming the hold strategy works

### Capital efficiency tradeoff
- Funds locked until resolution (days to weeks) instead of being recycled after premature exits
- With 7 concurrent positions and $20 bankroll, this means ~$3 per position held for the market's full duration
- Acceptable: the research shows the terminal $1 payoff more than compensates for locked capital
