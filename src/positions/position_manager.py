"""Position manager — CRUD, persistence, execution, rollback for derivative packages."""
import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger("positions.manager")

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_PARTIAL = "partial_exit"

STRATEGY_TYPES = ("spot_plus_hedge", "cross_platform_arb", "pure_prediction")


def create_package(name: str, strategy_type: str) -> dict:
    """Create a new derivative package dict with all required fields."""
    if strategy_type not in STRATEGY_TYPES:
        raise ValueError(f"Invalid strategy: {strategy_type}. Must be one of {STRATEGY_TYPES}")
    return {
        "id": f"pkg_{uuid.uuid4().hex[:12]}",
        "name": name,
        "strategy_type": strategy_type,
        "status": STATUS_OPEN,
        "legs": [],
        "exit_rules": [],
        "ai_strategy": "balanced",
        "execution_log": [],
        "itm_status": "ATM",
        "unrealized_pnl": 0.0,
        "unrealized_pnl_pct": 0.0,
        "total_cost": 0.0,
        "current_value": 0.0,
        "peak_value": 0.0,
        "created_at": time.time(),
        "updated_at": time.time(),
    }


def create_leg(platform: str, leg_type: str, asset_id: str, asset_label: str,
               entry_price: float, cost: float, expiry: str = "2026-12-31") -> dict:
    """Create a leg dict. Derives quantity from cost/price."""
    quantity = cost / entry_price if entry_price > 0 else 0
    return {
        "leg_id": f"leg_{uuid.uuid4().hex[:8]}",
        "platform": platform,
        "type": leg_type,
        "asset_id": asset_id,
        "asset_label": asset_label,
        "entry_price": entry_price,
        "current_price": entry_price,
        "quantity": quantity,
        "cost": cost,
        "current_value": cost,
        "expiry": expiry,
        "status": "open",
        "leg_status": "ATM",
        "tx_id": None,
    }


def create_exit_rule(rule_type: str, params: dict) -> dict:
    """Create an exit rule dict."""
    return {
        "rule_id": f"rule_{uuid.uuid4().hex[:8]}",
        "type": rule_type,
        "params": dict(params),
        "active": True,
        "created_at": time.time(),
    }


