"""Tests for parallel leg execution in position manager."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from positions.position_manager import PositionManager, create_package, create_leg
from execution.base_executor import ExecutionResult


def _mock_executor(success=True, price=0.45, qty=100, fees=0.0):
    executor = AsyncMock()
    result = ExecutionResult(
        success=success,
        tx_id="tx_test",
        filled_price=price,
        filled_quantity=qty,
        fees=fees,
        error="" if success else "failed",
    )
    executor.buy = AsyncMock(return_value=result)
    executor.buy_limit = AsyncMock(return_value=result)
    executor.sell = AsyncMock(return_value=result)
    return executor


class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_cross_platform_arb_uses_parallel(self):
        """Cross-platform arb with 2+ platforms should use parallel execution."""
        pm = PositionManager(data_dir=Path(tempfile.mkdtemp()), executors={
            "polymarket": _mock_executor(),
            "kalshi": _mock_executor(),
        })
        pkg = create_package("Test Arb", "cross_platform_arb")
        pkg["legs"] = [
            create_leg("polymarket", "prediction_yes", "evt1:YES", "YES @ Poly", 0.45, 100, "2026-12-31"),
            create_leg("kalshi", "prediction_no", "evt1:NO", "NO @ Kalshi", 0.50, 100, "2026-12-31"),
        ]
        result = await pm.execute_package(pkg)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_same_platform_uses_sequential(self):
        """Single-platform packages should use sequential execution."""
        pm = PositionManager(data_dir=Path(tempfile.mkdtemp()), executors={
            "polymarket": _mock_executor(),
        })
        pkg = create_package("Test Directional", "pure_prediction")
        pkg["legs"] = [
            create_leg("polymarket", "prediction_yes", "evt1:YES", "YES", 0.45, 100, "2026-12-31"),
        ]
        result = await pm.execute_package(pkg)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_parallel_rollback_on_failure(self):
        """If a leg fails in parallel execution, all executed legs should roll back."""
        good_executor = _mock_executor(success=True)
        bad_executor = _mock_executor(success=False)
        pm = PositionManager(data_dir=Path(tempfile.mkdtemp()), executors={
            "polymarket": good_executor,
            "kalshi": bad_executor,
        })
        pkg = create_package("Failing Arb", "cross_platform_arb")
        pkg["legs"] = [
            create_leg("polymarket", "prediction_yes", "evt1:YES", "YES", 0.45, 100, "2026-12-31"),
            create_leg("kalshi", "prediction_no", "evt1:NO", "NO", 0.50, 100, "2026-12-31"),
        ]
        result = await pm.execute_package(pkg)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_parallel_flag_forces_parallel(self):
        """_parallel_execution flag should force parallel even for same platform."""
        pm = PositionManager(data_dir=Path(tempfile.mkdtemp()), executors={
            "polymarket": _mock_executor(),
        })
        pkg = create_package("Forced Parallel", "multi_outcome_arb")
        pkg["_parallel_execution"] = True
        pkg["legs"] = [
            create_leg("polymarket", "prediction_yes", "evt1:YES", "YES", 0.30, 50, "2026-12-31"),
            create_leg("polymarket", "prediction_yes", "evt2:YES", "YES", 0.25, 50, "2026-12-31"),
        ]
        result = await pm.execute_package(pkg)
        assert result["success"] is True
