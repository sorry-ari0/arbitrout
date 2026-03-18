"""Tests for execution models and BaseExecutor ABC."""
import pytest
from execution.base_executor import ExecutionResult, BalanceResult, PositionInfo, BaseExecutor

class TestExecutionResult:
    def test_success(self):
        r = ExecutionResult(success=True, tx_id="tx_1", filled_price=0.65,
                           filled_quantity=10.0, fees=0.02, error=None)
        assert r.success and r.tx_id == "tx_1" and r.fees == 0.02

    def test_failure(self):
        r = ExecutionResult(success=False, tx_id=None, filled_price=0.0,
                           filled_quantity=0.0, fees=0.0, error="Insufficient balance")
        assert not r.success and r.error == "Insufficient balance"

    def test_to_dict(self):
        r = ExecutionResult(True, "tx_1", 0.5, 5.0, 0.01, None)
        assert r.to_dict()["success"] is True

class TestBaseExecutor:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            BaseExecutor()

    def test_valid_subclass(self):
        class Stub(BaseExecutor):
            async def buy(self, asset_id, amount_usd): return ExecutionResult(True,"t",1,1,0,None)
            async def sell(self, asset_id, quantity): return ExecutionResult(True,"t",1,1,0,None)
            async def get_balance(self): return BalanceResult(100, 100)
            async def get_positions(self): return []
            async def get_current_price(self, asset_id): return 1.0
            def is_configured(self): return True
        assert Stub().is_configured()
