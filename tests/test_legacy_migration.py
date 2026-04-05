"""Tests for legacy package migration and journal phase tagging (v2-fee-fix, 2026-03-22).

Covers:
- _migrate_legacy_packages() in server.py: flag injection, exit rule adjustment, idempotency
- _code_version tagging in trade_journal.py: new entries tagged, old entries untouched
"""
import json
import tempfile
import time
from pathlib import Path

import pytest

from positions.position_manager import (
    PositionManager, create_package, create_leg, create_exit_rule,
    STATUS_OPEN, STATUS_CLOSED,
)
from positions.trade_journal import TradeJournal
from server import _migrate_legacy_packages


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_legacy_package(name="Will BTC hit 100K?", strategy="pure_prediction",
                         entry_price=0.87, cost=50.0, expiry="2026-03-31"):
    """Create a package that looks like it was opened before the fee-fix overhaul.
    No _hold_to_resolution, no _category, no _use_limit_orders.
    Has old-style exit rules: 25% target, -40% stop, active trailing stop."""
    pkg = create_package(name, strategy)
    pkg["legs"].append(create_leg("polymarket", "prediction_no", "cond123:NO",
                                  f"{name} NO", entry_price, cost, expiry))
    pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 25}))
    pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -40}))
    pkg["exit_rules"].append(create_exit_rule("trailing_stop",
                                              {"current": 35, "bound_min": 15, "bound_max": 50}))
    return pkg


def _make_modern_package(name="Modern Trade", strategy="pure_prediction"):
    """Create a package with all post-fee-fix flags already set."""
    pkg = create_package(name, strategy)
    pkg["legs"].append(create_leg("polymarket", "prediction_no", "cond456:NO",
                                  f"{name} NO", 0.90, 50.0, "2026-04-01"))
    pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 10}))
    pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -60}))
    trail = create_exit_rule("trailing_stop", {"current": 35})
    trail["active"] = False
    pkg["exit_rules"].append(trail)
    pkg["_hold_to_resolution"] = True
    pkg["_use_limit_orders"] = True
    pkg["_category"] = "crypto"
    return pkg


@pytest.fixture
def pm(tmp_path):
    return PositionManager(data_dir=tmp_path, executors={})


# ─── Migration: Core Behavior ────────────────────────────────────────────────

class TestMigrationCore:
    def test_legacy_package_gets_all_flags(self, pm):
        """A package with no _hold_to_resolution should get all new flags."""
        pkg = _make_legacy_package()
        pm.add_package(pkg)

        _migrate_legacy_packages(pm)

        migrated = pm.get_package(pkg["id"])
        assert migrated["_hold_to_resolution"] is True
        assert migrated["_use_limit_orders"] is True
        assert migrated["_category"] == "crypto"  # "BTC" in title

    def test_target_profit_reduced(self, pm):
        """target_profit threshold should drop from 25% to 10%."""
        pkg = _make_legacy_package()
        pm.add_package(pkg)

        _migrate_legacy_packages(pm)

        target_rule = next(r for r in pm.get_package(pkg["id"])["exit_rules"]
                          if r["type"] == "target_profit")
        assert target_rule["params"]["target_pct"] == 10

    def test_stop_loss_widened(self, pm):
        """stop_loss threshold should widen from -40% to -60%."""
        pkg = _make_legacy_package()
        pm.add_package(pkg)

        _migrate_legacy_packages(pm)

        stop_rule = next(r for r in pm.get_package(pkg["id"])["exit_rules"]
                         if r["type"] == "stop_loss")
        assert stop_rule["params"]["stop_pct"] == -60

    def test_trailing_stop_deactivated(self, pm):
        """trailing_stop should be set to active=False."""
        pkg = _make_legacy_package()
        pm.add_package(pkg)

        _migrate_legacy_packages(pm)

        trail_rule = next(r for r in pm.get_package(pkg["id"])["exit_rules"]
                          if r["type"] == "trailing_stop")
        assert trail_rule["active"] is False

    def test_updated_at_refreshed(self, pm):
        """Migration should update the package's updated_at timestamp."""
        pkg = _make_legacy_package()
        old_time = pkg["updated_at"]
        pm.add_package(pkg)

        time.sleep(0.01)
        _migrate_legacy_packages(pm)

        assert pm.get_package(pkg["id"])["updated_at"] > old_time


# ─── Migration: Idempotency ──────────────────────────────────────────────────

