"""Exit engine — 30s scan loop with 18 heuristic triggers and safety overrides.

Evaluates open packages, fires triggers, routes to AI advisor or immediate exit.
Safety overrides (spread inversion, <24h expiry, <6h expiry) bypass LLM entirely.
"""
import asyncio
import logging
import time
from datetime import datetime, date, timedelta

logger = logging.getLogger("positions.exit_engine")

# ── Trigger IDs ─────────────────────────────────────────────────────────────
# Category: Profit Taking (1-3)
T_TARGET_HIT = 1
T_TRAILING_STOP = 2
T_PARTIAL_PROFIT = 3

# Category: Loss Prevention (4-6)
T_STOP_LOSS = 4
T_NEW_ATH_TRAILING = 5
T_CORRELATION_BREAK = 6

# Category: Spread / Arb (7-9)
T_SPREAD_INVERSION = 7   # SAFETY OVERRIDE
T_SPREAD_COMPRESSION = 8
T_VOLUME_DRY = 9

# Category: Time (10-12)
T_TIME_24H = 10           # SAFETY OVERRIDE
T_TIME_6H = 11            # SAFETY OVERRIDE
T_TIME_DECAY = 12

# Category: Volatility (13-15)
T_VOL_SPIKE = 13
T_VOL_CRUSH = 14
T_NEGATIVE_DRIFT = 15

# Category: Platform (16-18)
T_PLATFORM_ERROR = 16
T_LIQUIDITY_GAP = 17
T_FEE_SPIKE = 18