class PositionManager:
    """Manages derivative packages — CRUD, persistence, execution, rollback."""

    def __init__(self, data_dir: Path, executors: dict, trade_journal=None):
        self.data_dir = Path(data_dir)
        self.executors = executors  # platform_name -> executor instance
        self.packages: dict[str, dict] = {}
        self.alerts: list[dict] = []  # pending escalation alerts
        self.trade_journal = trade_journal
        self._lock = asyncio.Lock()
        self._load()

    def _load(self):
        """Load packages from positions.json."""
        path = self.data_dir / "positions.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "packages" in data:
                    self.packages = {p["id"]: p for p in data["packages"]}
                    self.alerts = data.get("alerts", [])
            except (json.JSONDecodeError, OSError, KeyError) as e:
                logger.warning("Failed to load positions: %s", e)

    def save(self):
        """Atomic save to positions.json."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "positions.json"
        tmp = str(path) + ".tmp"
        data = {
            "packages": list(self.packages.values()),
            "alerts": self.alerts,
            "saved_at": time.time(),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))

    def add_package(self, pkg: dict):
        """Add a package and persist."""
        self.packages[pkg["id"]] = pkg
        self.save()

    def get_package(self, pkg_id: str) -> dict | None:
        return self.packages.get(pkg_id)

    def list_packages(self, status: str | None = None) -> list[dict]:
        if status:
            return [p for p in self.packages.values() if p["status"] == status]
        return list(self.packages.values())

    def close_package(self, pkg_id: str, exit_trigger: str = "manual"):
        """Mark package as closed and record in trade journal."""
        pkg = self.packages.get(pkg_id)
        if pkg:
            pkg["status"] = STATUS_CLOSED
            pkg["updated_at"] = time.time()
            for leg in pkg["legs"]:
                if leg["status"] == "open":
                    leg["status"] = "closed"
            self.save()
            if self.trade_journal:
                try:
                    self.trade_journal.record_close(pkg, exit_trigger=exit_trigger)
                except Exception as e:
                    logger.warning("Failed to record trade journal: %s", e)

    def update_pnl(self, pkg_id: str):
        """Recalculate P&L and ITM/OTM status for a package.

        Accounts for estimated sell-side fees in unrealized P&L so the
        displayed profit/loss reflects what you'd actually get if you closed now.
        """
        pkg = self.packages.get(pkg_id)
        if not pkg:
            return

        total_cost = 0.0
        current_value = 0.0
        total_buy_fees = 0.0
        estimated_sell_fees = 0.0
        for leg in pkg["legs"]:
            if leg["status"] != "open":
                continue
            total_cost += leg["cost"]
            cur_price = leg.get("current_price", leg["entry_price"])
            leg_val = leg["quantity"] * cur_price
            leg["current_value"] = round(leg_val, 4)
            current_value += leg_val

            # Track fees
            total_buy_fees += leg.get("buy_fees", 0)
            # Estimate sell fees: 2% taker worst-case, 0% if we use limit orders
            # Use 1% as conservative middle ground (sometimes limit, sometimes market)
            estimated_sell_fees += leg_val * 0.01

            # Per-leg ITM/OTM
            if leg["type"] in ("prediction_yes", "spot_buy"):
                leg["leg_status"] = "ITM" if cur_price > leg["entry_price"] else "OTM"
            elif leg["type"] == "prediction_no":
                leg["leg_status"] = "ITM" if cur_price < leg["entry_price"] else "OTM"
            else:
                leg["leg_status"] = "ATM"

        # Net value after estimated sell fees
        net_value = current_value - estimated_sell_fees
        pkg["total_cost"] = total_cost
        pkg["current_value"] = round(current_value, 4)
        pkg["total_buy_fees"] = round(total_buy_fees, 4)
        pkg["estimated_sell_fees"] = round(estimated_sell_fees, 4)
        pkg["unrealized_pnl"] = round(net_value - total_cost, 4)
        pkg["unrealized_pnl_pct"] = round((pkg["unrealized_pnl"] / total_cost * 100), 2) if total_cost > 0 else 0
        pkg["peak_value"] = max(pkg.get("peak_value", 0), net_value)

        # Package-level ITM/OTM — no dead zone
        if pkg["unrealized_pnl"] > 0:
            pkg["itm_status"] = "ITM"
        elif pkg["unrealized_pnl"] < 0:
            pkg["itm_status"] = "OTM"
        else:
            pkg["itm_status"] = "ATM"

        pkg["updated_at"] = time.time()
        self.save()

    async def execute_package(self, pkg: dict) -> dict:
        """Execute all legs of a package. Rolls back on failure."""
        async with self._lock:
            return await self._execute_package_locked(pkg)

    async def _execute_package_locked(self, pkg: dict) -> dict:
        executed = []
        for leg in pkg["legs"]:
            platform = leg["platform"]
            executor = self.executors.get(platform)
            if not executor:
                if platform == "robinhood":
                    leg["status"] = "advisory"
                    leg["tx_id"] = "advisory_only"
                    continue
                await self._rollback(pkg, executed)
                return {"success": False, "error": f"No executor for platform: {platform}"}

            buy_kwargs = {"asset_id": leg["asset_id"], "amount_usd": leg["cost"]}
            if hasattr(executor, 'real'):
                buy_kwargs["fallback_price"] = leg["entry_price"]
            result = await executor.buy(**buy_kwargs)
            if result.success:
                leg["tx_id"] = result.tx_id
                leg["entry_price"] = result.filled_price if result.filled_price > 0 else leg["entry_price"]
                leg["quantity"] = result.filled_quantity if result.filled_quantity > 0 else leg["quantity"]
                leg["buy_fees"] = result.fees
                leg["status"] = "open"
                executed.append(leg)
                pkg["execution_log"].append({
                    "action": "buy", "leg_id": leg["leg_id"], "platform": platform,
                    "tx_id": result.tx_id, "price": result.filled_price,
                    "quantity": result.filled_quantity, "fees": result.fees,
                    "timestamp": time.time(),
                })
            else:
                logger.error("Leg execution failed for %s: %s", leg["leg_id"], result.error)
                await self._rollback(pkg, executed)
                return {"success": False, "error": f"Leg {leg['leg_id']} failed: {result.error}"}

        pkg["total_cost"] = sum(l["cost"] for l in pkg["legs"])
        pkg["current_value"] = pkg["total_cost"]
        pkg["peak_value"] = pkg["total_cost"]
        self.add_package(pkg)
        return {"success": True, "package_id": pkg["id"]}

    async def exit_leg(self, pkg_id: str, leg_id: str, trigger: str = "manual") -> dict:
        """Exit (sell) a single leg."""
        async with self._lock:
            return await self._exit_leg_locked(pkg_id, leg_id, trigger)

    async def _exit_leg_locked(self, pkg_id: str, leg_id: str, trigger: str = "manual") -> dict:
        pkg = self.packages.get(pkg_id)
        if not pkg:
            return {"success": False, "error": "Package not found"}

        leg = next((l for l in pkg["legs"] if l["leg_id"] == leg_id), None)
        if not leg or leg["status"] != "open":
            return {"success": False, "error": "Leg not found or not open"}

        executor = self.executors.get(leg["platform"])
        if not executor:
            return {"success": False, "error": f"No executor for {leg['platform']}"}

        # Pass last known price for paper executor fallback
        if hasattr(executor, 'real'):
            result = await executor.sell(leg["asset_id"], leg["quantity"],
                                         last_known_price=leg.get("current_price", 0))
        else:
            result = await executor.sell(leg["asset_id"], leg["quantity"])
        if result.success:
            leg["status"] = "closed"
            leg["exit_price"] = result.filled_price
            leg["exit_quantity"] = result.filled_quantity
            leg["sell_fees"] = result.fees
            # Update leg's current_value to reflect actual exit
            leg["current_value"] = round(result.filled_quantity * result.filled_price, 4)
            pkg["execution_log"].append({
                "action": "sell", "leg_id": leg_id, "platform": leg["platform"],
                "tx_id": result.tx_id, "price": result.filled_price,
                "fees": result.fees, "trigger": trigger, "timestamp": time.time(),
            })
            if all(l["status"] in ("closed", "advisory") for l in pkg["legs"]):
                pkg["status"] = STATUS_CLOSED
                # Recalculate current_value from actual exit data before journaling
                pkg["current_value"] = round(sum(
                    l.get("quantity", 0) * l.get("exit_price", l.get("current_price", l.get("entry_price", 0)))
                    for l in pkg["legs"] if l.get("status") != "advisory"
                ), 4)
                if self.trade_journal:
                    try:
                        self.trade_journal.record_close(pkg, exit_trigger=trigger)
                    except Exception as e:
                        logger.warning("Failed to record trade journal: %s", e)
            else:
                pkg["status"] = STATUS_PARTIAL
            pkg["updated_at"] = time.time()
            self.save()
            return {"success": True, "tx_id": result.tx_id}
        return {"success": False, "error": result.error}

    async def _rollback(self, pkg: dict, executed_legs: list[dict]):
        """Attempt to sell already-bought legs on failure. Persists rollback status."""
        pkg["status"] = "rollback"
        for leg in executed_legs:
            executor = self.executors.get(leg["platform"])
            if executor:
                try:
                    result = await executor.sell(leg["asset_id"], leg["quantity"])
                    if result.success:
                        leg["status"] = "rolled_back"
                        logger.info("Rolled back leg %s", leg["leg_id"])
                    else:
                        leg["status"] = "rollback_failed"
                        logger.error("Rollback failed for %s: %s", leg["leg_id"], result.error)
                except Exception as e:
                    leg["status"] = "rollback_failed"
                    logger.error("Rollback exception for %s: %s", leg["leg_id"], e)
        self.save()

    def add_alert(self, pkg_id: str, trigger_id: int, trigger_name: str, details: dict):
        """Add an escalation alert for human review. Deduplicates: skips if a pending
        alert already exists for the same package + trigger."""
        # Skip if duplicate pending alert exists for this pkg + trigger
        for existing in self.alerts:
            if (existing["package_id"] == pkg_id
                    and existing["trigger_name"] == trigger_name
                    and existing["status"] == "pending"):
                return existing
        alert = {
            "id": f"alert_{uuid.uuid4().hex[:8]}",
            "package_id": pkg_id,
            "trigger_id": trigger_id,
            "trigger_name": trigger_name,
            "details": details,
            "status": "pending",
            "created_at": time.time(),
        }
        self.alerts.append(alert)
        self.save()
        return alert

    def get_dashboard_stats(self) -> dict:
        """Aggregate portfolio statistics."""
        open_pkgs = self.list_packages(STATUS_OPEN)
        closed_pkgs = self.list_packages(STATUS_CLOSED)
        total_invested = sum(p.get("total_cost", 0) for p in open_pkgs)
        total_value = sum(p.get("current_value", 0) for p in open_pkgs)
        total_pnl = total_value - total_invested
        closed_pnl = sum(p.get("unrealized_pnl", 0) for p in closed_pkgs)
        wins = sum(1 for p in closed_pkgs if p.get("unrealized_pnl", 0) > 0)
        return {
            "open_packages": len(open_pkgs),
            "closed_packages": len(closed_pkgs),
            "total_invested": round(total_invested, 2),
            "total_value": round(total_value, 2),
            "unrealized_pnl": round(total_pnl, 2),
            "realized_pnl": round(closed_pnl, 2),
            "win_rate": round(wins / len(closed_pkgs), 2) if closed_pkgs else 0,
            "pending_alerts": len([a for a in self.alerts if a["status"] == "pending"]),
        }
