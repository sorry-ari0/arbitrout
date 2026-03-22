# Fee Elimination & Exit Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate fee drag ($57.40 on $25.02 net loss) by removing premature mechanical exits, switching safety overrides to 0% maker limit orders, and adding per-category Polymarket fee curves.

**Architecture:** 6 targeted changes across 4 files. No new files. TDD with existing test patterns (`tests/test_exit_engine.py`, `tests/test_auto_trader_improvements.py`). All tests run from repo root via `python -m pytest tests/ -v`.

**Tech Stack:** Python 3.14, pytest, asyncio, existing position management system.

**Spec:** `docs/superpowers/specs/2026-03-22-fee-elimination-exit-strategy-design.md`

---

## File Map

| File | Role | Changes |
|------|------|---------|
| `src/execution/paper_executor.py` | Fee simulation | Add `get_taker_fee_rate()`, update `sell()` to accept `category` |
| `src/positions/exit_engine.py` | Exit trigger routing | Remove stop_loss/trailing_stop from mechanical execution, safety overrides use limit |
| `src/positions/auto_trader.py` | Package creation | Add `_hold_to_resolution` to multi_outcome_arb, store `_category` on all packages |
| `src/positions/position_manager.py` | Exit execution | Configurable timeout for pending orders, pass category to executor |
| `tests/test_fee_model.py` | New test file | Tests for `get_taker_fee_rate()` |
| `tests/test_exit_engine.py` | Existing tests | Add tests for mechanical execution change and safety limit orders |
| `tests/test_auto_trader_improvements.py` | Existing tests | Add tests for `_hold_to_resolution` and `_category` |

---

### Task 1: Per-Category Fee Model

