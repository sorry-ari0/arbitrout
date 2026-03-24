"""Comprehensive Arbitrout Performance Analysis"""
import json
import os
from collections import defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
journal_path = os.path.join(BASE, "data", "positions", "trade_journal.json")
dlog_path = os.path.join(BASE, "data", "positions", "decision_log.jsonl")

with open(journal_path) as f:
    journal = json.load(f)
entries = journal.get("entries", [])

# ── 1. Overall P&L ──────────────────────────────────────────────────
print("=" * 70)
print("1. OVERALL P&L SUMMARY")
print("=" * 70)
total_pnl = sum(e.get("pnl", 0) for e in entries)
total_cost = sum(e.get("total_cost", 0) for e in entries)
total_fees = sum(e.get("total_fees", 0) for e in entries)
wins = [e for e in entries if e.get("outcome") == "win"]
losses = [e for e in entries if e.get("outcome") == "loss"]
flats = [e for e in entries if e.get("outcome") == "flat"]
print(f"  Trades: {len(entries)} | Wins: {len(wins)} | Losses: {len(losses)} | Flat: {len(flats)}")
print(f"  Total P&L: ${total_pnl:.2f}")
print(f"  Total Cost (capital deployed): ${total_cost:.2f}")
print(f"  Total Fees: ${total_fees:.2f}")
print(f"  Fee Drag: {(total_fees/abs(total_pnl)*100) if total_pnl != 0 else 0:.1f}% of total loss")
print(f"  Win Rate: {len(wins)/len(entries)*100:.1f}%")
print(f"  Avg Win: ${sum(e['pnl'] for e in wins)/len(wins):.2f}" if wins else "  Avg Win: N/A")
print(f"  Avg Loss: ${sum(e['pnl'] for e in losses)/len(losses):.2f}" if losses else "  Avg Loss: N/A")
print(f"  Profit Factor: {sum(e['pnl'] for e in wins) / abs(sum(e['pnl'] for e in losses)):.2f}" if losses and wins else "  Profit Factor: N/A")

# ── 2. P&L by Exit Trigger ──────────────────────────────────────────
print("\n" + "=" * 70)
print("2. P&L BY EXIT TRIGGER")
print("=" * 70)
by_trigger = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0, "fees": 0})
for e in entries:
    t = e.get("exit_trigger", "unknown")
    by_trigger[t]["count"] += 1
    by_trigger[t]["pnl"] += e.get("pnl", 0)
    by_trigger[t]["fees"] += e.get("total_fees", 0)
    if e.get("outcome") == "win":
        by_trigger[t]["wins"] += 1
for trigger, data in sorted(by_trigger.items(), key=lambda x: x[1]["pnl"]):
    wr = f"{data['wins']}/{data['count']}"
    print(f"  {trigger:30s}  {wr:>5s} wins  P&L: ${data['pnl']:>8.2f}  Fees: ${data['fees']:>6.2f}")

# ── 3. Entry Price Analysis ──────────────────────────────────────────
print("\n" + "=" * 70)
print("3. ENTRY PRICE DISTRIBUTION")
print("=" * 70)
price_buckets = {"longshot (0-0.20)": [], "underdog (0.20-0.40)": [], "tossup (0.40-0.60)": [], "favorite (0.60-0.80)": [], "heavy_fav (0.80-1.0)": []}
for e in entries:
    for leg in e.get("legs", []):
        ep = leg.get("entry_price", 0)
        pnl = leg.get("leg_pnl", 0)
        if ep <= 0.20:
            price_buckets["longshot (0-0.20)"].append(pnl)
        elif ep <= 0.40:
            price_buckets["underdog (0.20-0.40)"].append(pnl)
        elif ep <= 0.60:
            price_buckets["tossup (0.40-0.60)"].append(pnl)
        elif ep <= 0.80:
            price_buckets["favorite (0.60-0.80)"].append(pnl)
        else:
            price_buckets["heavy_fav (0.80-1.0)"].append(pnl)
