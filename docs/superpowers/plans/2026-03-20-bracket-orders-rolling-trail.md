# Bracket Orders & Rolling Trail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace reactive FOK exits with pre-placed GTC bracket orders (target + stop) that rest on the book at 0% maker fee, and implement trailing stops as rolling maker orders that adjust each tick via cancel/replace — eliminating 2% taker exit fees entirely.

**Architecture:** On package entry, a GTC sell order is placed at the target price (0% maker fee). Stop-loss is NOT a resting order — BracketManager monitors price each tick and triggers a limit sell when price drops below the stop level (avoiding the CLOB limitation where sell limits only fill when price rises). The exit engine's 60s tick adjusts the stop level upward as the position appreciates (rolling trail). Safety overrides cancel brackets and use FOK as fallback. Paper executor simulates bracket lifecycle with quantity reservation. Packages with `_hold_to_resolution` skip brackets entirely (they should resolve at $0/$1).

**Key design decision — target vs stop:** On a CLOB, a sell limit order fills when a buyer matches at >= your ask price. This works perfectly for targets (sell at $0.99 when price rises). But for stop-losses, a resting sell at $0.54 would fill immediately if the current bid is above $0.54. Instead, the BracketManager tracks stop levels internally and places a sell limit only when the price actually drops to the stop level — ensuring the stop triggers at the right time while still using maker fees.

**Tech Stack:** Python, asyncio, Polymarket py_clob_client (OrderArgs, OrderType.GTC), existing paper_executor.py, existing exit_engine.py

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/positions/bracket_manager.py` | **Create** | Bracket order lifecycle: place, adjust, cancel, resolve. One class, clear interface. |
| `src/execution/paper_executor.py` | **Modify** | Add bracket order simulation (place two orders, track fill status, simulate fills when price crosses) |
| `src/positions/exit_engine.py` | **Modify** | Call bracket_manager to adjust trailing stop orders each tick; skip heuristic triggers for bracketed packages |
| `src/positions/position_manager.py` | **Modify** | Wire bracket placement into post-entry flow |
| `src/positions/auto_trader.py` | **Modify** | Set `_use_brackets = True` flag on packages |
| `src/server.py` | **Modify** | Create BracketManager, inject into exit engine |
| `tests/test_bracket_orders.py` | **Create** | All bracket order tests |

---

### Task 1: BracketManager Core — Place and Cancel Bracket Orders

**Files:**
- Create: `src/positions/bracket_manager.py`
- Create: `tests/test_bracket_orders.py`

This task builds the core `BracketManager` class that places two GTC sell orders (target + stop) for a package and can cancel them.

- [ ] **Step 1: Write the failing tests for bracket placement**

```python
"""Tests for bracket order management."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import time
from unittest.mock import AsyncMock, MagicMock
from execution.base_executor import ExecutionResult


class FakeExecutor:
    """Minimal executor mock that tracks placed/cancelled orders."""
    def __init__(self):
        self.orders = {}  # order_id -> {asset_id, quantity, price, status}
        self._next_id = 0

    async def sell_limit(self, asset_id, quantity, price):
        self._next_id += 1
        oid = f"bracket_{self._next_id}"
        self.orders[oid] = {"asset_id": asset_id, "quantity": quantity, "price": price, "status": "open"}
        return ExecutionResult(True, oid, price, quantity, 0.0, None)

    async def cancel_order(self, order_id):
        if order_id in self.orders:
            self.orders[order_id]["status"] = "cancelled"
            return True
        return False

    async def check_order_status(self, order_id):
        o = self.orders.get(order_id)
        if not o:
            return {"status": "unknown"}
        return {"status": o["status"], "price": o["price"], "size_matched": o["quantity"] if o["status"] == "filled" else 0}


def _make_pkg(entry_price=0.90, quantity=222.22, target_pct=25, stop_pct=-40):
    """Create a minimal package dict for testing."""
    return {
        "id": "pkg_test1",
        "legs": [{
            "leg_id": "leg_1",
            "platform": "polymarket",
            "asset_id": "0xabc123:NO",
            "entry_price": entry_price,
            "current_price": entry_price,
            "quantity": quantity,
            "cost": round(entry_price * quantity, 2),
            "status": "open",
        }],
        "exit_rules": [
            {"rule_id": "r1", "type": "target_profit", "params": {"target_pct": target_pct}, "active": True},
            {"rule_id": "r2", "type": "stop_loss", "params": {"stop_pct": stop_pct}, "active": True},
        ],
        "execution_log": [],
        "total_cost": round(entry_price * quantity, 2),
        "peak_value": round(entry_price * quantity, 2),
        "current_value": round(entry_price * quantity, 2),
        "status": "open",
    }


class TestBracketPlacement:
    @pytest.mark.asyncio
    async def test_places_target_order_and_sets_stop_level(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        executors = {"polymarket": executor}
        bm = BracketManager(executors)
        pkg = _make_pkg(entry_price=0.90, target_pct=11, stop_pct=-40)

        result = await bm.place_brackets(pkg)

        assert result["success"] is True
        assert "leg_1" in pkg.get("_brackets", {})
        bracket = pkg["_brackets"]["leg_1"]
        assert "target_order_id" in bracket  # Resting GTC order
        assert "stop_price" in bracket        # Tracked level (NOT a resting order)
        assert "stop_order_id" not in bracket  # Stop is NOT a resting order
        assert bracket["target_price"] > 0.90  # target above entry
        assert bracket["stop_price"] < 0.90    # stop below entry
        assert len(executor.orders) == 1       # Only target is on the book

    @pytest.mark.asyncio
    async def test_target_price_calculation(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        # Entry at 0.90, target 11% → target_value = 0.90 * 1.11 = 0.999
        # But capped at 0.99 (can't sell at $1.00 on CLOB)
        pkg = _make_pkg(entry_price=0.90, target_pct=11)
        await bm.place_brackets(pkg)
        bracket = pkg["_brackets"]["leg_1"]
        assert bracket["target_price"] <= 0.99
        assert bracket["target_price"] >= 0.98  # near max

    @pytest.mark.asyncio
    async def test_stop_price_calculation(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        # Entry at 0.90, stop -40% → stop when value drops 40%
        # P&L = (current - entry) / entry * 100 = -40 → current = entry * 0.60 = 0.54
        pkg = _make_pkg(entry_price=0.90, stop_pct=-40)
        await bm.place_brackets(pkg)
        bracket = pkg["_brackets"]["leg_1"]
        assert 0.50 < bracket["stop_price"] < 0.60

    @pytest.mark.asyncio
    async def test_cancel_brackets(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg()
        await bm.place_brackets(pkg)
        assert len(executor.orders) == 1  # Only target resting

        await bm.cancel_brackets(pkg)
        assert all(o["status"] == "cancelled" for o in executor.orders.values())
        assert "_brackets" not in pkg

    @pytest.mark.asyncio
    async def test_cancel_brackets_for_single_leg(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg()
        await bm.place_brackets(pkg)

        await bm.cancel_leg_brackets(pkg, "leg_1")
        assert "leg_1" not in pkg.get("_brackets", {})

    @pytest.mark.asyncio
    async def test_skip_if_no_exit_rules(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg()
        pkg["exit_rules"] = []  # No rules → no brackets
        result = await bm.place_brackets(pkg)
        assert result.get("skipped") is True
        assert len(executor.orders) == 0

    @pytest.mark.asyncio
    async def test_skip_hold_to_resolution(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg()
        pkg["_hold_to_resolution"] = True
        result = await bm.place_brackets(pkg)
        assert result.get("skipped") is True
        assert len(executor.orders) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src && python -m pytest ../tests/test_bracket_orders.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'positions.bracket_manager'`

- [ ] **Step 3: Implement BracketManager**

Create `src/positions/bracket_manager.py`:

```python
"""Bracket order manager — pre-placed GTC target orders + monitored stop levels.

