"""Tests for position manager."""
import json, pytest, asyncio
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