class TestMigrationIdempotency:
    def test_modern_package_untouched(self, pm):
        """Packages that already have _hold_to_resolution should be skipped."""
        pkg = _make_modern_package()
        pm.add_package(pkg)

        original_time = pkg["updated_at"]
        time.sleep(0.01)
        _migrate_legacy_packages(pm)

        after = pm.get_package(pkg["id"])
        assert after["updated_at"] == original_time  # unchanged

    def test_double_migration_safe(self, pm):
        """Running migration twice should not change anything the second time."""
        pkg = _make_legacy_package()
        pm.add_package(pkg)

        _migrate_legacy_packages(pm)
        first_state = json.dumps(pm.get_package(pkg["id"]), sort_keys=True)

        time.sleep(0.01)
        _migrate_legacy_packages(pm)
        second_state = json.dumps(pm.get_package(pkg["id"]), sort_keys=True)

        # Only updated_at could differ, but since _hold_to_resolution is already set,
        # the second run should skip entirely — so states should be identical
        assert first_state == second_state

    def test_mixed_legacy_and_modern(self, pm):
        """Only legacy packages should be migrated; modern ones left alone."""
        legacy = _make_legacy_package("Old BTC Trade")
        modern = _make_modern_package("New ETH Trade")
        pm.add_package(legacy)
        pm.add_package(modern)

        modern_time = modern["updated_at"]
        time.sleep(0.01)
        _migrate_legacy_packages(pm)

        # Legacy should be migrated
        assert pm.get_package(legacy["id"])["_hold_to_resolution"] is True
        assert pm.get_package(legacy["id"])["_category"] == "crypto"

        # Modern should be untouched
        assert pm.get_package(modern["id"])["updated_at"] == modern_time


# ─── Migration: Category Detection ───────────────────────────────────────────

class TestMigrationCategoryDetection:
    @pytest.mark.parametrize("title,expected_category", [
        ("Will BTC hit 100K?", "crypto"),
        ("Bitcoin above $95000", "crypto"),
        ("ETH price above $5000 by June", "crypto"),
        ("Will Trump win the election?", "politics"),
        ("Senate race outcome", "politics"),
        ("NCAA basketball championship score", "sports"),
        ("NFL Super Bowl winner", "sports"),
        ("Fed interest rate decision", "finance"),
        ("GDP growth above 3%", "finance"),
        ("Temperature in NYC above 80F", "weather"),
        ("Hurricane landfall prediction", "weather"),
        ("Random obscure market question", "other"),
    ])
    def test_category_from_title(self, pm, title, expected_category):
        """Category should be detected from title keywords."""
        pkg = _make_legacy_package(name=title)
        pm.add_package(pkg)

        _migrate_legacy_packages(pm)

        assert pm.get_package(pkg["id"])["_category"] == expected_category


# ─── Migration: Edge Cases ───────────────────────────────────────────────────