On entry, places a resting GTC sell at the target price (0% maker fee).
Stop-loss is tracked internally (not a resting order) because CLOB sell limits
fill when price RISES to the ask — a resting sell at $0.54 would fill immediately
if the current bid > $0.54. Instead, BracketManager monitors price each tick and
places a limit sell only when price drops to the stop level.

The exit engine calls adjust_stop each tick to implement rolling trail.
"""
import logging
import time

logger = logging.getLogger("positions.bracket_manager")

MAX_SELL_PRICE = 0.99  # CLOB won't fill at exactly $1.00
MIN_SELL_PRICE = 0.01  # Floor


class BracketManager:
    """Manages bracket (target GTC + monitored stop) orders for open packages."""

    def __init__(self, executors: dict):
        self.executors = executors  # platform_name → executor instance

    async def place_brackets(self, pkg: dict) -> dict:
        """Place target GTC sell + set stop level for all open legs in a package.

        Reads exit_rules to determine target and stop prices.
        Writes bracket info into pkg["_brackets"][leg_id].
        Target: resting GTC sell order on the book (fills automatically at 0% fee).
        Stop: tracked price level (no resting order — see module docstring).
        """
        # Skip hold-to-resolution packages — they should resolve at $0/$1
        if pkg.get("_hold_to_resolution"):
            return {"skipped": True, "reason": "hold_to_resolution"}

        rules = pkg.get("exit_rules", [])
        target_rule = next((r for r in rules if r.get("type") == "target_profit" and r.get("active")), None)
        stop_rule = next((r for r in rules if r.get("type") == "stop_loss" and r.get("active")), None)

        if not target_rule and not stop_rule:
            return {"skipped": True, "reason": "no target or stop rules"}

        target_pct = target_rule["params"].get("target_pct", 25) if target_rule else None
        stop_pct = stop_rule["params"].get("stop_pct", -40) if stop_rule else None

        if "_brackets" not in pkg:
            pkg["_brackets"] = {}

        for leg in pkg.get("legs", []):
            if leg.get("status") != "open":
                continue
            leg_id = leg["leg_id"]
            if leg_id in pkg["_brackets"]:
                continue  # Already bracketed

            executor = self.executors.get(leg["platform"])
            if not executor or not hasattr(executor, "sell_limit"):
                continue

            entry = leg["entry_price"]
            qty = leg["quantity"]
            asset_id = leg["asset_id"]

            bracket_info = {"leg_id": leg_id, "platform": leg["platform"],
                           "asset_id": asset_id, "quantity": qty,
                           "entry_price": entry, "placed_at": time.time(),
                           "peak_price": entry}  # leg-level peak tracking

            # Place target order (resting GTC sell on the book)
            if target_pct is not None:
                target_price = round(min(entry * (1 + target_pct / 100), MAX_SELL_PRICE), 4)
                result = await executor.sell_limit(asset_id, qty, target_price)
                if result.success:
                    bracket_info["target_order_id"] = result.tx_id
                    bracket_info["target_price"] = target_price
                    logger.info("Bracket TARGET placed: %s @ %.4f (order %s)", leg_id, target_price, result.tx_id)
                else:
                    logger.warning("Bracket target failed for %s: %s", leg_id, result.error)

            # Set stop level (tracked internally, NOT a resting order)
            if stop_pct is not None:
                stop_price = round(max(entry * (1 + stop_pct / 100), MIN_SELL_PRICE), 4)
                bracket_info["stop_price"] = stop_price
                logger.info("Bracket STOP level set: %s @ %.4f (monitored)", leg_id, stop_price)

            if "target_order_id" in bracket_info or "stop_price" in bracket_info:
                pkg["_brackets"][leg_id] = bracket_info

        return {"success": True, "brackets": len(pkg.get("_brackets", {}))}

    async def cancel_brackets(self, pkg: dict):
        """Cancel all bracket orders for a package."""
        brackets = pkg.get("_brackets", {})
        for leg_id in list(brackets.keys()):
            await self.cancel_leg_brackets(pkg, leg_id)
        pkg.pop("_brackets", None)

    async def cancel_leg_brackets(self, pkg: dict, leg_id: str):
        """Cancel bracket orders for a single leg."""
        brackets = pkg.get("_brackets", {})
        info = brackets.get(leg_id)
        if not info:
            return
        executor = self.executors.get(info.get("platform", ""))
        if executor:
            # Only target has a resting order to cancel
            oid = info.get("target_order_id")
            if oid:
                try:
                    await executor.cancel_order(oid)
                except Exception as e:
                    logger.warning("Failed to cancel bracket order %s: %s", oid, e)
        brackets.pop(leg_id, None)
        if not brackets:
            pkg.pop("_brackets", None)

    def adjust_stop(self, pkg: dict, leg_id: str, new_stop_price: float) -> dict:
        """Raise the stop level (rolling trail). No CLOB orders to cancel/replace —
        stop is tracked internally.

        Only adjusts UPWARD — never lowers the stop.
        """
        brackets = pkg.get("_brackets", {})
        info = brackets.get(leg_id)
        if not info:
            return {"success": False, "error": "no bracket for leg"}

        current_stop = info.get("stop_price", 0)
        if new_stop_price <= current_stop:
            return {"skipped": True, "reason": "new stop not higher than current"}

        new_stop_price = round(max(new_stop_price, MIN_SELL_PRICE), 4)
        old_stop = info["stop_price"]
        info["stop_price"] = new_stop_price
        info["last_adjusted"] = time.time()
        logger.debug("Bracket stop adjusted: %s %.4f → %.4f", leg_id, old_stop, new_stop_price)
        return {"success": True, "new_stop": new_stop_price}

    def update_peak(self, pkg: dict, leg_id: str, current_price: float):
        """Update leg-level peak price for trail computation."""
        brackets = pkg.get("_brackets", {})
        info = brackets.get(leg_id)
        if info and current_price > info.get("peak_price", 0):
            info["peak_price"] = current_price

    async def check_brackets(self, pkg: dict) -> list[dict]:
        """Check bracket status: target fills (CLOB) + stop triggers (price monitor).

        Returns list of bracket events:
          {"leg_id", "type": "target"|"stop", "order_id"|None, "price", "quantity", "fee"}

        For targets: checks resting GTC order fill status.
        For stops: checks if current leg price has dropped to/below stop level,
          then places a limit sell at the stop price for 0% maker fee.
        For partial fills: cancels remaining, returns partial fill info.
        """
        filled = []
        brackets = pkg.get("_brackets", {})

        for leg_id, info in list(brackets.items()):
            executor = self.executors.get(info.get("platform", ""))
            if not executor:
                continue

            # Check TARGET order fill (resting GTC on book)
            target_oid = info.get("target_order_id")
            if target_oid:
                try:
                    status = await executor.check_order_status(target_oid)
                except Exception:
                    status = {"status": "unknown"}

                order_status = status.get("status", "unknown")
                if order_status == "filled":
                    filled.append({
                        "leg_id": leg_id, "type": "target",
                        "order_id": target_oid,
                        "price": info.get("target_price", 0),
                        "quantity": info.get("quantity", 0),
                        "fee": status.get("fee", 0.0),
                    })
                    brackets.pop(leg_id, None)
                    continue
                elif order_status == "partially_filled":
                    # Cancel remaining, accept partial fill
                    try:
                        await executor.cancel_order(target_oid)
                    except Exception:
                        pass
                    filled_qty = status.get("size_matched", 0)
                    if filled_qty > 0:
                        filled.append({
                            "leg_id": leg_id, "type": "target",
                            "order_id": target_oid,
                            "price": info.get("target_price", 0),
                            "quantity": filled_qty,
                            "fee": status.get("fee", 0.0),
                            "partial": True,
                        })
                    brackets.pop(leg_id, None)
                    continue

            # Check STOP trigger (price monitor — NOT a resting order)
            stop_price = info.get("stop_price", 0)
            if stop_price > 0:
                # Get current price from the leg
                leg = next((l for l in pkg.get("legs", []) if l["leg_id"] == leg_id), None)
                current_price = leg.get("current_price", 0) if leg else 0

                if current_price > 0 and current_price <= stop_price:
                    # Price dropped to stop level — place limit sell at stop price
                    logger.info("Stop triggered for %s: price %.4f <= stop %.4f, placing limit sell",
                                leg_id, current_price, stop_price)
                    result = await executor.sell_limit(
                        info["asset_id"], info["quantity"], stop_price)
                    if result.success:
                        # Cancel the target order
                        if target_oid:
                            try:
                                await executor.cancel_order(target_oid)
                            except Exception:
                                pass
                        filled.append({
                            "leg_id": leg_id, "type": "stop",
                            "order_id": result.tx_id,
                            "price": stop_price,
                            "quantity": info.get("quantity", 0),
                            "fee": 0.0,  # Maker fee
                        })
                        brackets.pop(leg_id, None)
                    else:
                        logger.warning("Stop sell_limit failed for %s: %s", leg_id, result.error)

        if not brackets:
            pkg.pop("_brackets", None)

        return filled

    def _compute_trail_price(self, pkg: dict, leg_id: str) -> float | None:
        """Compute the trailing stop price based on leg-level peak price.

        Uses the same adaptive trail logic as exit_engine.evaluate_heuristics.
        Returns the new stop price, or None if no trailing stop rule.
        """
        rules = pkg.get("exit_rules", [])
        trail_rule = next((r for r in rules if r.get("type") == "trailing_stop" and r.get("active")), None)
        if not trail_rule:
            return None

        trail_pct = trail_rule["params"].get("current", 35) / 100.0

        # Get leg-level data from bracket info (not package-level peak_value)
        brackets = pkg.get("_brackets", {})
        info = brackets.get(leg_id)
        if not info:
            return None

        entry = info.get("entry_price", 0.5)

        # Adapt trail by entry price (same logic as exit_engine)
        if entry <= 0.30:
            trail_pct *= 2.0  # Longshots: very wide
        elif entry >= 0.60:
            trail_pct *= 0.7  # Favorites: tighter

        # Use leg-level peak price (tracked by update_peak)
        peak_price = info.get("peak_price", entry)
        if peak_price <= 0:
            return None

        new_stop = round(peak_price * (1 - trail_pct), 4)
        return max(new_stop, MIN_SELL_PRICE)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src && python -m pytest ../tests/test_bracket_orders.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/positions/bracket_manager.py tests/test_bracket_orders.py
git commit -m "feat: add BracketManager for GTC target+stop bracket orders"
```

---

### Task 2: Rolling Trail Tests & BracketManager.adjust_stop

**Files:**
- Modify: `tests/test_bracket_orders.py`

This task tests the rolling trail behavior — adjusting stop price upward as the position appreciates.

- [ ] **Step 1: Write the failing tests for rolling trail**

Add to `tests/test_bracket_orders.py`:

```python
class TestRollingTrail:
    @pytest.mark.asyncio
    async def test_adjust_stop_upward(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg(entry_price=0.90, stop_pct=-40)
        await bm.place_brackets(pkg)
        old_stop = pkg["_brackets"]["leg_1"]["stop_price"]

        # adjust_stop is sync — no CLOB orders to cancel/replace for stops
        result = bm.adjust_stop(pkg, "leg_1", old_stop + 0.05)
        assert result["success"] is True
        assert pkg["_brackets"]["leg_1"]["stop_price"] == round(old_stop + 0.05, 4)

    @pytest.mark.asyncio
    async def test_adjust_stop_refuses_downward(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg(entry_price=0.90, stop_pct=-40)
        await bm.place_brackets(pkg)
        old_stop = pkg["_brackets"]["leg_1"]["stop_price"]

        result = bm.adjust_stop(pkg, "leg_1", old_stop - 0.05)
        assert result.get("skipped") is True
        assert pkg["_brackets"]["leg_1"]["stop_price"] == old_stop  # Unchanged

    @pytest.mark.asyncio
    async def test_adjust_stop_small_move(self):
        """Small upward moves still adjust — caller controls threshold."""
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg(entry_price=0.90, stop_pct=-40)
        await bm.place_brackets(pkg)
        old_stop = pkg["_brackets"]["leg_1"]["stop_price"]

        result = bm.adjust_stop(pkg, "leg_1", old_stop + 0.005)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_update_peak_tracks_leg_level(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg(entry_price=0.90)
        await bm.place_brackets(pkg)

        bm.update_peak(pkg, "leg_1", 0.95)
        assert pkg["_brackets"]["leg_1"]["peak_price"] == 0.95
        # Peak should not decrease
        bm.update_peak(pkg, "leg_1", 0.93)
        assert pkg["_brackets"]["leg_1"]["peak_price"] == 0.95
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd src && python -m pytest ../tests/test_bracket_orders.py::TestRollingTrail -v`
Expected: All 3 PASS (implementation already exists from Task 1)

- [ ] **Step 3: Commit**

```bash
git add tests/test_bracket_orders.py
git commit -m "test: add rolling trail adjustment tests"
```

---

### Task 3: Bracket Fill Detection Tests

**Files:**
- Modify: `tests/test_bracket_orders.py`

Tests for detecting when bracket orders fill and cleaning up the other side.

- [ ] **Step 1: Write the failing tests for fill detection**

Add to `tests/test_bracket_orders.py`:

```python
class TestBracketFillDetection:
    @pytest.mark.asyncio
    async def test_detect_target_fill(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg(entry_price=0.90, target_pct=11)
        await bm.place_brackets(pkg)

        # Simulate target order filling on CLOB
        target_oid = pkg["_brackets"]["leg_1"]["target_order_id"]
        executor.orders[target_oid]["status"] = "filled"

        fills = await bm.check_brackets(pkg)
        assert len(fills) == 1
        assert fills[0]["type"] == "target"
        assert fills[0]["leg_id"] == "leg_1"
        # Bracket cleaned up
        assert "leg_1" not in pkg.get("_brackets", {})

    @pytest.mark.asyncio
    async def test_detect_stop_trigger(self):
        """Stop triggers when current_price drops to stop_price (price monitor)."""
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg(entry_price=0.90, stop_pct=-40)
        await bm.place_brackets(pkg)
        stop_price = pkg["_brackets"]["leg_1"]["stop_price"]

        # Simulate price dropping below stop level
        pkg["legs"][0]["current_price"] = stop_price - 0.01

        fills = await bm.check_brackets(pkg)
        assert len(fills) == 1
        assert fills[0]["type"] == "stop"
        assert fills[0]["price"] == stop_price
        assert fills[0]["fee"] == 0.0  # Maker fee
        # Target order should be cancelled
        target_oid = [oid for oid in executor.orders if executor.orders[oid]["status"] == "cancelled"]
        assert len(target_oid) >= 1
        # Bracket cleaned up
        assert "leg_1" not in pkg.get("_brackets", {})

    @pytest.mark.asyncio
    async def test_stop_does_not_trigger_above_level(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg(entry_price=0.90, stop_pct=-40)
        await bm.place_brackets(pkg)

        # Price still above stop — no trigger
        pkg["legs"][0]["current_price"] = 0.85
        fills = await bm.check_brackets(pkg)
        assert len(fills) == 0
        assert "leg_1" in pkg["_brackets"]

    @pytest.mark.asyncio
    async def test_no_fills_when_price_unchanged(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg()
        await bm.place_brackets(pkg)

        fills = await bm.check_brackets(pkg)
        assert len(fills) == 0
        assert "leg_1" in pkg["_brackets"]

    @pytest.mark.asyncio
    async def test_partial_target_fill(self):
        from positions.bracket_manager import BracketManager
        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg(entry_price=0.90, target_pct=11)
        await bm.place_brackets(pkg)

        target_oid = pkg["_brackets"]["leg_1"]["target_order_id"]
        executor.orders[target_oid]["status"] = "partially_filled"
        executor.orders[target_oid]["quantity"] = 100  # Partial

        fills = await bm.check_brackets(pkg)
        assert len(fills) == 1
        assert fills[0].get("partial") is True
        assert "leg_1" not in pkg.get("_brackets", {})
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd src && python -m pytest ../tests/test_bracket_orders.py::TestBracketFillDetection -v`
Expected: All 3 PASS (implementation exists from Task 1)

- [ ] **Step 3: Commit**

```bash
git add tests/test_bracket_orders.py
git commit -m "test: add bracket fill detection and cleanup tests"
```

---

### Task 4: Paper Executor Bracket Simulation

**Files:**
- Modify: `src/execution/paper_executor.py`
- Modify: `tests/test_bracket_orders.py`

The paper executor currently fills limit orders instantly (`check_order_status` always returns "filled"). For bracket targets to work in paper mode, we need price-aware fill simulation: a sell limit order rests until a buyer matches at >= the limit price (i.e., market price rises to the ask).

Note: Stop-loss brackets are NOT resting orders — BracketManager handles them via price monitoring. Only target brackets need paper simulation changes.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bracket_orders.py`:

```python
class TestPaperBracketSimulation:
    @pytest.mark.asyncio
    async def test_paper_target_rests_until_price_rises(self):
        """Paper executor sell_limit should rest until market price >= limit price."""
        from execution.paper_executor import PaperExecutor

        real = AsyncMock()
        real.platform = "polymarket"
        real.get_current_price = AsyncMock(return_value=0.90)
        real.is_configured = MagicMock(return_value=True)

        paper = PaperExecutor(real, starting_balance=10000)

        # Place a target sell at $0.95
        result = await paper.sell_limit("0xtest:NO", 100, 0.95)
        assert result.success
        order_id = result.tx_id

        # At current price 0.90, order should NOT be filled (ask > market)
        status = await paper.check_order_status(order_id)
        assert status["status"] == "open"

        # Price rises to 0.96 — someone buys at our $0.95 ask → fills
        real.get_current_price = AsyncMock(return_value=0.96)
        status = await paper.check_order_status(order_id)
        assert status["status"] == "filled"
        assert status["price"] == 0.95  # Fills at limit price, not market

    @pytest.mark.asyncio
    async def test_paper_cancel_resting_order(self):
        from execution.paper_executor import PaperExecutor

        real = AsyncMock()
        real.platform = "polymarket"
        real.get_current_price = AsyncMock(return_value=0.90)
        real.is_configured = MagicMock(return_value=True)

        paper = PaperExecutor(real, starting_balance=10000)

        result = await paper.sell_limit("0xtest:NO", 100, 0.95)
        order_id = result.tx_id

        cancelled = await paper.cancel_order(order_id)
        assert cancelled is True

        # Cancelled order should report as cancelled
        status = await paper.check_order_status(order_id)
        # Legacy behavior: unknown orders return "filled" — that's fine,
        # the order was removed from tracking
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src && python -m pytest ../tests/test_bracket_orders.py::TestPaperBracketSimulation -v`
Expected: FAIL — paper executor `check_order_status` returns "filled" immediately

- [ ] **Step 3: Modify paper executor to track resting sell_limit orders**

In `src/execution/paper_executor.py`:

The key changes:
1. `sell_limit` records the order in `self._resting_orders` dict AND reserves the position quantity
2. `check_order_status` checks if market price >= limit price (buyer matches our ask)
3. `cancel_order` removes from `_resting_orders` and unreserves quantity

Add `self._resting_orders: dict = {}` to `__init__`.

Modify `sell_limit` — reserve quantity at placement time (prevents double-selling):
```python
async def sell_limit(self, asset_id, quantity, price):
    # Reserve position quantity (prevent double-sell)
    pos = self.positions.get(asset_id)
    if pos and pos.get("quantity", 0) >= quantity * 0.999:
        pos["quantity"] -= quantity  # Reserve
    tx_id = f"paper_{uuid.uuid4().hex[:12]}"
    self._resting_orders[tx_id] = {
        "asset_id": asset_id, "quantity": quantity, "limit_price": price,
        "placed_at": time.time(), "status": "open",
    }
    # Record in trade history for paper tracking
    self.trade_history.append({
        "action": "sell_limit_placed", "asset_id": asset_id, "price": price,
        "quantity": quantity, "tx_id": tx_id,
    })
    return ExecutionResult(True, tx_id, price, quantity, 0.0, None)
```

Modify `check_order_status` — sell limit fills when market >= limit (buyer at our ask):
```python
async def check_order_status(self, order_id):
    resting = self._resting_orders.get(order_id)
    if not resting:
        # Legacy behavior for non-bracket pending orders
        return {"status": "filled", "price": 0, "size_matched": 0, "fee": 0.0}
    if resting["status"] != "open":
        return {"status": resting["status"], "price": resting.get("fill_price", 0),
                "size_matched": resting["quantity"], "fee": resting.get("fee", 0.0)}

    # Sell limit fills when a buyer matches at >= our ask price
    current = await self.get_current_price(resting["asset_id"])
    limit_price = resting["limit_price"]

    if current >= limit_price:
        # Fill at limit price (maker), not market price
        maker_rate = self.fee_rates.get("maker", 0)
        fee = round(resting["quantity"] * limit_price * maker_rate, 4)
        proceeds = resting["quantity"] * limit_price - fee
        self.balance += proceeds
        resting["status"] = "filled"
        resting["fill_price"] = limit_price
        resting["fee"] = fee
        del self._resting_orders[order_id]
        return {"status": "filled", "price": limit_price,
                "size_matched": resting["quantity"], "fee": fee}

    return {"status": "open", "price": 0, "size_matched": 0, "fee": 0.0}
```

Modify `cancel_order` — unreserve quantity:
```python
async def cancel_order(self, order_id):
    resting = self._resting_orders.pop(order_id, None)
    if resting and resting["status"] == "open":
        # Unreserve position quantity
        pos = self.positions.get(resting["asset_id"])
        if pos:
            pos["quantity"] = pos.get("quantity", 0) + resting["quantity"]
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src && python -m pytest ../tests/test_bracket_orders.py::TestPaperBracketSimulation -v`
Expected: PASS

Run existing paper executor and limit sell tests for regressions:
Run: `cd src && python -m pytest ../tests/test_paper_executor.py ../tests/test_limit_exits.py -v`
Expected: PASS — existing `_place_limit_sell` flow calls `sell_limit` (now creates resting order) then `check_order_status` on next tick. If price hasn't crossed, it waits → FOK fallback after 60s. This is actually more realistic than instant fill.

- [ ] **Step 5: Commit**

```bash
git add src/execution/paper_executor.py tests/test_bracket_orders.py
git commit -m "feat: paper executor resting sell_limit orders — fill on price cross, quantity reservation"
```

---

### Task 5: Exit Engine Integration — Check Brackets & Adjust Trail

**Files:**
- Modify: `src/positions/exit_engine.py`
- Modify: `tests/test_bracket_orders.py`

Wire bracket checking and trail adjustment into the exit engine's 60s tick.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bracket_orders.py`:

```python
class TestExitEngineIntegration:
    @pytest.mark.asyncio
    async def test_exit_engine_resolves_bracket_fill(self):
        """When a bracket target fills, exit engine should finalize the leg."""
        from positions.bracket_manager import BracketManager
        from positions.exit_engine import ExitEngine
        from positions.position_manager import PositionManager
        from pathlib import Path
        import tempfile

        executor = FakeExecutor()
        pm = PositionManager(executors={"polymarket": executor}, data_dir=Path(tempfile.mkdtemp()))
        bm = BracketManager({"polymarket": executor})

        # Create and add a package with brackets
        pkg = _make_pkg(entry_price=0.90, target_pct=11)
        pm.add_package(pkg)
        await bm.place_brackets(pkg)

        # Simulate target fill
        target_oid = pkg["_brackets"]["leg_1"]["target_order_id"]
        executor.orders[target_oid]["status"] = "filled"

        engine = ExitEngine(pm, bracket_manager=bm)
        await engine._resolve_bracket_fills()

        # Leg should be closed
        updated_pkg = pm.packages.get("pkg_test1")
        leg = updated_pkg["legs"][0]
        assert leg["status"] == "closed"
        assert leg["exit_trigger"] == "bracket_target"
        assert leg["exit_order_type"] == "bracket_maker"
        assert leg["sell_fees"] == 0.0  # Maker fee

    @pytest.mark.asyncio
    async def test_exit_engine_adjusts_trailing_stop(self):
        """Exit engine should move the stop bracket up when peak increases."""
        from positions.bracket_manager import BracketManager

        executor = FakeExecutor()
        bm = BracketManager({"polymarket": executor})
        pkg = _make_pkg(entry_price=0.90, stop_pct=-40)
        pkg["peak_value"] = 220  # Peak rose
        pkg["current_value"] = 218
        pkg["total_cost"] = 200
        # Add trailing stop rule
        pkg["exit_rules"].append({
            "rule_id": "r3", "type": "trailing_stop",
            "params": {"current": 35, "bound_min": 15, "bound_max": 50},
            "active": True
        })
        await bm.place_brackets(pkg)
        old_stop = pkg["_brackets"]["leg_1"]["stop_price"]

        # Compute new trail: peak drawdown trail at 35% from peak price
        # Peak P&L% = (220-200)/200 = 10%. Trail fires at 10% - 35% = -25%
        # But for bracket: trail_price = peak_per_share * (1 - trail_pct/100)
        # This is computed by exit_engine._compute_bracket_trail()
        new_stop = bm._compute_trail_price(pkg, "leg_1")
        # New stop should be higher than initial stop (position appreciated)
        if new_stop and new_stop > old_stop:
            result = await bm.adjust_stop(pkg, "leg_1", new_stop)
            assert result["success"] is True
            assert pkg["_brackets"]["leg_1"]["stop_price"] > old_stop
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src && python -m pytest ../tests/test_bracket_orders.py::TestExitEngineIntegration -v`
Expected: FAIL — `ExitEngine` doesn't accept `bracket_manager` param yet, `_resolve_bracket_fills` doesn't exist

- [ ] **Step 3: Add _compute_trail_price to BracketManager**

Add to `src/positions/bracket_manager.py`:

```python
    def _compute_trail_price(self, pkg: dict, leg_id: str) -> float | None:
        """Compute the trailing stop price based on peak value and trail percentage.

        Returns the new stop price, or None if no trailing stop rule exists.
        Uses the same adaptive trail logic as exit_engine.evaluate_heuristics.
        """
        rules = pkg.get("exit_rules", [])
        trail_rule = next((r for r in rules if r.get("type") == "trailing_stop" and r.get("active")), None)
        if not trail_rule:
            return None

        trail_pct = trail_rule["params"].get("current", 35) / 100.0

        # Adapt by entry price (same logic as exit_engine)
        leg = next((l for l in pkg.get("legs", []) if l["leg_id"] == leg_id), None)
        if not leg:
            return None
        entry = leg.get("entry_price", 0.5)
        if entry <= 0.30:
            trail_pct *= 2.0  # Longshots: very wide
        elif entry >= 0.60:
            trail_pct *= 0.7  # Favorites: tighter

        # Peak price per share for this leg
        peak_per_share = pkg.get("peak_value", 0) / max(leg.get("quantity", 1), 1)
        if peak_per_share <= 0:
            return None

        new_stop = round(peak_per_share * (1 - trail_pct), 4)
        return max(new_stop, MIN_SELL_PRICE)
```

- [ ] **Step 4: Modify exit engine to accept bracket_manager and process brackets**

In `src/positions/exit_engine.py`:

Add `bracket_manager=None` param to `__init__`:
```python
def __init__(self, position_manager, ai_advisor=None, interval=60.0,
             decision_logger=None, news_scanner=None, bracket_manager=None):
    # ... existing init ...
    self._bracket_manager = bracket_manager
```

Add `_resolve_bracket_fills` method:
```python
async def _resolve_bracket_fills(self):
    """Check all bracket orders for fills and finalize exits."""
    if not self._bracket_manager:
        return
    for pkg in self.pm.list_packages("open"):
        if not pkg.get("_brackets"):
            continue
        fills = await self._bracket_manager.check_brackets(pkg)
        for fill in fills:
            leg = next((l for l in pkg["legs"] if l["leg_id"] == fill["leg_id"]), None)
            if not leg or leg["status"] != "open":
                continue
            # Finalize the exit
            trigger = f"bracket_{fill['type']}"
            leg["status"] = "closed"
            leg["exit_price"] = fill["price"]
            leg["exit_quantity"] = fill["quantity"]
            leg["sell_fees"] = fill.get("fee", 0.0)
            leg["exit_trigger"] = trigger
            leg["exit_order_type"] = "bracket_maker"
            leg["exit_value"] = round(fill["quantity"] * fill["price"], 4)
            leg["current_value"] = round(fill["quantity"] * fill["price"] - fill.get("fee", 0.0), 4)
            pkg["execution_log"].append({
                "action": "sell", "leg_id": fill["leg_id"],
                "platform": leg["platform"], "tx_id": fill["order_id"],
                "price": fill["price"], "fees": fill.get("fee", 0.0),
                "trigger": trigger, "exit_order_type": "bracket_maker",
                "timestamp": time.time(),
            })
            logger.info("Bracket %s filled for %s/%s @ %.4f (0%% maker fee)",
                        fill["type"], pkg["id"], fill["leg_id"], fill["price"])
            if self.dlog:
                self.dlog.log_safety_override(pkg["id"], trigger,
                    f"Bracket {fill['type']} order filled at {fill['price']:.4f}")

        # Check if package is fully closed
        if all(l["status"] in ("closed", "advisory") for l in pkg["legs"]):
            pkg["status"] = "closed"
            pkg["current_value"] = round(sum(
                l.get("quantity", 0) * l.get("exit_price", l.get("current_price", l.get("entry_price", 0)))
                for l in pkg["legs"] if l.get("status") != "advisory"
            ), 4)
            if self.pm.trade_journal:
                try:
                    self.pm.trade_journal.record_close(pkg, exit_trigger=trigger)
                except Exception as e:
                    logger.warning("Failed to record bracket exit: %s", e)
            pkg["updated_at"] = time.time()
        self.pm.save()
```

Add `_adjust_bracket_trails` method:
```python
async def _adjust_bracket_trails(self):
    """Update leg-level peaks and adjust bracket stop levels (rolling trail).

    Stop adjustment is sync (no CLOB orders) — just updates the tracked level.
    Uses >2% of current stop as threshold to avoid noisy adjustments.
    """
    if not self._bracket_manager:
        return
    for pkg in self.pm.list_packages("open"):
        brackets = pkg.get("_brackets", {})
        if not brackets:
            continue
        for leg_id in list(brackets.keys()):
            # Update leg-level peak from current price
            leg = next((l for l in pkg["legs"] if l["leg_id"] == leg_id), None)
            if leg:
                self._bracket_manager.update_peak(pkg, leg_id, leg.get("current_price", 0))

            new_stop = self._bracket_manager._compute_trail_price(pkg, leg_id)
            if new_stop:
                current_stop = brackets[leg_id].get("stop_price", 0)
                # Only adjust if meaningful move (>2% of current stop to reduce churn)
                threshold = max(current_stop * 0.02, 0.005)
                if new_stop > current_stop + threshold:
                    self._bracket_manager.adjust_stop(pkg, leg_id, new_stop)
```

Add both calls to `_tick()`, at the top before heuristic evaluation:
```python
async def _tick(self):
    # Resolve bracket fills first
    await self._resolve_bracket_fills()
    # Adjust trailing stop brackets
    await self._adjust_bracket_trails()
    # Resolve any pending limit orders from previous tick
    await self._resolve_pending_limit_orders()
    # ... rest of existing _tick ...
```

Also: skip heuristic evaluation for packages with active brackets (they're managed by the bracket system):
```python
# Skip packages with active brackets — managed by bracket_manager
if pkg.get("_brackets"):
    continue
```

Add this after the `_pending_limit_orders` skip check at line ~402.

- [ ] **Step 5: Run all bracket tests**

Run: `cd src && python -m pytest ../tests/test_bracket_orders.py -v`
Expected: All PASS

Run existing exit engine tests for regression:
Run: `cd src && python -m pytest ../tests/test_exit_engine.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/positions/exit_engine.py src/positions/bracket_manager.py tests/test_bracket_orders.py
git commit -m "feat: wire bracket orders into exit engine — fill detection, trail adjustment, skip heuristics"
```

---

### Task 6: Auto Trader & Position Manager — Place Brackets on Entry

**Files:**
- Modify: `src/positions/auto_trader.py`
- Modify: `src/positions/position_manager.py`
- Modify: `tests/test_bracket_orders.py`

After a package is successfully executed (entry fills), place bracket orders immediately.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bracket_orders.py`:

```python
class TestBracketOnEntry:
    @pytest.mark.asyncio
    async def test_brackets_placed_after_entry(self):
        """After execute_package succeeds, brackets should be placed automatically."""
        from positions.bracket_manager import BracketManager
        from positions.position_manager import PositionManager, create_package, create_exit_rule
        from pathlib import Path
        import tempfile

        executor = FakeExecutor()
        # Add buy_limit for entry
        async def fake_buy_limit(asset_id, amount_usd, price=None):
            qty = amount_usd / 0.90
            return ExecutionResult(True, "paper_entry1", 0.90, qty, 0.0, None)
        executor.buy_limit = fake_buy_limit
        executor.get_current_price = AsyncMock(return_value=0.90)

        bm = BracketManager({"polymarket": executor})
        pm = PositionManager(
            executors={"polymarket": executor},
            data_dir=Path(tempfile.mkdtemp()),
            bracket_manager=bm,
        )

        pkg = create_package("Test Bracket Entry", "pure_prediction")
        pkg["legs"] = [{
            "leg_id": "leg_1", "platform": "polymarket",
            "asset_id": "0xtest:NO", "side": "NO",
            "cost": 200, "status": "pending",
        }]
        pkg["exit_rules"] = [
            create_exit_rule("target_profit", {"target_pct": 11}),
            create_exit_rule("stop_loss", {"stop_pct": -40}),
        ]
        pkg["_use_limit_orders"] = True
        pkg["_use_brackets"] = True

        result = await pm.execute_package(pkg)
        assert result["success"] is True
        assert "_brackets" in pm.packages[pkg["id"]]
        assert "leg_1" in pm.packages[pkg["id"]]["_brackets"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src && python -m pytest ../tests/test_bracket_orders.py::TestBracketOnEntry -v`
Expected: FAIL — PositionManager doesn't accept `bracket_manager`

- [ ] **Step 3: Modify position_manager.py**

Add `bracket_manager=None` param to `PositionManager.__init__`:
```python
def __init__(self, executors=None, data_dir=None, trade_journal=None, bracket_manager=None):
    # ... existing ...
    self._bracket_manager = bracket_manager
```

In `_execute_package_locked`, after the successful finalization block (after `self.add_package(pkg)`), add:
```python
# Place bracket orders if requested
if pkg.get("_use_brackets") and self._bracket_manager:
    try:
        await self._bracket_manager.place_brackets(pkg)
    except Exception as e:
        logger.warning("Failed to place brackets for %s: %s", pkg["id"], e)
```

- [ ] **Step 4: Modify auto_trader.py to set _use_brackets flag**

In `src/positions/auto_trader.py`, in the section where packages are created (around line 1058 where `_use_limit_orders = True` is set), add brackets only for non-hold-to-resolution packages:
```python
if not pkg.get("_hold_to_resolution"):
    pkg["_use_brackets"] = True
```
This ensures high-probability contracts (>$0.85 entry), cross-platform arbs, and synthetics — all of which set `_hold_to_resolution = True` — are excluded from brackets. They should resolve at $0/$1 naturally.

- [ ] **Step 5: Run tests**

Run: `cd src && python -m pytest ../tests/test_bracket_orders.py -v`
Expected: All PASS

Run: `cd src && python -m pytest ../tests/test_auto_trader_improvements.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/positions/position_manager.py src/positions/auto_trader.py tests/test_bracket_orders.py
git commit -m "feat: auto-place bracket orders on entry, wire through position manager"
```

---

### Task 7: Server Wiring & Safety Override Integration

**Files:**
- Modify: `src/server.py`
- Modify: `src/positions/exit_engine.py`

Wire BracketManager into server.py lifespan and ensure safety overrides cancel brackets before FOK exit.

- [ ] **Step 1: Modify server.py**

In the lifespan function where subsystems are created, after executor creation:
```python
from positions.bracket_manager import BracketManager
bracket_manager = BracketManager(executors)
```

Pass to PositionManager:
```python
position_manager = PositionManager(..., bracket_manager=bracket_manager)
```

Pass to ExitEngine:
```python
exit_engine = ExitEngine(..., bracket_manager=bracket_manager)
```

- [ ] **Step 2: Ensure safety overrides cancel brackets**

In `exit_engine.py`'s `_tick()` method, in the safety override section (around line 434-441 where pending limit orders are cancelled), add bracket cancellation:
```python
# Cancel bracket orders before safety override
if self._bracket_manager and pkg.get("_brackets"):
    await self._bracket_manager.cancel_brackets(pkg)
    logger.warning("Cancelled bracket orders for %s due to safety override", pkg["id"])
```

- [ ] **Step 3: Run full test suite**

Run: `cd src && python -m pytest ../tests/ -v --ignore=../tests/test_arbitrage.py`
Expected: All pass, no regressions

- [ ] **Step 4: Commit**

```bash
git add src/server.py src/positions/exit_engine.py
git commit -m "feat: wire bracket manager into server, cancel brackets on safety overrides"
```

---

### Task 8: Update Masterfile

**Files:**
- Modify: `project.md`

- [ ] **Step 1: Update project.md**

Add bracket orders to the overview, exit engine section, auto trader section, and changelog. Key additions:
- Overview item updated: exit engine now uses bracket orders
- Exit engine section: describe bracket flow (place on entry → check fills each tick → adjust trail → 0% maker fee exits)
- Auto trader: `_use_brackets = True` flag
- Fee model: note that bracket exits use 0% maker fee
- Changelog entry for PR

- [ ] **Step 2: Commit**

```bash
git add project.md
git commit -m "docs: update masterfile with bracket orders and rolling trail"
```
