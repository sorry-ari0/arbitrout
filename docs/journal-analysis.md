# Arbitrout Trade Journal Analysis

> Persistent file for tracking performance trends across PRs and system changes. Updated each time the journal is reviewed.

---

## Snapshot: 2026-03-31 (optimization pass, no new closed trades)

### Evidence Review Window
- Trade journal still has no new closed paper trades after 2026-03-19, so this pass is based on scanner and decision-log evidence rather than new realized PnL.
- Latest reviewed decision-log window remains 2026-03-24 10:27Z-11:49Z.

### What This Pass Found

1. **Zero-volume opportunities were still reaching the decision layer.**
   - `opportunity_detected` entries continued to show `cross_platform_arb` setups with `volume: 0`.
   - Example markets in the log included Greenland and trade-agreement contracts.
   - This meant the system was still spending ranking/attention budget on non-executable ideas even after scanner-side actionable-pair work.

2. **Reference-model scanners existed but were not connected to autonomous trading.**
   - `theta_scanner.py` and `cross_asset_matcher.py` were available through API routes, but `auto_trader.py` was not consuming them.
   - Result: the app had newer analysis modules that improved visibility, but they were not affecting actual trade selection.

3. **Commodity suppression was too broad for the current architecture.**
   - The auto trader still hard-skipped all commodity titles with `commodities_market`.
   - That made sense before the reference-model additions, but after the commodity adapter/cross-asset work it prevented any reference-backed commodity idea from ever reaching execution.

### Changes Applied On 2026-03-31

- `src/positions/auto_trader.py`
  - Added a hard `zero_volume` skip before execution.
  - Added theta-scanner ingestion.
  - Added cross-asset reference-opportunity ingestion.
  - Added `preferred_side` support so scanner consensus can drive YES/NO selection directly.
  - Narrowed the commodity block so only raw/unreferenced commodity opportunities are skipped.

- `src/cross_asset_matcher.py`
  - Added `prediction_volume`, `reference_volume`, and `combined_volume` to emitted opportunities so downstream filters can enforce liquidity.

- `src/server.py`
  - Wired the theta scanner and cross-asset matcher into `AutoTrader` during startup.

### Verification
- Targeted regression suite: `83 passed in 1.52s`
- Compile check: `python -m py_compile src/positions/auto_trader.py src/cross_asset_matcher.py src/server.py`

### Expected Impact

1. **Less decision-log noise.** Zero-liquidity opportunities should now stop at the auto-trader gate instead of competing with executable candidates.
2. **Reference analysis now influences trading.** Theta and cross-asset scanners are no longer UI-only analysis features.
3. **Commodity handling is more precise.** The system still rejects raw commodity directional trades, but it can now consider reference-backed commodity setups.

### Remaining Validation Gap
- No new closed trades have occurred yet, so this is still an implementation improvement, not a realized-PnL improvement.
- The next journal pass should explicitly check whether:
  - `zero_volume` becomes a visible skip reason,
  - `commodities_market` skip frequency falls for reference-backed setups,
  - and `trade_opened` starts to include `theta_consensus` or `cross_asset_reference`-driven entries.

## Snapshot: 2026-03-21 (39 trades)

### Overall Stats
| Metric | Value |
|--------|-------|
| Total trades | 39 |
| Win rate | 17.9% (7W / 31L / 1F) |
| Total PnL | -$190.84 |
| Total fees | $119.96 |
| Capital deployed | $6,314.70 |
| Fee drag | 1.9% of capital |

### Phase Breakdown

**Phase 1: Pre-automation (trades 1-11, Mar 18 01:41–18:02)**
- PRs active: #80 (arb quality + exit engine), derivative-position-manager
- Exit method: 100% manual_full_exit
- Win rate: 18.2% (2W / 8L / 1F)
- PnL: +$0.58 (essentially break-even)
- Avg position: $185 | Avg hold: 13.1h | Fees: $37.51 (1.8%)
- **Key observation:** Manual exits preserved capital but 2% taker fees eroded all upside. Two BTC dip trades were the only wins (+7.3% each), but identical trade #5 lost -2.0%. System was entering the same market 3x (BTC dip) — no dedup guard yet.

