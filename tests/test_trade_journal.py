"""Tests for positions.trade_journal — TradeJournal record, performance, persistence."""
import tempfile
import time
from pathlib import Path
from positions.trade_journal import TradeJournal


def _make_closed_package(name, strategy, total_cost, current_value, legs=None):
    """Helper to construct a closed package dict for journal recording."""
    if legs is None:
        entry_price = 0.40
        quantity = total_cost / entry_price
        exit_price = current_value / quantity
        legs = [{
            "leg_id": "leg_test",
            "platform": "polymarket",
            "type": "prediction_yes",
            "asset_id": "test-asset",
            "entry_price": entry_price,
            "exit_price": exit_price,
            "current_price": exit_price,
            "quantity": quantity,
            "cost": total_cost,
            "status": "closed",
            "tx_id": "paper_abc123",
        }]
    return {
        "id": f"pkg_{name.replace(' ', '_').lower()}",
        "name": name,
        "strategy_type": strategy,
        "ai_strategy": "balanced",
        "status": "closed",
        "legs": legs,
        "exit_rules": [],
        "total_cost": total_cost,
        "current_value": current_value,
        "peak_value": max(total_cost, current_value),
        "created_at": time.time() - 3600,  # 1 hour ago
        "updated_at": time.time(),
    }


class TestJournal:
    def test_record_close(self):
        """Record a mock package close and verify entry fields."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(Path(tmp))

            pkg = _make_closed_package("Win Trade", "pure_prediction", 100.0, 120.0)
            entry = journal.record_close(pkg, exit_trigger="target_hit")

            assert entry is not None
            assert entry["id"].startswith("journal_")
            assert entry["package_id"] == pkg["id"]
            assert entry["name"] == "Win Trade"
            assert entry["strategy_type"] == "pure_prediction"
            assert entry["total_cost"] == 100.0
            assert entry["exit_value"] == 120.0
            assert entry["pnl"] == 20.0
            assert entry["pnl_pct"] == 20.0
            assert entry["outcome"] == "win"
            assert entry["exit_trigger"] == "target_hit"
            assert entry["mode"] == "paper"  # tx_id starts with "paper_"
            assert "hold_duration_hours" in entry
            assert "closed_at" in entry
            assert len(entry["legs"]) == 1

    def test_performance(self):
        """Record 2 trades (one win, one loss), verify aggregate stats."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(Path(tmp))

            # Win: cost=100, value=150 -> pnl=+50
            win_pkg = _make_closed_package("Winner", "cross_platform_arb", 100.0, 150.0)
            journal.record_close(win_pkg, exit_trigger="target_hit")

            # Loss: cost=100, value=70 -> pnl=-30
            loss_pkg = _make_closed_package("Loser", "pure_prediction", 100.0, 70.0)
            journal.record_close(loss_pkg, exit_trigger="stop_loss")

            perf = journal.get_performance()
            assert perf["total_trades"] == 2
            assert perf["wins"] == 1
            assert perf["losses"] == 1
            assert perf["win_rate"] == 0.5
            assert perf["total_pnl"] == 20.0  # 50 - 30 = 20
            assert perf["total_invested"] == 200.0
            assert perf["avg_pnl_per_trade"] == 10.0
            assert perf["best_trade"]["name"] == "Winner"
            assert perf["worst_trade"]["name"] == "Loser"

            # Per-strategy breakdown
            assert "cross_platform_arb" in perf["by_strategy"]
            assert "pure_prediction" in perf["by_strategy"]

            # Per-trigger breakdown
            assert "target_hit" in perf["by_trigger"]
            assert "stop_loss" in perf["by_trigger"]

    def test_persistence(self):
        """Save journal, create a new instance, verify entries loaded."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            # Create journal and record a trade
            j1 = TradeJournal(data_dir)
            pkg = _make_closed_package("Persist Trade", "pure_prediction", 80.0, 90.0)
            j1.record_close(pkg, exit_trigger="manual")

            assert len(j1.entries) == 1
            assert (data_dir / "trade_journal.json").exists()

            # Create new journal instance — should load from file
            j2 = TradeJournal(data_dir)
            assert len(j2.entries) == 1
            assert j2.entries[0]["name"] == "Persist Trade"
            assert j2.entries[0]["pnl"] == 10.0
            assert j2.entries[0]["outcome"] == "win"

    def test_get_recent(self):
        """get_recent returns entries sorted by closed_at descending."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(Path(tmp))

            for i in range(5):
                pkg = _make_closed_package(f"Trade {i}", "pure_prediction", 100.0, 100.0 + i * 10)
                journal.record_close(pkg)

            recent = journal.get_recent(limit=3)
            assert len(recent) == 3
            # Should be newest first
            assert recent[0]["closed_at"] >= recent[1]["closed_at"]
            assert recent[1]["closed_at"] >= recent[2]["closed_at"]

    def test_empty_performance(self):
        """get_performance with no trades returns a message."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(Path(tmp))
            perf = journal.get_performance()
            assert perf["total_trades"] == 0
            assert "message" in perf
