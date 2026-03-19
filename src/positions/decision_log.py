"""Decision logger — records all trading decisions to JSONL for later review.

Logs: buys, skips, trigger fires, AI verdicts, exits, safety overrides.
Each line is a JSON object with timestamp, type, and context-specific fields.
"""
import json
import logging
import os
import time
from datetime import datetime

logger = logging.getLogger("positions.decision_log")

LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "positions")
LOG_FILE = os.path.join(LOG_DIR, "decision_log.jsonl")


class DecisionLogger:
    """Append-only JSONL logger for trading decisions."""

    def __init__(self, path: str = LOG_FILE):
        self._path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _write(self, entry: dict):
        entry["timestamp"] = datetime.utcnow().isoformat() + "Z"
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write decision log: %s", e)

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
                         insider_signal: dict | None = None):
        self._write({
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
        })

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
                          total_cost: float, exit_value: float, pnl: float):
        self._write({
            "type": "exit_complete",
            "pkg_id": pkg_id,
            "pkg_name": pkg_name[:80],
            "trigger": trigger,
            "total_cost": round(total_cost, 2),
            "exit_value": round(exit_value, 2),
            "pnl": round(pnl, 2),
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
                                  event_ids: list[str]):
        """Log every opportunity found by arb scanner (for hindsight analysis)."""
        self._write({
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
        })

    # ── News Scanner decisions ─────────────────────────────────────────

    def log_news_trade(self, pkg_id: str, title: str, market: str,
                       side: str, confidence: int, urgency: str,
                       size: float, reasoning: str):
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
        })
