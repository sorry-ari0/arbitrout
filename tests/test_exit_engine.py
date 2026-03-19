"""Tests for exit engine heuristics."""
import pytest
from positions.exit_engine import evaluate_heuristics
from positions.position_manager import create_package, create_leg, create_exit_rule


def _make_pkg(strategy="cross_platform_arb"):
    pkg = create_package("Test", strategy)
    l1 = create_leg("polymarket","prediction_yes","tok1:YES","BTC>100k",0.60,10.0,"2026-12-31")
    l2 = create_leg("kalshi","prediction_no","tick1:NO","BTC>100k",0.35,10.0,"2026-12-31")
    pkg["legs"] = [l1, l2]
    pkg["exit_rules"].append(create_exit_rule("trailing_stop", {"bound_min":5,"bound_max":25,"current":12,"peak_value":20.0}))
    return pkg


class TestHeuristics:
    def test_spread_inversion_is_safety(self):
        pkg = _make_pkg()
        pkg["legs"][0]["current_price"] = 0.75
        pkg["legs"][1]["current_price"] = 0.30  # 1.0 - (0.75+0.30) = -0.05
        triggers = evaluate_heuristics(pkg)
        assert any(t.get("safety_override") for t in triggers)

    def test_new_ath_detected(self):
        pkg = _make_pkg()
        pkg["peak_value"] = 20.0
        pkg["current_value"] = 25.0  # > peak 20.0
        triggers = evaluate_heuristics(pkg)
        assert any(t["trigger_id"] == 5 for t in triggers)

    def test_time_24h_fires_review(self):
        """time_24h fires as a soft review trigger (not safety override).

        Changed from safety_override=True to False — prediction markets
        move most in final hours, so early exits destroy value.
        """
        pkg = _make_pkg()
        from datetime import datetime, timedelta
        tomorrow = (datetime.now() + timedelta(hours=20)).strftime("%Y-%m-%d")
        for l in pkg["legs"]: l["expiry"] = tomorrow
        triggers = evaluate_heuristics(pkg)
        time_triggers = [t for t in triggers if t["name"] == "time_24h"]
        assert len(time_triggers) >= 1
        assert time_triggers[0]["safety_override"] is False
        assert time_triggers[0]["action"] == "review"
