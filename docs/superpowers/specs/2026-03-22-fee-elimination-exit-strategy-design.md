# Fee Elimination & Exit Strategy Overhaul Design

**Goal:** Eliminate fee drag (currently $57.40 on $25.02 net loss — fees are 2.3x the actual losses) by switching all exits to 0% maker GTC limit orders, removing premature exit triggers, and defaulting to hold-to-resolution for binary prediction markets.

**Architecture:** Bracket-only exits + hold-to-resolution default. Standard prediction packages get GTC limit sell brackets at entry. Guaranteed-profit strategies (arb, synthetic, high-probability) use hold-to-resolution. No trailing stops, no AI exits, no mechanical price-based triggers besides target_hit. Safety overrides use limit orders first. Per-category fee model replaces the hardcoded 2% taker assumption.

**Tech Stack:** Python, existing modules (no new files or classes).

---

## 1. Exit Strategy Overhaul

Strip exit logic down to three paths:

| Exit Path | Trigger | Order Type | Fee |
|---|---|---|---|
| Bracket target | Price hits GTC limit sell | Maker | 0% |
| Bracket stop (monitored) | Price drops to stop level, places limit sell | Maker | 0% |
| Safety override | Spread inversion, political resolution | GTC limit (fallback to FOK after timeout) | 0% (2% fallback) |
| Resolution | Contract settles at $0/$1 | N/A | 0% |

### What gets removed/disabled

- `trailing_stop` removed from `_auto_execute_triggers` mechanical execution list — the one-line change from `("target_hit", "stop_loss", "trailing_stop")` to `("target_hit",)`. Note: trailing_stop is already functionally disabled via `_AI_EXIT_TRIGGERS` when `AI_EXITS_ENABLED = False` (line 19). This change is defense-in-depth.
- `stop_loss` removed from mechanical (heuristic-driven FOK) execution — no longer auto-executes via the exit engine's `_auto_execute_triggers`. However, bracket_manager's monitored stop level still functions (places a 0% maker limit sell, not a 2% taker FOK).
- AI exits remain `AI_EXITS_ENABLED=False` (already done, unchanged)
- `time_decay`, `negative_drift` triggers still evaluate for logging/monitoring but never execute

### What stays

- `target_hit` via bracket GTC orders (0% maker fee)
- Bracket-level monitored stops — `bracket_manager.check_brackets()` still monitors stop price levels and places maker limit sells (0% fee) when triggered. This is different from the heuristic `stop_loss` which used FOK (2% taker).
- Safety overrides: `spread_inversion`, `political_event_resolved` (all legs)
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

The fee **rate** (dimensionless) for a given price:
```
taker_fee_rate = feeRate * (price * (1 - price))^exponent
```

The actual dollar fee when selling: `fee = quantity * price * taker_fee_rate`

