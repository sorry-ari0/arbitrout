"""Trade journal — tracks completed trades, win/loss metrics, and learnings for iterative improvement.

Persists to trade_journal_{mode}.json alongside positions.json.
Records: entry/exit prices, P&L, triggers that fired, AI verdicts, and strategy performance.
"""
import json
import logging
import os
import random
import time
import uuid
from pathlib import Path

logger = logging.getLogger("positions.trade_journal")


class TradeJournal:
    """Tracks trade outcomes for performance analysis and strategy improvement."""

    def __init__(self, data_dir: Path, mode: str = "paper"):
        self.data_dir = Path(data_dir)
        self.mode = mode
        self.entries: list[dict] = []
        self._migrate_old_file()
        self._load()

    def _journal_filename(self) -> str:
        """Return the mode-specific journal filename."""
        return f"trade_journal_{self.mode}.json"

    def _migrate_old_file(self):
        """One-time migration: rename legacy trade_journal.json to trade_journal_paper.json."""
        if self.mode != "paper":
            return
        old_path = self.data_dir / "trade_journal.json"
        new_path = self.data_dir / self._journal_filename()
        if old_path.exists() and not new_path.exists():
            os.rename(str(old_path), str(new_path))
            logger.info("Migrated trade_journal.json → %s", self._journal_filename())

    def _load(self):
        path = self.data_dir / self._journal_filename()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.entries = data.get("entries", [])
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load trade journal: %s", e)

    def save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / self._journal_filename()
        # Backup rotation before save — protects against corruption
        backup = str(path) + ".backup"
        if path.exists():
            try:
                import shutil
                shutil.copy(str(path), backup)
            except OSError as e:
                logger.warning("Failed to create journal backup: %s", e)
        tmp = str(path) + ".tmp"
        data = {
            "entries": self.entries,
            "saved_at": time.time(),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))

    def get_cumulative_pnl(self) -> float:
        """Sum of all closed trade PnL in this journal."""
        return sum(e.get("pnl", 0.0) for e in self.entries)

    def record_close(self, pkg: dict, exit_trigger: str = "manual"):
        """Record a completed trade (package close) with full details including fees."""
        # Belt-and-suspenders idempotency: reject if this package was already journaled
        pkg_id = pkg.get("id")
        if pkg_id is not None and any(e.get("package_id") == pkg_id for e in self.entries):
            logger.debug("Package %s already journaled, skipping duplicate", pkg_id)
            return None

        closed_at = pkg.get("closed_at") or pkg.get("updated_at") or time.time()

        legs_detail = []
        total_buy_fees = 0.0
        total_sell_fees = 0.0
        for leg in pkg.get("legs", []):
            entry_p = leg.get("entry_price", 0)
            exit_p = leg.get("exit_price", leg.get("current_price", entry_p))
            cost = leg.get("cost", 0)
            exit_val = leg.get("quantity", 0) * exit_p
            buy_fees = leg.get("buy_fees", 0)
            sell_fees = leg.get("sell_fees", 0)
            total_buy_fees += buy_fees
            total_sell_fees += sell_fees
            # Leg P&L includes fees
            leg_pnl = exit_val - cost - buy_fees - sell_fees
            legs_detail.append({
                "leg_id": leg.get("leg_id"),
                "platform": leg.get("platform"),
                "type": leg.get("type"),
                "asset_id": leg.get("asset_id"),
                "entry_price": entry_p,
                "exit_price": exit_p,
                "quantity": leg.get("quantity", 0),
                "cost": cost,
                "exit_value": round(exit_val, 4),
                "buy_fees": round(buy_fees, 4),
                "sell_fees": round(sell_fees, 4),
                "leg_pnl": round(leg_pnl, 4),
                "leg_pnl_pct": round(leg_pnl / cost * 100, 2) if cost > 0 else 0,
                "status": leg.get("status"),
                "exit_order_type": leg.get("exit_order_type", "fok_direct"),
                "fee_model": leg.get("fee_model", "unknown"),
            })

        total_cost = pkg.get("total_cost", 0)
        total_fees = total_buy_fees + total_sell_fees
        # Recalculate exit value from actual leg exit data (not stale pkg["current_value"])
        current_value = sum(ld["exit_value"] for ld in legs_detail)
        # P&L after all fees
        pnl = current_value - total_cost - total_fees

        fee_models = {ld["fee_model"] for ld in legs_detail if ld.get("fee_model") and ld["fee_model"] != "unknown"}
        package_fee_model = next(iter(fee_models)) if len(fee_models) == 1 else ("mixed" if len(fee_models) > 1 else "unknown")

        entry = {
            "id": f"journal_{uuid.uuid4().hex[:8]}",
            "package_id": pkg.get("id"),
            "name": pkg.get("name"),
            "strategy_type": pkg.get("strategy_type"),
            "ai_strategy": pkg.get("ai_strategy"),
            "mode": "paper" if any("paper_" in (l.get("tx_id") or "") for l in pkg.get("legs", [])) else "live",
            "total_cost": round(total_cost, 4),
            "exit_value": round(current_value, 4),
            "total_fees": round(total_fees, 4),
            "buy_fees": round(total_buy_fees, 4),
            "sell_fees": round(total_sell_fees, 4),
            "pnl": round(pnl, 4),  # 4-decimal for computation
            "pnl_usd": round(pnl, 2),  # 2-decimal display value for cross-referencing
            "pnl_pct": round(pnl / total_cost * 100, 2) if total_cost > 0 else 0,
            "outcome": "win" if pnl > 0.001 else ("loss" if pnl < -0.001 else "flat"),
            "exit_trigger": exit_trigger,
            "exit_order_type": pkg.get("legs", [{}])[0].get("exit_order_type", "fok_direct"),
            "fee_model": package_fee_model,
            "legs": legs_detail,
            "exit_rules": pkg.get("exit_rules", []),
            "execution_log": pkg.get("execution_log", []),
            "hold_duration_hours": round((closed_at - pkg.get("created_at", closed_at)) / 3600, 1),
            "peak_value": pkg.get("peak_value", 0),
            "max_drawdown_from_peak": round(
                (pkg.get("peak_value", 0) - current_value) / pkg.get("peak_value", 1) * 100, 2
            ) if pkg.get("peak_value", 0) > 0 else 0,
            "created_at": pkg.get("created_at"),
            "closed_at": closed_at,
            "_code_version": "v2-fee-fix",
        }

        self.entries.append(entry)
        self.save()
        logger.info("Journal: %s %s — P&L: $%.2f (%.1f%%) via %s",
                     entry["outcome"].upper(), entry["name"], pnl, entry["pnl_pct"], exit_trigger)
        return entry

    def reconcile_closed_packages(self, packages: list[dict]) -> list[dict]:
        """Backfill journal entries for closed packages missing from the journal.

        This protects analytics from stale-state overwrites or historical closes that
        were persisted to positions.json but never made it into trade_journal_{mode}.json.
        """
        known_ids = {e.get("package_id") for e in self.entries if e.get("package_id")}
        added = []

        for pkg in packages:
            if pkg.get("status") != "closed":
                continue
            pkg_id = pkg.get("id")
            if not pkg_id or pkg_id in known_ids:
                continue

            trigger = self._infer_exit_trigger(pkg)
            entry = self.record_close(pkg, exit_trigger=trigger)
            if entry is None:
                continue

            pkg["_journal_recorded"] = True
            pkg["journal_entry_id"] = entry["id"]
            added.append(entry)
            known_ids.add(pkg_id)

        return added

    @staticmethod
    def _infer_exit_trigger(pkg: dict) -> str:
        """Infer a close trigger from persisted package state for backfills."""
        exec_log = pkg.get("execution_log", [])
        for action in reversed(exec_log):
            if action.get("action") in ("sell", "partial_sell"):
                trigger = action.get("trigger")
                if trigger:
                    return trigger

        for leg in pkg.get("legs", []):
            trigger = leg.get("exit_trigger")
            if trigger:
                return trigger

        return "reconciled_close"

    def get_performance(self, mode: str = None, strategy: str = None) -> dict:
        """Aggregate performance stats, optionally filtered by mode or strategy."""
        filtered = self.entries
        if mode:
            filtered = [e for e in filtered if e.get("mode") == mode]
        if strategy:
            filtered = [e for e in filtered if e.get("strategy_type") == strategy]

        if not filtered:
            return {"total_trades": 0, "message": "No trades recorded yet"}

        wins = [e for e in filtered if e["outcome"] == "win"]
        losses = [e for e in filtered if e["outcome"] == "loss"]
        total_pnl = sum(e["pnl"] for e in filtered)
        total_invested = sum(e["total_cost"] for e in filtered)
        total_fees = sum(e.get("total_fees", 0) for e in filtered)
        avg_hold = sum(e.get("hold_duration_hours", 0) for e in filtered) / len(filtered)

        # Best/worst trades
        best = max(filtered, key=lambda e: e["pnl"])
        worst = min(filtered, key=lambda e: e["pnl"])

        # Per-strategy breakdown
        strategies = {}
        for e in filtered:
            st = e.get("strategy_type", "unknown")
            if st not in strategies:
                strategies[st] = {"trades": 0, "wins": 0, "pnl": 0}
            strategies[st]["trades"] += 1
            if e["outcome"] == "win":
                strategies[st]["wins"] += 1
            strategies[st]["pnl"] += e["pnl"]
        for st in strategies:
            s = strategies[st]
            s["win_rate"] = round(s["wins"] / s["trades"], 2) if s["trades"] > 0 else 0
            s["pnl"] = round(s["pnl"], 2)

        # Per-trigger breakdown
        triggers = {}
        for e in filtered:
            t = e.get("exit_trigger", "unknown")
            if t not in triggers:
                triggers[t] = {"trades": 0, "wins": 0, "pnl": 0}
            triggers[t]["trades"] += 1
            if e["outcome"] == "win":
                triggers[t]["wins"] += 1
            triggers[t]["pnl"] += e["pnl"]
        for t in triggers:
            tr = triggers[t]
            tr["win_rate"] = round(tr["wins"] / tr["trades"], 2) if tr["trades"] > 0 else 0
            tr["pnl"] = round(tr["pnl"], 2)

        # Streak tracking
        current_streak = 0
        streak_type = None
        max_win_streak = 0
        max_loss_streak = 0
        win_streak = 0
        loss_streak = 0
        for e in sorted(filtered, key=lambda x: x.get("closed_at", 0)):
            if e["outcome"] == "win":
                win_streak += 1
                loss_streak = 0
                max_win_streak = max(max_win_streak, win_streak)
            elif e["outcome"] == "loss":
                loss_streak += 1
                win_streak = 0
                max_loss_streak = max(max_loss_streak, loss_streak)
            else:
                win_streak = 0
                loss_streak = 0

        return {
            "total_trades": len(filtered),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(filtered), 3),
            "total_pnl": round(total_pnl, 2),
            "total_invested": round(total_invested, 2),
            "total_fees": round(total_fees, 2),
            "fee_drag_pct": round(total_fees / total_invested * 100, 2) if total_invested > 0 else 0,
            "roi_pct": round(total_pnl / total_invested * 100, 2) if total_invested > 0 else 0,
            "avg_pnl_per_trade": round(total_pnl / len(filtered), 2),
            "avg_win": round(sum(e["pnl"] for e in wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(e["pnl"] for e in losses) / len(losses), 2) if losses else 0,
            "avg_hold_hours": round(avg_hold, 1),
            "best_trade": {"name": best["name"], "pnl": best["pnl"], "pnl_pct": best["pnl_pct"]},
            "worst_trade": {"name": worst["name"], "pnl": worst["pnl"], "pnl_pct": worst["pnl_pct"]},
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "by_strategy": strategies,
            "by_trigger": triggers,
        }

    def get_recent(self, limit: int = 20) -> list[dict]:
        """Get most recent journal entries."""
        return sorted(self.entries, key=lambda e: e.get("closed_at", 0), reverse=True)[:limit]

    def get_performance_by_hold_duration(self, mode: str | None = None) -> dict:
        """Bucket trades by hold duration and compute per-bucket metrics."""
        filtered = self.entries if not mode else [e for e in self.entries if e.get("mode") == mode]
        buckets = {
            "0-6h": {"max_hours": 6},
            "6-24h": {"max_hours": 24},
            "24h-3d": {"max_hours": 72},
            "3d-7d": {"max_hours": 168},
            "7d+": {"max_hours": float("inf")},
        }
        result = {}
        for name, cfg in buckets.items():
            result[name] = {"trades": 0, "wins": 0, "pnl": 0.0, "avg_pnl": 0.0, "win_rate": 0.0}

        for e in filtered:
            hours = e.get("hold_duration_hours", 0)
            for name, cfg in buckets.items():
                prev_max = {"0-6h": 0, "6-24h": 6, "24h-3d": 24, "3d-7d": 72, "7d+": 168}.get(name, 0)
                if prev_max <= hours < cfg["max_hours"]:
                    result[name]["trades"] += 1
                    result[name]["pnl"] += e.get("pnl", 0)
                    if e.get("outcome") == "win":
                        result[name]["wins"] += 1
                    break

        for name in result:
            b = result[name]
            if b["trades"] > 0:
                b["win_rate"] = round(b["wins"] / b["trades"], 2)
                b["avg_pnl"] = round(b["pnl"] / b["trades"], 2)
            b["pnl"] = round(b["pnl"], 2)

        return result

    def get_equity_curve(self, mode: str | None = None) -> dict:
        """Cumulative P&L over time — the authoritative USD equity tracker.

        Returns chronological list of (timestamp, cumulative_pnl, cumulative_fees,
        trade_count) plus summary stats. Survives server restarts (journal is persistent).
        """
        filtered = self.entries if not mode else [e for e in self.entries if e.get("mode") == mode]
        sorted_entries = sorted(filtered, key=lambda e: e.get("closed_at", 0))

        curve = []
        cumulative_pnl = 0.0
        cumulative_fees = 0.0
        peak_equity = 0.0
        max_drawdown = 0.0

        for i, e in enumerate(sorted_entries):
            pnl = e.get("pnl", 0)
            fees = e.get("total_fees", 0)
            cumulative_pnl += pnl
            cumulative_fees += fees
            peak_equity = max(peak_equity, cumulative_pnl)
            drawdown = peak_equity - cumulative_pnl
            max_drawdown = max(max_drawdown, drawdown)

            curve.append({
                "trade_num": i + 1,
                "closed_at": e.get("closed_at"),
                "name": e.get("name", ""),
                "pnl_usd": round(pnl, 2),
                "cumulative_pnl_usd": round(cumulative_pnl, 2),
                "cumulative_fees_usd": round(cumulative_fees, 2),
                "exit_trigger": e.get("exit_trigger", ""),
            })

        return {
            "total_trades": len(sorted_entries),
            "cumulative_pnl_usd": round(cumulative_pnl, 2),
            "cumulative_fees_usd": round(cumulative_fees, 2),
            "peak_equity_usd": round(peak_equity, 2),
            "max_drawdown_usd": round(max_drawdown, 2),
            "curve": curve,
        }

    def get_diagnostics(self, mode: str | None = None) -> dict:
        """Return a compact diagnostics report for evidence-based reviews."""
        filtered = self.entries if not mode else [e for e in self.entries if e.get("mode") == mode]
        if not filtered:
            return {
                "total_trades": 0,
                "message": "No trades recorded yet",
            }

        sorted_entries = sorted(filtered, key=lambda e: e.get("closed_at", 0))
        first_closed_at = sorted_entries[0].get("closed_at")
        last_closed_at = sorted_entries[-1].get("closed_at")
        coverage_days = 0.0
        if first_closed_at and last_closed_at:
            coverage_days = round((last_closed_at - first_closed_at) / 86400, 2)

        perf = self.get_performance(mode=mode)
        hold = self.get_performance_by_hold_duration(mode=mode)
        equity = self.get_equity_curve(mode=mode)
        robustness = self.validate_robustness(mode=mode, n_simulations=50)

        return {
            "mode": mode or "all",
            "total_trades": len(filtered),
            "first_closed_at": first_closed_at,
            "last_closed_at": last_closed_at,
            "coverage_days": coverage_days,
            "total_pnl": perf.get("total_pnl", 0),
            "roi_pct": perf.get("roi_pct", 0),
            "fee_drag_pct": perf.get("fee_drag_pct", 0),
            "win_rate": perf.get("win_rate", 0),
            "avg_hold_hours": perf.get("avg_hold_hours", 0),
            "max_loss_streak": perf.get("max_loss_streak", 0),
            "by_trigger": perf.get("by_trigger", {}),
            "by_strategy": perf.get("by_strategy", {}),
            "by_hold_duration": hold,
            "equity_summary": {
                "cumulative_pnl_usd": equity.get("cumulative_pnl_usd", 0),
                "cumulative_fees_usd": equity.get("cumulative_fees_usd", 0),
                "max_drawdown_usd": equity.get("max_drawdown_usd", 0),
            },
            "robustness": robustness,
        }

    def validate_robustness(self, mode: str | None = None, n_simulations: int = 100,
                            jitter_pct: float = 0.20, skip_pct: float = 0.10) -> dict:
        """Monte Carlo robustness validation of current strategy parameters.

        Research: walk-forward optimization prevents curve-fitting.
        Three tests:
        1. Parameter jitter: randomly adjust P&L by ±jitter_pct, check if still profitable
        2. Trade shuffle: randomize trade order, check if drawdown stays manageable
        3. Skip test: randomly drop skip_pct of trades, check if still profitable

        Returns verdict: "robust", "fragile", or "insufficient_data".
        """
        filtered = self.entries if not mode else [e for e in self.entries if e.get("mode") == mode]
        if len(filtered) < 10:
            return {
                "verdict": "insufficient_data",
                "total_trades": len(filtered),
                "message": "Need at least 10 trades for robustness validation",
            }

        pnls = [e.get("pnl", 0) for e in filtered]
        base_total_pnl = sum(pnls)

        # Test 1: Parameter jitter — simulate ±jitter_pct variance on each trade's P&L
        jitter_profitable = 0
        for _ in range(n_simulations):
            jittered_pnl = sum(
                p * (1 + random.uniform(-jitter_pct, jitter_pct)) for p in pnls
            )
            if jittered_pnl > 0:
                jitter_profitable += 1
        jitter_pass_rate = jitter_profitable / n_simulations

        # Test 2: Trade shuffle — randomize order, check max drawdown
        base_max_dd = self._calc_max_drawdown(pnls)
        shuffle_pass = 0
        for _ in range(n_simulations):
            shuffled = pnls.copy()
            random.shuffle(shuffled)
            dd = self._calc_max_drawdown(shuffled)
            # Pass if drawdown doesn't exceed 2x base drawdown
            if dd <= max(base_max_dd * 2, abs(base_total_pnl) * 0.5):
                shuffle_pass += 1
        shuffle_pass_rate = shuffle_pass / n_simulations

        # Test 3: Skip test — drop random 10% of trades
        skip_profitable = 0
        skip_count = max(1, int(len(pnls) * skip_pct))
        for _ in range(n_simulations):
            indices = random.sample(range(len(pnls)), max(0, len(pnls) - skip_count))
            skipped_pnl = sum(pnls[i] for i in indices)
            if skipped_pnl > 0:
                skip_profitable += 1
        skip_pass_rate = skip_profitable / n_simulations

        # Verdict: all three must pass at >60% to be "robust"
        all_pass = jitter_pass_rate > 0.60 and shuffle_pass_rate > 0.60 and skip_pass_rate > 0.60
        verdict = "robust" if all_pass else "fragile"

        result = {
            "verdict": verdict,
            "total_trades": len(filtered),
            "base_total_pnl": round(base_total_pnl, 2),
            "jitter_test": {
                "pass_rate": round(jitter_pass_rate, 3),
                "jitter_pct": jitter_pct,
                "passed": jitter_pass_rate > 0.60,
            },
            "shuffle_test": {
                "pass_rate": round(shuffle_pass_rate, 3),
                "base_max_drawdown": round(base_max_dd, 2),
                "passed": shuffle_pass_rate > 0.60,
            },
            "skip_test": {
                "pass_rate": round(skip_pass_rate, 3),
                "skip_pct": skip_pct,
                "passed": skip_pass_rate > 0.60,
            },
        }

        logger.info("Robustness validation: %s (jitter=%.0f%%, shuffle=%.0f%%, skip=%.0f%%)",
                     verdict, jitter_pass_rate * 100, shuffle_pass_rate * 100, skip_pass_rate * 100)
        return result

    @staticmethod
    def _calc_max_drawdown(pnls: list[float]) -> float:
        """Calculate maximum drawdown from a sequence of P&L values."""
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cumulative += p
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)
        return max_dd
