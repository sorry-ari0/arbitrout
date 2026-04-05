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

STRATEGY_TYPES = ("spot_plus_hedge", "cross_platform_arb", "synthetic_derivative", "pure_prediction", "news_driven", "political_synthetic", "btc_sniper", "multi_outcome_arb", "market_making", "portfolio_no", "weather_forecast", "crypto_synthetic")


def _is_execution_result_like(result) -> bool:
    """Check that an executor returned the fields the manager relies on."""
    if result is None:
        return False
    return (
        isinstance(getattr(result, "success", None), bool)
        and hasattr(result, "tx_id")
        and hasattr(result, "filled_price")
        and hasattr(result, "filled_quantity")
        and hasattr(result, "fees")
    )


def journal_fee_model_for_executor(executor) -> str:
    """Tag persisted on journal closes — distinguishes paper maker-0% vs taker sim vs live."""
    if executor is None:
        return "unknown"
    tag_fn = getattr(executor, "journal_fee_model_tag", None)
    if callable(tag_fn):
        return tag_fn()
    return "live"


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

    def __init__(self, data_dir: Path, executors: dict, trade_journal=None, bracket_manager=None, mode: str = "paper"):
        self.data_dir = Path(data_dir)
        self.executors = executors  # platform_name -> executor instance
        self.mode = mode  # "paper" or "live" — separates position files
        self.packages: dict[str, dict] = {}
        self.alerts: list[dict] = []  # pending escalation alerts
        self.trade_journal = trade_journal
        self._lock = asyncio.Lock()
        self._bracket_manager = bracket_manager
        self._load()

    @property
    def _positions_filename(self) -> str:
        """Return mode-specific positions filename."""
        return f"positions_{self.mode}.json"

    def _load(self):
        """Load packages from positions_{mode}.json with backup recovery.
        Falls back to legacy positions.json for migration."""
        path = self.data_dir / self._positions_filename
        backup = self.data_dir / f"{self._positions_filename}.backup"
        legacy = self.data_dir / "positions.json"
        self._load_failed = False

        for source in [path, backup, legacy]:
            if not source.exists():
                continue
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "packages" in data:
                    self.packages = {p["id"]: p for p in data["packages"]}
                    self.alerts = data.get("alerts", [])
                    if source == legacy:
                        logger.info("Migrated legacy positions.json → %s", self._positions_filename)
                        self.save()  # Save to mode-specific file
                    elif source == backup:
                        logger.warning("Loaded positions from BACKUP (primary was corrupt)")
                    return
            except (json.JSONDecodeError, OSError, KeyError) as e:
                logger.error("Failed to load %s: %s", source.name, e)

        if path.exists():
            # Primary exists but is corrupt and no backup worked
            self._load_failed = True
            logger.error("All position sources corrupt — save() blocked until manual fix")

    def save(self):
        """Atomic save to positions_{mode}.json with backup rotation."""
        if getattr(self, "_load_failed", False):
            logger.error("Refusing to save — load failed, would overwrite corrupt data")
            return

        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / self._positions_filename
        backup = self.data_dir / f"{self._positions_filename}.backup"
        tmp = str(path) + ".tmp"
        data = {
            "packages": list(self.packages.values()),
            "alerts": self.alerts,
            "saved_at": time.time(),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        # Rotate: current → backup before overwriting
        if path.exists():
            try:
                import shutil
                shutil.copy2(str(path), str(backup))
            except OSError:
                pass  # Best effort — don't block save
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
            now = time.time()
            for leg in pkg["legs"]:
                if leg["status"] == "open":
                    leg["status"] = "closed"
                    # Fill in exit data so trade journal has complete records
                    if "exit_price" not in leg:
                        leg["exit_price"] = leg.get("current_price", leg.get("entry_price", 0))
                    if "exit_time" not in leg:
                        leg["exit_time"] = now
                    if "exit_value" not in leg:
                        qty = leg.get("quantity", 0)
                        leg["exit_value"] = round(qty * leg["exit_price"], 4)
            if not pkg.get("_journal_recorded") and self.trade_journal:
                try:
                    self.trade_journal.record_close(pkg, exit_trigger=exit_trigger)
                    pkg["_journal_recorded"] = True
                except Exception as e:
                    logger.warning("Failed to record trade journal: %s", e)
            self.save()

    def update_pnl(self, pkg_id: str):
        """Recalculate P&L and ITM/OTM status for a package.

        Accounts for estimated sell-side fees in unrealized P&L so the
        displayed profit/loss reflects what you'd actually get if you closed now.
        """
        pkg = self.packages.get(pkg_id)
        if not pkg:
            return

        # Skip recalculation for fully closed packages
        if pkg.get("status") == "closed":
            return

        total_cost = 0.0
        current_value = 0.0
        total_buy_fees = 0.0
        estimated_sell_fees = 0.0
        for leg in pkg["legs"]:
            # Always include cost from all legs (open or closed)
            total_cost += leg["cost"]
            total_buy_fees += leg.get("buy_fees", 0)

            if leg["status"] != "open":
                # Include closed legs' realized exit value so partial-exit P&L is correct
                current_value += leg.get("exit_value", leg.get("current_value", 0))
                continue
            cur_price = leg.get("current_price", leg["entry_price"])
            leg_val = leg["quantity"] * cur_price
            leg["current_value"] = round(leg_val, 4)
            current_value += leg_val

            # Sell fees: 0% maker (all exits use GTC limit orders)
            estimated_sell_fees += 0

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
        pkg["unrealized_pnl"] = round(net_value - total_cost - total_buy_fees, 4)
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
        # Dedup guard: reject packages with condition IDs already open
        # EXCEPTION: allow re-entry when news or insider signals warrant it
        # (the new position is linked to the existing one, not independent)
        has_signal = bool(pkg.get("_news_signal") or pkg.get("insider_signal")
                         or pkg.get("_news_driven") or pkg.get("_insider_driven"))
        if not has_signal:
            open_cids = set()
            for p in self.packages.values():
                if p.get("status") != "open":
                    continue
                for leg in p.get("legs", []):
                    if leg.get("status") == "open":
                        aid = leg.get("asset_id", "")
                        cid = (aid.split(":")[0] if ":" in aid else aid).lower()
                        if cid:
                            open_cids.add(cid)
            for leg in pkg.get("legs", []):
                aid = leg.get("asset_id", "")
                cid = (aid.split(":")[0] if ":" in aid else aid).lower()
                if cid and cid in open_cids:
                    return {"success": False, "error": f"Duplicate: {cid[:16]}... already open"}

        # Slippage protection: verify prices haven't moved >5% since opportunity was detected
        max_slippage = float(os.environ.get("MAX_SLIPPAGE_PCT", "5")) / 100
        for leg in pkg.get("legs", []):
            if leg.get("platform") == "robinhood":
                continue
            executor = self.executors.get(leg["platform"])
            if not executor:
                continue
            try:
                live_price = await executor.get_current_price(leg["asset_id"])
                expected = leg.get("entry_price", 0)
                if live_price > 0 and expected > 0:
                    slippage = abs(live_price - expected) / expected
                    if slippage > max_slippage:
                        return {
                            "success": False,
                            "error": f"Slippage {slippage:.1%} > {max_slippage:.0%} on {leg['asset_id']}: "
                                     f"expected ${expected:.4f} but now ${live_price:.4f}",
                        }
            except Exception:
                pass  # Price fetch failure — proceed anyway, executor has its own checks

        # Determine if parallel execution is appropriate:
        # Cross-platform arb legs should execute simultaneously to minimize slippage
        use_parallel = pkg.get("_parallel_execution", False)
        leg_platforms = set(l["platform"] for l in pkg["legs"] if l["platform"] != "robinhood")
        if len(leg_platforms) > 1 and pkg.get("strategy_type") in ("cross_platform_arb",):
            use_parallel = True

        if use_parallel:
            result = await self._execute_legs_parallel(pkg)
        else:
            result = await self._execute_legs_sequential(pkg)

        if result is not None:
            return result  # Error occurred

        # Finalize package
        pkg["total_cost"] = sum(l["cost"] for l in pkg["legs"])
        pkg["current_value"] = pkg["total_cost"]
        pkg["peak_value"] = pkg["total_cost"]
        self.add_package(pkg)
        # Place bracket orders if requested
        if pkg.get("_use_brackets") and self._bracket_manager:
            try:
                await self._bracket_manager.place_brackets(pkg)
            except Exception as e:
                logger.warning("Failed to place brackets for %s: %s", pkg["id"], e)
        return {"success": True, "package_id": pkg["id"]}

    async def _execute_legs_parallel(self, pkg: dict) -> dict:
        """Execute all legs concurrently via asyncio.gather for cross-platform arb."""

        async def _exec_one(leg):
            platform = leg["platform"]
            executor = self.executors.get(platform)
            if not executor:
                if platform == "robinhood":
                    leg["status"] = "advisory"
                    leg["tx_id"] = "advisory_only"
                    return leg, None, None
                return leg, None, f"No executor for platform: {platform}"

            use_limit = pkg.get("_use_limit_orders", True)  # Default maker (0% fee)
            max_price = round(leg["entry_price"] * 1.05, 4) if leg.get("entry_price", 0) > 0 else 0

            result = await self._submit_entry_order(executor, leg, use_limit, max_price)
            if result is None:
                return leg, None, f"No supported entry method for platform: {platform}"
            return leg, result, None

        # Execute all legs concurrently
        tasks = [_exec_one(leg) for leg in pkg["legs"]]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process ALL results first to build complete executed list,
        # then check for failures. This prevents orphaning successful legs
        # that appear after a failed leg in the results list.
        executed = []
        failed = False
        error_msg = ""
        for item in results:
            if isinstance(item, Exception):
                failed = True
                error_msg = str(item)
                continue  # don't break — process remaining results
            leg, result, err = item
            if err:
                failed = True
                error_msg = err
                continue  # don't break — process remaining results
            if result is None:
                continue  # advisory leg
            if result.success:
                leg["tx_id"] = result.tx_id
                leg["entry_price"] = result.filled_price if result.filled_price > 0 else leg["entry_price"]
                leg["quantity"] = result.filled_quantity if result.filled_quantity > 0 else leg["quantity"]
                leg["buy_fees"] = result.fees
                leg["status"] = "open"
                executed.append(leg)
                pkg["execution_log"].append({
                    "action": "buy", "leg_id": leg["leg_id"], "platform": leg["platform"],
                    "tx_id": result.tx_id, "price": result.filled_price,
                    "quantity": result.filled_quantity, "fees": result.fees,
                    "timestamp": time.time(),
                })
            else:
                failed = True
                error_msg = f"Leg {leg['leg_id']} failed: {result.error}"
                continue  # don't break — collect all successful legs for rollback

        if failed:
            logger.error("Parallel execution failed: %s", error_msg)
            await self._rollback(pkg, executed)
            return {"success": False, "error": error_msg}

        return None  # Signal caller to continue with finalization

    async def _execute_legs_sequential(self, pkg: dict) -> dict | None:
        """Execute legs one at a time (default for same-platform strategies)."""
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

            # Route to fill-confirmed buy for live executors, regular buy for paper
            use_limit = pkg.get("_use_limit_orders", True)  # Default maker (0% fee)
            max_price = round(leg["entry_price"] * 1.05, 4) if leg.get("entry_price", 0) > 0 else 0

            result = await self._submit_entry_order(executor, leg, use_limit, max_price)
            if result is None:
                await self._rollback(pkg, executed)
                return {"success": False, "error": f"No supported entry method for platform: {platform}"}
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

        return None  # Success — caller finalizes

    async def _submit_entry_order(self, executor, leg: dict, use_limit: bool, max_price: float):
        """Submit an entry order using the best available executor method."""
        asset_id = leg["asset_id"]
        amount_usd = leg["cost"]

        buy_and_confirm = getattr(executor, "buy_and_confirm", None)
        if callable(buy_and_confirm):
            result = await buy_and_confirm(
                asset_id=asset_id,
                amount_usd=amount_usd,
                max_price=max_price,
            )
            if _is_execution_result_like(result):
                return result

        buy_limit = getattr(executor, "buy_limit", None)
        if use_limit and callable(buy_limit):
            result = await buy_limit(
                asset_id=asset_id,
                amount_usd=amount_usd,
                price=leg["entry_price"],
            )
            if _is_execution_result_like(result):
                return result

        buy = getattr(executor, "buy", None)
        if callable(buy):
            buy_kwargs = {"asset_id": asset_id, "amount_usd": amount_usd}
            if hasattr(executor, 'real'):
                buy_kwargs["fallback_price"] = leg["entry_price"]
            result = await buy(**buy_kwargs)
            if _is_execution_result_like(result):
                return result

        return None

    async def exit_leg(self, pkg_id: str, leg_id: str, trigger: str = "manual",
                       use_limit: bool = False, timeout: int = 60) -> dict:
        """Exit (sell) a single leg. If use_limit=True, places a GTC limit order
        and returns {"pending": True, "order_id": ...} — caller resolves later.
        timeout: seconds before falling back to FOK (60s normal, 300s safety)."""
        async with self._lock:
            if use_limit:
                return await self._place_limit_sell(pkg_id, leg_id, trigger, timeout=timeout)
            return await self._exit_leg_locked(pkg_id, leg_id, trigger)

    async def _exit_leg_locked(self, pkg_id: str, leg_id: str, trigger: str = "manual",
                               after_limit_attempt: bool = False) -> dict:
        pkg = self.packages.get(pkg_id)
        if not pkg:
            return {"success": False, "error": "Package not found"}

        leg = next((l for l in pkg["legs"] if l["leg_id"] == leg_id), None)
        if not leg or leg["status"] != "open":
            return {"success": False, "error": "Leg not found or not open"}

        executor = self.executors.get(leg["platform"])
        if not executor:
            return {"success": False, "error": f"No executor for {leg['platform']}"}

        # All sells use GTC limit (maker, 0% fee) — executor.sell() on Polymarket is already GTC
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
            leg["exit_trigger"] = trigger
            exit_kind = "fok_fallback" if after_limit_attempt else "fok_direct"
            leg["exit_order_type"] = exit_kind
            leg["fee_model"] = journal_fee_model_for_executor(executor)
            # exit_value = gross proceeds from the sell (before fees deducted)
            leg["exit_value"] = round(result.filled_quantity * result.filled_price, 4)
            # current_value tracks net (after fees) for P&L display
            leg["current_value"] = round(result.filled_quantity * result.filled_price - result.fees, 4)
            pkg["execution_log"].append({
                "action": "sell", "leg_id": leg_id, "platform": leg["platform"],
                "tx_id": result.tx_id, "price": result.filled_price,
                "fees": result.fees, "trigger": trigger,
                "exit_order_type": exit_kind, "fee_model": leg["fee_model"],
                "timestamp": time.time(),
            })
            if all(l["status"] in ("closed", "advisory") for l in pkg["legs"]):
                pkg["status"] = STATUS_CLOSED
                # Recalculate current_value from actual exit data before journaling (net of sell fees)
                pkg["current_value"] = round(sum(
                    l.get("quantity", 0) * l.get("exit_price", l.get("current_price", l.get("entry_price", 0)))
                    - l.get("sell_fees", 0)
                    for l in pkg["legs"] if l.get("status") != "advisory"
                ), 4)
                if not pkg.get("_journal_recorded") and self.trade_journal:
                    try:
                        self.trade_journal.record_close(pkg, exit_trigger=trigger)
                        pkg["_journal_recorded"] = True
                    except Exception as e:
                        logger.warning("Failed to record trade journal: %s", e)
            else:
                pkg["status"] = STATUS_PARTIAL
            pkg["updated_at"] = time.time()
            self.save()
            return {"success": True, "tx_id": result.tx_id}
        return {"success": False, "error": result.error}

    async def _place_limit_sell(self, pkg_id: str, leg_id: str, trigger: str,
                                timeout: int = 60) -> dict:
        """Place a GTC limit sell order. Does NOT finalize the exit — returns pending."""
        pkg = self.packages.get(pkg_id)
        if not pkg:
            return {"success": False, "error": "Package not found"}

        leg = next((l for l in pkg["legs"] if l["leg_id"] == leg_id), None)
        if not leg or leg["status"] != "open":
            return {"success": False, "error": "Leg not found or not open"}

        executor = self.executors.get(leg["platform"])
        if not executor:
            return {"success": False, "error": f"No executor for {leg['platform']}"}

        # Limit price: current price (maker ask sits at or near the spread)
        # The executor's sell_limit uses GTC which rests as maker for 0% fees.
        # Using mid directly — the executor handles spread-edge placement.
        mid = leg.get("current_price", 0)
        if mid <= 0:
            return await self._exit_leg_locked(pkg_id, leg_id, trigger, after_limit_attempt=True)
        limit_price = round(mid, 4)
        if limit_price <= 0:
            return await self._exit_leg_locked(pkg_id, leg_id, trigger, after_limit_attempt=True)

        # Place limit order (same call for paper or real executor)
        result = await executor.sell_limit(leg["asset_id"], leg["quantity"], limit_price)

        if not result.success:
            logger.warning("Limit sell failed for %s, falling back to FOK: %s", leg_id, result.error)
            return await self._exit_leg_locked(pkg_id, leg_id, trigger, after_limit_attempt=True)

        # Record pending order
        if "_pending_limit_orders" not in pkg:
            pkg["_pending_limit_orders"] = {}
        pkg["_pending_limit_orders"][leg_id] = {
            "order_id": result.tx_id,
            "placed_at": time.time(),
            "quantity": leg["quantity"],
            "asset_id": leg["asset_id"],
            "platform": leg["platform"],
            "trigger": trigger,
            "limit_price": limit_price,
            "timeout": timeout,
        }
        self.save()
        logger.info("Placed limit sell for %s @ %.4f (order %s)", leg_id, limit_price, result.tx_id)
        return {"pending": True, "order_id": result.tx_id}

    async def resolve_pending_order(self, pkg_id: str, leg_id: str) -> dict:
        """Check a pending limit order and finalize or cancel+FOK.
        Per spec: check status OUTSIDE the lock (network call), then acquire
        lock only for finalization (writing exit data)."""
        # --- Phase 1: read state and check order status WITHOUT lock ---
        pkg = self.packages.get(pkg_id)
        if not pkg:
            return {"success": False, "error": "Package not found"}
        pending = pkg.get("_pending_limit_orders", {}).get(leg_id)
        if not pending:
            return {"success": False, "error": "No pending order for this leg"}
        executor = self.executors.get(pending["platform"])
        if not executor:
            return {"success": False, "error": f"No executor for {pending['platform']}"}

        order_id = pending["order_id"]
        status = await executor.check_order_status(order_id)  # Network call — no lock held
        order_status = status.get("status", "unknown").lower()

        # --- Phase 2: acquire lock only for finalization ---
        async with self._lock:
            # Re-read state in case it changed while we were unlocked
            pkg = self.packages.get(pkg_id)
            if not pkg:
                return {"success": False, "error": "Package not found"}
            pending = pkg.get("_pending_limit_orders", {}).get(leg_id)
            if not pending:
                return {"success": False, "error": "No pending order (resolved by safety override?)"}
            leg = next((l for l in pkg["legs"] if l["leg_id"] == leg_id), None)
            if not leg:
                return {"success": False, "error": "Leg not found"}

            if order_status == "filled":
                fill_price = status.get("price", pending["limit_price"])
                fill_qty = status.get("size_matched", pending["quantity"])
                fill_fee = status.get("fee", 0.0)
                self._finalize_exit(pkg, leg, pending["trigger"], fill_price, fill_qty, fill_fee, "limit_filled")
                del pkg["_pending_limit_orders"][leg_id]
                if not pkg["_pending_limit_orders"]:
                    del pkg["_pending_limit_orders"]
                self.save()
                return {"success": True, "exit_order_type": "limit_filled"}

            elif order_status == "partially_filled":
                await executor.cancel_order(order_id)
                filled_qty = float(status.get("size_matched", 0))
                remaining = pending["quantity"] - filled_qty
                limit_fill_fee = status.get("fee", 0.0)
                limit_fill_price = status.get("price", pending["limit_price"])

                if remaining > 0.001:
                    # Use sell_limit (maker, 0% fee) for remainder — never taker
                    result = await executor.sell_limit(pending["asset_id"], remaining, limit_fill_price)
                    if result.success:
                        # Combine fees from both limit fills (both maker)
                        total_fees = limit_fill_fee + result.fees
                        total_qty = pending["quantity"]
                        # Weighted average price across both fills
                        if filled_qty > 0:
                            avg_price = (limit_fill_price * filled_qty +
                                         result.filled_price * remaining) / total_qty
                        else:
                            avg_price = result.filled_price
                        self._finalize_exit(pkg, leg, pending["trigger"], avg_price,
                                            total_qty, total_fees, "limit_partial_maker")
                    else:
                        # FOK failed — record partial limit fill if any, keep leg open for retry
                        if filled_qty > 0:
                            leg["quantity"] = remaining  # Reduce to what we still hold
                            leg["sell_fees"] = leg.get("sell_fees", 0) + limit_fill_fee
                            pkg["execution_log"].append({
                                "action": "partial_sell", "leg_id": leg["leg_id"],
                                "platform": leg["platform"], "tx_id": None,
                                "price": limit_fill_price, "fees": limit_fill_fee,
                                "trigger": pending["trigger"],
                                "exit_order_type": "limit_partial_only",
                                "quantity_sold": filled_qty,
                                "timestamp": time.time(),
                            })
                            logger.warning("Partial exit: sold %.4f of %.4f at %.4f, %.4f remaining (FOK failed: %s)",
                                           filled_qty, pending["quantity"], limit_fill_price,
                                           remaining, result.error if result else "unknown")
                elif filled_qty > 0:
                    # Fully filled by limit (remaining < 0.001 dust)
                    self._finalize_exit(pkg, leg, pending["trigger"], limit_fill_price,
                                        filled_qty, limit_fill_fee, "limit_filled")
                del pkg["_pending_limit_orders"][leg_id]
                if not pkg["_pending_limit_orders"]:
                    del pkg["_pending_limit_orders"]
                self.save()
                return {"success": True, "exit_order_type": "limit_partial_maker"}

            elif order_status == "cancelled":
                # Order was cancelled or lost (e.g., server restart wiped _resting_orders).
                # Fall back to FOK to actually close the position.
                del pkg["_pending_limit_orders"][leg_id]
                if not pkg["_pending_limit_orders"]:
                    del pkg["_pending_limit_orders"]
                logger.warning("Limit order %s cancelled/lost for %s, falling back to FOK", order_id, leg_id)
                return await self._exit_leg_locked(pkg_id, leg_id, pending["trigger"], after_limit_attempt=True)

            elif time.time() - pending["placed_at"] > pending.get("timeout", 60):
                await executor.cancel_order(order_id)
                del pkg["_pending_limit_orders"][leg_id]
                if not pkg["_pending_limit_orders"]:
                    del pkg["_pending_limit_orders"]
                logger.info("Limit order timed out for %s, falling back to FOK", leg_id)
                return await self._exit_leg_locked(pkg_id, leg_id, pending["trigger"], after_limit_attempt=True)

            else:
                return {"pending": True, "order_id": order_id}

    def _finalize_exit(self, pkg: dict, leg: dict, trigger: str,
                       fill_price: float, fill_qty: float, fees: float,
                       exit_order_type: str):
        """Finalize a leg exit — same logic as _exit_leg_locked but with provided fill data."""
        leg["status"] = "closed"
        leg["exit_price"] = fill_price
        leg["exit_quantity"] = fill_qty
        leg["sell_fees"] = fees
        leg["exit_trigger"] = trigger
        leg["exit_order_type"] = exit_order_type
        ex = self.executors.get(leg.get("platform", ""))
        leg["fee_model"] = journal_fee_model_for_executor(ex)
        leg["exit_value"] = round(fill_qty * fill_price, 4)
        leg["current_value"] = round(fill_qty * fill_price - fees, 4)
        pkg["execution_log"].append({
            "action": "sell", "leg_id": leg["leg_id"], "platform": leg["platform"],
            "tx_id": None, "price": fill_price, "fees": fees,
            "trigger": trigger, "exit_order_type": exit_order_type,
            "fee_model": leg["fee_model"],
            "timestamp": time.time(),
        })
        if all(l["status"] in ("closed", "advisory") for l in pkg["legs"]):
            pkg["status"] = STATUS_CLOSED
            pkg["current_value"] = round(sum(
                l.get("quantity", 0) * l.get("exit_price", l.get("current_price", l.get("entry_price", 0)))
                - l.get("sell_fees", 0)
                for l in pkg["legs"] if l.get("status") != "advisory"
            ), 4)
            if not pkg.get("_journal_recorded") and self.trade_journal:
                try:
                    self.trade_journal.record_close(pkg, exit_trigger=trigger)
                    pkg["_journal_recorded"] = True
                except Exception as e:
                    logger.warning("Failed to record trade journal: %s", e)
        else:
            pkg["status"] = STATUS_PARTIAL
        pkg["updated_at"] = time.time()

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
        # Realized P&L: calculate from actual exit data (current_value - total_cost)
        closed_pnl = sum(
            p.get("current_value", 0) - p.get("total_cost", 0)
            for p in closed_pkgs
        )
        wins = sum(1 for p in closed_pkgs
                   if p.get("current_value", 0) - p.get("total_cost", 0) > 0.01)
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
