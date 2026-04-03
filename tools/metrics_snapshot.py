#!/usr/bin/env python3
"""Compute trade-journal KPIs for PR review and regression checks.

Usage:
  python tools/metrics_snapshot.py              # print JSON to stdout
  python tools/metrics_snapshot.py --days 7     # rolling window (last N days)
  python tools/metrics_snapshot.py --write tools/metrics_baseline.json
  python tools/metrics_snapshot.py --compare tools/metrics_baseline.json

Exit code 1 on --compare if any guarded metric regresses beyond tolerance.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL = ROOT / "src/data/positions/trade_journal_paper.json"

# Regression guards: metric path -> max allowed drop (absolute for pnl, or use special)
TOLERANCE_PNL = 5.0  # USD: allow small drift; tighten in CI if desired
TOLERANCE_RATIO = 0.05  # fee/gross ratio may not worsen by more than 5 percentage points
FEE_ANOMALY_EPSILON = 0.005  # USD — align with audit_maker_fee_compliance


def load_entries(path: Path, days: float | None = None) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    if days is not None and days > 0:
        cutoff = time.time() - days * 86400.0
        entries = [e for e in entries if float(e.get("closed_at") or 0) >= cutoff]
    return entries


def compute_metrics(entries: list[dict]) -> dict:
    total_pnl = sum(e.get("pnl", 0) or 0 for e in entries)
    total_fees = sum(e.get("total_fees", 0) or 0 for e in entries)
    gross = sum(
        (e.get("exit_value", 0) or 0) - (e.get("total_cost", 0) or 0) for e in entries
    )
    n = len(entries)
    wins = sum(1 for e in entries if (e.get("pnl") or 0) > 0.001)
    losses = sum(1 for e in entries if (e.get("pnl") or 0) < -0.001)

    by_strategy: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0, "fees": 0.0, "gross": 0.0, "wins": 0})
    for e in entries:
        st = e.get("strategy_type") or "unknown"
        by_strategy[st]["n"] += 1
        by_strategy[st]["pnl"] += e.get("pnl", 0) or 0
        by_strategy[st]["fees"] += e.get("total_fees", 0) or 0
        c, ex = e.get("total_cost", 0) or 0, e.get("exit_value", 0) or 0
        by_strategy[st]["gross"] += ex - c
        if (e.get("pnl") or 0) > 0.001:
            by_strategy[st]["wins"] += 1

    for st in by_strategy:
        b = by_strategy[st]
        b["pnl"] = round(b["pnl"], 4)
        b["fees"] = round(b["fees"], 4)
        b["gross"] = round(b["gross"], 4)
        b["win_rate"] = round(b["wins"] / b["n"], 4) if b["n"] else 0.0

    fee_to_gross = (total_fees / gross) if gross > 0 else None
    if gross <= 0 and total_fees > 0:
        fee_to_gross = None

    fee_anomaly_count = sum(
        1 for e in entries if abs(float(e.get("total_fees") or 0)) >= FEE_ANOMALY_EPSILON
    )

    last_close = max((e.get("closed_at") or 0 for e in entries), default=0)
    last_close_iso = ""
    if last_close:
        last_close_iso = datetime.fromtimestamp(last_close, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    pure = by_strategy.get("pure_prediction", {})
    news = by_strategy.get("news_driven", {})
    xarb = by_strategy.get("cross_platform_arb", {})

    pure_fee_to_gross = (
        (pure["fees"] / pure["gross"]) if pure.get("gross", 0) > 0 else None
    )
    milestones = {
        "portfolio_fee_to_gross_below_1": fee_to_gross is not None and fee_to_gross < 1.0,
        "pure_prediction_fee_to_gross_below_0_5": pure_fee_to_gross is not None
        and pure_fee_to_gross < 0.5,
    }

    return {
        "schema_version": 1,
        "source_journal": str(DEFAULT_JOURNAL.relative_to(ROOT)).replace("\\", "/"),
        "rolling_window_days": None,
        "trade_count": n,
        "win_count": wins,
        "loss_count": losses,
        "win_rate": round(wins / n, 4) if n else 0.0,
        "total_pnl_usd": round(total_pnl, 4),
        "total_fees_usd": round(total_fees, 4),
        "gross_before_fees_usd": round(gross, 4),
        "fee_to_gross_ratio": round(fee_to_gross, 4) if fee_to_gross is not None else None,
        "fee_anomaly_count": fee_anomaly_count,
        "milestones": milestones,
        "last_close_at": last_close_iso,
        "by_strategy": dict(sorted(by_strategy.items())),
        "guards": {
            "pure_prediction_pnl_usd": round(pure.get("pnl", 0), 4),
            "pure_prediction_fee_to_gross": round(
                pure["fees"] / pure["gross"], 4
            )
            if pure.get("gross", 0) > 0
            else None,
            "news_driven_pnl_usd": round(news.get("pnl", 0), 4),
            "cross_platform_arb_win_rate": xarb.get("win_rate", 0.0),
        },
    }


def compare_metrics(current: dict, baseline: dict) -> list[str]:
    problems = []
    if baseline.get("schema_version") != current.get("schema_version"):
        problems.append("schema_version mismatch between baseline and snapshot")

    def pnl_ok(key: str, path: str):
        b = baseline.get(key)
        c = current.get(key)
        if b is None or c is None:
            return
        if c < b - TOLERANCE_PNL:
            problems.append(f"{path}: regressed from {b} to {c} (tolerance ${TOLERANCE_PNL})")

    pnl_ok("total_pnl_usd", "total_pnl_usd")

    bg = baseline.get("fee_to_gross_ratio")
    cg = current.get("fee_to_gross_ratio")
    if bg is not None and cg is not None and cg > bg + TOLERANCE_RATIO:
        problems.append(
            f"fee_to_gross_ratio: worsened from {bg} to {cg} (tolerance +{TOLERANCE_RATIO})"
        )

    b_news = baseline.get("guards", {}).get("news_driven_pnl_usd")
    c_news = current.get("guards", {}).get("news_driven_pnl_usd")
    if b_news is not None and c_news is not None and c_news < b_news - 1.0:
        problems.append(
            f"news_driven_pnl_usd: dropped from {b_news} to {c_news} — check news pipeline"
        )

    bx = baseline.get("guards", {}).get("cross_platform_arb_win_rate")
    cx = current.get("guards", {}).get("cross_platform_arb_win_rate")
    if bx is not None and cx is not None and cx < bx and baseline.get("guards", {}).get(
        "cross_platform_arb_win_rate", 0
    ) > 0:
        problems.append(
            f"cross_platform_arb_win_rate: dropped from {bx} to {cx}"
        )

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Journal metrics snapshot / PR regression check")
    ap.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL, help="Path to trade_journal_paper.json")
    ap.add_argument(
        "--days",
        type=float,
        default=None,
        metavar="N",
        help="Only include closes from the last N days (rolling window)",
    )
    ap.add_argument("--write", type=Path, help="Write snapshot JSON to this path")
    ap.add_argument("--compare", type=Path, help="Compare to baseline JSON; exit 1 on regression")
    args = ap.parse_args()

    entries = load_entries(args.journal, days=args.days)
    metrics = compute_metrics(entries)
    metrics["rolling_window_days"] = args.days
    jp = args.journal.resolve()
    try:
        metrics["source_journal"] = str(jp.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        metrics["source_journal"] = str(jp).replace("\\", "/")
    text = json.dumps(metrics, indent=2)

    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {args.write}", file=sys.stderr)

    if args.compare:
        if not args.compare.exists():
            print(f"Baseline not found: {args.compare}", file=sys.stderr)
            return 2
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        issues = compare_metrics(metrics, baseline)
        if issues:
            print("REGRESSION CHECK FAILED:", file=sys.stderr)
            for i in issues:
                print(f"  - {i}", file=sys.stderr)
            print(text)
            return 1
        print("OK: no regression vs baseline.", file=sys.stderr)

    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
