# tests/test_limit_exits.py
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from execution.paper_executor import PaperExecutor
from execution.base_executor import BaseExecutor, ExecutionResult


class StubPolymarketExecutor(BaseExecutor):
    """Minimal real executor stub for PaperExecutor wrapping."""
    async def buy(self, asset_id, amount_usd, **kw):
        return ExecutionResult(False, None, 0, 0, 0, "stub")
    async def sell(self, asset_id, quantity, **kw):
        return ExecutionResult(False, None, 0, 0, 0, "stub")
    async def get_current_price(self, asset_id):
        return 0.50
    async def get_balance(self):
        from execution.base_executor import BalanceResult
        return BalanceResult(0, 0)
    async def get_positions(self):
        return []
    def is_configured(self):
        return True


def make_paper_executor(balance=1000.0):
    """Create a PaperExecutor wrapping a stub Polymarket executor."""
    StubPolymarketExecutor.__name__ = "polymarketexecutor"
    real = StubPolymarketExecutor()
    pe = PaperExecutor(real, starting_balance=balance)
    return pe


def test_sell_limit_uses_maker_fee():
    """sell_limit() should use 0% maker fee for Polymarket, not 2% taker."""
    pe = make_paper_executor(balance=1000.0)
    pe.positions["test:YES"] = {"quantity": 100.0, "avg_entry_price": 0.50}
    result = asyncio.run(
        pe.sell_limit("test:YES", 100.0, 0.60)
    )
    assert result.success
    assert result.fees == 0.0, f"Expected 0% maker fee, got {result.fees}"
    assert result.filled_price == 0.60, "Should fill at the limit price"
    assert result.filled_quantity == 100.0


def test_sell_limit_uses_limit_price_not_market():
    """sell_limit() places a resting order at the limit price; balance credited on fill."""
    pe = make_paper_executor(balance=1000.0)
    pe.positions["test:YES"] = {"quantity": 50.0, "avg_entry_price": 0.40}
    result = asyncio.run(
        pe.sell_limit("test:YES", 50.0, 0.75)
    )
    assert result.success
    assert result.filled_price == 0.75
    # Resting order: balance not credited yet (order is open, not filled)
    assert pe.balance == 1000.0
    # Verify resting order was placed
    assert result.tx_id is not None
    assert result.tx_id in pe._resting_orders
    assert pe._resting_orders[result.tx_id]["limit_price"] == 0.75


def test_sell_market_charges_maker_fee():
    """Regular sell() should charge 0% maker fee (all orders now use GTC limit)."""
    pe = make_paper_executor(balance=1000.0)
    pe.positions["test:YES"] = {"quantity": 100.0, "avg_entry_price": 0.50}
    result = asyncio.run(
        pe.sell("test:YES", 100.0)
    )
    assert result.success
    assert result.fees == 0.0, "Polymarket sell should charge 0% maker fee"


def test_check_order_status_unknown_order_is_cancelled():
    """Unknown order_id must not phantom-fill (matches lost-on-restart safety)."""
    pe = make_paper_executor()
    result = asyncio.run(pe.check_order_status("paper_nonexistent"))
    assert result["status"] == "cancelled"


def test_check_order_status_filled_when_price_crosses_limit():
    """Resting sell fills when market price >= limit (0% maker fee)."""
    pe = make_paper_executor()
    pe.positions["test:YES"] = {"quantity": 100.0, "avg_entry_price": 0.50}
    r = asyncio.run(pe.sell_limit("test:YES", 100.0, 0.60))
    assert r.success and r.tx_id
    # Stub returns mid 0.50 — still open
    st = asyncio.run(pe.check_order_status(r.tx_id))
    assert st["status"] == "open"

    async def high_price(_aid):
        return 0.65

    pe.real.get_current_price = high_price
    st2 = asyncio.run(pe.check_order_status(r.tx_id))
    assert st2["status"] == "filled"
    assert st2["fee"] == 0.0


def test_cancel_order_returns_true():
    """Paper executor cancel_order() should return True (no-op)."""
    pe = make_paper_executor()
    result = asyncio.run(
        pe.cancel_order("paper_abc123")
    )
    assert result is True


def test_exit_leg_with_limit_returns_pending():
    """exit_leg(use_limit=True) should place a limit order and return pending status."""
    from positions.position_manager import PositionManager, create_package, create_leg
    from execution.base_executor import ExecutionResult
    import tempfile
    from pathlib import Path

    pm = PositionManager(data_dir=Path(tempfile.mkdtemp()), executors={})
    # Create a mock executor that supports sell_limit
    mock_exec = AsyncMock()
    mock_exec.sell_limit = AsyncMock(return_value=ExecutionResult(
        success=True, tx_id="order_123", filled_price=0.60,
        filled_quantity=100.0, fees=0.0, error=None
    ))
    mock_exec.check_order_status = AsyncMock(return_value={"status": "open"})
    pm.executors["polymarket"] = mock_exec

    pkg = create_package("Test pkg", "pure_prediction")
    leg = create_leg("polymarket", "prediction_yes", "cond:YES", "Cond YES", 0.50, 100.0, "2026-12-31")
    leg["current_price"] = 0.60
    pkg["legs"].append(leg)
    pm.packages[pkg["id"]] = pkg

    result = asyncio.run(
        pm.exit_leg(pkg["id"], leg["leg_id"], trigger="ai_approved:target_hit", use_limit=True)
    )
    assert result.get("pending"), "Expected pending status for limit order"
    assert result.get("order_id") == "order_123"
    assert leg["status"] == "open", "Leg should stay open while order is pending"
    assert "_pending_limit_orders" in pkg