**Files:**
- Create: `tests/test_fee_model.py`
- Modify: `src/execution/paper_executor.py:1-36`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fee_model.py`:

```python
"""Tests for per-category Polymarket fee model."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from execution.paper_executor import get_taker_fee_rate


class TestGetTakerFeeRate:
    """Test the Polymarket fee curve: rate = feeRate * (price * (1-price))^exponent"""

    def test_politics_zero_at_any_price(self):
        """Politics/entertainment markets have 0% taker fee on Polymarket."""
        assert get_taker_fee_rate("politics", 0.50) == 0.0
        assert get_taker_fee_rate("politics", 0.10) == 0.0
        assert get_taker_fee_rate("other", 0.50) == 0.0

    def test_crypto_peak_at_half(self):
        """Crypto fee peaks at p=0.50: 0.25 * (0.25)^2 = 0.015625."""
        rate = get_taker_fee_rate("crypto", 0.50)
        assert abs(rate - 0.015625) < 1e-6

    def test_crypto_low_at_extreme(self):
        """Crypto fee near-zero at p=0.10: 0.25 * (0.09)^2 ≈ 0.002025."""
        rate = get_taker_fee_rate("crypto", 0.10)
        assert abs(rate - 0.002025) < 1e-6

    def test_sports_peak_at_half(self):
        """Sports fee at p=0.50: 0.0175 * (0.25)^1 = 0.004375."""
        rate = get_taker_fee_rate("sports", 0.50)
        assert abs(rate - 0.004375) < 1e-6

    def test_sports_low_at_extreme(self):
        """Sports fee at p=0.10: 0.0175 * 0.09 = 0.001575."""
        rate = get_taker_fee_rate("sports", 0.10)
        assert abs(rate - 0.001575) < 1e-6

    def test_boundary_prices_zero(self):
        """Fee is 0 at price=0 and price=1."""
        assert get_taker_fee_rate("crypto", 0.0) == 0.0
        assert get_taker_fee_rate("crypto", 1.0) == 0.0

    def test_finance_uses_default(self):
        """Unknown Polymarket categories default to 0%."""
        assert get_taker_fee_rate("finance", 0.50) == 0.0
        assert get_taker_fee_rate("weather", 0.50) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fee_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'get_taker_fee_rate'`

- [ ] **Step 3: Implement `get_taker_fee_rate` in paper_executor.py**

Add after line 35 (`DEFAULT_FEE_RATE = 0.0`) in `src/execution/paper_executor.py`:

```python
# ── Per-category Polymarket fee curve ─────────────────────────────────────────
# Rate = feeRate * (price * (1 - price))^exponent
# Crypto: feeRate=0.25, exponent=2 → peak 1.5625% at p=0.50
# Sports: feeRate=0.0175, exponent=1 → peak 0.4375% at p=0.50
# Politics/entertainment/other: 0% on Polymarket
_POLY_FEE_PARAMS = {
    "crypto": (0.25, 2),
    "sports": (0.0175, 1),
}


def get_taker_fee_rate(category: str, price: float) -> float:
    """Return the dimensionless taker fee rate for a Polymarket category at a given price.

    Non-Polymarket platforms (Kalshi, Coinbase, etc.) use their own flat rates
    in TAKER_FEE_RATES — this function is only for Polymarket's price curve.
    """
    params = _POLY_FEE_PARAMS.get(category)
    if not params:
        return 0.0
    fee_rate, exponent = params
    p = max(0.0, min(1.0, price))
    return fee_rate * (p * (1 - p)) ** exponent
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fee_model.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_fee_model.py src/execution/paper_executor.py
git commit -m "feat: add per-category Polymarket fee curve (get_taker_fee_rate)"
```

---

### Task 2: Category-Aware sell() and Category Threading

**Files:**
- Modify: `src/execution/paper_executor.py:96-116`
- Modify: `src/positions/position_manager.py:390-408`
- Modify: `src/positions/auto_trader.py:885,1462`
- Add tests to: `tests/test_fee_model.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fee_model.py`:

```python
import asyncio
from unittest.mock import MagicMock, AsyncMock
from execution.paper_executor import PaperExecutor
from execution.base_executor import ExecutionResult


class TestSellWithCategory:
    """Test that sell() uses category-aware fees when category is provided."""

    def _make_executor(self):
        """Create a paper executor wrapping a mock Polymarket executor."""
        real = MagicMock()
        real.__class__.__name__ = "PolymarketExecutor"
        real.get_current_price = AsyncMock(return_value=0.50)
        ex = PaperExecutor(real, starting_balance=1000.0)
        # Seed a position
        ex.positions["tok1:YES"] = {"quantity": 10.0, "avg_entry_price": 0.40}
        return ex

    def test_sell_without_category_uses_flat_rate(self):
        """sell() without category uses self.sell_fee_rate (flat maker 0%)."""
        ex = self._make_executor()
        result = asyncio.get_event_loop().run_until_complete(
            ex.sell("tok1:YES", 10.0))
        assert result.success
        # Polymarket maker sell_fee_rate = 0.0
        assert result.fees == 0.0

    def test_sell_with_crypto_category_uses_curve(self):
        """sell() with category='crypto' uses fee curve instead of flat rate."""
        ex = self._make_executor()
        result = asyncio.get_event_loop().run_until_complete(
            ex.sell("tok1:YES", 10.0, category="crypto"))
        assert result.success
        # At price=0.50, crypto rate = 0.015625
        # proceeds = 10 * 0.50 = 5.0, fee = 5.0 * 0.015625 = 0.0781
        assert abs(result.fees - 0.0781) < 0.001

    def test_sell_with_politics_category_zero_fee(self):
        """sell() with category='politics' has 0% taker fee."""
        ex = self._make_executor()
        result = asyncio.get_event_loop().run_until_complete(
            ex.sell("tok1:YES", 10.0, category="politics"))
        assert result.success
        assert result.fees == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fee_model.py::TestSellWithCategory -v`
Expected: FAIL — `sell()` does not accept `category` parameter

- [ ] **Step 3: Update sell() to accept optional category**

In `src/execution/paper_executor.py`, change the `sell` method (line 96):

Replace:
```python
    async def sell(self, asset_id: str, quantity: float, last_known_price: float = 0) -> ExecutionResult:
```
With:
```python
    async def sell(self, asset_id: str, quantity: float, last_known_price: float = 0,
                   category: str = "") -> ExecutionResult:
```

And replace line 108:
```python
        fee = round(proceeds * self.sell_fee_rate, 4)
```
With:
```python
        if category:
            fee = round(proceeds * get_taker_fee_rate(category, price), 4)
        else:
            fee = round(proceeds * self.sell_fee_rate, 4)
```

- [ ] **Step 4: Store `_category` on packages in auto_trader**

In `src/positions/auto_trader.py`, add after line 885 (`pkg["_use_limit_orders"] = True`):
```python
                pkg["_category"] = self._detect_category(opp_title)
```

And add after line 1463 (`pkg["_use_limit_orders"] = True`):
```python
            pkg["_category"] = self._detect_category(opp_title)
```

- [ ] **Step 5: Pass category to executor.sell() in position_manager**

In `src/positions/position_manager.py`, change lines 404-408 in `_exit_leg_locked`:

Replace:
```python
        # Pass last known price for paper executor fallback
        if hasattr(executor, 'real'):
            result = await executor.sell(leg["asset_id"], leg["quantity"],
                                         last_known_price=leg.get("current_price", 0))
        else:
            result = await executor.sell(leg["asset_id"], leg["quantity"])
```
With:
```python
        # Pass last known price for paper executor fallback + category for fee model
        category = pkg.get("_category", "")
        if hasattr(executor, 'real'):
            result = await executor.sell(leg["asset_id"], leg["quantity"],
                                         last_known_price=leg.get("current_price", 0),
                                         category=category)
        else:
            result = await executor.sell(leg["asset_id"], leg["quantity"])
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_fee_model.py -v`
Expected: All 11 tests PASS

Run: `python -m pytest tests/ -v`
Expected: All 512+ tests PASS (no regressions)

- [ ] **Step 7: Commit**

```bash
git add src/execution/paper_executor.py src/positions/auto_trader.py src/positions/position_manager.py tests/test_fee_model.py
git commit -m "feat: category-aware sell fees and store _category on packages"
```

---

### Task 3: Remove trailing_stop and stop_loss from Mechanical Execution

**Files:**
- Modify: `src/positions/exit_engine.py:697`
- Add tests to: `tests/test_exit_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_exit_engine.py`:

```python
import asyncio
from unittest.mock import MagicMock, AsyncMock


class TestAutoExecuteTriggers:
    """Test that _auto_execute_triggers only executes target_hit mechanically."""

    def _make_engine(self):
        pm = MagicMock()
        pm.exit_leg = AsyncMock(return_value={"success": True})
        pm.list_packages = MagicMock(return_value=[])
        from positions.exit_engine import ExitEngine
        engine = ExitEngine(pm)
        return engine, pm

    def test_target_hit_executes(self):
        """target_hit should auto-execute."""
        engine, pm = self._make_engine()
        pkg = _make_pkg()
        triggers = [{"name": "target_hit", "action": "full_exit",
                      "details": "Target reached"}]
        asyncio.get_event_loop().run_until_complete(
            engine._auto_execute_triggers(pkg, triggers))
        assert pm.exit_leg.called

    def test_stop_loss_does_not_execute(self):
        """stop_loss should NOT auto-execute after fee elimination change."""
        engine, pm = self._make_engine()
        pkg = _make_pkg()
        triggers = [{"name": "stop_loss", "action": "full_exit",
                      "details": "Stop hit"}]
        asyncio.get_event_loop().run_until_complete(
            engine._auto_execute_triggers(pkg, triggers))
        assert not pm.exit_leg.called

    def test_trailing_stop_does_not_execute(self):
        """trailing_stop should NOT auto-execute after fee elimination change."""
        engine, pm = self._make_engine()
        pkg = _make_pkg()
        triggers = [{"name": "trailing_stop", "action": "full_exit",
                      "details": "Trail hit"}]
        asyncio.get_event_loop().run_until_complete(
            engine._auto_execute_triggers(pkg, triggers))
        assert not pm.exit_leg.called
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_exit_engine.py::TestAutoExecuteTriggers -v`
Expected: `test_stop_loss_does_not_execute` and `test_trailing_stop_does_not_execute` FAIL (exit_leg IS called)

- [ ] **Step 3: Change the mechanical execution tuple**

In `src/positions/exit_engine.py` line 697, replace:
```python
            if trigger["name"] in ("target_hit", "stop_loss", "trailing_stop"):
```
With:
```python
            if trigger["name"] in ("target_hit",):
```

Also clean up the dead `is_stop` logic (lines 704-710). Since only `target_hit` enters this branch now, `is_stop` is always False. Replace:
```python
                if trigger.get("action") == "full_exit":
                    # Use maker (limit) orders for non-stop exits (0% fee)
                    is_stop = trigger["name"] == "stop_loss"
                    for leg in pkg["legs"]:
                        if leg["status"] == "open":
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                trigger=f"auto:{trigger['name']}",
                                use_limit=not is_stop)
```
With:
```python
                if trigger.get("action") == "full_exit":
                    for leg in pkg["legs"]:
                        if leg["status"] == "open":
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                trigger=f"auto:{trigger['name']}",
                                use_limit=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_exit_engine.py -v`
Expected: All tests PASS (including existing ones — they test `evaluate_heuristics`, not `_auto_execute_triggers`)

Run: `python -m pytest tests/ -v`
Expected: All 512+ tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/positions/exit_engine.py tests/test_exit_engine.py
git commit -m "feat: remove stop_loss and trailing_stop from mechanical auto-execution"
```

---

### Task 4: Safety Overrides Use Limit Orders

**Files:**
- Modify: `src/positions/exit_engine.py:583-593`
- Add tests to: `tests/test_exit_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_exit_engine.py`:

```python
class TestSafetyOverrideLimitOrders:
    """Safety overrides should use limit orders (0% maker fee)."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_safety_override_passes_use_limit(self):
        """The exit_engine safety override loop should call exit_leg(use_limit=True)."""
        pm = MagicMock()
        pm.exit_leg = AsyncMock(return_value={"success": True})
        pm.list_packages = MagicMock(return_value=[])
        pm.resolve_pending_order = AsyncMock(return_value={"success": True})
        pm.packages = {}

        from positions.exit_engine import ExitEngine
        engine = ExitEngine(pm)

        # Build a package that will trigger spread_inversion (safety override)
        pkg = _make_pkg()
        pkg["legs"][0]["current_price"] = 0.80
        pkg["legs"][1]["current_price"] = 0.30  # combined > 1.05

        pm.list_packages.return_value = [pkg]
        pm.packages = {pkg["id"]: pkg}

        # Run one tick
        self._run(engine._tick())

        # Verify exit_leg was called AND with use_limit=True
        assert pm.exit_leg.called, "Safety override should have triggered exit_leg"
        for call in pm.exit_leg.call_args_list:
            _, kwargs = call
            assert kwargs.get("use_limit") is True, \
                f"Safety override should use limit orders, got: {kwargs}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_exit_engine.py::TestSafetyOverrideLimitOrders -v`
