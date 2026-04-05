#!/usr/bin/env python3
"""Timeline + fee anomaly report for paper trade journal (cross_arb vs pure_prediction).

Usage:
  python tools/journal_strategy_audit.py
  python tools/journal_strategy_audit.py --journal path/to/trade_journal_paper.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL = ROOT / "src/data/positions/trade_journal_paper.json"
FEE_EPS = 0.005


def ts_iso(t: float | int | None) -> str:
    if not t:
        return ""
    return datetime.fromtimestamp(float(t), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def month_key(t: float | int | None) -> str:
    if not t:
        return "unknown"
    return datetime.fromtimestamp(float(t), tz=timezone.utc).strftime("%Y-%m")


def main() -> None:
    ap = argparse.ArgumentParser(description="Journal fee anomalies + strategy timelines")
    ap.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    args = ap.parse_args()

    entries = json.loads(args.journal.read_text(encoding="utf-8")).get("entries", [])

    anomalies = [e for e in entries if abs(float(e.get("total_fees") or 0)) >= FEE_EPS]
    print("=== FEE ANOMALIES (total_fees >= 0.005) ===")
    print(f"Count: {len(anomalies)} / {len(entries)} closes\n")
    for e in sorted(anomalies, key=lambda x: x.get("closed_at") or 0):
        fees = float(e.get("total_fees") or 0)
        pnl = float(e.get("pnl") or 0)
        st = e.get("strategy_type", "?")
        fm = e.get("fee_model", "?")
        eo = e.get("exit_order_type", "?")
        print(
            f"{ts_iso(e.get('closed_at'))} | {st:22} | fees=${fees:.4f} | pnl=${pnl:.2f} | "
            f"exit={e.get('exit_trigger', '?')} | order_type={eo} | fee_model={fm}"
        )
        print(f"    pkg={e.get('package_id')} | {str(e.get('name', ''))[:72]}")

    by_trig: dict[str, int] = defaultdict(int)
    for e in anomalies:
        by_trig[e.get("exit_trigger") or "unknown"] += 1
    print("\n=== ANOMALIES BY exit_trigger ===")
    for k, v in sorted(by_trig.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")

    for strat in ("cross_platform_arb", "pure_prediction"):
        xs = [e for e in entries if e.get("strategy_type") == strat]
        print(f"\n=== {strat} — all {len(xs)} closes (chronological) ===")
        for e in sorted(xs, key=lambda x: x.get("closed_at") or 0):
            fees = float(e.get("total_fees") or 0)
            pnl = float(e.get("pnl") or 0)
            oc = e.get("outcome", "?")
            print(
                f"{ts_iso(e.get('closed_at'))} | pnl=${pnl:8.2f} | fees=${fees:6.2f} | {oc:6} | {e.get('exit_trigger', '?')}"
            )

        print(f"\n=== {strat} — by calendar month ===")
        months: dict[str, list] = defaultdict(list)
        for e in xs:
            months[month_key(e.get("closed_at"))].append(e)
        for m in sorted(months.keys()):
            g = months[m]
            pnls = [float(e.get("pnl") or 0) for e in g]
            fees = [float(e.get("total_fees") or 0) for e in g]
            wins = sum(1 for e in g if (e.get("pnl") or 0) > 0.001)
            losses = sum(1 for e in g if (e.get("pnl") or 0) < -0.001)
            print(
                f"  {m}: n={len(g)} sum_pnl=${sum(pnls):.2f} sum_fees=${sum(fees):.2f} "
                f"wins={wins} losses={losses}"
            )


if __name__ == "__main__":
    main()
