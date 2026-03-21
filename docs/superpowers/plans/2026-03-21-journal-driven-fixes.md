# Journal-Driven Auto-Trader Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix duplicate journal entries (idempotency guard) and phantom PredictIt arb opportunities (executor registry filter).

**Architecture:** Two independent fixes to existing modules. Fix 1 adds a `_journal_recorded` flag on package dicts checked at all 4 `record_close()` call sites, plus a belt-and-suspenders package_id dedup in `record_close()` itself. Fix 2 adds an executor registry check in `_arb_to_opportunity()` to skip arbs on platforms without executors, matching the existing pattern in `_events_to_opportunities()`.

**Tech Stack:** Python 3.11+, pytest, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-03-20-journal-driven-fixes-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/positions/trade_journal.py` | Modify (line 45) | Belt-and-suspenders dedup in `record_close()` |
| `src/positions/position_manager.py` | Modify (lines 138-142, 433-437, 588-592) | `_journal_recorded` flag at 3 call sites |
| `src/positions/exit_engine.py` | Modify (lines 421-425) | `_journal_recorded` flag at 1 call site |
| `src/positions/auto_trader.py` | Modify (after line 1263) | Executor registry filter in `_arb_to_opportunity()` |
| `tests/test_trade_journal.py` | Modify | Add idempotency tests |
| `tests/test_auto_trader_improvements.py` | Modify | Add PredictIt filter tests |

---

### Task 1: Journal Idempotency — Belt-and-Suspenders Guard in `record_close()`

**Files:**
- Modify: `src/positions/trade_journal.py:45-46`
- Test: `tests/test_trade_journal.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_trade_journal.py` at the end of class `TestJournal`:

```python
def test_record_close_idempotency_same_package(self):
    """Calling record_close twice with the same package creates only one entry."""
    with tempfile.TemporaryDirectory() as tmp:
        journal = TradeJournal(Path(tmp))

        pkg = _make_closed_package("Duplicate Test", "cross_platform_arb", 100.0, 120.0)
        entry1 = journal.record_close(pkg, exit_trigger="target_hit")
        entry2 = journal.record_close(pkg, exit_trigger="target_hit")

        assert entry1 is not None
        assert entry2 is None  # Second call should be rejected
        assert len(journal.entries) == 1

def test_record_close_different_packages(self):
    """Different packages should both be recorded."""
    with tempfile.TemporaryDirectory() as tmp:
        journal = TradeJournal(Path(tmp))

        pkg1 = _make_closed_package("Trade A", "cross_platform_arb", 100.0, 120.0)
        pkg2 = _make_closed_package("Trade B", "pure_prediction", 80.0, 90.0)

        entry1 = journal.record_close(pkg1, exit_trigger="target_hit")
        entry2 = journal.record_close(pkg2, exit_trigger="stop_loss")

        assert entry1 is not None
        assert entry2 is not None
        assert len(journal.entries) == 2
        assert journal.entries[0]["package_id"] == "pkg_trade_a"
        assert journal.entries[1]["package_id"] == "pkg_trade_b"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src && python -m pytest tests/test_trade_journal.py::TestJournal::test_record_close_idempotency_same_package tests/test_trade_journal.py::TestJournal::test_record_close_different_packages -v`
Expected: `test_record_close_idempotency_same_package` FAILS (entry2 is not None, len is 2). `test_record_close_different_packages` PASSES.

- [ ] **Step 3: Implement the dedup guard in `record_close()`**

In `src/positions/trade_journal.py`, add a package_id check at the top of `record_close()`, immediately after line 46 (`"""Record a completed trade..."""`):

```python
def record_close(self, pkg: dict, exit_trigger: str = "manual"):
    """Record a completed trade (package close) with full details including fees."""
    # Belt-and-suspenders idempotency: reject if this package was already journaled
    pkg_id = pkg.get("id")
    if any(e.get("package_id") == pkg_id for e in self.entries):
        logger.debug("Package %s already journaled, skipping duplicate", pkg_id)
        return None

    legs_detail = []
```

Insert the 4 new lines (comment + pkg_id + if/return) between line 46 (the docstring) and line 47 (`legs_detail = []`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src && python -m pytest tests/test_trade_journal.py -v`
Expected: ALL tests pass, including the two new ones.

- [ ] **Step 5: Commit**

```bash
git add src/positions/trade_journal.py tests/test_trade_journal.py
git commit -m "fix: add idempotency guard to trade_journal.record_close()

Reject duplicate record_close() calls for the same package_id.
Belt-and-suspenders safety net — primary guard is the _journal_recorded
flag added in the next commit."
```

---

### Task 2: Journal Idempotency — `_journal_recorded` Flag at All Call Sites

**Files:**
- Modify: `src/positions/position_manager.py:138-142, 433-437, 588-592`
- Modify: `src/positions/exit_engine.py:421-425`
- Test: `tests/test_trade_journal.py`

- [ ] **Step 1: Write the cross-site idempotency test**

Add to `tests/test_trade_journal.py` at the end of class `TestJournal`:

```python
def test_cross_site_idempotency(self):
    """Simulate two call sites racing to journal the same package.

    First caller journals and sets _journal_recorded flag.
    Second caller checks the flag and skips.
    Belt-and-suspenders in record_close() also blocks if flag was missed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        journal = TradeJournal(Path(tmp))

        pkg = _make_closed_package("Cross Site", "cross_platform_arb", 100.0, 110.0)

        # Simulate call site 1 (e.g. position_manager._exit_leg_locked):
        # checks flag, journals, sets flag
        assert not pkg.get("_journal_recorded")
        entry1 = journal.record_close(pkg, exit_trigger="trailing_stop")
        pkg["_journal_recorded"] = True
        assert entry1 is not None

        # Simulate call site 2 (e.g. exit_engine._resolve_bracket_fills):
        # checks flag — flag is set, so it skips the call entirely
        assert pkg.get("_journal_recorded") is True

        # Even if call site 2 ignores the flag, belt-and-suspenders catches it
        entry2 = journal.record_close(pkg, exit_trigger="bracket_tp")
        assert entry2 is None
        assert len(journal.entries) == 1
```

Note: The `_journal_recorded` flag is set by the **callers** (position_manager, exit_engine), not by `record_close()` itself. The flag prevents callers from even reaching `record_close()`. The belt-and-suspenders check inside `record_close()` (from Task 1) is the safety net.

- [ ] **Step 2: Run test to verify it passes**

Run: `cd src && python -m pytest tests/test_trade_journal.py::TestJournal::test_cross_site_idempotency -v`
Expected: PASS (the belt-and-suspenders guard from Task 1 blocks the second call; the flag check is caller-side logic verified by assertion).

- [ ] **Step 3: Add `_journal_recorded` guard at position_manager.py call site 1 (line 138-142)**

In `src/positions/position_manager.py`, replace lines 138-142:

**Before:**
```python
            if self.trade_journal:
                try:
                    self.trade_journal.record_close(pkg, exit_trigger=exit_trigger)
                except Exception as e:
                    logger.warning("Failed to record trade journal: %s", e)
```

**After:**
```python
            if not pkg.get("_journal_recorded") and self.trade_journal:
                try:
                    self.trade_journal.record_close(pkg, exit_trigger=exit_trigger)
                    pkg["_journal_recorded"] = True
                except Exception as e:
                    logger.warning("Failed to record trade journal: %s", e)
```

- [ ] **Step 4: Add `_journal_recorded` guard at position_manager.py call site 2 (line 433-437)**

In `src/positions/position_manager.py`, replace lines 433-437:

**Before:**
```python
                if self.trade_journal:
                    try:
                        self.trade_journal.record_close(pkg, exit_trigger=trigger)
                    except Exception as e:
                        logger.warning("Failed to record trade journal: %s", e)
```

**After:**
```python
                if not pkg.get("_journal_recorded") and self.trade_journal:
                    try:
                        self.trade_journal.record_close(pkg, exit_trigger=trigger)
                        pkg["_journal_recorded"] = True
                    except Exception as e:
                        logger.warning("Failed to record trade journal: %s", e)
```

- [ ] **Step 5: Add `_journal_recorded` guard at position_manager.py call site 3 (line 588-592)**

In `src/positions/position_manager.py`, replace lines 588-592:

**Before:**
```python
            if self.trade_journal:
                try:
                    self.trade_journal.record_close(pkg, exit_trigger=trigger)
                except Exception as e:
                    logger.warning("Failed to record trade journal: %s", e)
```

**After:**
```python
            if not pkg.get("_journal_recorded") and self.trade_journal:
                try:
                    self.trade_journal.record_close(pkg, exit_trigger=trigger)
                    pkg["_journal_recorded"] = True
                except Exception as e:
                    logger.warning("Failed to record trade journal: %s", e)
```

- [ ] **Step 6: Add `_journal_recorded` guard at exit_engine.py call site (line 421-425)**

In `src/positions/exit_engine.py`, replace lines 421-425:

**Before:**
```python
                if self.pm.trade_journal:
                    try:
                        self.pm.trade_journal.record_close(pkg, exit_trigger=trigger)
                    except Exception as e:
                        logger.warning("Failed to record bracket exit: %s", e)
```

**After:**
```python
                if not pkg.get("_journal_recorded") and self.pm.trade_journal:
                    try:
                        self.pm.trade_journal.record_close(pkg, exit_trigger=trigger)
                        pkg["_journal_recorded"] = True
                    except Exception as e:
                        logger.warning("Failed to record bracket exit: %s", e)
```

- [ ] **Step 7: Run all existing tests to verify nothing broke**

Run: `cd src && python -m pytest tests/ -v`
Expected: ALL tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/positions/position_manager.py src/positions/exit_engine.py tests/test_trade_journal.py
git commit -m "fix: add _journal_recorded flag guard at all 4 record_close() call sites