Expected: FAIL — `use_limit` not in kwargs (currently called without it)

- [ ] **Step 3: Change safety override loop to use limit orders**

In `src/positions/exit_engine.py`, replace lines 588-591:
```python
                pkg["_exiting"] = True
                try:
                    for leg in pkg["legs"]:
                        if leg["status"] == "open":
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"], trigger=trigger["name"])
```
With:
```python
                pkg["_exiting"] = True
                try:
                    for leg in pkg["legs"]:
                        if leg["status"] == "open":
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                                   trigger=trigger["name"], use_limit=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_exit_engine.py -v`
Expected: All tests PASS

Run: `python -m pytest tests/ -v`
Expected: All 512+ tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/positions/exit_engine.py tests/test_exit_engine.py
git commit -m "feat: safety overrides use limit orders (0% maker) instead of FOK (2% taker)"
```

---

### Task 5: Configurable Pending Order Timeout

**Files:**
- Modify: `src/positions/position_manager.py:480-488,555`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_exit_engine.py`:

```python
class TestSafetyOverrideTimeout:
    """Safety override limit orders get 300s timeout vs default 60s."""

    def test_pending_order_has_timeout_field(self):
        """_place_limit_sell should store a timeout in the pending order metadata."""
        from positions.position_manager import PositionManager, create_package, create_leg
        import asyncio

        pm = PositionManager.__new__(PositionManager)
        pm.packages = {}
        pm.executors = {}
        pm._lock = asyncio.Lock()
        pm.save = MagicMock()
        pm.trade_journal = MagicMock()

        # Create a package with a leg
        pkg = create_package("Test", "pure_prediction")
        leg = create_leg("polymarket", "prediction_yes", "tok1:YES", "Test", 0.60, 10.0, "2026-12-31")
        leg["current_price"] = 0.65
        pkg["legs"] = [leg]
        pm.packages[pkg["id"]] = pkg

        # Mock executor
        mock_exec = MagicMock()
        mock_exec.sell_limit = AsyncMock(
            return_value=ExecutionResult(True, "order123", 0.65, 10.0, 0.0, None))
        pm.executors["polymarket"] = mock_exec

        # Place limit sell with timeout=300
        result = asyncio.get_event_loop().run_until_complete(
            pm._place_limit_sell(pkg["id"], leg["leg_id"], "spread_inversion", timeout=300))

        assert result.get("pending")
        pending = pkg["_pending_limit_orders"][leg["leg_id"]]
        assert pending["timeout"] == 300
```

