#!/usr/bin/env python3
"""Flag journal closes with non-zero fees under maker-only paper policy.

PaperExecutor forces 0% fees when use_limit_orders=True. Rows with material
total_fees usually predate the fix or indicate a live/taker path worth tracing.

Exit code: always 0 (informational).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL = ROOT / "src/data/positions/trade_journal_paper.json"
FEE_EPSILON = 0.005  # USD — ignore float dust


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit journal fees vs maker-only paper policy")
    ap.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    ap.add_argument("--epsilon", type=float, default=FEE_EPSILON, help="Min |fee| to flag")
    args = ap.parse_args()

    if not args.journal.exists():
        print(f"No journal at {args.journal}", file=sys.stderr)
        return 0

    data = json.loads(args.journal.read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    flagged = []
    for e in entries:
        fees = float(e.get("total_fees") or 0)
        if abs(fees) >= args.epsilon:
            flagged.append(
                {
                    "package_id": e.get("package_id"),
                    "strategy_type": e.get("strategy_type"),
                    "closed_at": e.get("closed_at"),
                    "total_fees": fees,
                    "pnl": e.get("pnl"),
                }
            )

    print(
        json.dumps(
            {
                "journal": str(args.journal).replace("\\", "/"),
                "entry_count": len(entries),
                "fee_anomaly_count": len(flagged),
                "epsilon": args.epsilon,
                "anomalies": flagged[:200],
                "truncated": len(flagged) > 200,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
