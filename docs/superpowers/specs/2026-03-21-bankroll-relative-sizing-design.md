# Bankroll-Relative Position Sizing Design

**Goal:** Replace all hardcoded dollar amounts with a single `INITIAL_BANKROLL` constant, so position sizes, exposure limits, and module budgets scale proportionally as the bankroll grows or shrinks from trading P&L.

**Architecture:** Single constant + fixed ratios. Every dollar-denominated limit is `current_bankroll * ratio`. The bankroll auto-adjusts each scan cycle by reading cumulative P&L from the trade journal. Separate journal files per mode (paper vs live) prevent cross-contamination.

**Tech Stack:** Python, existing modules (no new files or classes).

---

## 1. Core Bankroll Constant & Auto-Adjustment

A single `INITIAL_BANKROLL` constant lives in `auto_trader.py`:
- Paper mode: `2000.0` (preserves current behavior)
- Live mode: `20.0` (real money starting balance)

Each scan cycle computes:
```python
current_bankroll = INITIAL_BANKROLL + journal.get_cumulative_pnl()
```

All dollar-denominated limits derive from `current_bankroll`:

| Derived value | Ratio | Formula | At $20 | At $25 | At $15 |
|---|---|---|---|---|---|
| max_trade_size | 0.025 | `bankroll * 0.025` | $0.50 | $0.63 | $0.38 |
| min_trade_size | 0.05 (floor $1) | `max(1.0, bankroll * 0.05)` | $1.00 | $1.25 | $1.00 |
| max_total_exposure | 0.175 | `bankroll * 0.175` | $3.50 | $4.38 | $2.63 |
| max_concurrent | fixed | 7 | 7 | 7 | 7 |
| Kelly portfolio cap | 0.40 | `bankroll * 0.40` | $8.00 | $10.00 | $6.00 |

The `$1.00` floor on `min_trade_size` ensures Polymarket orders are practical (no formal minimum, but sub-$1 orders face liquidity issues).

**Auto-stop:** If `current_bankroll` drops below $5.71 (where `max_total_exposure < min_trade_size`), the system cannot open new trades. This is correct behavior — stop trading when nearly broke.

**Ratios are derived from the current hardcoded relationship to the $2000 bankroll:**
- $50 / $2000 = 0.025 (max_trade_size)
- $10 / $2000 = 0.005, but raised to 0.05 with $1 floor for Polymarket practicality
- $350 / $2000 = 0.175 (max_total_exposure)
- 0.40 portfolio cap (already a ratio, unchanged)

## 2. Separate Journals by Mode

The trade journal currently writes to `data/positions/trade_journal.json`. Split into:
- `data/positions/trade_journal_paper.json` — paper trades (existing data migrated here)
- `data/positions/trade_journal_live.json` — real money trades (starts empty)

Changes to `TradeJournal`:
- Constructor takes a `mode` parameter (`"paper"` or `"live"`) that determines which file to read/write
- New `get_cumulative_pnl()` method: returns `sum(entry["pnl"] for entry in closed_entries)` — sums ALL entries in the file (no mode filter needed since the file itself is mode-specific)
- Existing `get_performance(mode=...)` and `get_equity_curve(mode=...)` methods: the `mode` parameter becomes optional/ignored since the file is already mode-scoped. Keep the parameter for backwards compatibility but default to reading all entries in the file.

**Migration:** On first startup after deployment, the constructor checks for `trade_journal.json` — if it exists and `trade_journal_paper.json` does not, rename it. If both exist, prefer the mode-specific file. This handles the race condition of a crash mid-migration.

Regime detection (5-loss streak) and walk-forward validation only read from the active mode's journal. Paper trading history does not influence live trading decisions.

## 3. News Scanner & BTC Sniper Scaling

Both modules derive limits from the same `current_bankroll`:

### News Scanner
| Value | Ratio | Formula | At $20 |
|---|---|---|---|
| max_trade_size | 0.10 | `bankroll * 0.10` | $2.00 |
| min_trade_size | 0.0025 (floor $0.50) | `max(0.50, bankroll * 0.0025)` | $0.50 |
| max_total_exposure | 1.0 | `bankroll * 1.0` | $20.00 |

The news scanner's `MAX_TOTAL_EXPOSURE` is the global ceiling ($2000 currently = 1.0x bankroll), not the auto trader's tighter $350 cap. The auto trader's 7 concurrent slots naturally consume ~70% of the shared position manager, so the news scanner's effective budget is the remainder. The 1.0x ratio preserves this relationship.

