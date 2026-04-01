# Narrow-Range Filter & Category Scoring Fix

**Date:** 2026-03-29
**Status:** Implemented
**Approach:** B + audit-driven additions (15% upside gate, arb exemption, synth disable)

## Problem

The auto trader is losing money on narrow-range bets and miscategorized markets. Journal analysis (40 trades, -$90.16 total P&L) shows:

- **Narrow-range bets:** 8 trades, -$74.91 — the single largest loss category
- **Tweet/post count bets:** 3 closed, -$30.18 — subset of narrow-range
- **eSports (LoL):** 2 open positions, not filtered by SPORTS_KEYWORDS
- **P&L classification bug:** -$0.00 classified as "loss" instead of "flat"
- **100% wipeouts:** 2 trades (-$49.43) from single-leg NO bets on narrow events

## Changes

### 1. Narrow-Range Market Filter (auto_trader.py)

Add a regex-based filter that detects questions asking about specific numeric ranges. Hard-skip these markets — they are lottery tickets with asymmetric downside.

**Detection patterns:**
- `"between X and Y"` — e.g., "between $1,900 and $2,000"
- `"X-Y tweets/posts/times"` — e.g., "300-319 tweets"
- `"X to Y inches/mm/cm"` — e.g., "5 to 6 inches"
- `"X and Y strikes/attacks"` — e.g., "14 and 17 US strikes"

**Regex:**
```python
import re

NARROW_RANGE_PATTERN = re.compile(
    r"(?:"
    r"between\s+\$?[\d,.]+\s+and\s+\$?[\d,.]+"  # "between $X and $Y"
    r"|"
    r"\d+[\s-]+(?:to|and)[\s-]+\d+\s+(?:tweets?|posts?|times?|strikes?|inches|mm|cm)"
    r"|"
    r"\d+-\d+\s+(?:tweets?|posts?|times?|strikes?)"  # "300-319 tweets"
    r")",
    re.IGNORECASE,
)
```

**Placement:** After the existing `is_sports_exact_score` / `is_commodities` hard-skip block (line ~674). Same pattern — `self._record_skip("narrow_range_market")` + `continue`.

**Exception:** Multi-outcome arb and portfolio_no strategies are exempt. These bet on market structure, not predicting the range outcome.

### 2. eSports Keywords (auto_trader.py)

Add eSports game titles to SPORTS_KEYWORDS so they get the 0.3x score penalty:

```python
# eSports — no documented edge, treat as sports
"lol", "league of legends", "dota", "cs:go", "csgo", "counter-strike",
"valorant", "overwatch", "esports", "e-sports", "bo1", "bo3", "bo5",
```

These map to `is_sports = True` → 0.3x multiplier, same as other sports.

### 3. Single-Leg NO Bet Wipeout Protection (auto_trader.py)

When the auto trader enters a single-leg NO bet (buying NO shares), cap the position cost based on downside risk. A NO bet on a narrow-range event risks 100% loss.

**Rule:** For pure_prediction strategy, single-leg NO bets, if the NO price >= $0.85 (i.e., market says 85%+ chance the event does NOT happen), cap position size at `min(kelly_size, bankroll * 0.5%)`. This limits wipeout damage to ~$5 on a $1000 bankroll.

**Placement:** In the `_kelly_size` method or at trade execution, after size is calculated.

**Rationale:** The Solana $90 bet was a NO at $0.935 (93.5% implied probability of NO). The system sized it at $18.25 because Kelly saw it as a near-certain winner. But the 6.5% chance of YES meant 100% wipeout when it happened. Similarly, the Musk tweet bet was $31.18. These are the classic favorite-longshot trap in reverse.

### 4. Outcome Classification Bug Fix (trade_journal.py)

**File:** `trade_journal.py`, line 136

**Current:**
```python
"outcome": "win" if pnl > 0 else ("loss" if pnl < 0 else "flat"),
```

**Problem:** IEEE 754 negative zero (`-0.0`) satisfies `pnl < 0` in Python (`-0.0 < 0` is `False` actually — but `round(-0.0001, 4)` can produce `-0.0` which then compares as `== 0`, so the real issue is entries with `pnl = -0.0` from floating-point rounding that get classified as "loss" because the raw computation was slightly negative before rounding). The actual check needs a tolerance band.

**Fix:**
```python
"outcome": "win" if pnl > 0.001 else ("loss" if pnl < -0.001 else "flat"),
```

Trades within $0.001 of zero are "flat". This matches the 4-decimal rounding used for pnl.

### 5. Retroactive Journal Cleanup (one-time)

Reclassify the 3 Stranger Things trades that have wrong outcomes:
- journal_c318e7c3: outcome "win" → "flat" (pnl = $0.00)
- journal_f92c3c81: outcome "loss" → "flat" (pnl = -$0.00)
- journal_fcdd7bf6: outcome "loss" → "flat" (pnl = -$0.00)

This corrects the win/loss counts: actual record is 14W / 19L / 7F (not 14W / 21L / 5F).

## Files Changed

| File | Change |
|------|--------|
| `positions/auto_trader.py` | Add NARROW_RANGE_PATTERN, eSports keywords, NO-bet cap |
| `positions/trade_journal.py` | Fix outcome classification tolerance |
| `data/positions/trade_journal_paper.json` | Fix 3 misclassified entries |

## What This Does NOT Change

- Exit engine logic (no changes)
- Position manager (no changes)
- Kelly sizing formula (no changes beyond the NO-bet cap)
- Strategy scoring multipliers for existing categories
- Any guaranteed-profit strategy paths (multi_outcome_arb, portfolio_no)

## Expected Impact

Based on journal replay:
- **Narrow-range filter** would have prevented: -$74.91 in losses (8 trades blocked)
- **eSports filter** would have penalized 2 current positions by 0.3x (may not have entered)
- **NO-bet cap** would have limited Solana loss to ~$5 (vs $18.25) and Musk loss to ~$5 (vs $31.18), saving ~$39
- **Classification fix** corrects win rate from 35.0% to 42.4% (14W/33 non-flat)
- **Net P&L impact if applied retroactively:** Losses reduced by ~$75-114
