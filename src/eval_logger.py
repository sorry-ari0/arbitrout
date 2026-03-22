"""Universal evaluation logger for hindsight analysis across all Arbitrout strategy types.

Records EVERY opportunity the system encounters (entered, skipped, rejected)
so we can later ask: "did we make the right call?"

Append-only JSONL format. Each line is a JSON object with timestamp and type.
Two entry types:
  - "opportunity" — logged at decision time (enter/skip/reject)
  - "backfill"    — logged later when the market resolves, with actual P&L
"""
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("eval_logger")

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "data", "arbitrage", "eval_log.jsonl")


class EvalLogger:
    """Append-only JSONL logger for opportunity evaluation and hindsight analysis."""

    def __init__(self, path: str = DEFAULT_PATH):
        self._path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    # ── Core write ──────────────────────────────────────────────────────

    def _write(self, entry: dict):
        """Append a single JSON line with timestamp. Silently logs errors."""
        entry["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write eval log: %s", e)

    # ── Logging methods ─────────────────────────────────────────────────

    def log_opportunity(
        self,
        strategy_type: str,
        opportunity_id: str,
        action: str,
        action_reason: str,
        reason_detail: str = "",
        markets: list | None = None,
        score: float | None = None,
        spread_pct: float | None = None,
        expected_value_pct: float | None = None,
        prices_at_decision: dict | None = None,
        metadata: dict | None = None,
    ):
        """Log an opportunity at decision time (entered, skipped, rejected)."""
        entry = {
            "type": "opportunity",
            "strategy_type": strategy_type,
            "opportunity_id": opportunity_id,
            "action": action,
            "action_reason": action_reason,
            "reason_detail": reason_detail,
        }
        if markets is not None:
            entry["markets"] = markets
        if score is not None:
            entry["score"] = score
        if spread_pct is not None:
            entry["spread_pct"] = spread_pct
        if expected_value_pct is not None:
            entry["expected_value_pct"] = expected_value_pct
        if prices_at_decision is not None:
            entry["prices_at_decision"] = prices_at_decision
        if metadata is not None:
            entry["metadata"] = metadata
        self._write(entry)

    def backfill_outcome(
        self,
        opportunity_id: str,
        actual_pnl_pct: float,
        actual_outcome: str,
        resolution_date: str,
        prices_at_resolution: dict | None = None,
    ):
        """Append a backfill entry once market resolves with actual P&L."""
        entry = {
            "type": "backfill",
            "opportunity_id": opportunity_id,
            "actual_pnl_pct": actual_pnl_pct,
            "actual_outcome": actual_outcome,
            "resolution_date": resolution_date,
        }
        if prices_at_resolution is not None:
            entry["prices_at_resolution"] = prices_at_resolution
        self._write(entry)

    # ── Read helpers ────────────────────────────────────────────────────

    def _read_all(self) -> list[dict]:
        """Read all entries from the log file. Skips corrupt lines individually."""
        entries = []
        if not os.path.exists(self._path):
            return entries
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Corrupt JSONL line %d in eval log, skipping", line_num)
        except OSError as e:
            logger.error("Failed to read eval log: %s", e)
        return entries

    def _build_backfill_map(self, entries: list[dict]) -> dict[str, dict]:
        """Build a map of opportunity_id -> backfill entry."""
        backfills = {}
        for e in entries:
            if e.get("type") == "backfill" and e.get("opportunity_id"):
                backfills[e["opportunity_id"]] = e
        return backfills

    # ── Query methods ───────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Count entries by strategy_type and action.

        Returns: {"cross_platform_arb": {"entered": 5, "skipped": 12}, ...}
        """
        entries = self._read_all()
        summary: dict[str, dict[str, int]] = {}
        for e in entries:
            if e.get("type") != "opportunity":
                continue
            st = e.get("strategy_type", "unknown")
            action = e.get("action", "unknown")
            if st not in summary:
                summary[st] = {}
            summary[st][action] = summary[st].get(action, 0) + 1
        return summary

    def get_missed_opportunities(
        self, strategy_type: str | None = None, min_pnl: float = 0
    ) -> list[dict]:
        """Find skipped opportunities where backfill shows positive P&L.

        Returns merged skip+backfill entries.
        """
        entries = self._read_all()
        backfills = self._build_backfill_map(entries)
        missed = []
        for e in entries:
            if e.get("type") != "opportunity":
                continue
            if e.get("action") != "skipped":
                continue
            if strategy_type and e.get("strategy_type") != strategy_type:
                continue
            oid = e.get("opportunity_id")
            bf = backfills.get(oid)
            if bf and bf.get("actual_pnl_pct", 0) > min_pnl:
                merged = {**e, **bf}
                merged["type"] = "missed_opportunity"
                missed.append(merged)
        return missed

    def get_calibration(self) -> dict:
        """For each action_reason, shows correct_skips vs missed_opportunities.

        Returns: {"low_score": {"total_skips": 50, "resolved": 20,
                  "correct_skips": 15, "missed_opportunities": 5,
                  "correct_skip_rate": 0.75}, ...}
        """
        entries = self._read_all()
        backfills = self._build_backfill_map(entries)

        # Gather all skips grouped by action_reason
        reasons: dict[str, dict] = {}
        for e in entries:
            if e.get("type") != "opportunity":
                continue
            if e.get("action") != "skipped":
                continue
            reason = e.get("action_reason", "unknown")
            if reason not in reasons:
                reasons[reason] = {
                    "total_skips": 0,
                    "resolved": 0,
                    "correct_skips": 0,
                    "missed_opportunities": 0,
                    "correct_skip_rate": 0.0,
                }
            bucket = reasons[reason]
            bucket["total_skips"] += 1

            oid = e.get("opportunity_id")
            bf = backfills.get(oid)
            if bf:
                bucket["resolved"] += 1
                if bf.get("actual_pnl_pct", 0) <= 0:
                    bucket["correct_skips"] += 1
                else:
                    bucket["missed_opportunities"] += 1

        # Compute rates
        for bucket in reasons.values():
            resolved = bucket["resolved"]
            if resolved > 0:
                bucket["correct_skip_rate"] = round(
                    bucket["correct_skips"] / resolved, 4
                )
            else:
                bucket["correct_skip_rate"] = 0.0

        return reasons

    def get_details(self, opportunity_id: str) -> dict | None:
        """Return the full merged entry (opportunity + backfill) for a specific ID."""
        entries = self._read_all()
        opp = None
        bf = None
        for e in entries:
            oid = e.get("opportunity_id")
            if oid != opportunity_id:
                continue
            if e.get("type") == "opportunity":
                opp = e
            elif e.get("type") == "backfill":
                bf = e
        if opp is None:
            return None
        if bf is not None:
            merged = {**opp, **bf}
            merged["type"] = "opportunity"
            return merged
        return opp

    def get_unresolved_skips(self) -> list[dict]:
        """Return skipped entries without a corresponding backfill."""
        entries = self._read_all()
        backfills = self._build_backfill_map(entries)
        unresolved = []
        for e in entries:
            if e.get("type") != "opportunity":
                continue
            if e.get("action") != "skipped":
                continue
            if e.get("opportunity_id") not in backfills:
                unresolved.append(e)
        return unresolved

    async def resolve_via_polymarket(self, entry: dict, http_client) -> bool:
        """Check if a skipped opportunity's market has resolved on Polymarket.

        Queries Polymarket Gamma API for the condition. If resolved, calls
        backfill_outcome with the actual result.

        Returns True if resolved and backfilled, False otherwise.
        """
        markets = entry.get("markets") or []
        if not markets:
            return False

        condition_id = None
        for m in markets:
            if m.get("platform") == "polymarket":
                condition_id = m.get("condition_id") or m.get("asset_id", "").split(":")[0]
                break
        if not condition_id:
            return False

        try:
            resp = await http_client.get(
                f"https://gamma-api.polymarket.com/markets",
                params={"condition_id": condition_id},
                timeout=10,
            )
            if resp.status_code != 200:
                return False

            data = resp.json()
            if not data:
                return False

            market = data[0] if isinstance(data, list) else data
            if not market.get("closed"):
                return False

            try:
                outcome_prices = json.loads(market.get("outcomePrices", "[0,0]"))
                resolution_price = float(outcome_prices[0])
            except (json.JSONDecodeError, IndexError, TypeError):
                return False

            prices_at_decision = entry.get("prices_at_decision", {})
            entry_yes = prices_at_decision.get("yes", 0.5)

            if entry_yes < 0.5:
                pnl_pct = round((resolution_price - entry_yes) / entry_yes * 100, 2) if entry_yes > 0 else 0
            else:
                entry_no = 1 - entry_yes
                resolution_no = 1 - resolution_price
                pnl_pct = round((resolution_no - entry_no) / entry_no * 100, 2) if entry_no > 0 else 0

            outcome = "win" if pnl_pct > 0 else ("loss" if pnl_pct < 0 else "flat")

            self.backfill_outcome(
                opportunity_id=entry["opportunity_id"],
                actual_pnl_pct=pnl_pct,
                actual_outcome=outcome,
                resolution_date=market.get("endDate", ""),
                prices_at_resolution={"resolution_price": resolution_price},
            )
            return True
        except Exception as e:
            logger.debug("Polymarket resolution check failed for %s: %s",
                         entry.get("opportunity_id"), e)
            return False
