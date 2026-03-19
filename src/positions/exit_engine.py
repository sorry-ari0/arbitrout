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
    """60-second scan loop that evaluates open packages and routes triggers."""

    # Cooldown (seconds) per trigger type — prevents spamming the same trigger
    # every tick. Safety overrides and mechanical exits have no cooldown.
    TRIGGER_COOLDOWN = {
        "time_decay": 3600,      # 1 hour — not actionable every 60s
        "new_ath": 1800,         # 30 min — tighten trail is infrequent
        "vol_spike": 3600,       # 1 hour — informational
        "spread_compression": 1800,
        "negative_drift": 900,   # 15 min — more urgent
        "correlation_break": 600,
    }
    # No cooldown: target_hit, stop_loss, trailing_stop, safety overrides

    def __init__(self, position_manager, ai_advisor=None, interval: float = 60.0, decision_logger=None):
        self.pm = position_manager
        self.ai = ai_advisor
        self.interval = interval
        self.dlog = decision_logger
        self._task: asyncio.Task | None = None
        self._running = False
        # Cooldown tracker: {(pkg_id, trigger_name): last_fire_timestamp}
        self._trigger_cooldowns: dict[tuple[str, str], float] = {}

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
        ai_reviews_this_tick = 0
        for pkg in open_pkgs:
            await self._update_prices(pkg)
            self.pm.update_pnl(pkg["id"])

            # I6: Track negative streak regardless of triggers
            if pkg.get("unrealized_pnl", 0) < 0:
                pkg["_neg_streak"] = pkg.get("_neg_streak", 0) + 1
            else:
                pkg["_neg_streak"] = 0

            # C5 fix: skip packages currently being exited by another trigger
            if pkg.get("_exiting"):
                continue

            triggers = evaluate_heuristics(pkg)
            if not triggers:
                continue

            # Filter out triggers that are on cooldown (prevents spam)
            now = time.time()
            pkg_id = pkg["id"]
            filtered = []
            for t in triggers:
                tname = t.get("name", "")
                cooldown = self.TRIGGER_COOLDOWN.get(tname, 0)
                if cooldown > 0:
                    key = (pkg_id, tname)
                    last_fire = self._trigger_cooldowns.get(key, 0)
                    if now - last_fire < cooldown:
                        continue  # Still on cooldown, skip
                    self._trigger_cooldowns[key] = now
                filtered.append(t)

            if not filtered:
                continue

            if self.dlog:
                self.dlog.log_triggers_fired(pkg_id, pkg.get("name", "?"), filtered)

            # Limit AI reviews to 3 per tick to avoid rate limiting
            has_ai_triggers = any(not t.get("safety_override") for t in filtered)
            if has_ai_triggers and ai_reviews_this_tick >= 3:
                continue  # will catch up on next tick
            if has_ai_triggers:
                ai_reviews_this_tick += 1

            await self._process_triggers(pkg, filtered)

            # Brief delay between packages to space out API calls
            if has_ai_triggers:
                await asyncio.sleep(2)

    async def _update_prices(self, pkg: dict):
        """Fetch current prices for all open legs via their platform executors."""
        for leg in pkg.get("legs", []):
            if leg["status"] != "open":
                continue
            executor = self.pm.executors.get(leg["platform"])
            if not executor:
                logger.debug("No executor for platform %s (leg %s)", leg.get("platform"), leg.get("leg_id"))
                continue
            try:
                price = await executor.get_current_price(leg["asset_id"])
                if price > 0:
                    old_price = leg.get("current_price", 0)
                    leg["current_price"] = price
                    leg["current_value"] = leg["quantity"] * price
                    pkg["_platform_errors"] = 0  # I5: Reset on success
                    if old_price > 0 and abs(price - old_price) / old_price > 0.05:
                        logger.info("Price moved >5%% for %s: %.4f -> %.4f", leg["asset_id"], old_price, price)
                else:
                    logger.warning("Price fetch returned 0 for %s — keeping stale price %.4f",
                                   leg["asset_id"], leg.get("current_price", 0))
                    pkg["_platform_errors"] = pkg.get("_platform_errors", 0) + 1
            except Exception as e:
                logger.warning("Price fetch failed for %s: %s", leg["asset_id"], e)
                pkg["_platform_errors"] = pkg.get("_platform_errors", 0) + 1

    async def _process_triggers(self, pkg: dict, triggers: list[dict]):
        """Route triggers — safety overrides execute immediately, others go to AI."""
        # C5 fix: guard against double-fire on same package
        if pkg.get("_exiting"):
            return
        pkg["_exiting"] = True
        try:
            safety_triggers = [t for t in triggers if t.get("safety_override")]
            ai_triggers = [t for t in triggers if not t.get("safety_override")]

            # Safety overrides — immediate exit, no AI
            for trigger in safety_triggers:
                logger.warning("SAFETY OVERRIDE [%s] on %s: %s", trigger["name"], pkg["id"], trigger["details"])
                if self.dlog:
                    self.dlog.log_safety_override(pkg["id"], trigger["name"], trigger["details"])
                for leg in pkg["legs"]:
                    if leg["status"] == "open":
                        await self.pm.exit_leg(pkg["id"], leg["leg_id"], trigger=trigger["name"])

            # Non-safety triggers — try AI advisor first, fall back to auto-execute
            if ai_triggers:
                ai_handled = False
                if self.ai and self.ai.is_available:
                    try:
                        t0 = time.time()
                        verdicts = await self.ai.review_proposals(pkg, ai_triggers)
                        elapsed_ms = int((time.time() - t0) * 1000)
                        if verdicts:
                            await self._apply_verdicts(pkg, ai_triggers, verdicts)
                            ai_handled = True
                            logger.info("AI reviewed %d triggers for %s", len(ai_triggers), pkg["id"])
                            if self.dlog:
                                self.dlog.log_ai_review(
                                    pkg["id"],
                                    provider=getattr(self.ai, '_last_provider', '?'),
                                    triggers=[t["name"] for t in ai_triggers],
                                    verdicts=verdicts,
                                    elapsed_ms=elapsed_ms,
                                )
                    except Exception as e:
                        logger.error("AI review failed for %s: %s — falling back to auto-execute", pkg["id"], e)
                        if self.dlog:
                            self.dlog.log_ai_failure(pkg["id"], str(e))

                if not ai_handled:
                    # No AI available or AI returned empty — auto-execute mechanical triggers
                    for trigger in ai_triggers:
                        if trigger["name"] in ("target_hit", "stop_loss", "trailing_stop"):
                            logger.info("Auto-executing %s on %s: %s",
                                        trigger["name"], pkg["id"], trigger["details"])
                            if self.dlog:
                                self.dlog.log_auto_execute(pkg["id"], trigger["name"],
                                                           trigger.get("action", "full_exit"), trigger["details"])
                            if trigger.get("action") == "full_exit":
                                for leg in pkg["legs"]:
                                    if leg["status"] == "open":
                                        await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                            trigger=f"auto:{trigger['name']}")
                        elif trigger["name"] == "partial_profit":
                            logger.info("Auto-executing partial_profit on %s: %s",
                                        pkg["id"], trigger["details"])
                            if self.dlog:
                                self.dlog.log_auto_execute(pkg["id"], "partial_profit",
                                                           "partial_exit", trigger["details"])
                            for leg in pkg["legs"]:
                                if leg["status"] == "open":
                                    await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                        trigger=f"auto:{trigger['name']}")
                                    break
                        elif trigger["name"] in ("correlation_break", "time_decay", "negative_drift", "platform_error"):
                            # Only escalate truly actionable ambiguous triggers
                            self.pm.add_alert(pkg["id"], trigger["trigger_id"], trigger["name"],
                                {"details": trigger["details"], "action": trigger["action"]})
                        else:
                            # Skip noisy triggers (vol_spike, new_ath, spread_compression) — not actionable without AI
                            if self.dlog:
                                self.dlog.log_trigger_suppressed(pkg["id"], trigger["name"], "noisy_without_ai")
        finally:
            pkg.pop("_exiting", None)

    def _find_verdict(self, trigger: dict, verdicts: dict) -> dict:
        """Find the verdict for a trigger, handling various AI response key formats.

        The AI may respond with keys like:
        - "time_decay" (exact match — ideal)
        - "Trigger #12 (time_decay)" (wrapped with trigger ID)
        - "new_ath" or "negative_drift" (just the name)
        """
        name = trigger.get("name", "")
        rule_id = trigger.get("rule_id", name)

        # Try exact match first
        if rule_id in verdicts:
            return verdicts[rule_id]
        if name in verdicts:
            return verdicts[name]

        # Fuzzy match: look for the trigger name inside any verdict key
        for key, val in verdicts.items():
            if name and name in key:
                return val

        return {}

    async def _apply_verdicts(self, pkg: dict, triggers: list[dict], verdicts: dict):
        """Apply AI verdicts — APPROVE=execute, MODIFY=adjust, REJECT=skip."""
        for trigger in triggers:
            verdict = self._find_verdict(trigger, verdicts)
            action = verdict.get("action", "REJECT")

            if action == "APPROVE":
                trig_action = trigger.get("action", "review")
                if trig_action in ("full_exit", "review"):
                    # "review" triggers approved by AI → execute as full exit
                    for leg in pkg["legs"]:
                        if leg["status"] == "open":
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                trigger=f"ai_approved:{trigger['name']}")
                elif trig_action == "partial_exit":
                    # Exit first open leg as partial
                    for leg in pkg["legs"]:
                        if leg["status"] == "open":
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                trigger=f"ai_partial:{trigger['name']}")
                            break
                elif trig_action == "tighten_trail":
                    # new_ath: tighten trailing stop by 2% on approval
                    for rule in pkg.get("exit_rules", []):
                        if rule.get("type") == "trailing_stop" and rule.get("active"):
                            current = rule["params"].get("current", 12)
                            bmin = rule["params"].get("bound_min", 5)
                            new_val = max(current - 2, bmin)
                            if new_val != current:
                                rule["params"]["current"] = new_val
                                logger.info("AI approved tighten_trail: %s -> %s", current, new_val)
                                self.pm.save()

            elif action == "MODIFY":
                # Adjust rule parameters within bounds
                new_value = verdict.get("value")
                if new_value is not None:
                    matched_rule = False
                    for rule in pkg.get("exit_rules", []):
                        if rule.get("type") == trigger.get("name"):
                            bounds = rule["params"]
                            bmin = bounds.get("bound_min", 0)
                            bmax = bounds.get("bound_max", 100)
                            if bmin <= new_value <= bmax:
                                rule["params"]["current"] = new_value
                                logger.info("AI modified %s to %s", rule["type"], new_value)
                                matched_rule = True
                            else:
                                self.pm.add_alert(pkg["id"], trigger["trigger_id"],
                                    f"modify_out_of_bounds:{trigger['name']}",
                                    {"requested": new_value, "bounds": [bmin, bmax]})
                                matched_rule = True
                    if not matched_rule:
                        logger.debug("MODIFY for %s has no matching exit rule — skipping", trigger.get("name"))
                    else:
                        self.pm.save()

            # REJECT = do nothing, just log
            elif action == "REJECT":
                logger.info("AI rejected trigger %s for %s: %s",
                    trigger["name"], pkg["id"], verdict.get("reason", ""))
                if self.dlog:
                    self.dlog.log_trigger_suppressed(pkg["id"], trigger["name"],
                                                     f"ai_rejected: {verdict.get('reason', '')}")
