# tests/test_arb_budget.py
"""Tests for reserved arb budget — arbs should always have capital access."""
import tempfile
from pathlib import Path
from positions.auto_trader import AutoTrader, ARB_BUDGET_RESERVE_PCT
from positions.position_manager import PositionManager, create_package, create_leg


class TestArbBudget:
    def _make_trader_at_capacity(self, tmp_path, exposure_pct=0.95):
        """Create a trader near max exposure with directional bets."""
        pm = PositionManager(data_dir=tmp_path, executors={})
        trader = AutoTrader(pm, scanner=None, initial_bankroll=2000.0)
        # Fill up with directional bets
        total_to_fill = trader._max_total_exposure * exposure_pct
        pkg = create_package("Filler Trade", "pure_prediction")
        pkg["legs"].append(create_leg("polymarket", "prediction_yes",
                                       "filler:YES", "Filler", 0.5, total_to_fill))
        pkg["total_cost"] = total_to_fill
        pm.add_package(pkg)
        return trader, pm

    def test_arb_budget_constant_exists(self):
        """ARB_BUDGET_RESERVE_PCT should be 0.40."""
        assert ARB_BUDGET_RESERVE_PCT == 0.40

    def test_arb_budget_reserved_when_near_capacity(self, tmp_path):
        """At 95% exposure from directional bets, arb budget should still exist."""
        trader, pm = self._make_trader_at_capacity(tmp_path, exposure_pct=0.95)
        open_pkgs = pm.list_packages("open")
        total_exposure = sum(p.get("total_cost", 0) for p in open_pkgs)

        # Directional budget should be exhausted
        directional_budget = trader._max_total_exposure * (1 - ARB_BUDGET_RESERVE_PCT) - total_exposure
        assert directional_budget < trader._min_trade_size

        # Arb budget (full remaining) should still have room
        arb_budget = trader._max_total_exposure - total_exposure
        assert arb_budget > trader._min_trade_size