Peak fee rate at price=0.50, drops to near-zero at extremes ($0.01, $0.99). Verified: crypto at p=0.50 → `0.25 * (0.25)^2 = 1.5625%`, crypto at p=0.10 → `0.25 * (0.09)^2 = 0.2025%`. These match Polymarket's published rates. The auto_trader's `_detect_category()` returns generic "crypto" or "sports" — we use a single feeRate/exponent pair per category (no sub-category detection needed since the auto_trader doesn't distinguish 5-min from 15-min crypto markets).

### Implementation

Add a `get_taker_fee_rate(category: str, price: float) -> float` function to `paper_executor.py` that returns the dimensionless rate. The auto_trader already classifies markets via `_detect_category()` — store category on the package dict at creation time (`pkg["_category"] = self._detect_category(opp_title)`). The position_manager reads it from the package when calling executor.sell() for FOK fallbacks.

Since we're moving to all-maker exits, taker fees only matter for:
- The rare FOK safety fallback on safety overrides
- Trade selection math (deciding whether spread > fee threshold)

`ROUND_TRIP_FEE_PCT = 0.0` in auto_trader remains correct for maker-only round trips on non-crypto markets. For crypto markets, the auto_trader should compute the actual taker fee cost when evaluating whether a spread is profitable enough.

---

## 3. Bracket Orders & Hold-to-Resolution

### Current state

Standard prediction packages (lines 1462-1465) already get `_use_brackets = True` when `_hold_to_resolution` is not set. Hold-to-resolution packages (cross_platform_arb, synthetic_derivative, high-probability pure_prediction) intentionally skip brackets and resolve at $0/$1.

The gap: `multi_outcome_arb` handler (line 876-885) has neither `_use_brackets` nor `_hold_to_resolution`. It also has no `target_profit` exit rule — only `stop_loss`. Since multi_outcome_arb is a guaranteed-profit strategy (sum of outcomes < $1), it should use hold-to-resolution.

### New behavior

Two categories:

1. **Standard prediction packages** — Already get `_use_brackets = True` (line 1464). Bracket target GTC sell at `entry_price * (1 + target_pct)`. Bracket-level monitored stop places a maker limit sell (0% fee) if price drops to stop level. Trail adjustment rolls target up as price rises. **No change needed.**
2. **Guaranteed-profit packages** (multi_outcome_arb) — Add `_hold_to_resolution = True`. These resolve naturally at $0/$1 with zero fees. bracket_manager.place_brackets() already skips hold_to_resolution packages (line 34-36).

Hold-to-resolution packages: cross_platform_arb, synthetic_derivative, high-probability pure_prediction (already set), and now multi_outcome_arb.

### When brackets don't fill

- Short-duration markets (<2 weeks): Hold to resolution. Contract pays $1 or $0.
- Long-duration markets (>1 month): Bracket target sits on the book. If it fills, great. If not, the position resolves naturally.

### Changes

- `auto_trader.py` line 879 (multi_outcome_arb handler): Add `pkg["_hold_to_resolution"] = True` — guaranteed profit resolves at $1, no bracket needed
- `exit_engine.py`: Already skips heuristic-driven exits for bracketed packages (line 596: `if pkg.get("_brackets"): continue`)
- `position_manager.py`: No changes — `execute_package()` already calls bracket_manager when `_use_brackets` is set (lines 256-260), and bracket_manager already skips hold_to_resolution

---

## 4. Safety Override Limit Orders

### Current state

Safety overrides execute via `exit_leg()` with FOK (2% taker fee).

### New behavior

Safety overrides try GTC limit first, fall back to FOK after timeout:

| Safety Trigger | Order Type | Rationale |
|---|---|---|
| `spread_inversion` | GTC limit, 5min timeout then FOK | Mispricing — resting sell may fill quickly |
| `political_event_resolved` (all legs) | GTC limit, 5min timeout then FOK | Known prices — limit at $0.99/$0.01 fills fast |

Note: `expired_contract` does not exist as a trigger in the codebase. The existing expiry triggers (`time_24h` #10, `time_6h` #11) are intentionally soft review triggers, not safety overrides — force-exiting before expiry lost $38.88 across 25 trades (per exit_engine docstring). No change to expiry behavior.

### Implementation

Modify safety override loop in `exit_engine.py` (lines 583-593):
- For safety triggers: call `exit_leg(use_limit=True)` instead of `exit_leg()`
- The existing `_place_limit_sell` → `resolve_pending_order` path handles the limit logic
- **Timeout:** The existing `resolve_pending_order` has a hardcoded 60-second timeout at line 555 of `position_manager.py`. Change this to accept a `timeout` parameter: default 60s for normal exits, 300s (5 minutes) for safety overrides. Pass the timeout through the pending order metadata (`pending["timeout"] = 300` for safety, `60` for normal).

---

## 5. File Changes

### `exit_engine.py`
- Line 697: Change `("target_hit", "stop_loss", "trailing_stop")` to `("target_hit",)` — removes mechanical trailing_stop and stop_loss execution from `_auto_execute_triggers`. Defense-in-depth: trailing_stop is already blocked by `_AI_EXIT_TRIGGERS` when `AI_EXITS_ENABLED = False`, but this ensures it stays disabled even if AI exits are re-enabled later.
- Lines 583-593: Safety overrides pass `use_limit=True` for all safety triggers (`spread_inversion`, `political_event_resolved`)

### `auto_trader.py`
- Line 879 (multi_outcome_arb handler): Add `pkg["_hold_to_resolution"] = True` — guaranteed arb profit resolves at $1
- All strategy handlers: Add `pkg["_category"] = self._detect_category(opp_title)` to store category on the package for fee model lookups

### `paper_executor.py`
- Add `get_taker_fee_rate(category: str, price: float) -> float` function returning the dimensionless fee rate using the Polymarket fee curve
- Update `sell()` method to accept optional `category` parameter and use category-aware fee rates instead of the flat `self.sell_fee_rate`
- `sell_limit()` unchanged — already returns 0% maker fee

### `position_manager.py`
- `_exit_leg_locked()`: Read `pkg.get("_category")` and pass to `executor.sell(category=...)` for FOK exits
- `resolve_pending_order()` line 555: Change hardcoded `> 60` timeout to read from `pending.get("timeout", 60)` — allows safety overrides to set 300s timeout
- `_place_limit_sell()`: Accept optional `timeout` param and store in pending order metadata
- No structural changes — `execute_package` already calls bracket_manager when `_use_brackets` is set

### No changes needed
- `bracket_manager.py` — already handles GTC target placement, monitored stop levels (0% maker), trail adjustment, and fill resolution correctly. Bracket-level stops (monitored price → maker limit sell) remain active — these are distinct from heuristic stop_loss (FOK taker) which is being removed.
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
