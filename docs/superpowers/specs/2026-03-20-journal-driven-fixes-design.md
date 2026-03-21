# Journal-Driven Auto-Trader Fixes — Design Spec

## Goal

Fix two bugs identified from trade journal analysis: duplicate journal entries from missing idempotency guards, and phantom PredictIt arbitrage opportunities that can't be executed. All changes are to existing modules — no new files.

## Problem Summary

| # | Bug | Root Cause | Impact |
|---|-----|-----------|--------|
| 1 | Journal records 33 entries from 23 packages | `record_close()` has no idempotency guard; multiple exit paths call it on the same package | All per-trade analytics are wrong. Loss counts inflated 1.4x. |
| 2 | 4,831 phantom PredictIt arb opportunities (25% of total) | `find_arbitrage()` and `_arb_to_opportunity()` don't filter by tradeable platform | Wastes scoring cycles. 2 failed trades ("No executor for platform: predictit"). |

## Fix 1: Journal Idempotency Guard

### Root Cause Detail

`trade_journal.py:record_close()` (line 45) unconditionally appends a new entry with a fresh UUID. It is called from 4 locations:

1. `position_manager.close_package()` (line 140) — manual close
2. `position_manager._exit_leg_locked()` (line 435) — when last leg closes
3. `position_manager._finalize_exit()` (line 590) — limit order fills
4. `exit_engine._resolve_bracket_fills()` (line 423) — bracket order fills

The duplicate journal entries arise from **cross-site races**: when a package closes, multiple call sites may detect the close independently and each call `record_close()`. For example, bracket fills at exit_engine.py line 423 close the package, and then `_exit_leg_locked()` at position_manager.py line 435 also detects "all legs closed" and calls `record_close()` again. Note: the "all legs closed" check in `_resolve_bracket_fills()` at line 414 is **outside** the fill loop (line 387) — it runs once per package per tick, not per fill. The duplicates come from different call sites, not from within the fill loop.

### Fix

Add a `_journal_recorded` flag on the package dict. Check before every `record_close()` call:

**In `position_manager.py`**, before each `record_close()` call (lines 140, 435, 590):
```python
if not pkg.get("_journal_recorded") and self.trade_journal:
    self.trade_journal.record_close(pkg, exit_trigger=exit_trigger)
    pkg["_journal_recorded"] = True
```

**In `exit_engine.py` `_resolve_bracket_fills()`** (line 423):
```python
if not pkg.get("_journal_recorded") and self.pm.trade_journal:
    self.pm.trade_journal.record_close(pkg, exit_trigger=trigger)
    pkg["_journal_recorded"] = True
```

**Belt-and-suspenders in `trade_journal.py:record_close()`** (line 45):
```python
pkg_id = pkg.get("id")
if any(e.get("package_id") == pkg_id for e in self.entries):
    logger.debug("Package %s already journaled, skipping duplicate", pkg_id)
    return None
```

The flag on the package is the primary guard (cheap O(1) check, persists with the package). The journal-level check is a safety net for edge cases where the flag might not have been set (e.g., packages from before this fix was deployed).

### Behavioral Note

Existing duplicate entries in `trade_journal.json` are NOT cleaned up by this fix. A one-time dedup script could be run separately if needed, but the journal is append-only in production and retroactive cleanup is not required.

## Fix 2: PredictIt No-Trade Filter

### Root Cause Detail

The arb scanner matches events across all platforms including PredictIt. `find_arbitrage()` in `arbitrage_engine.py` computes spreads for PredictIt pairs — 4,831 opportunities with 30%+ spreads and volume=0. These are almost certainly stale prices or resolution mismatches.

The auto-trader's `_arb_to_opportunity()` (line 1250) converts these into tradeable opportunities without checking whether executors exist for both platforms. When the auto-trader tries to execute, `position_manager.execute_package()` fails with "No executor for platform: predictit" because the PredictIt executor only initializes when `PREDICTIT_SESSION` env var is set.

Note: `_events_to_opportunities()` (line 1349) already has a `tradeable_platforms` filter using the same executor registry pattern. Fix 2 applies the same proven pattern to the arb conversion path, which was missing it.

### Fix

Filter in `_arb_to_opportunity()` using the executor registry — don't hardcode platform names:

**In `auto_trader.py` `_arb_to_opportunity()`**, after determining `buy_yes_platform` and `buy_no_platform` (around line 1264):
```python
# Skip opportunities on platforms we can't trade on
tradeable = set(self.pm.executors.keys()) if hasattr(self.pm, 'executors') else set()
if tradeable:
    if buy_yes_platform not in tradeable or buy_no_platform not in tradeable:
        logger.debug("Skipping arb on non-tradeable platform: %s/%s",
                      buy_yes_platform, buy_no_platform)
        return None
```

This is superior to a hardcoded `_NO_TRADE_PLATFORMS` set because:
- It automatically adapts when executors are added/removed
- It catches any platform without an executor, not just PredictIt
- When `PREDICTIT_SESSION` IS configured, PredictIt arbs start working without code changes

### Behavioral Note

This removes ~4,831 opportunities per scan from the scoring pipeline. The remaining ~14,000 opportunities are all on tradeable platforms (Polymarket, Kalshi, Limitless). Scan time should decrease as fewer opportunities are scored.

## Why Fix 3 (Multi-Close Guard) Was Dropped

Initial analysis hypothesized that the fill loop in `_resolve_bracket_fills()` re-entered the "all legs closed" branch per fill. Code review revealed the all-legs-closed check (line 414) is a **sibling** of the fill loop (line 387), not nested inside it — it runs once per package per tick. The duplicates come from cross-site races (multiple call sites detecting the same close), which Fix 1's `_journal_recorded` flag already handles completely.

## Testing

**Fix 1 tests** (`tests/test_trade_journal.py`):
- Call `record_close()` twice with the same package → only one entry created
- Call `record_close()` with different packages → both entries created
- Verify `_journal_recorded` flag is set on package after first call
- Cross-site test: simulate `record_close()` from two different call sites (e.g., exit_engine + position_manager) → only one entry

**Fix 2 tests** (`tests/test_auto_trader_improvements.py`):
- Mock `pm.executors` without predictit → PredictIt arb returns None from `_arb_to_opportunity`
- Mock `pm.executors` with predictit → PredictIt arb proceeds normally
- Non-PredictIt arb unaffected regardless of executor config

## What This Does NOT Include

- No retroactive cleanup of existing duplicate journal entries
- No changes to the exit rule assignment logic (trailing_stop on high-prob was fixed in a prior commit)
- No changes to trigger suppression or AI review prompts
- No changes to cooldown logic (already working correctly)
- No new modules or API endpoints
