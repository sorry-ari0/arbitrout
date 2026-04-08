"""Tests for position manager."""
import json, pytest, asyncio
from unittest.mock import MagicMock
from positions.position_manager import (
    PositionManager, create_package, create_leg, create_exit_rule,
    STATUS_OPEN, STATUS_CLOSED,
)

@pytest.fixture
def manager(tmp_path):
    return PositionManager(data_dir=tmp_path, executors={})

class TestHelpers:
    def test_create_package(self):
        p = create_package("Test", "cross_platform_arb")
        assert p["id"].startswith("pkg_") and p["status"] == STATUS_OPEN
    def test_create_leg(self):
        l = create_leg("polymarket", "prediction_yes", "tok:YES", "BTC>100k", 0.65, 10.0, "2026-12-31")
        assert l["cost"] == 10.0 and l["quantity"] == pytest.approx(10/0.65, rel=0.01)
        assert l["expiry"] == "2026-12-31"

class TestPersistence:
    def test_save_load(self, manager):
        p = create_package("P", "pure_prediction")
        manager.packages[p["id"]] = p; manager.save()
        m2 = PositionManager(data_dir=manager.data_dir, executors={})
        assert p["id"] in m2.packages

class TestCRUD:
    def test_add_get(self, manager):
        p = create_package("G", "spot_plus_hedge"); manager.add_package(p)
        assert manager.get_package(p["id"])["name"] == "G"
    def test_close(self, manager):
        p = create_package("C", "pure_prediction"); manager.add_package(p)
        manager.close_package(p["id"])
        assert manager.packages[p["id"]]["status"] == STATUS_CLOSED

class TestPnL:
    def test_update_pnl(self, manager):
        p = create_package("PnL","pure_prediction")
        l = create_leg("polymarket","prediction_yes","tok:YES","T",0.50,10.0)
        p["legs"].append(l); manager.add_package(p)
        l["current_price"] = 0.70; l["current_value"] = l["quantity"] * 0.70
        manager.update_pnl(p["id"])
        assert manager.get_package(p["id"])["itm_status"] == "ITM"


class TestInsiderResolutionFeedback:
    def test_record_insider_resolutions_infers_binary_outcomes(self, manager):
        tracker = MagicMock()
        manager.insider_tracker = tracker
        pkg = {
            "legs": [
                {
                    "platform": "polymarket",
                    "asset_id": "cid_yes:YES",
                    "exit_price": 1.0,
                    "status": "closed",
                },
                {
                    "platform": "polymarket",
                    "asset_id": "cid_no:NO",
                    "exit_price": 0.0,
                    "status": "closed",
                },
                {
                    "platform": "kalshi",
                    "asset_id": "ignored:YES",
                    "exit_price": 1.0,
                    "status": "closed",
                },
            ]
        }

        manager._record_insider_resolutions(pkg)

        tracker.record_resolution.assert_any_call("cid_yes", "YES")
        tracker.record_resolution.assert_any_call("cid_no", "YES")
        assert tracker.record_resolution.call_count == 2
