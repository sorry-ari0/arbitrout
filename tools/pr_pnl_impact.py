#!/usr/bin/env python3
"""Correlate PR merge times with paper trade journal PnL (associative, not causal).

For each PR (first occurrence on main), reports:
  - cumulative paper journal PnL just before merge time
  - sum PnL and trade count in the N days before merge
  - sum PnL and trade count in the N days after merge

Usage:
  python tools/pr_pnl_impact.py
  python tools/pr_pnl_impact.py --journal path/to/trade_journal_paper.json --window-days 7
  python tools/pr_pnl_impact.py --csv pr_pnl_segments.csv
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL = ROOT / "src/data/positions/trade_journal_paper.json"


def _git_pr_events() -> list[tuple[int, int, str]]:
    """Return sorted (unix_ts, pr_number, subject) — first time each PR appears in main history."""
    try:
        out = subprocess.check_output(
            ["git", "log", "main", "--format=%ct\t%s", "--reverse"],
            cwd=str(ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print("git log failed:", e, file=sys.stderr)
        return []

    seen_pr: set[int] = set()
    events: list[tuple[int, int, str]] = []
    for line in out.strip().splitlines():
        if "\t" not in line:
            continue
        ct_s, subj = line.split("\t", 1)
        try:
            ct = int(ct_s)
        except ValueError:
            continue
        m = re.search(r"pull request #(\d+)", subj, re.I) or re.search(r"#(\d+)\b", subj)
        if not m:
            continue
        pr = int(m.group(1))
        if pr in seen_pr:
            continue
        seen_pr.add(pr)
        events.append((ct, pr, subj.strip()[:100]))
    return sorted(events, key=lambda x: x[0])


def _load_paper_closes(journal_path: Path) -> list[tuple[float, float, str]]:
    """(closed_at, pnl, strategy_type) sorted by closed_at; paper mode only."""
    if not journal_path.exists():
        return []
    data = json.loads(journal_path.read_text(encoding="utf-8"))
    rows: list[tuple[float, float, str]] = []
    for e in data.get("entries") or []:
        if not isinstance(e, dict):
            continue
        if (e.get("mode") or "paper") != "paper":
            continue
        ca = e.get("closed_at")
        if ca is None:
            continue
        try:
            ts = float(ca)
        except (TypeError, ValueError):
            continue
        pnl = float(e.get("pnl") or 0)
        st = str(e.get("strategy_type") or "unknown")
        rows.append((ts, pnl, st))
    rows.sort(key=lambda x: x[0])
    return rows


def _segment_sum(closes: list[tuple[float, float, str]], t0: float, t1: float) -> tuple[float, int]:
    """Sum pnl and count for closed_at in [t0, t1)."""
    s = 0.0
    n = 0
    for ts, pnl, _ in closes:
        if ts >= t1:
            break
        if ts >= t0:
            s += pnl
            n += 1
    return s, n


def _cumulative_before(closes: list[tuple[float, float, str]], prefix: list[float], t_merge: float) -> float:
    """Total pnl for all closes strictly before t_merge."""
    ts_list = [c[0] for c in closes]
    j = bisect.bisect_left(ts_list, t_merge)
    return prefix[j] if j <= len(prefix) else prefix[-1]


def main() -> None:
    ap = argparse.ArgumentParser(description="PR merge times vs paper journal PnL segments")
    ap.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    ap.add_argument("--window-days", type=float, default=7.0, help="Days before/after merge for segment sums")
    ap.add_argument("--csv", type=Path, default=None, help="Write segment table to CSV")
    args = ap.parse_args()

    window = max(0.25, args.window_days) * 86400.0
    closes = _load_paper_closes(args.journal)
    ts_list = [c[0] for c in closes]
    prefix = [0.0]
    for _, pnl, _ in closes:
        prefix.append(prefix[-1] + pnl)

    pr_events = _git_pr_events()
    if not pr_events:
        print("No PR-tagged commits found on main.")
        return

    print("=== Paper journal ===")
    print(f"Path: {args.journal}")
    print(f"Closes (paper): {len(closes)}")
    if closes:
        print(f"First close UTC: {datetime_from_ts(closes[0][0])}")
        print(f"Last close UTC:  {datetime_from_ts(closes[-1][0])}")
        print(f"Total PnL (all paper closes): ${prefix[-1]:.2f}")
    print()
    print(f"=== Per PR (first main appearance) - {args.window_days:g}d before vs after merge ===")
    print("(Association only: deployment lag, overlapping changes, and market regime confound causality.)\n")

    rows_out = []
    hdr = (
        "pr",
        "merge_utc",
        "subject",
        "cum_pnl_before_merge",
        f"pnl_prev_{args.window_days:g}d",
        f"n_prev_{args.window_days:g}d",
        f"pnl_next_{args.window_days:g}d",
        f"n_next_{args.window_days:g}d",
    )
    print("\t".join(hdr))

    for t_merge, pr, subj in pr_events:
        if not closes:
            row = (pr, datetime_from_ts(t_merge), subj, 0.0, 0.0, 0, 0.0, 0)
            print_row(row)
            rows_out.append(row)
            continue
        cum_before = _cumulative_before(closes, prefix, float(t_merge))
        prev_lo = t_merge - window
        pnl_prev, n_prev = _segment_sum(closes, prev_lo, t_merge)
        pnl_next, n_next = _segment_sum(closes, t_merge, t_merge + window)
        row = (pr, datetime_from_ts(t_merge), subj, cum_before, pnl_prev, n_prev, pnl_next, n_next)
        print_row(row)
        rows_out.append(row)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "pr",
                    "merge_utc",
                    "subject",
                    "cum_pnl_before_merge",
                    f"pnl_prev_{args.window_days:g}d",
                    f"n_prev_{args.window_days:g}d",
                    f"pnl_next_{args.window_days:g}d",
                    f"n_next_{args.window_days:g}d",
                ]
            )
            for r in rows_out:
                w.writerow([r[0], r[1], r[2], f"{r[3]:.4f}", f"{r[4]:.4f}", r[5], f"{r[6]:.4f}", r[7]])
        print(f"\nWrote {args.csv}")


def datetime_from_ts(ts: float | int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def print_row(row: tuple) -> None:
    pr, dt, subj, cum_b, p_prev, n_prev, p_next, n_next = row
    subj_short = subj.replace("\t", " ")[:70]
    print(
        f"{pr}\t{dt}\t{subj_short}\t{cum_b:.2f}\t{p_prev:.2f}\t{n_prev}\t{p_next:.2f}\t{n_next}"
    )


if __name__ == "__main__":
    main()
