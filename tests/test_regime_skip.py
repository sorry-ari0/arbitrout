# tests/test_regime_skip.py
"""Tests for regime penalty — skip trades entirely instead of min-sizing."""
import tempfile
from pathlib import Path
from positions.auto_trader import AutoTrader
from positions.position_manager import PositionManager


class TestRegimeSkip:
    def _make_trader(self, tmp_path):
        pm = PositionManager(data_dir=tmp_path, executors={})
        trader = AutoTrader(pm, scanner=None)
        return trader

    def test_normal_regime_returns_positive_size(self, tmp_path):
        """With regime_penalty=1.0, _kelly_size should return a trade size."""
        trader = self._make_trader(tmp_path)
        trader._regime_penalty = 1.0
        size = trader._kelly_size("pure_prediction", remaining_budget=500.0,
                                   implied_prob=0.70, spread_pct=15.0)
        assert size > 0

    def test_bad_regime_returns_zero(self, tmp_path):
        """With regime_penalty<1.0, _kelly_size should return 0 (skip the trade)."""
        trader = self._make_trader(tmp_path)
        trader._regime_penalty = 0.5
        size = trader._kelly_size("pure_prediction", remaining_budget=500.0,
                                   implied_prob=0.70, spread_pct=15.0)
        assert size == 0

    def test_regime_recovery_resumes_trading(self, tmp_path):
        """After regime_penalty returns to 1.0, trades should resume."""
        trader = self._make_trader(tmp_path)
        trader._regime_penalty = 0.5
        assert trader._kelly_size("pure_prediction", 500.0, 0.70, 15.0) == 0

        trader._regime_penalty = 1.0
        assert trader._kelly_size("pure_prediction", 500.0, 0.70, 15.0) > 0

    def test_arb_bypasses_regime_penalty(self, tmp_path):
        """Arb strategies (guaranteed profit) should bypass regime penalty."""
        trader = self._make_trader(tmp_path)
        trader._regime_penalty = 0.5
        # Speculative should return 0
        assert trader._kelly_size("pure_prediction", 500.0, 0.70, 15.0) == 0
        # Arb should still return positive (bypass_regime=True)
        size = trader._kelly_size("multi_outcome_arb", 500.0, spread_pct=15.0, bypass_regime=True)
        assert size > 0
