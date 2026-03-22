"""Tests for bankroll-relative position sizing."""
import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock


class TestJournalMode:
    def test_paper_mode_uses_paper_file(self, tmp_path):
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="paper")
        tj.save()
        assert (tmp_path / "trade_journal_paper.json").exists()
        assert not (tmp_path / "trade_journal_live.json").exists()

    def test_live_mode_uses_live_file(self, tmp_path):
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="live")
        tj.save()
        assert (tmp_path / "trade_journal_live.json").exists()
        assert not (tmp_path / "trade_journal_paper.json").exists()

    def test_default_mode_is_paper(self, tmp_path):
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path)
        tj.save()
        assert (tmp_path / "trade_journal_paper.json").exists()

    def test_migration_renames_old_file(self, tmp_path):
        old_path = tmp_path / "trade_journal.json"
        old_path.write_text(json.dumps({"entries": [{"pnl": 5.0}]}))
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="paper")
        assert not old_path.exists()
        assert (tmp_path / "trade_journal_paper.json").exists()
        assert len(tj.entries) == 1

    def test_migration_skips_if_paper_exists(self, tmp_path):
        old_path = tmp_path / "trade_journal.json"
        old_path.write_text(json.dumps({"entries": [{"pnl": -10.0}]}))
        paper_path = tmp_path / "trade_journal_paper.json"
        paper_path.write_text(json.dumps({"entries": [{"pnl": 5.0}, {"pnl": 3.0}]}))
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="paper")
        assert len(tj.entries) == 2


class TestCumulativePnl:
    def test_empty_journal_returns_zero(self, tmp_path):
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="paper")
        assert tj.get_cumulative_pnl() == 0.0

    def test_sums_all_pnl(self, tmp_path):
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="live")
        tj.entries = [
            {"pnl": 10.0, "outcome": "win"},
            {"pnl": -5.0, "outcome": "loss"},
            {"pnl": 3.0, "outcome": "win"},
        ]
        assert tj.get_cumulative_pnl() == 8.0

    def test_handles_missing_pnl_field(self, tmp_path):
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="paper")
        tj.entries = [{"outcome": "win"}, {"pnl": 5.0}]
        assert tj.get_cumulative_pnl() == 5.0


class TestBankrollDerivedLimits:
    def _make_trader(self, initial_bankroll=20.0, cumulative_pnl=0.0):
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        journal = MagicMock()
        journal.get_cumulative_pnl = MagicMock(return_value=cumulative_pnl)
        pm.trade_journal = journal
        pm.list_packages = MagicMock(return_value=[])
        trader = AutoTrader(pm, initial_bankroll=initial_bankroll)
        return trader

    def test_current_bankroll_includes_pnl(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=5.0)
        assert trader._get_current_bankroll() == 25.0

    def test_current_bankroll_decreases_with_losses(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=-8.0)
        assert trader._get_current_bankroll() == 12.0

    def test_max_trade_size_scales(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=0.0)
        bankroll = trader._get_current_bankroll()
        assert bankroll * 0.025 == pytest.approx(0.50)

    def test_min_trade_size_has_floor(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=0.0)
        bankroll = trader._get_current_bankroll()
        assert max(1.0, bankroll * 0.005) == 1.0

    def test_max_total_exposure_scales(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=0.0)
        bankroll = trader._get_current_bankroll()
        assert bankroll * 0.175 == pytest.approx(3.50)

    def test_kelly_portfolio_cap_scales(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=0.0)
        bankroll = trader._get_current_bankroll()
        assert bankroll * 0.40 == pytest.approx(8.0)

    def test_paper_mode_default_bankroll(self):
        trader = self._make_trader(initial_bankroll=2000.0, cumulative_pnl=0.0)
        assert trader._get_current_bankroll() == 2000.0

    def test_bankroll_grows_after_wins(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=30.0)
        bankroll = trader._get_current_bankroll()
        assert bankroll == 50.0
        assert bankroll * 0.025 == pytest.approx(1.25)
        assert bankroll * 0.175 == pytest.approx(8.75)

    def test_kelly_size_uses_bankroll_derived_limits(self):
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        journal = MagicMock()
        journal.get_cumulative_pnl = MagicMock(return_value=0.0)
        pm.trade_journal = journal
        pm.list_packages = MagicMock(return_value=[])
        trader = AutoTrader(pm, initial_bankroll=20.0)
        trader._refresh_limits()
        sized = trader._kelly_size("cross_platform_arb", remaining_budget=20.0,
                                    implied_prob=0.5, spread_pct=12.0)
        # With $20 bankroll: max_trade=$0.50, min_trade=$1.00 (floor dominates)
        # Kelly returns min_trade_size since floor > max at small bankrolls
        assert sized >= trader._min_trade_size
        assert sized == trader._min_trade_size