for bucket, pnls in price_buckets.items():
    if pnls:
        w = sum(1 for p in pnls if p > 0)
        print(f"  {bucket:25s}  Legs: {len(pnls):>3d}  Wins: {w:>2d}  P&L: ${sum(pnls):>8.2f}  Avg: ${sum(pnls)/len(pnls):>7.2f}")

# ── 4. Market Category Analysis ─────────────────────────────────────
print("\n" + "=" * 70)
print("4. MARKET CATEGORY PERFORMANCE")
print("=" * 70)
categories = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0, "fees": 0})
for e in entries:
    name = e.get("name", "").lower()
    if any(k in name for k in ["ncaa", "nba", "nfl", "basketball", "football", "soccer", "sports"]):
        cat = "Sports"
    elif any(k in name for k in ["bitcoin", "btc", "eth", "crypto", "ethereum"]):
        cat = "Crypto"
    elif any(k in name for k in ["election", "trump", "democrat", "republican", "political", "president"]):
        cat = "Politics"
    elif any(k in name for k in ["oil", "crude", "gold", "commodity"]):
        cat = "Commodities"
    else:
        cat = "Other"
    categories[cat]["count"] += 1
    categories[cat]["pnl"] += e.get("pnl", 0)
    categories[cat]["fees"] += e.get("total_fees", 0)
    if e.get("outcome") == "win":
        categories[cat]["wins"] += 1
for cat, data in sorted(categories.items(), key=lambda x: x[1]["pnl"]):
    wr = f"{data['wins']}/{data['count']}"
    print(f"  {cat:15s}  {wr:>5s} wins  P&L: ${data['pnl']:>8.2f}  Fees: ${data['fees']:>6.2f}")

# ── 5. Repeat Market Analysis (Churning) ────────────────────────────
print("\n" + "=" * 70)
print("5. REPEAT MARKET ANALYSIS (CHURNING)")
print("=" * 70)
market_trades = defaultdict(lambda: {"count": 0, "pnl": 0, "fees": 0, "names": []})
for e in entries:
    name = e.get("name", "")
    norm = name.replace("Auto: ", "").replace("News: ", "").lower().strip()[:50]
    market_trades[norm]["count"] += 1
    market_trades[norm]["pnl"] += e.get("pnl", 0)
    market_trades[norm]["fees"] += e.get("total_fees", 0)
    market_trades[norm]["names"].append(name)
repeats = {k: v for k, v in market_trades.items() if v["count"] > 1}
if repeats:
    for market, data in sorted(repeats.items(), key=lambda x: x[1]["pnl"]):
        print(f"  {market[:45]:45s}  x{data['count']}  P&L: ${data['pnl']:>8.2f}  Fees: ${data['fees']:>6.2f}")
    repeat_pnl = sum(v["pnl"] for v in repeats.values())
    repeat_fees = sum(v["fees"] for v in repeats.values())
    print(f"\n  TOTAL REPEAT MARKET DAMAGE: P&L ${repeat_pnl:.2f}, Fees ${repeat_fees:.2f}")
else:
    print("  No repeat markets found")

# ── 6. Hold Duration Analysis ────────────────────────────────────────
print("\n" + "=" * 70)
print("6. HOLD DURATION ANALYSIS")
print("=" * 70)
duration_buckets = {"< 1h": [], "1-6h": [], "6-24h": [], "1-3d": [], "3-7d": [], "> 7d": []}
for e in entries:
    hrs = e.get("hold_duration_hours", 0)
    pnl = e.get("pnl", 0)
    if hrs < 1:
        duration_buckets["< 1h"].append(pnl)
    elif hrs < 6:
        duration_buckets["1-6h"].append(pnl)
    elif hrs < 24:
        duration_buckets["6-24h"].append(pnl)
    elif hrs < 72:
        duration_buckets["1-3d"].append(pnl)
    elif hrs < 168:
        duration_buckets["3-7d"].append(pnl)
    else:
        duration_buckets["> 7d"].append(pnl)