Primary idempotency guard: cheap O(1) flag check on the package dict
prevents any call site from recording a close that was already journaled
by another call site (cross-site race prevention)."
```

---

### Task 3: PredictIt No-Trade Filter in `_arb_to_opportunity()`

**Files:**
- Modify: `src/positions/auto_trader.py:1262-1263`
- Test: `tests/test_auto_trader_improvements.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_auto_trader_improvements.py` at the end of the file:

```python
class TestTradeablePlatformFilter:
    """Fix 2: _arb_to_opportunity should skip arbs on non-tradeable platforms."""

    def _make_arb(self, yes_platform="polymarket", no_platform="kalshi"):
        """Helper to build a minimal arb dict."""
        return {
            "matched_event": {
                "canonical_title": "Will BTC exceed $100k?",
                "category": "crypto",
                "expiry": "2026-12-31",
                "markets": [
                    {"platform": yes_platform, "event_id": "evt_yes",
                     "yes_price": 0.40, "no_price": 0.55, "volume": 50000},
                    {"platform": no_platform, "event_id": "evt_no",
                     "yes_price": 0.50, "no_price": 0.45, "volume": 40000},
                ],
            },
            "buy_yes_platform": yes_platform,
            "buy_no_platform": no_platform,
            "buy_yes_price": 0.40,
            "buy_no_price": 0.45,
            "spread": 0.15,
            "profit_pct": 15.0,
            "net_profit_pct": 13.0,
            "combined_volume": 90000,
            "confidence": "high",
        }

    def test_predictit_arb_filtered_when_no_executor(self):
        """PredictIt arb should return None when PredictIt executor is absent."""
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        pm.list_packages = MagicMock(return_value=[])
        pm.executors = {"polymarket": MagicMock(), "kalshi": MagicMock()}
        trader = AutoTrader(pm)

        arb = self._make_arb(yes_platform="predictit", no_platform="polymarket")
        result = trader._arb_to_opportunity(arb)
        assert result is None

    def test_predictit_arb_passes_when_executor_present(self, caplog):
        """PredictIt arb should NOT be filtered when PredictIt executor IS configured."""
        import logging
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        pm.list_packages = MagicMock(return_value=[])
        pm.executors = {
            "polymarket": MagicMock(),
            "kalshi": MagicMock(),
            "predictit": MagicMock(),
        }
        trader = AutoTrader(pm)

        arb = self._make_arb(yes_platform="predictit", no_platform="polymarket")
        with caplog.at_level(logging.DEBUG, logger="positions.auto_trader"):
            trader._arb_to_opportunity(arb)
        # The tradeable filter should NOT have triggered
        assert "Skipping arb on non-tradeable platform" not in caplog.text

    def test_polymarket_kalshi_arb_unaffected(self, caplog):
        """Standard Polymarket/Kalshi arb should not trigger the tradeable filter."""
        import logging
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        pm.list_packages = MagicMock(return_value=[])
        pm.executors = {"polymarket": MagicMock(), "kalshi": MagicMock()}
        trader = AutoTrader(pm)

        arb = self._make_arb(yes_platform="polymarket", no_platform="kalshi")
        with caplog.at_level(logging.DEBUG, logger="positions.auto_trader"):
            trader._arb_to_opportunity(arb)
        # The tradeable filter should NOT have triggered
        assert "Skipping arb on non-tradeable platform" not in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src && python -m pytest tests/test_auto_trader_improvements.py::TestTradeablePlatformFilter::test_predictit_arb_filtered_when_no_executor -v`
Expected: FAIL (result is not None — the arb is not filtered).

- [ ] **Step 3: Implement the executor registry filter**

In `src/positions/auto_trader.py`, add the filter after line 1263 (after `buy_no_platform = arb.get("buy_no_platform", "")`), before the same-platform check at line 1265:

```python
        buy_yes_platform = arb.get("buy_yes_platform", "")
        buy_no_platform = arb.get("buy_no_platform", "")

        # Skip opportunities on platforms we can't trade on
        tradeable = set(self.pm.executors.keys()) if hasattr(self.pm, 'executors') else set()
        if tradeable:
            if buy_yes_platform not in tradeable or buy_no_platform not in tradeable:
                logger.debug("Skipping arb on non-tradeable platform: %s/%s",
                              buy_yes_platform, buy_no_platform)
                return None

        # CRITICAL FIX: reject same-platform "arb" ...
```

Insert the 6 new lines (blank line + comment + 4 code lines) between line 1263 and line 1265.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src && python -m pytest tests/test_auto_trader_improvements.py::TestTradeablePlatformFilter -v`
Expected: ALL 3 tests pass.

- [ ] **Step 5: Run full test suite to verify nothing broke**

Run: `cd src && python -m pytest tests/ -v`
Expected: ALL tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/positions/auto_trader.py tests/test_auto_trader_improvements.py
git commit -m "fix: filter arb opportunities on non-tradeable platforms

Skip arbs in _arb_to_opportunity() when either platform lacks an
executor. Uses same executor registry pattern as _events_to_opportunities().
Eliminates ~4,831 phantom PredictIt opportunities per scan."
```
