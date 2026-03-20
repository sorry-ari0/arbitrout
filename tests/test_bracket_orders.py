"""Tests for bracket order management."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import time
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
        assert bracket["stop_price"] == pytest.approx(0.54, abs=0.001)

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