def evaluate_heuristics(pkg: dict) -> list[dict]:
    """Evaluate all 18 heuristic triggers against a package. Returns list of fired triggers."""
    triggers: list[dict] = []
    strategy = pkg.get("strategy_type", "")
    legs = pkg.get("legs", [])
    rules = pkg.get("exit_rules", [])

    total_cost = pkg.get("total_cost", 0) or sum(l.get("cost", 0) for l in legs)
    current_value = pkg.get("current_value", 0) or sum(
        l.get("current_value", l.get("quantity", 0) * l.get("current_price", l.get("entry_price", 0)))
        for l in legs
    )
    pnl_pct = ((current_value - total_cost) / total_cost * 100) if total_cost > 0 else 0

    peak_value = pkg.get("peak_value", total_cost)

    # ── 1: Target Hit ───────────────────────────────────────────────────────
    for rule in rules:
        if rule.get("type") == "target_profit" and rule.get("active"):
            target = rule["params"].get("target_pct", 20)
            if pnl_pct >= target:
                triggers.append({"trigger_id": T_TARGET_HIT, "name": "target_hit",
                    "details": f"P&L {pnl_pct:.1f}% >= target {target}%",
                    "action": "full_exit", "safety_override": False})

    # ── 2: Trailing Stop ────────────────────────────────────────────────────
    for rule in rules:
        if rule.get("type") == "trailing_stop" and rule.get("active"):
            trail_pct = rule["params"].get("current", 12)
            if peak_value > 0:
                drawdown = (peak_value - current_value) / peak_value * 100
                if drawdown >= trail_pct:
                    triggers.append({"trigger_id": T_TRAILING_STOP, "name": "trailing_stop",
                        "details": f"Drawdown {drawdown:.1f}% >= trail {trail_pct}%",
                        "action": "full_exit", "safety_override": False})

    # ── 3: Partial Profit ───────────────────────────────────────────────────
    for rule in rules:
        if rule.get("type") == "partial_profit" and rule.get("active"):
            threshold = rule["params"].get("threshold_pct", 15)
            if pnl_pct >= threshold:
                triggers.append({"trigger_id": T_PARTIAL_PROFIT, "name": "partial_profit",
                    "details": f"P&L {pnl_pct:.1f}% >= partial threshold {threshold}%",
                    "action": "partial_exit", "safety_override": False})

    # ── 4: Stop Loss ───────────────────────────────────────────────────────
    for rule in rules:
        if rule.get("type") == "stop_loss" and rule.get("active"):
            stop = rule["params"].get("stop_pct", -15)
            if pnl_pct <= stop:
                triggers.append({"trigger_id": T_STOP_LOSS, "name": "stop_loss",
                    "details": f"P&L {pnl_pct:.1f}% <= stop {stop}%",
                    "action": "full_exit", "safety_override": False})

    # ── 5: New ATH (trailing adjustment) ────────────────────────────────────
    if current_value > peak_value and peak_value > 0:
        triggers.append({"trigger_id": T_NEW_ATH_TRAILING, "name": "new_ath",
            "details": f"New peak: {current_value:.2f} > prev {peak_value:.2f}",
            "action": "tighten_trail", "safety_override": False})

    # ── 6: Correlation Break ────────────────────────────────────────────────
    if strategy == "cross_platform_arb" and len(legs) >= 2:
        yes_legs = [l for l in legs if "yes" in l.get("type", "").lower()]
        no_legs = [l for l in legs if "no" in l.get("type", "").lower()]
        if yes_legs and no_legs:
            yes_move = (yes_legs[0].get("current_price", 0) - yes_legs[0].get("entry_price", 0)) / max(yes_legs[0].get("entry_price", 1), 0.01)
            no_move = (no_legs[0].get("current_price", 0) - no_legs[0].get("entry_price", 0)) / max(no_legs[0].get("entry_price", 1), 0.01)
            # In arb, YES and NO should move inversely. Same direction = correlation break
            if yes_move > 0.05 and no_move > 0.05:
                triggers.append({"trigger_id": T_CORRELATION_BREAK, "name": "correlation_break",
                    "details": f"Both legs moving same direction: YES +{yes_move:.1%}, NO +{no_move:.1%}",
                    "action": "review", "safety_override": False})

    # ── 7: Spread Inversion (SAFETY) ────────────────────────────────────────
    if strategy == "cross_platform_arb" and len(legs) >= 2:
        yes_price = sum(l.get("current_price", 0) for l in legs if "yes" in l.get("type", "").lower())
        no_price = sum(l.get("current_price", 0) for l in legs if "no" in l.get("type", "").lower())
        combined = yes_price + no_price
        if combined > 1.0:
            triggers.append({"trigger_id": T_SPREAD_INVERSION, "name": "spread_inversion",
                "details": f"Combined price {combined:.4f} > 1.0 — spread inverted",
                "action": "immediate_exit", "safety_override": True})

    # ── 8: Spread Compression ───────────────────────────────────────────────
    if strategy == "cross_platform_arb" and len(legs) >= 2:
        yes_price = sum(l.get("current_price", 0) for l in legs if "yes" in l.get("type", "").lower())
        no_price = sum(l.get("current_price", 0) for l in legs if "no" in l.get("type", "").lower())
        spread = 1.0 - (yes_price + no_price)
        entry_spread = 1.0 - sum(l.get("entry_price", 0) for l in legs)
        if entry_spread > 0 and spread < entry_spread * 0.3:
            triggers.append({"trigger_id": T_SPREAD_COMPRESSION, "name": "spread_compression",
                "details": f"Spread compressed to {spread:.4f} (was {entry_spread:.4f})",
                "action": "review", "safety_override": False})

    # ── 9: Volume Dry-Up ───────────────────────────────────────────────────
    # Placeholder — requires volume history tracking
    pass

    # ── 10: Time <24h (SAFETY) ──────────────────────────────────────────────
    _check_expiry_triggers(legs, triggers)

    # ── 12: Time Decay (general) ────────────────────────────────────────────
    for leg in legs:
        if leg.get("expiry"):
            try:
                exp = datetime.strptime(leg["expiry"], "%Y-%m-%d").date()
                days_left = (exp - date.today()).days
                if 3 <= days_left <= 7:
                    triggers.append({"trigger_id": T_TIME_DECAY, "name": "time_decay",
                        "details": f"Leg {leg['leg_id']} expires in {days_left} days",
                        "action": "review", "safety_override": False})
            except (ValueError, TypeError):
                pass

    # ── 13: Volatility Spike ────────────────────────────────────────────────
    # Requires price history — check if any leg moved >10% in last tick
    for leg in legs:
        entry = leg.get("entry_price", 0)
        current = leg.get("current_price", 0)
        if entry > 0 and abs(current - entry) / entry > 0.15:
            triggers.append({"trigger_id": T_VOL_SPIKE, "name": "vol_spike",
                "details": f"Leg {leg['leg_id']} moved {abs(current-entry)/entry:.1%} from entry",
                "action": "review", "safety_override": False})

    # ── 14: Volatility Crush ───────────────────────────────────────────────
    # Placeholder — requires historical vol tracking
    pass

    # ── 15: Negative Drift ──────────────────────────────────────────────────
    neg_streak = pkg.get("_neg_streak", 0)
    if pnl_pct < -2 and neg_streak >= 3:
        triggers.append({"trigger_id": T_NEGATIVE_DRIFT, "name": "negative_drift",
            "details": f"Sustained negative P&L ({pnl_pct:.1f}%) for {neg_streak} ticks",
            "action": "review", "safety_override": False})

    # ── 16: Platform Error ──────────────────────────────────────────────────
    platform_errors = pkg.get("_platform_errors", 0)
    if platform_errors >= 3:
        triggers.append({"trigger_id": T_PLATFORM_ERROR, "name": "platform_error",
            "details": f"{platform_errors} consecutive platform errors",
            "action": "review", "safety_override": False})

    # ── 17: Liquidity Gap ──────────────────────────────────────────────────
    # Placeholder — requires order book depth
    pass

    # ── 18: Fee Spike ──────────────────────────────────────────────────────
    # Placeholder — requires fee monitoring
    pass

    return triggers