for bucket, pnls in duration_buckets.items():
    if pnls:
        w = sum(1 for p in pnls if p > 0)
        print(f"  {bucket:10s}  Trades: {len(pnls):>3d}  Wins: {w}  P&L: ${sum(pnls):>8.2f}  Avg: ${sum(pnls)/len(pnls):>7.2f}")

# ── 7. Fee Impact Simulation ────────────────────────────────────────
print("\n" + "=" * 70)
print("7. FEE IMPACT SIMULATION")
print("=" * 70)
pnl_no_fees = total_pnl + total_fees
print(f"  Actual P&L (with fees):    ${total_pnl:.2f}")
print(f"  P&L without fees:          ${pnl_no_fees:.2f}")
print(f"  Fees as % of capital:      {total_fees/total_cost*100:.2f}%")
print(f"  Break-even fee rate:       {(pnl_no_fees/total_cost*100):.2f}% (need this much edge to cover fees)")
# Simulate maker orders (0% fee)
maker_pnl = pnl_no_fees  # If all maker, 0% fee
print(f"  P&L if all maker orders:   ${maker_pnl:.2f}")
print(f"  P&L if 50/50 maker/taker:  ${pnl_no_fees - total_fees/2:.2f}")

# ── 8. Automated vs Manual Exits ────────────────────────────────────
print("\n" + "=" * 70)
print("8. AUTOMATED vs MANUAL EXITS")
print("=" * 70)
auto_triggers = ["trailing_stop", "time_decay", "negative_drift", "target_profit", "stop_loss"]
auto = [e for e in entries if any(t in e.get("exit_trigger", "") for t in auto_triggers)]
manual = [e for e in entries if e not in auto]
print(f"  Automated exits: {len(auto)} trades, P&L: ${sum(e['pnl'] for e in auto):.2f}, Wins: {sum(1 for e in auto if e.get('outcome')=='win')}")
print(f"  Manual exits:    {len(manual)} trades, P&L: ${sum(e['pnl'] for e in manual):.2f}, Wins: {sum(1 for e in manual if e.get('outcome')=='win')}")

# ── 9. Open Positions Status ─────────────────────────────────────────
print("\n" + "=" * 70)
print("9. OPEN POSITIONS CHECK")
print("=" * 70)
pm_path = os.path.join(BASE, "data", "positions", "packages.json")
if os.path.exists(pm_path):
    with open(pm_path) as f:
        pkgs = json.load(f)
    open_pkgs = [p for p in pkgs if p.get("status") == "open"]
    print(f"  Open positions: {len(open_pkgs)}")
    total_open_cost = 0
    total_open_unrealized = 0
    for p in open_pkgs:
        cost = p.get("total_cost", 0)
        upnl = p.get("unrealized_pnl", 0)
        total_open_cost += cost
        total_open_unrealized += upnl
        name = p.get("name", "unknown")[:50]
        hrs = 0
        if p.get("created_at"):
            import time
            hrs = (time.time() - p["created_at"]) / 3600
        print(f"    {name:50s}  Cost: ${cost:>7.2f}  uPnL: ${upnl:>7.2f}  Hold: {hrs:.0f}h")
    print(f"\n  Total Exposure: ${total_open_cost:.2f}")
    print(f"  Total Unrealized P&L: ${total_open_unrealized:.2f}")
else:
    print("  No packages.json found")

