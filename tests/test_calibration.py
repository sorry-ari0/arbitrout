# tests/test_calibration.py
import pytest
from unittest.mock import MagicMock

def make_calibration_engine():
    from positions.calibration import CalibrationEngine

    mock_eval = MagicMock()
    mock_eval.get_calibration.return_value = {
        "low_score": {"total_skips": 20, "resolved": 10, "correct_skips": 8, "missed_opportunities": 2, "correct_skip_rate": 0.80},
        "max_concurrent": {"total_skips": 15, "resolved": 10, "correct_skips": 4, "missed_opportunities": 6, "correct_skip_rate": 0.40},
    }
    mock_eval.get_missed_opportunities.return_value = [
        {"action_reason": "max_concurrent", "actual_pnl_pct": 15.0},
        {"action_reason": "max_concurrent", "actual_pnl_pct": 22.0},
    ]

    mock_journal = MagicMock()
    mock_journal.get_performance.return_value = {
        "total_trades": 20,
        "total_fees": 40.0,
        "total_invested": 2000.0,
        "fee_drag_pct": 2.0,
        "by_trigger": {
            "trailing_stop": {"trades": 8, "wins": 0, "pnl": -72.0, "win_rate": 0.0},
            "target_hit": {"trades": 5, "wins": 4, "pnl": 65.0, "win_rate": 0.80},
        },
    }
    mock_journal.get_performance_by_hold_duration.return_value = {
        "0-6h": {"trades": 5, "wins": 0, "pnl": -30.0, "avg_pnl": -6.0, "win_rate": 0.0},
        "24h-3d": {"trades": 8, "wins": 4, "pnl": 40.0, "avg_pnl": 5.0, "win_rate": 0.50},
    }
    mock_journal.entries = [
        {"exit_order_type": "limit_filled"},
        {"exit_order_type": "limit_filled"},
        {"exit_order_type": "fok_fallback"},
        {"exit_order_type": "fok_direct"},
    ]

    return CalibrationEngine(mock_eval, mock_journal)

def test_generate_report_has_all_sections():
    ce = make_calibration_engine()
    report = ce.generate_report()
    assert "entry_calibration" in report
    assert "exit_calibration" in report
    assert "hold_duration_analysis" in report
    assert "fee_analysis" in report
    assert "generated_at" in report

def test_low_correct_skip_rate_flagged():
    ce = make_calibration_engine()
    report = ce.generate_report()
    suggestion = report["entry_calibration"]["max_concurrent"]["suggestion"]
    assert "REVIEW" in suggestion

def test_zero_win_rate_trigger_flagged():
    ce = make_calibration_engine()
    report = ce.generate_report()
    suggestion = report["exit_calibration"]["trailing_stop"]["suggestion"]
    assert "WIDEN" in suggestion

def test_limit_fill_rate_calculated():
    ce = make_calibration_engine()
    report = ce.generate_report()
    # 2 limit_filled out of 3 limit attempts (limit_filled + fok_fallback)
    assert report["fee_analysis"]["limit_fill_rate"] == pytest.approx(0.67, abs=0.01)