Add the import at the top of this test class:
```python
from execution.base_executor import ExecutionResult
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_exit_engine.py::TestSafetyOverrideTimeout -v`
Expected: FAIL — `_place_limit_sell` doesn't accept `timeout` parameter

- [ ] **Step 3: Add timeout parameter to _place_limit_sell**

In `src/positions/position_manager.py`, change the `_place_limit_sell` method signature (around line 445):

Replace:
```python
    async def _place_limit_sell(self, pkg_id: str, leg_id: str, trigger: str) -> dict:
```
With:
```python
    async def _place_limit_sell(self, pkg_id: str, leg_id: str, trigger: str,
                                timeout: int = 60) -> dict:
```

And in the pending order dict (line 480-488), add the timeout field. Replace:
```python
        pkg["_pending_limit_orders"][leg_id] = {
            "order_id": result.tx_id,
            "placed_at": time.time(),
            "quantity": leg["quantity"],
            "asset_id": leg["asset_id"],
            "platform": leg["platform"],
            "trigger": trigger,
            "limit_price": limit_price,
        }
```
With:
```python
        pkg["_pending_limit_orders"][leg_id] = {
            "order_id": result.tx_id,
            "placed_at": time.time(),
            "quantity": leg["quantity"],
            "asset_id": leg["asset_id"],
            "platform": leg["platform"],
            "trigger": trigger,
            "limit_price": limit_price,
            "timeout": timeout,
        }
```

