#!/usr/bin/env python3
"""Paper journal: PnL by entry-price bucket and before/after a deploy cutoff (Kelly regime).

Uses persisted closes in trade_journal_paper.json. For pure_prediction, side price is taken
as the first leg's entry_price (matches auto_trader directional sizing). Older rows may lack
active_release; period is then inferred from closed_at vs --cutoff (default: variable-Kelly merge day).

Usage:
  python tools/journal_kelly_bucket_analysis.py
  python tools/journal_kelly_bucket_analysis.py --journal path/to/trade_journal_paper.json
  python tools/journal_kelly_bucket_analysis.py --cutoff 2026-03-20 --strategy pure_prediction
  python tools/journal_kelly_bucket_analysis.py --exclude-news-sleeve
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL = ROOT / "src/data/positions/trade_journal_paper.json"

# Approximate landing of variable-Kelly / scoring change (9e867da); override with --cutoff.
DEFAULT_CUTOFF = "2026-03-19"


def _parse_cutoff_iso(s: str) -> float:
    s = s.strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        dt = datetime.fromisoformat(s + "T00:00:00+00:00")
    else:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _side_price(entry: dict) -> float | None:
    """Directional entry price for bucketing; None if not applicable."""
    legs = entry.get("legs") or []
    if not legs:
        return None
    st = entry.get("strategy_type") or ""
    if st == "pure_prediction":
        p = float(legs[0].get("entry_price") or 0)
        return p if p > 0 else None
    # Single-leg directional (e.g. some news-driven packages)
    if len(legs) == 1:
        t = str(legs[0].get("type") or "")
        if t.startswith("prediction_"):
            p = float(legs[0].get("entry_price") or 0)
            return p if p > 0 else None
    return None


def _bucket(price: float) -> str:
    if price <= 0.30:
        return "longshot (p<=0.30)"
    if price >= 0.70:
        return "favorite (p>=0.70)"
    return "mid (0.30<p<0.70)"


def _period_label(closed_at: float, cutoff_ts: float) -> str:
    return "on_or_after_cutoff" if closed_at >= cutoff_ts else "before_cutoff"


def _win(e: dict) -> bool:
    return float(e.get("pnl") or 0) > 0.001


def _loss(e: dict) -> bool:
    return float(e.get("pnl") or 0) < -0.001


def _load_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [e for e in (data.get("entries") or []) if isinstance(e, dict)]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Journal PnL by entry-price bucket vs before/after cutoff (paper Kelly regime analysis)"
    )
    ap.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    ap.add_argument(
        "--cutoff",
        default=DEFAULT_CUTOFF,
        help="UTC cutoff (YYYY-MM-DD or full ISO). Closes at/after this instant are 'on_or_after_cutoff'.",
    )
    ap.add_argument(
        "--strategy",
        choices=("pure_prediction", "any_directional", "all"),
        default="pure_prediction",
        help="pure_prediction: strategy_type match; any_directional: pure_prediction or single prediction_* leg; "
        "all: any entry with a side price (excludes multi-leg arb without a single directional price)",
    )
    ap.add_argument(
        "--exclude-news-sleeve",
        action="store_true",
        help="Drop entries with news_sleeve=true",
    )
    args = ap.parse_args()

    cutoff_ts = _parse_cutoff_iso(args.cutoff)
    entries = _load_entries(args.journal)

    filtered: list[dict] = []
    for e in entries:
        if (e.get("mode") or "paper") != "paper":
            continue
        ca = e.get("closed_at")
        if ca is None:
            continue
        try:
            ts = float(ca)
        except (TypeError, ValueError):
            continue
        if args.exclude_news_sleeve and e.get("news_sleeve"):
            continue

        st = e.get("strategy_type") or ""
        sp = _side_price(e)

        if args.strategy == "pure_prediction":
            if st != "pure_prediction":
                continue
        elif args.strategy == "any_directional":
            if st != "pure_prediction" and sp is None:
                continue
        else:  # all
            if sp is None:
                continue

        if sp is None:
            continue

        filtered.append({**e, "_closed_ts": ts, "_side_price": sp})

    print(f"Journal: {args.journal}")
    print(f"Cutoff (UTC instant): {args.cutoff}")
    print(f"Strategy filter: {args.strategy}  news_sleeve excluded: {args.exclude_news_sleeve}")
    print(f"Matched closes: {len(filtered)} / {len(entries)} total journal entries\n")

    # Overall before / after
    by_period: dict[str, list[dict]] = defaultdict(list)
    for e in filtered:
        by_period[_period_label(e["_closed_ts"], cutoff_ts)].append(e)

    print("=== Period totals (all matched entries) ===")
    for label in ("before_cutoff", "on_or_after_cutoff"):
        g = by_period[label]
        pnls = [float(x.get("pnl") or 0) for x in g]
        n = len(g)
        s = sum(pnls)
        mean = s / n if n else 0.0
        wr = sum(1 for x in g if _win(x))
        lr = sum(1 for x in g if _loss(x))
        flat = n - wr - lr
        print(
            f"  {label:22}  n={n:4}  sum_pnl=${s:10.2f}  mean=${mean:8.4f}  "
            f"wins={wr} losses={lr} flat={flat}"
        )
    print()

    # Bucket x period
    cell: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in filtered:
        b = _bucket(float(e["_side_price"]))
        p = _period_label(e["_closed_ts"], cutoff_ts)
        cell[(b, p)].append(e)

    bucket_order = ("longshot (p<=0.30)", "mid (0.30<p<0.70)", "favorite (p>=0.70)")
    period_order = ("before_cutoff", "on_or_after_cutoff")

    print("=== By side_price bucket x period ===")
    header = f"{'bucket':<22} {'period':<22} {'n':>5} {'sum_pnl':>12} {'mean_pnl':>10} {'win%':>7}"
    print(header)
    print("-" * len(header))
    for b in bucket_order:
        for p in period_order:
            g = cell.get((b, p), [])
            pnls = [float(x.get("pnl") or 0) for x in g]
            n = len(g)
            sm = sum(pnls)
            mean = sm / n if n else 0.0
            wpct = (100.0 * sum(1 for x in g if _win(x)) / n) if n else 0.0
            print(f"{b:<22} {p:<22} {n:5} ${sm:11.2f} {mean:10.4f} {wpct:6.1f}%")

    # Optional: median for non-empty cells
    print("\n=== Median PnL per cell (n>0) ===")
    for b in bucket_order:
        for p in period_order:
            g = cell.get((b, p), [])
            if len(g) < 2:
                continue
            meds = statistics.median(float(x.get("pnl") or 0) for x in g)
            print(f"  {b} | {p} | median=${meds:.4f} (n={len(g)})")

    # active_release coverage (informational)
    with_rel = sum(1 for e in filtered if isinstance(e.get("active_release"), dict))
    print(f"\nEntries with active_release dict: {with_rel} / {len(filtered)} (rest: infer period from closed_at only)")


if __name__ == "__main__":
    main()
