"""Decision logger — records all trading decisions to JSONL for later review.

Logs: buys, skips, trigger fires, AI verdicts, exits, safety overrides.
Each line is a JSON object with timestamp, type, and context-specific fields.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger("positions.decision_log")

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "positions")
LOG_FILE = os.path.join(LOG_DIR, "decision_log.jsonl")


class DecisionLogger:
    """Append-only JSONL logger for trading decisions."""

    def __init__(self, path: str = LOG_FILE):
        self._path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    @staticmethod
    def _normalize_timestamp(ts=None) -> str:
        """Return an ISO-8601 UTC timestamp for persisted log entries."""
        if ts is None:
            return datetime.utcnow().isoformat() + "Z"
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(ts, str):
            return ts
        return str(ts)

    def _write(self, entry: dict):
        entry["timestamp"] = self._normalize_timestamp(entry.get("timestamp"))
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write decision log: %s", e)

    def _load_entries(self) -> list[dict]:
        """Load existing log entries for reconciliation/indexing."""
        if not os.path.exists(self._path):
            return []
        entries = []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Skipping invalid decision-log line during reconciliation")
        except OSError as e:
            logger.warning("Failed to read decision log for reconciliation: %s", e)
        return entries

    @staticmethod
    def _infer_side(pkg: dict) -> str:
        side = pkg.get("_bet_side")
        if side in ("YES", "NO"):
            return side
        first_leg = next((l for l in pkg.get("legs", []) if l.get("status") != "advisory"), {})
        leg_type = first_leg.get("type", "")
        if leg_type.endswith("_no"):
            return "NO"
        return "YES"

    @staticmethod
    def _infer_entry_price(pkg: dict) -> float:
        legs = [l for l in pkg.get("legs", []) if l.get("status") != "advisory"]
        if not legs:
            return 0.0
        total_cost = sum(l.get("cost", 0) for l in legs)
        if total_cost <= 0:
            return round(sum(l.get("entry_price", 0) for l in legs) / len(legs), 4)
        weighted = sum(l.get("entry_price", 0) * l.get("cost", 0) for l in legs) / total_cost
        return round(weighted, 4)

    @staticmethod
    def _infer_days_to_expiry(pkg: dict) -> int:
        legs = [l for l in pkg.get("legs", []) if l.get("expiry")]
        if not legs:
            return 0
        expiries = []
        for leg in legs:
            try:
                exp = datetime.fromisoformat(leg["expiry"].replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                expiries.append(exp)
            except (ValueError, TypeError):
                continue
        if not expiries:
            return 0
        created_at = pkg.get("created_at")
        if isinstance(created_at, (int, float)):
            created_dt = datetime.fromtimestamp(created_at, tz=timezone.utc)
        else:
            created_dt = datetime.now(timezone.utc)
        return max(0, int((min(expiries) - created_dt).total_seconds() // 86400))

    @staticmethod
    def _infer_exit_trigger(pkg: dict, journal_entry: dict | None = None) -> str:
        if journal_entry and journal_entry.get("exit_trigger"):
            return journal_entry["exit_trigger"]
        for action in reversed(pkg.get("execution_log", [])):
            if action.get("action") in ("sell", "partial_sell") and action.get("trigger"):
                return action["trigger"]
        for leg in pkg.get("legs", []):
            if leg.get("exit_trigger"):
                return leg["exit_trigger"]
        return "reconciled_close"

    def reconcile_packages(self, packages: list[dict], journal_entries: list[dict] | None = None) -> dict:
        """Backfill missing durable package events into the append-only decision log.

        This prevents restarts or past logger outages from leaving permanent holes in the
        historical record used for audits.
        """
        existing = self._load_entries()
        seen = set()
        for entry in existing:
            pkg_id = entry.get("pkg_id")
            etype = entry.get("type")
            if pkg_id and etype:
                seen.add((pkg_id, etype))

        journal_by_pkg = {
            e.get("package_id"): e for e in (journal_entries or []) if e.get("package_id")
        }
        counts = {"trade_opened": 0, "news_trade": 0, "exit_complete": 0}

        for pkg in packages:
            pkg_id = pkg.get("id")
            if not pkg_id:
                continue
            created_ts = pkg.get("created_at") or time.time()
            strategy = pkg.get("strategy_type", "unknown")
            side = self._infer_side(pkg)
            entry_price = self._infer_entry_price(pkg)
            size = round(pkg.get("total_cost", 0), 2)
            conviction = round(pkg.get("_entry_conviction", entry_price), 3)
            opened_title = pkg.get("name", "")[:100]

            if (pkg_id, "trade_opened") not in seen:
                self.log_trade_opened(
                    pkg_id=pkg_id,
                    title=opened_title,
                    strategy=strategy,
                    side=side,
                    price=entry_price,
                    size=size,
                    score=0.0,
                    spread_pct=0.0,
                    conviction=conviction,
                    days_to_expiry=self._infer_days_to_expiry(pkg),
                    volume=0,
                    score_metadata={"reconciled": True},
                    timestamp=created_ts,
                )
                seen.add((pkg_id, "trade_opened"))
                counts["trade_opened"] += 1

            if strategy == "news_driven" and (pkg_id, "news_trade") not in seen:
                market = ""
                if pkg.get("legs"):
                    market = pkg["legs"][0].get("asset_label", "")
                self.log_news_trade(
                    pkg_id=pkg_id,
                    title=pkg.get("name", ""),
                    market=market,
                    side=side,
                    confidence=int(pkg.get("_news_confidence", 0) or 0),
                    urgency=pkg.get("_news_urgency", "unknown"),
                    size=size,
                    reasoning=pkg.get("_news_reasoning", "reconciled from positions/journal state"),
                    timestamp=created_ts,
                    reconciled=True,
                )
                seen.add((pkg_id, "news_trade"))
                counts["news_trade"] += 1

            if pkg.get("status") == "closed" and (pkg_id, "exit_complete") not in seen:
                journal_entry = journal_by_pkg.get(pkg_id)
                exit_ts = None
                pnl = 0.0
                exit_value = pkg.get("current_value", 0)
                if journal_entry:
                    exit_ts = journal_entry.get("closed_at")
                    pnl = journal_entry.get("pnl", 0.0)
                    exit_value = journal_entry.get("exit_value", exit_value)
                else:
                    exit_ts = pkg.get("closed_at") or pkg.get("updated_at") or time.time()
                    pnl = exit_value - pkg.get("total_cost", 0)
                self.log_exit_complete(
                    pkg_id=pkg_id,
                    pkg_name=pkg.get("name", ""),
                    trigger=self._infer_exit_trigger(pkg, journal_entry),
                    total_cost=pkg.get("total_cost", 0),
                    exit_value=exit_value,
                    pnl=pnl,
                    timestamp=exit_ts,
                    reconciled=True,
                )
                seen.add((pkg_id, "exit_complete"))
                counts["exit_complete"] += 1

        return counts

    # ── Auto Trader decisions ──────────────────────────────────────────

    def log_scan_start(self, open_count: int, exposure: float, budget: float, slots: int):
        self._write({
            "type": "scan_start",
            "open_positions": open_count,
            "total_exposure": round(exposure, 2),
            "remaining_budget": round(budget, 2),
            "remaining_slots": slots,
        })

    def log_scan_skip(self, reason: str, **kwargs):
        """Log when the entire scan cycle is skipped."""
        self._write({"type": "scan_skip", "reason": reason, **kwargs})

    def log_opportunity_skip(self, title: str, reason: str, **kwargs):
        """Log when an individual opportunity is skipped."""
        self._write({
            "type": "opportunity_skip",
            "title": title[:100],
            "reason": reason,
            **kwargs,
        })

    def log_trade_opened(self, pkg_id: str, title: str, strategy: str,
                         side: str, price: float, size: float,
                         score: float, spread_pct: float, conviction: float,
                         days_to_expiry: int, volume: float,
                         insider_signal: dict | None = None,
                         score_metadata: dict | None = None,
                         timestamp=None):
        entry = {
            "type": "trade_opened",
            "pkg_id": pkg_id,
            "title": title[:100],
            "strategy": strategy,
            "side": side,
            "entry_price": round(price, 4),
            "size_usd": round(size, 2),
            "score": round(score, 1),
            "spread_pct": round(spread_pct, 1),
            "conviction": round(conviction, 3),
            "days_to_expiry": days_to_expiry,
            "volume": round(volume, 0),
            "insider_signal": bool(insider_signal),
            "timestamp": timestamp,
        }
        if score_metadata is not None:
            entry["score_metadata"] = score_metadata
        self._write(entry)

    def log_trade_failed(self, title: str, error: str):
        self._write({"type": "trade_failed", "title": title[:100], "error": error})

    # ── Exit Engine decisions ──────────────────────────────────────────

    def log_triggers_fired(self, pkg_id: str, pkg_name: str, triggers: list[dict]):
        self._write({
            "type": "triggers_fired",
            "pkg_id": pkg_id,
            "pkg_name": pkg_name[:80],
            "trigger_count": len(triggers),
            "triggers": [
                {"name": t["name"], "details": t.get("details", ""),
                 "safety": t.get("safety_override", False)}
                for t in triggers
            ],
        })

    def log_safety_override(self, pkg_id: str, trigger_name: str, details: str):
        self._write({
            "type": "safety_override",
            "pkg_id": pkg_id,
            "trigger": trigger_name,
            "details": details,
        })

    def log_ai_review(self, pkg_id: str, provider: str, triggers: list[str],
                      verdicts: dict, elapsed_ms: int):
        self._write({
            "type": "ai_review",
            "pkg_id": pkg_id,
            "provider": provider,
            "triggers_reviewed": triggers,
            "verdicts": verdicts,
            "elapsed_ms": elapsed_ms,
        })

    def log_ai_failure(self, pkg_id: str, error: str):
        self._write({"type": "ai_failure", "pkg_id": pkg_id, "error": error})

    def log_auto_execute(self, pkg_id: str, trigger_name: str, action: str, details: str):
        self._write({
            "type": "auto_execute",
            "pkg_id": pkg_id,
            "trigger": trigger_name,
            "action": action,
            "details": details,
        })

    def log_trigger_suppressed(self, pkg_id: str, trigger_name: str, reason: str):
        self._write({
            "type": "trigger_suppressed",
            "pkg_id": pkg_id,
            "trigger": trigger_name,
            "reason": reason,
        })

    def log_exit_complete(self, pkg_id: str, pkg_name: str, trigger: str,
                          total_cost: float, exit_value: float, pnl: float,
                          timestamp=None, reconciled: bool = False):
        self._write({
            "type": "exit_complete",
            "pkg_id": pkg_id,
            "pkg_name": pkg_name[:80],
            "trigger": trigger,
            "total_cost": round(total_cost, 2),
            "exit_value": round(exit_value, 2),
            "pnl": round(pnl, 2),
            "reconciled": reconciled,
            "timestamp": timestamp,
        })

    # ── News Scanner decisions ─────────────────────────────────────────

    def log_news_headline(self, title: str, source: str, category: str,
                          action: str, match_details: dict | None = None):
        self._write({
            "type": "news_headline",
            "title": title[:120],
            "source": source,
            "category": category,
            "action": action,
            **({"match": match_details} if match_details else {}),
        })

    def log_news_signal(self, title: str, market: str, side: str,
                        confidence: int, urgency: str,
                        article_fetched: bool, deep_dive_result: str):
        self._write({
            "type": "news_signal",
            "title": title[:120],
            "market": market[:100],
            "side": side,
            "confidence": confidence,
            "urgency": urgency,
            "article_fetched": article_fetched,
            "deep_dive_result": deep_dive_result,
        })

    # ── Arb Scanner decisions ─────────────────────────────────────────

    def log_arb_scan_summary(self, events_count: int, matched_count: int,
                              multi_platform: int, opportunities_count: int,
                              elapsed_ms: int, platform_counts: dict):
        """Log each arb scan cycle summary for monitoring."""
        self._write({
            "type": "arb_scan_summary",
            "events_count": events_count,
            "matched_count": matched_count,
            "multi_platform_matches": multi_platform,
            "opportunities_count": opportunities_count,
            "elapsed_ms": elapsed_ms,
            "platform_counts": platform_counts,
        })

    def log_opportunity_detected(self, title: str, strategy_type: str,
                                  spread_pct: float, platforms: list[str],
                                  yes_price: float, no_price: float,
                                  is_synthetic: bool, volume: int,
                                  event_ids: list[str],
                                  calculation_audit: dict | None = None):
        """Log every opportunity found by arb scanner (for hindsight analysis)."""
        entry = {
            "type": "opportunity_detected",
            "title": title[:120],
            "strategy_type": strategy_type,
            "spread_pct": round(spread_pct, 2),
            "platforms": platforms,
            "yes_price": round(yes_price, 4),
            "no_price": round(no_price, 4),
            "is_synthetic": is_synthetic,
            "volume": volume,
            "event_ids": event_ids,
        }
        if calculation_audit:
            entry["calculation_audit"] = calculation_audit
        self._write(entry)

    # ── News Scanner decisions ─────────────────────────────────────────

    def log_news_trade(self, pkg_id: str, title: str, market: str,
                       side: str, confidence: int, urgency: str,
                       size: float, reasoning: str,
                       timestamp=None, reconciled: bool = False):
        self._write({
            "type": "news_trade",
            "pkg_id": pkg_id,
            "title": title[:120],
            "market": market[:100],
            "side": side,
            "confidence": confidence,
            "urgency": urgency,
            "size_usd": round(size, 2),
            "reasoning": reasoning[:200],
            "reconciled": reconciled,
            "timestamp": timestamp,
        })

    # ── Political Analyzer decisions ──────────────────────────────────────

    def log_political_analysis(self, cluster_id: str, race: str,
                                contracts_count: int, relationships_count: int,
                                strategies_found: int, strategies_valid: int,
                                cache_hit: bool, elapsed_ms: int):
        self._write({
            "type": "political_synthetic_analysis",
            "cluster_id": cluster_id,
            "race": race[:100],
            "contracts_count": contracts_count,
            "relationships_count": relationships_count,
            "strategies_found": strategies_found,
            "strategies_valid": strategies_valid,
            "cache_hit": cache_hit,
            "elapsed_ms": elapsed_ms,
        })

    # ── System diagnostics ──────────────────────────────────────────

    def log_startup_summary(self, arbitrage_available: bool, positions_available: bool,
                            adapters_registered: int, executors_configured: list[str],
                            mode: str, import_errors: list[str] | None = None):
        self._write({
            "type": "startup_summary",
            "arbitrage_available": arbitrage_available,
            "positions_available": positions_available,
            "adapters_registered": adapters_registered,
            "executors_configured": executors_configured,
            "mode": mode,
            "import_errors": import_errors or [],
        })

    def log_adapter_error(self, platform: str, error: str, consecutive_count: int):
        self._write({
            "type": "adapter_error",
            "platform": platform,
            "error": error[:200],
            "consecutive_errors": consecutive_count,
        })

    def log_reconciliation_summary(self, component: str, counts: dict, details: dict | None = None):
        self._write({
            "type": "reconciliation_summary",
            "component": component,
            "counts": counts,
            "details": details or {},
        })
