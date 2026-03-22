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
        pkg["legs"][0]["current_price"] = 0.80
        pkg["legs"][1]["current_price"] = 0.30  # combined 1.10 > 1.05 fee-aware threshold
        triggers = evaluate_heuristics(pkg)
        assert any(t.get("safety_override") for t in triggers)

    def test_new_ath_detected(self):
        pkg = _make_pkg()
        pkg["peak_value"] = 20.0
        pkg["current_value"] = 25.0  # > peak 20.0
        triggers = evaluate_heuristics(pkg)
        assert any(t["trigger_id"] == 5 for t in triggers)

    def test_expiry_triggers_fire_as_review(self):
        """Expiry triggers fire as soft review triggers (not safety override).

        Changed from safety_override=True to False — prediction markets
        move most in final hours, so early exits destroy value.

        Note: strftime('%Y-%m-%d') truncates to midnight, so actual hours_left
        depends on time-of-day. We accept either time_6h or time_24h since both
        are soft review triggers with identical properties.
        """
        pkg = _make_pkg()
        from datetime import datetime, timedelta
        tomorrow = (datetime.now() + timedelta(hours=20)).strftime("%Y-%m-%d")
        for l in pkg["legs"]: l["expiry"] = tomorrow
        triggers = evaluate_heuristics(pkg)
        time_triggers = [t for t in triggers if t["name"] in ("time_24h", "time_6h")]
        assert len(time_triggers) >= 1
        assert time_triggers[0]["safety_override"] is False
        assert time_triggers[0]["action"] == "review"

    # ── Minimum hold period tests ──────────────────────────────────────────

    def test_min_hold_suppresses_trailing_stop(self):
        """During hold period, trailing_stop should be suppressed."""
        import time
        pkg = _make_pkg(strategy="pure_prediction")
        pkg["_min_hold_until"] = time.time() + 86400
        pkg["peak_value"] = 20.0
        pkg["current_value"] = 5.0  # massive drawdown
        triggers = evaluate_heuristics(pkg)
        assert not any(t["name"] == "trailing_stop" for t in triggers)

    def test_min_hold_allows_stop_loss(self):
        """During hold period, stop_loss should still fire."""
        import time
        pkg = _make_pkg()
        pkg["_min_hold_until"] = time.time() + 86400
        pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -40}))
        pkg["total_cost"] = 10.0
        pkg["current_value"] = 3.0  # -70% loss
        triggers = evaluate_heuristics(pkg)
        assert any(t["name"] == "stop_loss" for t in triggers)

    def test_min_hold_allows_safety_override(self):
        """During hold period, spread_inversion (safety) should still fire."""
        import time
        pkg = _make_pkg()
        pkg["_min_hold_until"] = time.time() + 86400
        pkg["legs"][0]["current_price"] = 0.80
        pkg["legs"][1]["current_price"] = 0.30  # combined 1.10 > 1.05
        triggers = evaluate_heuristics(pkg)
        assert any(t.get("safety_override") for t in triggers)

    def test_min_hold_allows_target_hit(self):
        """During hold period, target_hit should still fire."""
        import time
        pkg = _make_pkg()
        pkg["_min_hold_until"] = time.time() + 86400
        pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 20}))
        pkg["total_cost"] = 10.0
        pkg["current_value"] = 15.0  # +50%
        triggers = evaluate_heuristics(pkg)
        assert any(t["name"] == "target_hit" for t in triggers)

    def test_expired_hold_allows_all_triggers(self):
        """After hold period expires, all triggers fire normally."""
        import time
        pkg = _make_pkg(strategy="pure_prediction")
        pkg["_min_hold_until"] = time.time() - 1  # already expired
        # Set entry prices >= 0.60 so trailing stop is eligible (entries < 0.60 skip it)
        for leg in pkg["legs"]:
            leg["entry_price"] = 0.70
        pkg["peak_value"] = 20.0
        pkg["current_value"] = 5.0
        triggers = evaluate_heuristics(pkg)
        assert any(t["name"] == "trailing_stop" for t in triggers)


import asyncio
from unittest.mock import MagicMock, AsyncMock


class TestAutoExecuteTriggers:
    """Test that _auto_execute_triggers only executes target_hit mechanically."""

    def _make_engine(self):
        pm = MagicMock()
        pm.exit_leg = AsyncMock(return_value={"success": True})
        pm.list_packages = MagicMock(return_value=[])
        from positions.exit_engine import ExitEngine
        engine = ExitEngine(pm)
        return engine, pm

    def test_target_hit_executes(self):
        """target_hit should auto-execute."""
        engine, pm = self._make_engine()
        pkg = _make_pkg()
        triggers = [{"name": "target_hit", "action": "full_exit",
                      "details": "Target reached"}]
        asyncio.run(engine._auto_execute_triggers(pkg, triggers))
        assert pm.exit_leg.called

    def test_stop_loss_does_not_execute(self):
        """stop_loss should NOT auto-execute after fee elimination change."""
        engine, pm = self._make_engine()
        pkg = _make_pkg()
        triggers = [{"name": "stop_loss", "action": "full_exit",
                      "details": "Stop hit"}]
        asyncio.run(engine._auto_execute_triggers(pkg, triggers))
        assert not pm.exit_leg.called

    def test_trailing_stop_does_not_execute(self):
        """trailing_stop should NOT auto-execute after fee elimination change."""
        engine, pm = self._make_engine()
        pkg = _make_pkg()
        triggers = [{"name": "trailing_stop", "action": "full_exit",
                      "details": "Trail hit"}]
        asyncio.run(engine._auto_execute_triggers(pkg, triggers))
        assert not pm.exit_leg.called


class TestSafetyOverrideLimitOrders:
    """Safety overrides should use limit orders (0% maker fee)."""

    def test_safety_override_passes_use_limit(self):
        """The exit_engine safety override loop should call exit_leg(use_limit=True)."""
        pm = MagicMock()
        pm.exit_leg = AsyncMock(return_value={"success": True})
        pm.list_packages = MagicMock(return_value=[])
        pm.resolve_pending_order = AsyncMock(return_value={"success": True})
        pm.packages = {}

        from positions.exit_engine import ExitEngine
        engine = ExitEngine(pm)

        # Build a package that will trigger spread_inversion (safety override)
        pkg = _make_pkg()
        pkg["legs"][0]["current_price"] = 0.80
        pkg["legs"][1]["current_price"] = 0.30  # combined > 1.05

        pm.list_packages.return_value = [pkg]
        pm.packages = {pkg["id"]: pkg}

        # Run one tick
        asyncio.run(engine._tick())

        # Verify exit_leg was called AND with use_limit=True
        assert pm.exit_leg.called, "Safety override should have triggered exit_leg"
        for call in pm.exit_leg.call_args_list:
            _, kwargs = call
            assert kwargs.get("use_limit") is True, \
                f"Safety override should use limit orders, got: {kwargs}"
