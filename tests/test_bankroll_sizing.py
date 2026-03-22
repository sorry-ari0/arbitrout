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