class TestMigrationEdgeCases:
    def test_no_open_packages(self, pm):
        """Migration on empty portfolio should be a no-op (no crash)."""
        _migrate_legacy_packages(pm)  # should not raise

    def test_closed_legacy_packages_ignored(self, pm):
        """Closed packages should not be migrated (list_packages filters by open)."""
        pkg = _make_legacy_package()
        pkg["status"] = STATUS_CLOSED
        pm.packages[pkg["id"]] = pkg
        pm.save()

        _migrate_legacy_packages(pm)

        # Should still lack new flags since it's closed
        assert pm.get_package(pkg["id"]).get("_hold_to_resolution") is None

    def test_package_with_no_exit_rules(self, pm):
        """Package with empty exit_rules should not crash during migration."""
        pkg = create_package("No Rules Trade", "pure_prediction")
        pkg["legs"].append(create_leg("polymarket", "prediction_no", "cond:NO",
                                      "Test NO", 0.90, 50.0))
        # No exit rules at all
        pkg["exit_rules"] = []
        pm.add_package(pkg)

        _migrate_legacy_packages(pm)

        migrated = pm.get_package(pkg["id"])
        assert migrated["_hold_to_resolution"] is True
        assert migrated["exit_rules"] == []  # still empty, no crash

    def test_package_with_extra_exit_rules(self, pm):
        """Only target_profit, stop_loss, trailing_stop rules should be modified.
        Other rules should be left untouched."""
        pkg = _make_legacy_package()
        # Add a non-standard rule
        pkg["exit_rules"].append(create_exit_rule("time_decay", {"days": 7}))
        pm.add_package(pkg)

        _migrate_legacy_packages(pm)

        rules = pm.get_package(pkg["id"])["exit_rules"]
        time_rule = next(r for r in rules if r["type"] == "time_decay")
        assert time_rule["params"]["days"] == 7  # untouched
        assert time_rule["active"] is True  # untouched

    def test_hold_to_resolution_explicitly_false_counts_as_set(self, pm):
        """If _hold_to_resolution is explicitly False (not None), skip migration.
        The flag's presence (even False) means the package was created post-fix."""
        pkg = _make_legacy_package()
        pkg["_hold_to_resolution"] = False  # explicitly set
        pm.add_package(pkg)

        original_target = next(r for r in pkg["exit_rules"]
                               if r["type"] == "target_profit")["params"]["target_pct"]
        _migrate_legacy_packages(pm)

        # Should be untouched — _hold_to_resolution is not None
        still_25 = next(r for r in pm.get_package(pkg["id"])["exit_rules"]
                        if r["type"] == "target_profit")["params"]["target_pct"]
        assert still_25 == original_target

    def test_persistence_after_migration(self, tmp_path):
        """Migration should persist to disk — new PM instance sees migrated flags."""
        pm1 = PositionManager(data_dir=tmp_path, executors={})
        pkg = _make_legacy_package()
        pm1.add_package(pkg)
        pkg_id = pkg["id"]

        _migrate_legacy_packages(pm1)

        # Create fresh PM from same directory
        pm2 = PositionManager(data_dir=tmp_path, executors={})
        reloaded = pm2.get_package(pkg_id)
        assert reloaded["_hold_to_resolution"] is True
        assert reloaded["_use_limit_orders"] is True
        assert reloaded["_category"] == "crypto"

    def test_multiple_legacy_packages(self, pm):
        """All legacy packages should be migrated, not just the first one."""
        pkgs = [
            _make_legacy_package("Will BTC hit 100K?"),
            _make_legacy_package("Trump election odds"),
            _make_legacy_package("Some obscure market"),
        ]
        for p in pkgs:
            pm.add_package(p)

        _migrate_legacy_packages(pm)

        for p in pkgs:
            migrated = pm.get_package(p["id"])
            assert migrated["_hold_to_resolution"] is True
            assert migrated["_use_limit_orders"] is True

        # Verify categories differ
        assert pm.get_package(pkgs[0]["id"])["_category"] == "crypto"
        assert pm.get_package(pkgs[1]["id"])["_category"] == "politics"
        assert pm.get_package(pkgs[2]["id"])["_category"] == "other"


# ─── Journal: Phase Tagging ──────────────────────────────────────────────────

def _make_closed_pkg(name, total_cost=100.0, current_value=120.0, pkg_id=None):
    """Helper to construct a closed package for journal recording."""
    entry_price = 0.40
    quantity = total_cost / entry_price
    exit_price = current_value / quantity
    return {
        "id": pkg_id or f"pkg_{name.replace(' ', '_').lower()}",
        "name": name,
        "strategy_type": "pure_prediction",
        "ai_strategy": "balanced",
        "status": "closed",
        "legs": [{
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
        }],
        "exit_rules": [],
        "total_cost": total_cost,
        "current_value": current_value,
        "peak_value": max(total_cost, current_value),
        "created_at": time.time() - 3600,
        "updated_at": time.time(),
    }