- [ ] **Step 4: Use configurable timeout in resolve_pending_order**

In `src/positions/position_manager.py`, change line 555:

Replace:
```python
            elif time.time() - pending["placed_at"] > 60:
```
With:
```python
            elif time.time() - pending["placed_at"] > pending.get("timeout", 60):
```

- [ ] **Step 5: Thread timeout from exit_leg through _place_limit_sell**

In `src/positions/position_manager.py`, update `exit_leg` (line 381):

Replace:
```python
    async def exit_leg(self, pkg_id: str, leg_id: str, trigger: str = "manual",
                       use_limit: bool = False) -> dict:
        """Exit (sell) a single leg. If use_limit=True, places a GTC limit order
        and returns {"pending": True, "order_id": ...} — caller resolves later."""
        async with self._lock:
            if use_limit:
                return await self._place_limit_sell(pkg_id, leg_id, trigger)
            return await self._exit_leg_locked(pkg_id, leg_id, trigger)
```
With:
```python
    async def exit_leg(self, pkg_id: str, leg_id: str, trigger: str = "manual",
                       use_limit: bool = False, timeout: int = 60) -> dict:
        """Exit (sell) a single leg. If use_limit=True, places a GTC limit order
        and returns {"pending": True, "order_id": ...} — caller resolves later.
        timeout: seconds before falling back to FOK (60s normal, 300s safety)."""
        async with self._lock:
            if use_limit:
                return await self._place_limit_sell(pkg_id, leg_id, trigger, timeout=timeout)
            return await self._exit_leg_locked(pkg_id, leg_id, trigger)
```