# ── 10. Decision Log Analysis (Recent) ───────────────────────────────
print("\n" + "=" * 70)
print("10. DECISION LOG ANALYSIS (Last 1000 entries)")
print("=" * 70)
dlog_entries = []
if os.path.exists(dlog_path):
    with open(dlog_path) as f:
        lines = f.readlines()
    # Take last 1000
    for line in lines[-1000:]:
        line = line.strip()
        if line:
            try:
                dlog_entries.append(json.loads(line))
            except:
                pass

    event_counts = defaultdict(int)
    for d in dlog_entries:
        event_counts[d.get("event", "unknown")] += 1
    print("  Event distribution (last 1000):")
    for evt, cnt in sorted(event_counts.items(), key=lambda x: -x[1]):
        print(f"    {evt:35s}  {cnt:>5d}")

    # Check skip reasons
    skips = [d for d in dlog_entries if d.get("event") == "opportunity_skip"]
    skip_reasons = defaultdict(int)
    for s in skips:
        skip_reasons[s.get("reason", "unknown")] += 1
    if skip_reasons:
        print("\n  Skip reasons:")
        for reason, cnt in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason:45s}  {cnt:>5d}")

    # Check trigger suppression reasons
    suppressions = [d for d in dlog_entries if d.get("event") == "trigger_suppressed"]
    supp_reasons = defaultdict(int)
    for s in suppressions:
        supp_reasons[s.get("reason", "unknown")] += 1
    if supp_reasons:
        print("\n  Suppression reasons:")
        for reason, cnt in sorted(supp_reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason:45s}  {cnt:>5d}")

# ── 11. What-If: Had New Rules Been Active From Start ────────────────
print("\n" + "=" * 70)
print("11. WHAT-IF ANALYSIS: NEW RULES FROM START")
print("=" * 70)
# Simulate: block after 2 losses per market, require 8% spread, 24h cooldown
sim_market_losses = defaultdict(int)
sim_trades = []
sim_blocked = 0
sim_cooldown_blocked = 0
last_exit_time = {}
for e in sorted(entries, key=lambda x: x.get("created_at", 0)):
    name = e.get("name", "").replace("Auto: ", "").replace("News: ", "").lower().strip()[:50]
    created = e.get("created_at", 0)

    # Check max losses
    if sim_market_losses[name] >= 2:
        sim_blocked += 1
        continue

    # Check 24h cooldown
    if name in last_exit_time and created - last_exit_time[name] < 86400:
        sim_cooldown_blocked += 1
        continue

    sim_trades.append(e)
    if e.get("outcome") == "loss":
        sim_market_losses[name] += 1
    last_exit_time[name] = e.get("closed_at", created)

sim_pnl = sum(e.get("pnl", 0) for e in sim_trades)
sim_fees = sum(e.get("total_fees", 0) for e in sim_trades)
sim_wins = sum(1 for e in sim_trades if e.get("outcome") == "win")
print(f"  Original: {len(entries)} trades, P&L: ${total_pnl:.2f}, Fees: ${total_fees:.2f}")
print(f"  Simulated: {len(sim_trades)} trades, P&L: ${sim_pnl:.2f}, Fees: ${sim_fees:.2f}")
print(f"  Blocked by max-losses: {sim_blocked}")
print(f"  Blocked by cooldown: {sim_cooldown_blocked}")
print(f"  Trades saved: {len(entries) - len(sim_trades)}")
print(f"  Losses avoided: ${total_pnl - sim_pnl:.2f}")
print(f"  Fees saved: ${total_fees - sim_fees:.2f}")
if sim_trades:
    print(f"  Simulated win rate: {sim_wins/len(sim_trades)*100:.1f}%")

# ── 12. Top Individual Trade Analysis ────────────────────────────────
print("\n" + "=" * 70)
print("12. TOP / BOTTOM INDIVIDUAL TRADES")
print("=" * 70)
sorted_by_pnl = sorted(entries, key=lambda x: x.get("pnl", 0))
print("  WORST 5:")
for e in sorted_by_pnl[:5]:
    print(f"    ${e.get('pnl',0):>8.2f}  {e.get('exit_trigger','?'):25s}  {e.get('name','?')[:40]}")
print("  BEST 5:")
for e in sorted_by_pnl[-5:]:
    print(f"    ${e.get('pnl',0):>8.2f}  {e.get('exit_trigger','?'):25s}  {e.get('name','?')[:40]}")

print("\n" + "=" * 70)
print("ANALYSIS COMPLETE")
print("=" * 70)
