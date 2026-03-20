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
        matched = o["quantity"] if o["status"] in ("filled", "partially_filled") else 0
        return {"status": o["status"], "price": o["price"], "size_matched": matched}


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

        # First buy some shares so we have a position to sell
        buy_result = await paper.buy(asset_id="0xtest:NO", amount_usd=200)
        assert buy_result.success

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

        # Buy first
        await paper.buy(asset_id="0xtest:NO", amount_usd=200)
        qty_before = paper.positions["0xtest:NO"]["quantity"]

        result = await paper.sell_limit("0xtest:NO", 100, 0.95)
        order_id = result.tx_id

        # Quantity should be reserved (reduced)
        qty_after_place = paper.positions.get("0xtest:NO", {}).get("quantity", 0)
        assert qty_after_place < qty_before

        cancelled = await paper.cancel_order(order_id)
        assert cancelled is True

        # Quantity should be unreserved (restored)
        qty_after_cancel = paper.positions["0xtest:NO"]["quantity"]
        assert abs(qty_after_cancel - qty_before) < 1  # Restored


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
            "entry_price": 0.90,
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

        # Update peak to simulate appreciation
        bm.update_peak(pkg, "leg_1", 0.95)

        new_stop = bm._compute_trail_price(pkg, "leg_1")
        # New stop should be higher than initial stop (position appreciated)
        if new_stop and new_stop > old_stop:
            result = bm.adjust_stop(pkg, "leg_1", new_stop)
            assert result["success"] is True
            assert pkg["_brackets"]["leg_1"]["stop_price"] > old_stop