**Phase 2: Exit engine + AI exits (trades 12-35, Mar 18 18:06–Mar 21 11:38)**
- PRs active: #81-87 (political synth, performance fix, arbigab, exit optimization, dedup guards, signal reentry)
- Exit methods: auto:trailing_stop (8), ai_approved:time_decay (7), ai_approved:negative_drift (4), manual (2), ai_trailing (2), spread_inversion (1)
- Win rate: 4.2% (1W / 23L)
- PnL: **-$201.33** (the bleeding phase)
- Avg position: $177 | Avg hold: 9.7h | Fees: $82.24 (1.9%)
- **What went wrong:**
  - AI exits (time_decay, negative_drift) were trigger-happy: 13 trades closed at -2% to -9%, never giving positions time to recover. 0 wins from AI exits.
  - auto:trailing_stop hit 8 times, 0 wins — stops were too tight, catching normal volatility. NCAA basketball and sports exact-score markets were especially bad (-13.5% to -13.7%).
  - Sports and commodities markets killed us: NCAA entries lost $68, Crude Oil entries lost $46, exact-score bets lost $24. All from AI or trailing stop exits.
  - One bright spot: manual_full_exit still got 1 win (BTC dip +0.9%)

**Phase 3: Post-journal fixes (trades 36-39, Mar 21 10:35–13:16)**
- PRs active: #91 (bracket orders), #93 (journal-driven fixes), #94 (Kyle's lambda)
- Exit method: 100% bracket_target
- Win rate: **100.0%** (4W / 0L)
- PnL: **+$9.91** (+40.7% avg return!)
- Avg position: **$7** | Avg hold: 2.7h | Fees: **$0.20** (0.7%)
- **What changed (PR #93 journal-driven fixes):**
  - AI exits DISABLED (AI_EXITS_ENABLED=False)
  - Stops widened to -35% minimum
  - Trailing stop base 35% (bounds 15-50%)
  - Position size halved: $200→$100 max, $1400→$700 exposure
  - Maker orders: 0% fee via GTC limit (was 2% taker)
- **What changed (PR #91 bracket orders):**
  - bracket_target exit type: GTC limit at target price, 0% maker fee
  - Rolling trail for exits
- **Caveat:** Only 4 trades with tiny $5-12 positions. High win rate may not hold at larger sizes. But 0% fees is structural — that advantage scales.

### Exit Trigger Scorecard (all time)

| Trigger | Trades | Wins | Win% | Total PnL | Avg PnL% | Verdict |
|---------|--------|------|------|-----------|----------|---------|
| bracket_target | 4 | 4 | 100% | +$9.91 | +40.7% | **Best performer** |
| manual_full_exit | 13 | 3 | 23% | -$1.66 | -1.4% | Break-even, slow |
| auto:trailing_stop | 8 | 0 | 0% | -$88.79 | -8.7% | **Worst performer** |
| ai_approved:time_decay | 7 | 0 | 0% | -$29.07 | -2.1% | Premature exits |
| ai_approved:negative_drift | 4 | 0 | 0% | -$55.36 | -6.8% | Killed Crude Oil trades |
| ai_approved:trailing_stop | 2 | 0 | 0% | -$23.26 | -13.6% | NCAA sports slaughter |
| spread_inversion | 1 | 0 | 0% | -$2.62 | -1.8% | Sample too small |

### Market Category Scorecard

| Category | Trades | Wins | Win% | PnL | Notes |
|----------|--------|------|------|-----|-------|
| Crypto | 23 | 5 | 22% | -$42.60 | Best category. BTC dip + SOL/LINK bracket wins |
| Sports | 10 | 2 | 20% | -$91.99 | NCAA was catastrophic. Exact-score bets risky |
| Commodities | 3 | 0 | 0% | -$45.76 | Crude Oil: 3 entries, 3 AI-exit losses |
| Politics | 1 | 0 | 0% | -$2.00 | Too few to judge |
| Social/Celebrity | 1 | 0 | 0% | -$5.86 | Elon Musk posts — novelty market |
| Other | 1 | 0 | 0% | -$2.62 | Soccer cross-platform arb |

### PR Impact Timeline

| Date | PR | What Changed | Impact on Trades |
|------|-----|-------------|-----------------|
| 03/18 | #80 | Arb quality + exit engine | Enabled auto trading. Phase 1 = manual exits only |
| 03/18 | derivative-position-manager | Position system foundation | Trades 1-11 all use this |
| 03/19 | #81 | Political synthetic analysis | No direct trade impact |
| 03/19 | #82 | Performance fix (three-phase) | Faster scanning, same outcomes |
| 03/19 | #83 | Arbigab improvements | Better opportunity detection |
| 03/20 | #84 | Exit optimization | AI exits, trailing stops. **Caused Phase 2 bleeding** |
| 03/20 | #85 | Duplicate entries dedup | Fixed journal double-counting |
| 03/20 | #86 | Execute package dedup guard | Prevented duplicate order execution |
| 03/20 | #87 | Signal-driven reentry | Re-entered after exit signals. Made things worse |
| 03/20 | #91 | Bracket orders + rolling trail | **bracket_target exits = 100% win rate** |
| 03/21 | #93 | Journal-driven fixes | **Disabled AI exits, widened stops, halved size, 0% fees** |
| 03/21 | #94 | Kyle's lambda | Adverse selection signal. Too new to measure effect |

### What's Getting Better

1. **Fee structure is dramatically improved.** Phase 1-2: 1.8-1.9% fee drag. Phase 3: 0.7%. Maker orders (0% fee) save ~$4/trade on $200 positions. This is structural and permanent.
2. **AI exits are off.** They were responsible for $107.69 in losses across 13 trades with 0 wins. Turning them off was the single biggest improvement.
3. **Bracket target exits work.** 4/4 wins, avg +40.7%. GTC limit orders at target price = patient, fee-free profit taking.
4. **Position sizing is more conservative.** $100 max (from $200) limits downside per trade. Phase 3 positions were $5-12, which is too small, but the principle is right.
5. **Duplicate trade prevention.** PRs #85-86 fixed journal double-counting and execution dedup. Phase 1 had 3 identical BTC dip entries.

### What's Getting Worse / Not Improving

1. **Win rate is still poor overall (17.9%).** Phase 3's 100% is tiny sample (4 trades). Need 20+ Phase 3 trades to validate.
2. **Sports markets are a money pit.** 10 trades, $91.99 in losses. NCAA basketball and exact-score bets are high-variance, low-edge. The system keeps entering them.
3. **Commodities (Crude Oil) had 0 wins.** 3 entries, all closed by AI negative_drift. May have been right trades with wrong exits, but can't tell without the AI exits running.
4. **Cross-platform arbs are untested.** Only 2 trades (#2 and #19), both losses. The spread wasn't real or closed too fast.
5. **pnl_usd is $0.00 for all trades.** Paper executor doesn't track actual USD P&L — only pnl_pct. Makes capital analysis unreliable.

### What Still Needs Work

1. **Market category filter.** Sports exact-score and NCAA markets should be avoided or heavily discounted. They contributed -$91.99 from 10 trades. Consider: exclude "Exact Score" markets, cap sports at lower position sizes, or require higher spreads for sports.
2. **Commodities caution.** Crude Oil was 0/3. The system entered 3 separate Crude Oil positions — same lack of dedup seen with BTC dip in Phase 1. May need market-level cooldown for commodities.
3. **Position size ramp-up.** Phase 3 sizes ($5-12) are too small for meaningful P&L. Need gradual increase as confidence grows: $25 → $50 → $100.
4. **Trailing stop calibration.** 8 trailing stop exits, 0 wins. Even at 35% base, need to verify the adaptive widening is actually triggering. Sports events may need even wider stops or no trailing stop at all (binary outcomes).
5. **Kyle's lambda validation.** Just deployed — zero trades with lambda signal yet. Need 20+ trades to see if it meaningfully filters adverse selection.
6. **Cross-platform arb quality.** Only 2 attempts, both losses. The arb scanner may be finding stale or thin spreads. Need volume/liquidity filters.
7. **pnl_usd tracking.** Paper executor should compute actual USD P&L so we can do real capital analysis.

---

## How to Update This File

When reviewing the journal again:
1. Run the journal analysis script (see below)
2. Add a new snapshot section with the date and trade count
3. Compare phase metrics to previous snapshot
4. Note any new PRs and their observed impact
5. Update the "Getting Better / Worse / Needs Work" sections

---

## Cross-Reference Data Sources

### 1. Trade Journal (`src/data/positions/trade_journal.json`)
- **Primary source for P&L.** 39 entries as of 2026-03-21.
- Fields: pnl (USD), pnl_pct, total_cost, exit_value, total_fees, exit_trigger, legs, hold_duration_hours
- New: `pnl_usd` field added for explicit USD tracking, `get_equity_curve()` for cumulative P&L

### 2. Decision Log (`src/data/positions/decision_log.jsonl`)
- **55,032 entries** as of 2026-03-21. Append-only JSONL.
- Key types: opportunity_detected (20,925), triggers_fired (8,624), trigger_suppressed (8,559), news_headline (7,051), arb_scan_summary (4,318), scan_skip (3,927), ai_review (1,462), trade_opened (25)
- **Skip reasons (3,963 skips):** kelly_portfolio_cap (2,493), max_concurrent (1,325), daily_limit (109), already_open (26), too_near_expiry (10)
- **Insight:** 62.9% of skips are Kelly cap — system finds more opportunities than bankroll allows. max_concurrent (33.4%) also significant.

### 3. Eval Logger (`src/data/arbitrage/eval_log.jsonl`)
- Records EVERY opportunity at decision time (entered, skipped, rejected)
- Backfill entries with actual P&L after market resolves
- Useful for "what if" analysis: would the skipped trades have won?

### 4. Positions File (`src/data/positions/positions.json`)
- Currently open packages with real-time values
- Historical closed packages with realized_pnl
- Used by auto_trader for cooldown/dedup checks

### 5. Git History (`git log`)
- Project started 2026-03-11 (initial scaffold)
- First trades: 2026-03-18 (derivative-position-manager PR)
- 15 PRs merged since first trade (#80-94)

---

## Project Timeline (Full History)

| Date | Milestone | Impact |
|------|-----------|--------|
| 03/11 | Initial commit: scaffold + design spec | - |
| 03/14 | Arb scanner implementation (8 tasks) | Core scanning |
| 03/15 | Auto-scan, Limitless API, trade ratios | Market coverage |
| 03/16 | Entity-based event matcher rewrite | Match quality |
| 03/17 | Aider dispatcher: 8 tasks automated | Build automation |
| 03/18 | Position system + first trades | **Trading begins** |
| 03/18 | PR #80: Arb quality + exit engine | Automated exits |
| 03/18 | **Phase 1 trades 1-11** | Manual exits, break-even (+$0.58) |
| 03/18 | **Phase 2 begins** (trade 12) | AI exits active, bleeding starts |
| 03/19 | PRs #81-83: Political synth, perf fix, arbigab | More opportunity types |
| 03/20 | PRs #84-87: Exit optimization, dedup guards | Exit engine refinement |
| 03/20 | PR #91: Bracket orders | **bracket_target = 100% WR** |
| 03/21 | PR #93: Journal-driven fixes | AI exits OFF, stops widened, size halved |
| 03/21 | PR #94: Kyle's lambda | Adverse selection signal |
| 03/21 | **Phase 3 trades 36-39** | 4/4 wins, +$9.91, 0% fees |

**Key turning points:**
- 03/18 18:06: Phase 2 begins, AI exits destroy value (-$201 over 24 trades)
- 03/20: Bracket orders merged — first structural fix
- 03/21: Journal-driven fixes — AI exits disabled, the single biggest improvement

### Equity Curve (Cumulative P&L)

Use `TradeJournal.get_equity_curve()` for the authoritative curve. Summary:

| Trade # | Cumulative P&L | Key Event |
|---------|---------------|-----------|
| 1 | $0.00 | ETH Prediction (flat) |
| 3-4 | +$29.33 | BTC dip wins (+7.3% each) — **peak equity** |
| 5-11 | +$0.58 | Fees erode remaining gains |
| 12-14 | -$13.02 | AI exits begin (time_decay, negative_drift) |
| 21-25 | -$110.67 | NCAA + trailing stop massacre |
| 31 | -$183.00 | Crude Oil final AI exit |
| 35 | -$200.75 | **trough** (end of Phase 2) |
| 36-39 | **-$190.84** | Bracket wins begin recovery |

**Early positive unrealized gains (trades 3-4):** The BTC dip bets bought NO at $0.84 and it resolved to $1.00 — +$14.67 each after fees. This was the system at its best: crypto favorable, high-probability entry, patient exit. The challenge was replicating this consistently across non-crypto markets.

---

## How to Update This File

When reviewing the journal again:
1. Read this file first for baseline context
2. Pull fresh data from `src/data/positions/trade_journal.json`
3. Cross-reference with decision log for skip patterns: `src/data/positions/decision_log.jsonl`
4. Add a new snapshot section with the date and trade count
5. Compare phase metrics to previous snapshot
6. Note any new PRs and their observed impact
7. Update the "Getting Better / Worse / Needs Work" sections

### Quick Equity Check
```python
from positions.trade_journal import TradeJournal
from pathlib import Path
journal = TradeJournal(Path("src/data/positions"))
curve = journal.get_equity_curve(mode="paper")
print(f"Cumulative P&L: ${curve['cumulative_pnl_usd']}")
print(f"Max drawdown: ${curve['max_drawdown_usd']}")
print(f"Total fees: ${curve['cumulative_fees_usd']}")
```