- [ ] **Step 6: Pass 300s timeout from exit_engine safety overrides**

In `src/positions/exit_engine.py`, update the safety override loop (the code changed in Task 4):

Replace:
```python
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                                   trigger=trigger["name"], use_limit=True)
```
With:
```python
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                                   trigger=trigger["name"], use_limit=True,
                                                   timeout=300)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_exit_engine.py -v`
Expected: All tests PASS

Run: `python -m pytest tests/ -v`
Expected: All 512+ tests PASS

- [ ] **Step 8: Commit**

```bash
git add src/positions/position_manager.py src/positions/exit_engine.py tests/test_exit_engine.py
git commit -m "feat: configurable timeout for pending limit orders (300s for safety overrides)"
```

---

### Task 6: multi_outcome_arb Gets hold_to_resolution

**Files:**
- Modify: `src/positions/auto_trader.py:876-885`
- Add tests to: `tests/test_auto_trader_improvements.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_auto_trader_improvements.py`:

```python
class TestMultiOutcomeArbHoldToResolution:
    """multi_outcome_arb is guaranteed profit — should hold to resolution."""

    def test_multi_outcome_arb_has_hold_to_resolution(self):
        """multi_outcome_arb packages should have _hold_to_resolution=True."""
        # The multi_outcome_arb handler is at auto_trader.py line 840-885
        # It's a guaranteed profit strategy — should resolve at $1, not exit early
        # We verify by inspecting the _scan_and_trade method source
        from positions.auto_trader import AutoTrader
        import inspect
        source = inspect.getsource(AutoTrader._scan_and_trade)
        # Find the multi_outcome_arb section and check _hold_to_resolution is set
        lines = source.split("\n")
        in_multi_outcome = False
        found_hold = False
        for line in lines:
            if "multi_outcome_arb" in line.lower() and "guaranteed" in line.lower():
                in_multi_outcome = True
            if in_multi_outcome and "_hold_to_resolution" in line:
                found_hold = True
                break
            if in_multi_outcome and "execute_package" in line:
                break  # Past the handler
        assert found_hold, "multi_outcome_arb handler must set _hold_to_resolution = True"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_auto_trader_improvements.py::TestMultiOutcomeArbHoldToResolution -v`
Expected: FAIL — `_hold_to_resolution` not found in multi_outcome_arb handler

- [ ] **Step 3: Add _hold_to_resolution to multi_outcome_arb handler**

In `src/positions/auto_trader.py`, add after line 879 (the `stop_loss` exit rule):

```python
                pkg["_hold_to_resolution"] = True
```

So lines 876-880 become:
```python
                # Multi-outcome arb: guaranteed profit — only exit on safety
                # Widened from -15% to -35%: trade journal showed tight stops
                # cut arb positions before resolution (88.6% loss rate)
                pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -35}))
                pkg["_hold_to_resolution"] = True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_auto_trader_improvements.py::TestMultiOutcomeArbHoldToResolution -v`
Expected: PASS

Run: `python -m pytest tests/ -v`
Expected: All 512+ tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/positions/auto_trader.py tests/test_auto_trader_improvements.py
git commit -m "feat: multi_outcome_arb uses hold_to_resolution (guaranteed profit)"
```

---

## Verification

After all 6 tasks:

- [ ] `python -m pytest tests/ -v` — all tests pass
- [ ] Review `git log --oneline -6` — 6 clean commits
- [ ] Verify the key changes:
  - `exit_engine.py:697` has `("target_hit",)` only
  - `exit_engine.py:591` has `use_limit=True, timeout=300`
  - `paper_executor.py` has `get_taker_fee_rate()` function
  - `paper_executor.py` `sell()` accepts `category` parameter
  - `auto_trader.py:880` has `_hold_to_resolution = True`
  - `auto_trader.py` stores `_category` on packages
  - `position_manager.py:555` reads `pending.get("timeout", 60)`
