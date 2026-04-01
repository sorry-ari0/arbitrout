"""Comprehensive Arbitrout performance audit from durable paper artifacts."""
import json
import os
from collections import Counter, defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data", "positions")
journal_path = os.path.join(DATA, "trade_journal_paper.json")
dlog_path = os.path.join(DATA, "decision_log.jsonl")
positions_path = os.path.join(DATA, "positions.json")


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


journal = _load_json(journal_path, {}).get("entries", [])
dlog = _load_jsonl(dlog_path)
positions = _load_json(positions_path, {}).get("packages", [])

print("=" * 70)
print("1. OVERALL PAPER JOURNAL SUMMARY")
print("=" * 70)
total_pnl = sum(e.get("pnl", 0) for e in journal)
total_cost = sum(e.get("total_cost", 0) for e in journal)
total_fees = sum(e.get("total_fees", 0) for e in journal)
wins = [e for e in journal if e.get("outcome") == "win"]
losses = [e for e in journal if e.get("outcome") == "loss"]
flats = [e for e in journal if e.get("outcome") == "flat"]
print(f"Trades: {len(journal)} | Wins: {len(wins)} | Losses: {len(losses)} | Flat: {len(flats)}")
print(f"Total P&L: ${total_pnl:.2f}")
print(f"Total Cost: ${total_cost:.2f}")
print(f"Total Fees: ${total_fees:.2f}")
print(f"Win Rate: {(len(wins)/len(journal)*100):.1f}%" if journal else "Win Rate: N/A")
if wins:
    print(f"Avg Win: ${sum(e['pnl'] for e in wins)/len(wins):.2f}")
if losses:
    print(f"Avg Loss: ${sum(e['pnl'] for e in losses)/len(losses):.2f}")

print("\n" + "=" * 70)
print("2. STRATEGY / EXIT BREAKDOWN")
print("=" * 70)
by_strategy = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0})
by_trigger = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0})
for e in journal:
    strategy = e.get("strategy_type", "unknown")
    trigger = e.get("exit_trigger", "unknown")
    by_strategy[strategy]["count"] += 1
    by_strategy[strategy]["wins"] += int(e.get("outcome") == "win")
    by_strategy[strategy]["pnl"] += e.get("pnl", 0)
    by_trigger[trigger]["count"] += 1
    by_trigger[trigger]["wins"] += int(e.get("outcome") == "win")
    by_trigger[trigger]["pnl"] += e.get("pnl", 0)
print("By strategy:")
for strategy, data in sorted(by_strategy.items(), key=lambda x: x[1]["pnl"]):
    print(f"  {strategy:20s} trades={data['count']:>2d} wins={data['wins']:>2d} pnl=${data['pnl']:>7.2f}")
print("By exit trigger:")
for trigger, data in sorted(by_trigger.items(), key=lambda x: x[1]["pnl"]):
    print(f"  {trigger:24s} trades={data['count']:>2d} wins={data['wins']:>2d} pnl=${data['pnl']:>7.2f}")

print("\n" + "=" * 70)
print("3. OPEN POSITION STATUS")
print("=" * 70)
open_pkgs = [p for p in positions if p.get("status") == "open"]
closed_pkgs = [p for p in positions if p.get("status") == "closed"]
print(f"Open packages: {len(open_pkgs)} | Closed packages: {len(closed_pkgs)}")
for pkg in sorted(open_pkgs, key=lambda p: p.get("created_at", 0), reverse=True):
    print(
        f"  {pkg.get('strategy_type','unknown'):20s} "
        f"cost=${pkg.get('total_cost',0):>7.2f} "
        f"uPnL=${pkg.get('unrealized_pnl',0):>7.2f} "
        f"{pkg.get('name','')[:55]}"
    )

print("\n" + "=" * 70)
print("4. DECISION LOG DURABILITY / RECENCY")
print("=" * 70)
type_counts = Counter(e.get("type") for e in dlog)
print("Event counts:")
for key in ("trade_opened", "news_trade", "exit_complete", "opportunity_skip", "reconciliation_summary"):
    print(f"  {key:22s} {type_counts.get(key, 0):>5d}")

recent = sorted(
    dlog,
    key=lambda e: (_parse_iso(e.get("timestamp")) or datetime.min),
    reverse=True,
)[:20]
reconciled_recent = sum(1 for e in recent if e.get("reconciled"))
print(f"Recent 20 by event timestamp: reconciled={reconciled_recent}")
for e in recent[:10]:
    print(
        f"  {e.get('timestamp',''):25s} "
        f"{e.get('type','unknown'):22s} "
        f"reconciled={str(bool(e.get('reconciled'))):5s} "
        f"{(e.get('title') or e.get('pkg_name') or e.get('component') or '')[:45]}"
    )

print("\n" + "=" * 70)
print("5. DURABILITY CHECKS")
print("=" * 70)
journal_ids = {e.get("package_id") for e in journal}
closed_ids = {p.get("id") for p in closed_pkgs}
exit_ids = {e.get("pkg_id") for e in dlog if e.get("type") == "exit_complete"}
opened_ids = Counter(e.get("pkg_id") for e in dlog if e.get("type") == "trade_opened" and e.get("pkg_id"))
print(f"Closed missing from journal: {len(closed_ids - journal_ids)}")
print(f"Closed missing exit_complete: {len(closed_ids - exit_ids)}")
print(f"Duplicate trade_opened package ids: {sum(1 for _, count in opened_ids.items() if count > 1)}")
