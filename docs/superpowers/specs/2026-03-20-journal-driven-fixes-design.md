# Journal-Driven Auto-Trader Fixes — Design Spec

## Goal

Fix three bugs identified from trade journal analysis: duplicate journal entries from missing idempotency guards, phantom PredictIt arbitrage opportunities that can't be executed, and multi-close exit paths that fire on already-closed packages. All changes are to existing modules — no new files.

## Problem Summary

| # | Bug | Root Cause | Impact |
|---|-----|-----------|--------|
| 1 | Journal records 33 entries from 23 packages | `record_close()` has no idempotency guard; multiple exit paths call it on the same package | All per-trade analytics are wrong. Loss counts inflated 1.4x. |
| 2 | 4,831 phantom PredictIt arb opportunities (25% of total) | `find_arbitrage()` and `_arb_to_opportunity()` don't filter by tradeable platform | Wastes scoring cycles. 2 failed trades ("No executor for platform: predictit"). |
| 3 | Exit engine fires triggers on already-journaled packages | `_resolve_bracket_fills()` iterates multiple fills per package; each fill re-checks "all legs closed" and re-calls `record_close()` | Creates 2-5 duplicate journal entries per multi-fill close. |

## Fix 1: Journal Idempotency Guard

### Root Cause Detail

`trade_journal.py:record_close()` (line 45) unconditionally appends a new entry with a fresh UUID. It is called from 4 locations:

1. `position_manager.close_package()` (line 140) — manual close
2. `position_manager._exit_leg_locked()` (line 435) — when last leg closes
3. `position_manager._finalize_exit()` (line 590) — limit order fills
4. `exit_engine._resolve_bracket_fills()` (line 423) — bracket order fills

When multiple bracket fills arrive in the same tick, the for-fill loop at exit_engine.py line 387 processes each fill, marks the leg closed, checks if all legs are now closed, and calls `record_close()` each time the check passes. After fill #1 closes the last open leg, fills #2-#5 each see "all legs closed = True" and create duplicate entries.

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

The auto-trader's `_arb_to_opportunity()` (line 1175) converts these into tradeable opportunities without checking whether executors exist for both platforms. When the auto-trader tries to execute, `position_manager.execute_package()` fails with "No executor for platform: predictit" because the PredictIt executor only initializes when `PREDICTIT_SESSION` env var is set.

### Fix

Filter in `_arb_to_opportunity()` using the executor registry — don't hardcode platform names:

**In `auto_trader.py` `_arb_to_opportunity()`**, after determining `buy_yes_platform` and `buy_no_platform` (around line 1190):
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

## Fix 3: Multi-Close Guard in Exit Engine

### Root Cause Detail

`_resolve_bracket_fills()` at exit_engine.py line 379 processes bracket order fills. When multiple legs fill in the same tick, the inner loop (line 387: `for fill in fills`) marks each leg closed and checks if all legs are now closed. After the first fill closes the last open leg, every subsequent fill re-enters the "all legs closed" branch and calls `record_close()` again.

Additionally, `_resolve_bracket_fills()` runs at line 464 of `_tick()`, BEFORE the main trigger evaluation loop at line 473. If bracket fills close a package at line 464, the package status is set to "closed" and `save()` is called. The subsequent `list_packages("open")` at line 469 should then exclude it. This path is safe.

The unsafe path is within `_resolve_bracket_fills` itself: the for-fill loop at line 387 processes all fills for one package sequentially, and the "all legs closed" check at line 415 doesn't short-circuit after the first successful close.

### Fix

**In `exit_engine.py` `_resolve_bracket_fills()`**, after the first successful close, break out of the fill loop or skip subsequent close attempts:

```python
# After line 415-425 block
if pkg.get("status") == "closed":
    break  # All legs closed, no need to process more fills
```

This is in addition to the `_journal_recorded` flag from Fix 1, which provides defense-in-depth.

## Testing

**Fix 1 tests** (`tests/test_trade_journal.py`):
- Call `record_close()` twice with the same package → only one entry created
- Call `record_close()` with different packages → both entries created
- Verify `_journal_recorded` flag is set on package after first call

**Fix 2 tests** (`tests/test_auto_trader_improvements.py`):
- Mock `pm.executors` without predictit → PredictIt arb returns None from `_arb_to_opportunity`
- Mock `pm.executors` with predictit → PredictIt arb proceeds normally
- Non-PredictIt arb unaffected regardless of executor config

**Fix 3 tests** (`tests/test_exit_engine.py` or new test file):
- Simulate multiple bracket fills for same package in one tick → `record_close` called exactly once
- Verify break after package closes prevents processing remaining fills

## What This Does NOT Include

- No retroactive cleanup of existing duplicate journal entries
- No changes to the exit rule assignment logic (trailing_stop on high-prob was fixed in a prior commit)
- No changes to trigger suppression or AI review prompts
- No changes to cooldown logic (already working correctly)
- No new modules or API endpoints