def _check_expiry_triggers(legs: list[dict], triggers: list[dict]):
    """Check time-based safety triggers (10, 11)."""
    now = datetime.now()
    for leg in legs:
        if not leg.get("expiry"):
            continue
        try:
            exp = datetime.strptime(leg["expiry"], "%Y-%m-%d")
            hours_left = (exp - now).total_seconds() / 3600

            if hours_left <= 6:
                triggers.append({"trigger_id": T_TIME_6H, "name": "time_6h",
                    "details": f"Leg {leg['leg_id']} expires in {hours_left:.1f}h",
                    "action": "immediate_exit", "safety_override": True})
            elif hours_left <= 24:
                triggers.append({"trigger_id": T_TIME_24H, "name": "time_24h",
                    "details": f"Leg {leg['leg_id']} expires in {hours_left:.1f}h",
                    "action": "immediate_exit", "safety_override": True})
        except (ValueError, TypeError):
            pass


class ExitEngine:
    """30-second scan loop that evaluates open packages and routes triggers."""

    def __init__(self, position_manager, ai_advisor=None, interval: float = 30.0):
        self.pm = position_manager
        self.ai = ai_advisor
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self):
        """Start the exit engine scan loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("Exit engine started (interval=%.1fs)", self.interval)

    def stop(self):
        """Stop the scan loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Exit engine stopped")

    async def _loop(self):
        """Main scan loop."""
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Exit engine tick error: %s", e)
            await asyncio.sleep(self.interval)

    async def _tick(self):
        """Process one scan cycle — evaluate all open packages."""
        open_pkgs = self.pm.list_packages("open")
        for pkg in open_pkgs:
            await self._update_prices(pkg)
            self.pm.update_pnl(pkg["id"])

            # I6: Track negative streak regardless of triggers
            if pkg.get("unrealized_pnl", 0) < 0:
                pkg["_neg_streak"] = pkg.get("_neg_streak", 0) + 1
            else:
                pkg["_neg_streak"] = 0

            triggers = evaluate_heuristics(pkg)
            if not triggers:
                continue

            await self._process_triggers(pkg, triggers)

    async def _update_prices(self, pkg: dict):
        """Fetch current prices for all open legs via their platform executors."""
        for leg in pkg.get("legs", []):
            if leg["status"] != "open":
                continue
            executor = self.pm.executors.get(leg["platform"])
            if not executor:
                continue
            try:
                price = await executor.get_current_price(leg["asset_id"])
                if price > 0:
                    leg["current_price"] = price
                    leg["current_value"] = leg["quantity"] * price
                    pkg["_platform_errors"] = 0  # I5: Reset on success
            except Exception as e:
                logger.warning("Price fetch failed for %s: %s", leg["asset_id"], e)
                pkg["_platform_errors"] = pkg.get("_platform_errors", 0) + 1

    async def _process_triggers(self, pkg: dict, triggers: list[dict]):
        """Route triggers — safety overrides execute immediately, others go to AI."""
        safety_triggers = [t for t in triggers if t.get("safety_override")]
        ai_triggers = [t for t in triggers if not t.get("safety_override")]

        # Safety overrides — immediate exit, no AI
        for trigger in safety_triggers:
            logger.warning("SAFETY OVERRIDE [%s] on %s: %s", trigger["name"], pkg["id"], trigger["details"])
            for leg in pkg["legs"]:
                if leg["status"] == "open":
                    await self.pm.exit_leg(pkg["id"], leg["leg_id"], trigger=trigger["name"])

        # Non-safety triggers — batch to AI advisor
        if ai_triggers and self.ai:
            try:
                verdicts = await self.ai.review_proposals(pkg, ai_triggers)
                await self._apply_verdicts(pkg, ai_triggers, verdicts)
            except Exception as e:
                logger.error("AI review failed for %s: %s", pkg["id"], e)
                # Escalate as alert
                self.pm.add_alert(pkg["id"], 0, "ai_review_failed",
                    {"error": str(e), "triggers": [t["name"] for t in ai_triggers]})
        elif ai_triggers:
            # No AI advisor — auto-execute clear-cut triggers, escalate ambiguous ones
            for trigger in ai_triggers:
                if trigger["name"] in ("target_hit", "stop_loss", "trailing_stop"):
                    # These are mechanical rules — safe to auto-execute without AI review
                    logger.info("Auto-executing %s on %s (no AI): %s",
                                trigger["name"], pkg["id"], trigger["details"])
                    if trigger.get("action") == "full_exit":
                        for leg in pkg["legs"]:
                            if leg["status"] == "open":
                                await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                    trigger=f"auto:{trigger['name']}")
                elif trigger["name"] == "partial_profit":
                    # Partial profit — exit first open leg
                    logger.info("Auto-executing partial_profit on %s (no AI): %s",
                                pkg["id"], trigger["details"])
                    for leg in pkg["legs"]:
                        if leg["status"] == "open":
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                trigger=f"auto:{trigger['name']}")
                            break
                else:
                    # Ambiguous triggers (correlation_break, vol_spike, etc.) — escalate
                    self.pm.add_alert(pkg["id"], trigger["trigger_id"], trigger["name"],
                        {"details": trigger["details"], "action": trigger["action"]})

    async def _apply_verdicts(self, pkg: dict, triggers: list[dict], verdicts: dict):
        """Apply AI verdicts — APPROVE=execute, MODIFY=adjust, REJECT=skip."""
        for trigger in triggers:
            rule_id = trigger.get("rule_id", trigger.get("name", ""))
            verdict = verdicts.get(rule_id, {})
            action = verdict.get("action", "REJECT")

            if action == "APPROVE":
                if trigger.get("action") == "full_exit":
                    for leg in pkg["legs"]:
                        if leg["status"] == "open":
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                trigger=f"ai_approved:{trigger['name']}")
                elif trigger.get("action") == "partial_exit":
                    # Exit first open leg as partial
                    for leg in pkg["legs"]:
                        if leg["status"] == "open":
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                trigger=f"ai_partial:{trigger['name']}")
                            break

            elif action == "MODIFY":
                # Adjust rule parameters within bounds
                new_value = verdict.get("value")
                if new_value is not None:
                    for rule in pkg.get("exit_rules", []):
                        if rule.get("type") == trigger.get("name"):
                            bounds = rule["params"]
                            bmin = bounds.get("bound_min", 0)
                            bmax = bounds.get("bound_max", 100)
                            if bmin <= new_value <= bmax:
                                rule["params"]["current"] = new_value
                                logger.info("AI modified %s to %s", rule["type"], new_value)
                            else:
                                # Out of bounds — escalate
                                self.pm.add_alert(pkg["id"], trigger["trigger_id"],
                                    f"modify_out_of_bounds:{trigger['name']}",
                                    {"requested": new_value, "bounds": [bmin, bmax]})
                    self.pm.save()

            # REJECT = do nothing, just log
            elif action == "REJECT":
                logger.info("AI rejected trigger %s for %s: %s",
                    trigger["name"], pkg["id"], verdict.get("reason", ""))
