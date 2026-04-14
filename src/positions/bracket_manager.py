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
        # Skip hold-to-resolution packages UNLESS they explicitly requested brackets.
        # portfolio_no sets both _hold_to_resolution and _use_brackets — brackets
        # capture the 15% target at 0% maker fee, resolution handles the downside.
        # (bracket_target is 100% WR, +$15.54 — the best performing exit trigger.)
        if pkg.get("_hold_to_resolution") and not pkg.get("_use_brackets"):
            return {"skipped": True, "reason": "hold_to_resolution"}

        rules = pkg.get("exit_rules", [])
        target_rule = next((r for r in rules if r.get("type") == "target_profit" and r.get("active")), None)
        # stop_loss rules are PERMANENTLY BANNED — never read, never honored.
        # Any stop_loss rule in exit_rules is legacy data and must be ignored here.

        if not target_rule:
            return {"skipped": True, "reason": "no target rule"}

        target_pct = target_rule["params"].get("target_pct", 25)

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
            target_price = round(min(entry * (1 + target_pct / 100), MAX_SELL_PRICE), 4)
            result = await executor.sell_limit(asset_id, qty, target_price)
            if result.success:
                bracket_info["target_order_id"] = result.tx_id
                bracket_info["target_price"] = target_price
                logger.info("Bracket TARGET placed: %s @ %.4f (order %s)", leg_id, target_price, result.tx_id)
            else:
                logger.warning("Bracket target failed for %s: %s", leg_id, result.error)

            # NOTE: stop_price is NEVER set. stop_loss is PERMANENTLY BANNED
            # (journal EV: every fired stop_loss lost money on binary markets).
            if "target_order_id" in bracket_info:
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
        """DISABLED. Trailing stops are PERMANENTLY BANNED. Kept as a no-op for
        any legacy caller; never writes a stop level into the bracket record.
        """
        return {"skipped": True, "reason": "trailing_stop_permanently_banned"}

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

            # Stop monitoring is PERMANENTLY REMOVED. stop_loss is banned —
            # brackets only track resting GTC target orders. Any stop_price
            # carried over from legacy data in `info` is intentionally ignored.

        if not brackets:
            pkg.pop("_brackets", None)

        return filled

    def _compute_trail_price(self, pkg: dict, leg_id: str) -> float | None:
        """Compute the trailing stop price based on leg-level peak price.

        DISABLED: Trailing stops cause massive losses on binary prediction markets.
        Phase 1 journal showed 0/8 trailing stop wins. Prices are inherently volatile
        between $0-$1 and trailing stops cut winners early on normal noise.
        Returns None always — trailing stops should not fire.
        """
        return None
