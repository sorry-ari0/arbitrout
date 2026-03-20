"""Bracket order manager — pre-placed GTC target orders + monitored stop levels.

On entry, places a resting GTC sell at the target price (0% maker fee).
Stop-loss is tracked internally (not a resting order) because CLOB sell limits
fill when price RISES to the ask — a resting sell at $0.54 would fill immediately
if the current bid > $0.54. Instead, BracketManager monitors price each tick and
places a limit sell only when price drops to the stop level.

The exit engine calls adjust_stop each tick to implement rolling trail.
"""
import logging
import time

logger = logging.getLogger("positions.bracket_manager")

MAX_SELL_PRICE = 0.99  # CLOB won't fill at exactly $1.00
MIN_SELL_PRICE = 0.01  # Floor


class BracketManager:
    """Manages bracket (target GTC + monitored stop) orders for open packages."""

    def __init__(self, executors: dict):
        self.executors = executors  # platform_name → executor instance

    async def place_brackets(self, pkg: dict) -> dict:
        """Place target GTC sell + set stop level for all open legs in a package.

        Reads exit_rules to determine target and stop prices.
        Writes bracket info into pkg["_brackets"][leg_id].
        Target: resting GTC sell order on the book (fills automatically at 0% fee).
        Stop: tracked price level (no resting order — see module docstring).
        """
        # Skip hold-to-resolution packages — they should resolve at $0/$1
        if pkg.get("_hold_to_resolution"):
            return {"skipped": True, "reason": "hold_to_resolution"}

        rules = pkg.get("exit_rules", [])
        target_rule = next((r for r in rules if r.get("type") == "target_profit" and r.get("active")), None)
        stop_rule = next((r for r in rules if r.get("type") == "stop_loss" and r.get("active")), None)

        if not target_rule and not stop_rule:
            return {"skipped": True, "reason": "no target or stop rules"}

        target_pct = target_rule["params"].get("target_pct", 25) if target_rule else None
        stop_pct = stop_rule["params"].get("stop_pct", -40) if stop_rule else None

        if "_brackets" not in pkg:
            pkg["_brackets"] = {}

        for leg in pkg.get("legs", []):
            if leg.get("status") != "open":
                continue
            leg_id = leg["leg_id"]
            if leg_id in pkg["_brackets"]:
                continue  # Already bracketed

            executor = self.executors.get(leg["platform"])
            if not executor or not hasattr(executor, "sell_limit"):
                continue

            entry = leg["entry_price"]
            qty = leg["quantity"]
            asset_id = leg["asset_id"]

            bracket_info = {"leg_id": leg_id, "platform": leg["platform"],
                           "asset_id": asset_id, "quantity": qty,
                           "entry_price": entry, "placed_at": time.time(),
                           "peak_price": entry}  # leg-level peak tracking

            # Place target order (resting GTC sell on the book)
            if target_pct is not None:
                target_price = round(min(entry * (1 + target_pct / 100), MAX_SELL_PRICE), 4)
                result = await executor.sell_limit(asset_id, qty, target_price)
                if result.success:
                    bracket_info["target_order_id"] = result.tx_id
                    bracket_info["target_price"] = target_price
                    logger.info("Bracket TARGET placed: %s @ %.4f (order %s)", leg_id, target_price, result.tx_id)
                else:
                    logger.warning("Bracket target failed for %s: %s", leg_id, result.error)

            # Set stop level (tracked internally, NOT a resting order)
            if stop_pct is not None:
                stop_price = round(max(entry * (1 + stop_pct / 100), MIN_SELL_PRICE), 4)
                bracket_info["stop_price"] = stop_price
                logger.info("Bracket STOP level set: %s @ %.4f (monitored)", leg_id, stop_price)

            if "target_order_id" in bracket_info or "stop_price" in bracket_info:
                pkg["_brackets"][leg_id] = bracket_info

        return {"success": True, "brackets": len(pkg.get("_brackets", {}))}

    async def cancel_brackets(self, pkg: dict):
        """Cancel all bracket orders for a package."""
        brackets = pkg.get("_brackets", {})
        for leg_id in list(brackets.keys()):
            await self.cancel_leg_brackets(pkg, leg_id)
        pkg.pop("_brackets", None)

    async def cancel_leg_brackets(self, pkg: dict, leg_id: str):
        """Cancel bracket orders for a single leg."""
        brackets = pkg.get("_brackets", {})
        info = brackets.get(leg_id)
        if not info:
            return
        executor = self.executors.get(info.get("platform", ""))
        if executor:
            # Only target has a resting order to cancel
            oid = info.get("target_order_id")
            if oid:
                try:
                    await executor.cancel_order(oid)
                except Exception as e:
                    logger.warning("Failed to cancel bracket order %s: %s", oid, e)
        brackets.pop(leg_id, None)
        if not brackets:
            pkg.pop("_brackets", None)

    def adjust_stop(self, pkg: dict, leg_id: str, new_stop_price: float) -> dict:
        """Raise the stop level (rolling trail). No CLOB orders to cancel/replace —
        stop is tracked internally.

        Only adjusts UPWARD — never lowers the stop.
        """
        brackets = pkg.get("_brackets", {})
        info = brackets.get(leg_id)
        if not info:
            return {"success": False, "error": "no bracket for leg"}

        current_stop = info.get("stop_price", 0)
        if new_stop_price <= current_stop:
            return {"skipped": True, "reason": "new stop not higher than current"}

        new_stop_price = round(max(new_stop_price, MIN_SELL_PRICE), 4)
        old_stop = info["stop_price"]
        info["stop_price"] = new_stop_price
        info["last_adjusted"] = time.time()
        logger.debug("Bracket stop adjusted: %s %.4f → %.4f", leg_id, old_stop, new_stop_price)
        return {"success": True, "new_stop": new_stop_price}

    def update_peak(self, pkg: dict, leg_id: str, current_price: float):
        """Update leg-level peak price for trail computation."""
        brackets = pkg.get("_brackets", {})
        info = brackets.get(leg_id)
        if info and current_price > info.get("peak_price", 0):
            info["peak_price"] = current_price

    async def check_brackets(self, pkg: dict) -> list[dict]:
        """Check bracket status: target fills (CLOB) + stop triggers (price monitor).

        Returns list of bracket events:
          {"leg_id", "type": "target"|"stop", "order_id"|None, "price", "quantity", "fee"}

        For targets: checks resting GTC order fill status.
        For stops: checks if current leg price has dropped to/below stop level,
          then places a limit sell at the stop price for 0% maker fee.
        For partial fills: cancels remaining, returns partial fill info.
        """
        filled = []
        brackets = pkg.get("_brackets", {})

        for leg_id, info in list(brackets.items()):
            executor = self.executors.get(info.get("platform", ""))
            if not executor:
                continue

            # Check TARGET order fill (resting GTC on book)
            target_oid = info.get("target_order_id")
            if target_oid:
                try:
                    status = await executor.check_order_status(target_oid)
                except Exception:
                    status = {"status": "unknown"}

                order_status = status.get("status", "unknown")
                if order_status == "filled":
                    filled.append({
                        "leg_id": leg_id, "type": "target",
                        "order_id": target_oid,
                        "price": info.get("target_price", 0),
                        "quantity": info.get("quantity", 0),
                        "fee": status.get("fee", 0.0),
                    })
                    brackets.pop(leg_id, None)
                    continue
                elif order_status == "partially_filled":
                    # Cancel remaining, accept partial fill
                    try:
                        await executor.cancel_order(target_oid)
                    except Exception:
                        pass
                    filled_qty = status.get("size_matched", 0)
                    if filled_qty > 0:
                        filled.append({
                            "leg_id": leg_id, "type": "target",
                            "order_id": target_oid,
                            "price": info.get("target_price", 0),
                            "quantity": filled_qty,
                            "fee": status.get("fee", 0.0),
                            "partial": True,
                        })
                    brackets.pop(leg_id, None)
                    continue

            # Check STOP trigger (price monitor — NOT a resting order)
            stop_price = info.get("stop_price", 0)
            if stop_price > 0:
                # Get current price from the leg
                leg = next((l for l in pkg.get("legs", []) if l["leg_id"] == leg_id), None)
                current_price = leg.get("current_price", 0) if leg else 0

                if current_price > 0 and current_price <= stop_price:
                    # Price dropped to stop level — place limit sell at stop price
                    logger.info("Stop triggered for %s: price %.4f <= stop %.4f, placing limit sell",
                                leg_id, current_price, stop_price)
                    result = await executor.sell_limit(
                        info["asset_id"], info["quantity"], stop_price)
                    if result.success:
                        # Cancel the target order
                        if target_oid:
                            try:
                                await executor.cancel_order(target_oid)
                            except Exception:
                                pass
                        filled.append({
                            "leg_id": leg_id, "type": "stop",
                            "order_id": result.tx_id,
                            "price": stop_price,
                            "quantity": info.get("quantity", 0),
                            "fee": 0.0,  # Maker fee
                        })
                        brackets.pop(leg_id, None)
                    else:
                        logger.warning("Stop sell_limit failed for %s: %s", leg_id, result.error)

        if not brackets:
            pkg.pop("_brackets", None)

        return filled

    def _compute_trail_price(self, pkg: dict, leg_id: str) -> float | None:
        """Compute the trailing stop price based on leg-level peak price.

        Uses the same adaptive trail logic as exit_engine.evaluate_heuristics.
        Returns the new stop price, or None if no trailing stop rule.
        """
        rules = pkg.get("exit_rules", [])
        trail_rule = next((r for r in rules if r.get("type") == "trailing_stop" and r.get("active")), None)
        if not trail_rule:
            return None

        trail_pct = trail_rule["params"].get("current", 35) / 100.0

        # Get leg-level data from bracket info (not package-level peak_value)
        brackets = pkg.get("_brackets", {})
        info = brackets.get(leg_id)
        if not info:
            return None

        entry = info.get("entry_price", 0.5)

        # Adapt trail by entry price (same logic as exit_engine)
        if entry <= 0.30:
            trail_pct *= 2.0  # Longshots: very wide
        elif entry >= 0.60:
            trail_pct *= 0.7  # Favorites: tighter

        # Use leg-level peak price (tracked by update_peak)
        peak_price = info.get("peak_price", entry)
        if peak_price <= 0:
            return None

        new_stop = round(peak_price * (1 - trail_pct), 4)
        return max(new_stop, MIN_SELL_PRICE)
