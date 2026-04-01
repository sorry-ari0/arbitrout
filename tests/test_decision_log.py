"""Tests for durable decision-log reconciliation."""
import json
import tempfile
from pathlib import Path

from positions.decision_log import DecisionLogger


def _make_package(strategy="pure_prediction", status="open"):
    return {
        "id": "pkg_test123",
        "name": "News: Test Trade" if strategy == "news_driven" else "Auto: Test Trade",
        "strategy_type": strategy,
        "status": status,
        "total_cost": 100.0,
        "current_value": 130.0,
        "created_at": 1_700_000_000,
        "updated_at": 1_700_007_200,
        "_bet_side": "YES",
        "_entry_conviction": 0.77,
        "_news_confidence": 8,
        "_news_urgency": "high",
        "_news_reasoning": "Reconciled test reasoning.",
        "legs": [
            {
                "leg_id": "leg_1",
                "platform": "polymarket",
                "type": "prediction_yes",
                "asset_id": "market-1:YES",
                "asset_label": "YES - Test Market",
                "entry_price": 0.77,
                "cost": 100.0,
                "expiry": "2026-12-31",
                "status": "closed" if status == "closed" else "open",
                "exit_trigger": "target_profit" if status == "closed" else "",
            }
        ],
        "execution_log": [
            {"action": "buy", "timestamp": 1_700_000_000},
            {"action": "sell", "trigger": "target_profit", "timestamp": 1_700_007_200},
        ] if status == "closed" else [{"action": "buy", "timestamp": 1_700_000_000}],
    }


class TestDecisionLogReconciliation:
    def test_reconcile_closed_news_package_backfills_missing_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision_log.jsonl"
            logger = DecisionLogger(str(path))
            pkg = _make_package(strategy="news_driven", status="closed")
            journal_entries = [{
                "package_id": pkg["id"],
                "closed_at": 1_700_007_200,
                "exit_trigger": "target_profit",
                "pnl": 30.0,
                "exit_value": 130.0,
            }]

            counts = logger.reconcile_packages([pkg], journal_entries)

            assert counts == {"trade_opened": 1, "news_trade": 1, "exit_complete": 1}
            entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            assert [e["type"] for e in entries] == ["trade_opened", "news_trade", "exit_complete"]
            assert entries[0]["timestamp"] == "2023-11-14T22:13:20Z"
            assert "recorded_at" in entries[0]
            assert entries[0]["reconciled"] is True
            assert entries[1]["timestamp"] == "2023-11-14T22:13:20Z"
            assert entries[1]["reconciled"] is True
            assert entries[2]["timestamp"] == "2023-11-15T00:13:20Z"
            assert entries[2]["trigger"] == "target_profit"
            assert entries[2]["reconciled"] is True

    def test_reconcile_is_idempotent_against_existing_log_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision_log.jsonl"
            logger = DecisionLogger(str(path))
            pkg = _make_package(strategy="news_driven", status="closed")
            journal_entries = [{
                "package_id": pkg["id"],
                "closed_at": 1_700_007_200,
                "exit_trigger": "target_profit",
                "pnl": 30.0,
                "exit_value": 130.0,
            }]

            first = logger.reconcile_packages([pkg], journal_entries)
            second = logger.reconcile_packages([pkg], journal_entries)

            assert first == {"trade_opened": 1, "news_trade": 1, "exit_complete": 1}
            assert second == {"trade_opened": 0, "news_trade": 0, "exit_complete": 0}
            entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            assert len(entries) == 3