### BTC Sniper

**Disabled when `current_bankroll < 40.0`.** At $20 live bankroll, the sniper's $5 allocation produces max bets of $1.25 — too small to overcome the 1.56% taker fee on 5-minute crypto markets. The sniper activates when bankroll grows enough for meaningful bets.

When enabled:
| Value | Ratio | Formula | At $40 (threshold) | At $100 |
|---|---|---|---|---|
| bankroll | 0.25 | `current_bankroll * 0.25` | $10.00 | $25.00 |
| min_bet | 0.002 (floor $0.50) | `max(0.50, sniper_bankroll * 0.002)` | $0.50 | $0.50 |
| paper_bet | 0.02 | `sniper_bankroll * 0.02` | $0.20 | $0.50 |
| safe_bet_fraction | 0.25 | unchanged (already a ratio) | 25% | 25% |

### Market Maker (if enabled)
| Value | Ratio | Formula | At $20 |
|---|---|---|---|
| capital | 0.50 | `current_bankroll * 0.50` | $10.00 |
| max_capital_per_market | 0.25 | `current_bankroll * 0.25` | $5.00 |

All modules read `current_bankroll` from the same journal-derived calculation. No separate bankroll constants per module.

## 4. File Changes

No new files or classes. Changes to existing files:

### `auto_trader.py`
- Add `INITIAL_BANKROLL` constant (20.0 live, 2000.0 paper)
- Remove hardcoded `MAX_TRADE_SIZE`, `MIN_TRADE_SIZE`, `MAX_TOTAL_EXPOSURE`, `TOTAL_BANKROLL`
- Add `_get_current_bankroll()` method: reads `INITIAL_BANKROLL + journal.get_cumulative_pnl()`
- Derive all dollar limits from `current_bankroll` at the start of each `_scan_and_trade()` cycle

### `trade_journal.py`
- Constructor takes `mode` param, selects `trade_journal_paper.json` or `trade_journal_live.json`
- Add `get_cumulative_pnl()` method returning sum of closed trade PnL
- Existing data migrated: rename `trade_journal.json` → `trade_journal_paper.json`

### `news_scanner.py`
- Replace 3 hardcoded constants (`MAX_TRADE_SIZE`, `MIN_TRADE_SIZE`, `MAX_TOTAL_EXPOSURE`) with bankroll-derived values
- Read `current_bankroll` from the shared bankroll source

### `btc_sniper.py`
- Replace `DEFAULT_BANKROLL` and `MIN_BET` with bankroll-derived values
- Replace hardcoded `$10` paper bet (line 459) with `sniper_bankroll * 0.02`
- Add bankroll threshold check: disable sniper when `current_bankroll < 40.0`

### `market_maker.py`
- Replace `MAX_CAPITAL_PER_MARKET = 500.0` with `current_bankroll * 0.25`

### `server.py`
- Pass mode (`"paper"` or `"live"`) to journal and auto trader constructors
- Remove hardcoded env var defaults for `SNIPER_BANKROLL` and `MM_CAPITAL`
- Skip sniper initialization when bankroll < 40.0

### `wallet_config.py`
- `PAPER_STARTING_BALANCE` (default 10000) is **intentionally unchanged**. This is the simulated wallet balance for paper executors, separate from `INITIAL_BANKROLL` which is the trading budget. The paper executor deducts from its own balance independently — the bankroll system tracks cumulative P&L, not wallet state.

### No changes needed
- `paper_executor.py` — fees are already percentages
- `exit_engine.py` — all percentage-based thresholds
- `bracket_manager.py` — percentage-based exit rules
- `position_manager.py` — no dollar-denominated constants
- `insider_tracker.py` — no dollar-denominated constants

## 5. Percentage-Based Rules (Unchanged)

These are already ratios and need no modification:
- Kelly fractions (0.50, 0.25, 0.20 by strategy)
- Kelly edge estimates (3%-12% by strategy)
- Exit rules (target_pct, stop_pct, trailing_stop)
- Fee structures (0% maker, 2% taker on Polymarket)
- Signal decay tiers (1.0x → 0.1x)
- Category concentration cap (30%)
- Regime size reduction (50%)
- Portfolio exposure cap (40%)
- Favorite-longshot multipliers (0.1x → 3.0x)