class TestJournalPhaseTagging:
    def test_new_entry_has_code_version(self):
        """New journal entries should have _code_version = 'v2-fee-fix'."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(Path(tmp))
            pkg = _make_closed_pkg("Tagged Trade")
            entry = journal.record_close(pkg, exit_trigger="target_hit")

            assert entry["_code_version"] == "v2-fee-fix"
            assert "active_release" in entry
            assert isinstance(entry["active_release"], dict)
            assert "note" in entry["active_release"]

    def test_code_version_persists_to_disk(self):
        """_code_version should survive save/reload."""
        with tempfile.TemporaryDirectory() as tmp:
            j1 = TradeJournal(Path(tmp))
            pkg = _make_closed_pkg("Persist Version")
            j1.record_close(pkg, exit_trigger="manual")

            j2 = TradeJournal(Path(tmp))
            assert j2.entries[0]["_code_version"] == "v2-fee-fix"

    def test_old_entries_lack_code_version(self):
        """Entries created before the fix (manually constructed) won't have the field.
        This simulates Phase 1 data."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(Path(tmp))
            # Manually inject an old-style entry without _code_version
            old_entry = {
                "id": "journal_old123",
                "package_id": "pkg_old",
                "name": "Old Phase 1 Trade",
                "strategy_type": "pure_prediction",
                "total_cost": 100.0,
                "exit_value": 80.0,
                "pnl": -20.0,
                "pnl_pct": -20.0,
                "outcome": "loss",
                "exit_trigger": "stop_loss",
                "closed_at": time.time() - 86400,
            }
            journal.entries.append(old_entry)
            journal.save()

            # Now record a new trade
            pkg = _make_closed_pkg("New Trade", pkg_id="pkg_new")
            new_entry = journal.record_close(pkg, exit_trigger="target_hit")

            # Old entry should NOT have _code_version
            assert "_code_version" not in journal.entries[0]
            # New entry should have it
            assert journal.entries[1]["_code_version"] == "v2-fee-fix"

    def test_phase_separation(self):
        """Can filter entries by _code_version presence to separate Phase 1 vs Phase 2."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(Path(tmp))

            # Inject 3 Phase 1 entries (no _code_version)
            for i in range(3):
                journal.entries.append({
                    "id": f"journal_phase1_{i}",
                    "package_id": f"pkg_p1_{i}",
                    "name": f"Phase 1 Trade {i}",
                    "pnl": -5.0,
                    "outcome": "loss",
                    "closed_at": time.time() - 86400 + i,
                })

            # Record 2 Phase 2 entries (will have _code_version)
            for i in range(2):
                pkg = _make_closed_pkg(f"Phase 2 Trade {i}", pkg_id=f"pkg_p2_{i}")
                journal.record_close(pkg, exit_trigger="target_hit")

            phase1 = [e for e in journal.entries if "_code_version" not in e]
            phase2 = [e for e in journal.entries if e.get("_code_version") == "v2-fee-fix"]

            assert len(phase1) == 3
            assert len(phase2) == 2

    def test_code_version_on_loss(self):
        """_code_version should be present on losses too, not just wins."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(Path(tmp))
            pkg = _make_closed_pkg("Losing Trade", total_cost=100.0, current_value=60.0)
            entry = journal.record_close(pkg, exit_trigger="stop_loss")

            assert entry["outcome"] == "loss"
            assert entry["_code_version"] == "v2-fee-fix"

    def test_code_version_on_flat(self):
        """_code_version should be present on flat trades too."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(Path(tmp))
            pkg = _make_closed_pkg("Flat Trade", total_cost=100.0, current_value=100.0)
            entry = journal.record_close(pkg, exit_trigger="manual")

            assert entry["outcome"] == "flat"
            assert entry["_code_version"] == "v2-fee-fix"

    def test_code_version_with_fees(self):
        """_code_version should be present even when legs have fees."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(Path(tmp))
            pkg = _make_closed_pkg("Fee Trade")
            # Add fees to the leg
            pkg["legs"][0]["buy_fees"] = 1.50
            pkg["legs"][0]["sell_fees"] = 0.0  # maker exit
            entry = journal.record_close(pkg, exit_trigger="target_hit")

            assert entry["_code_version"] == "v2-fee-fix"
            assert entry["buy_fees"] == 1.50
            assert entry["sell_fees"] == 0.0


# ─── Integration: Migration + Journal Together ───────────────────────────────

class TestMigrationJournalIntegration:
    def test_migrated_package_gets_tagged_on_close(self, tmp_path):
        """A legacy package that gets migrated and then closed should have
        _code_version in its journal entry."""
        journal = TradeJournal(data_dir=tmp_path, mode="paper")
        pm = PositionManager(data_dir=tmp_path, executors={}, trade_journal=journal)

        # Create and add a legacy package
        pkg = _make_legacy_package("Will ETH hit 5K?")
        pm.add_package(pkg)

        # Migrate it
        _migrate_legacy_packages(pm)

        migrated = pm.get_package(pkg["id"])
        assert migrated["_hold_to_resolution"] is True

        # Simulate closing it (manually set exit data on leg)
        leg = migrated["legs"][0]
        leg["exit_price"] = 0.95
        leg["current_price"] = 0.95
        leg["sell_fees"] = 0.0
        pm.close_package(pkg["id"], exit_trigger="resolution")

        # Journal should have the entry with _code_version
        assert len(journal.entries) == 1
        assert journal.entries[0]["_code_version"] == "v2-fee-fix"
        assert journal.entries[0]["name"] == "Will ETH hit 5K?"
