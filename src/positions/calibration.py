"""Calibration engine — generates threshold tuning reports from eval and trade data."""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data" / "calibration"


class CalibrationEngine:
    def __init__(self, eval_logger, trade_journal):
        self.eval_logger = eval_logger
        self.journal = trade_journal

    def generate_report(self) -> dict:
        """Generate calibration report from all available data."""
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trade_count": 0,
            "entry_calibration": {},
            "exit_calibration": {},
            "hold_duration_analysis": {},
            "fee_analysis": {},
        }

        # --- Entry calibration ---
        try:
            calibration = self.eval_logger.get_calibration()
            missed = self.eval_logger.get_missed_opportunities()
            missed_by_reason = {}
            for m in missed:
                reason = m.get("action_reason", "unknown")
                if reason not in missed_by_reason:
                    missed_by_reason[reason] = {"count": 0, "pnl": 0.0}
                missed_by_reason[reason]["count"] += 1
                missed_by_reason[reason]["pnl"] += m.get("actual_pnl_pct", 0)

            for reason, data in calibration.items():
                rate = data.get("correct_skip_rate", 1.0)
                resolved = data.get("resolved", 0)
                missed_info = missed_by_reason.get(reason, {"count": 0, "pnl": 0.0})
                if resolved >= 5 and rate < 0.60:
                    suggestion = f"REVIEW — {rate:.0%} correct skip rate, missing ${missed_info['pnl']:.0f}. Threshold may be too aggressive."
                elif resolved < 5:
                    suggestion = f"INSUFFICIENT DATA — only {resolved} resolved trades. Need 5+."
                else:
                    suggestion = f"KEEP — {rate:.0%} correct skip rate is healthy."
                report["entry_calibration"][reason] = {
                    "correct_skip_rate": round(rate, 2),
                    "missed_count": missed_info["count"],
                    "missed_pnl": round(missed_info["pnl"], 2),
                    "resolved": resolved,
                    "suggestion": suggestion,
                }
        except Exception as e:
            logger.warning("Entry calibration failed: %s", e)

        # --- Exit calibration ---
        try:
            perf = self.journal.get_performance()
            report["trade_count"] = perf.get("total_trades", 0)
            by_trigger = perf.get("by_trigger", {})
            for trigger, data in by_trigger.items():
                trades = data.get("trades", 0)
                win_rate = data.get("win_rate", 0)
                pnl = data.get("pnl", 0)
                if trades >= 5 and win_rate == 0:
                    suggestion = f"WIDEN — 0% win rate across {trades} trades. Threshold too tight."
                elif trades >= 5 and win_rate >= 0.70:
                    suggestion = "KEEP — performing well."
                elif trades < 5:
                    suggestion = f"INSUFFICIENT DATA — only {trades} trades."
                else:
                    suggestion = f"MONITOR — {win_rate:.0%} win rate, ${pnl:.0f} P&L."
                report["exit_calibration"][trigger] = {
                    "trades": trades,
                    "win_rate": win_rate,
                    "total_pnl": round(pnl, 2),
                    "suggestion": suggestion,
                }
        except Exception as e:
            logger.warning("Exit calibration failed: %s", e)

        # --- Hold duration analysis ---
        try:
            report["hold_duration_analysis"] = self.journal.get_performance_by_hold_duration()
        except Exception as e:
            logger.warning("Hold duration analysis failed: %s", e)

        # --- Fee analysis ---
        try:
            perf = self.journal.get_performance()
            total_fees = perf.get("total_fees", 0)
            fee_drag = perf.get("fee_drag_pct", 0)

            limit_attempts = 0
            limit_fills = 0
            for entry in getattr(self.journal, "entries", []):
                etype = entry.get("exit_order_type", "")
                if etype in ("limit_filled", "limit_partial_fok"):
                    limit_fills += 1
                    limit_attempts += 1
                elif etype == "fok_fallback":
                    limit_attempts += 1
            fill_rate = round(limit_fills / limit_attempts, 2) if limit_attempts > 0 else None

            if fee_drag > 2.0:
                fee_suggestion = f"HIGH — {fee_drag:.1f}% fee drag. Investigate execution quality."
            elif fill_rate is not None and fill_rate < 0.50:
                fee_suggestion = f"LOW FILL RATE — {fill_rate:.0%} limit fill rate. Consider widening limit price offset."
            elif fill_rate is not None:
                fee_suggestion = f"GOOD — {fill_rate:.0%} limit fill rate."
            else:
                fee_suggestion = "TRACK — no limit order data yet."

            report["fee_analysis"] = {
                "total_fees": round(total_fees, 2),
                "fee_drag_pct": round(fee_drag, 2),
                "limit_fill_rate": fill_rate,
                "suggestion": fee_suggestion,
            }
        except Exception as e:
            logger.warning("Fee analysis failed: %s", e)

        return report

    def save_report(self) -> str:
        """Generate and save report to data/calibration/YYYY-MM-DD.json. Returns path."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        report = self.generate_report()
        filename = datetime.now().strftime("%Y-%m-%d") + ".json"
        path = DATA_DIR / filename
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info("Calibration report saved to %s", path)
        return str(path)
