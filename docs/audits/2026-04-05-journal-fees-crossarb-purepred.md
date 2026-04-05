# Audit: journal fees, cross-platform arb, pure prediction (2026-04-05)

Source data: `src/data/positions/trade_journal_paper.json` (58 closes at audit time).  
Re-run: `python tools/journal_strategy_audit.py` and `python tools/audit_maker_fee_compliance.py`.

## 1. Fee anomalies — what actually happened

Twenty closes have `total_fees >= $0.005`. All fees in the journal are **leg-level sell (and sometimes buy) fees** rolled up in `trade_journal.record_close()` from `leg["sell_fees"]` / buy fees.

### 1.1 By `exit_trigger`

| Trigger | Count | Interpretation |
|--------|-------|----------------|
| `manual_full_exit` | 10 | Same-second burst **2026-03-19** — direct `exit_leg(..., use_limit=False)` path → `PaperExecutor.sell()` **without** resting GTC; historically matched **taker-style** fee sim (~2% on Polymarket paper). |
| `market_resolved` | 4 | Safety exits at $0/$1; **Mar 28–30** rows still show fees → likely **FOK fallback** after failed limit, or paper stack **before** strict 0% maker enforcement on all sell paths. |
| `ai_approved:time_decay` | 3 | Early regime: AI-approved exits, same taker/maker ambiguity window as above. |
| `auto:trailing_stop` | 1 | Same era; trailing stop since removed from trusted exits. |
| `ai_approved:negative_drift` | 1 | Same era. |
| `ai_approved:stop_loss` | 1 | Synthetic leg; tiny fee. |

### 1.2 Clean break for `pure_prediction`

- **Through ~2026-03-19**: many closes with **~$3.25–$4.38** fees (consistent with **~2% × ~$100** notional per leg or round-trip accounting on simulated taker).
- **From ~2026-03-22 onward**: almost all `pure_prediction` closes show **`$0` fees** for `bracket_target` and `market_resolved` — aligned with **`_use_limit_orders` + `PaperExecutor` 0% maker** and bracket fills journaling correctly.

So: **anomalies are overwhelmingly legacy rows + one resolution window**, not ongoing “mystery” fees in the current maker-only design.

### 1.3 `cross_platform_arb` fees

All **four** historical closes carry **non-zero fees** (manual batch + three `market_resolved`). There are **no** post-fix cross-arb closes in the journal yet, so **Kelly gate / precheck** improvements are **forward-looking** (no proof in closes).

## 2. Cross-platform arbitrage — over time

| Period | n | Sum PnL | Sum fees | Wins | Losses |
|--------|---|---------|----------|------|--------|
| 2026-03 | 4 | **−$9.43** | **$7.20** | 0 | 4 |

**Observations**

- **Sample size is tiny** (4 trades); statistics are indicative only.
- Losses cluster around **manual resolution batch** and **March 28–30 market_resolved** exits with fee drag.
- **Code changes aimed at improvement** (chronological, high level):
  - **Arb budget reserve** (`auto_trader`) — reduce starvation of structural arbs.
  - **Maker-only paper** (`paper_executor`) — new closes should show **$0** fees if exits use GTC/limit path only.
  - **PR #128**: cross-arb **pre-trade re-quote**, **staleness**, **cluster ID check**, **Kelly cap until first journal win** — should **reduce** low-edge / stale arbs; **no journal wins yet** to lift the cap.

**Better vs worse**

- **Better (expected, not yet in journal):** fewer bad arbs opened; smaller Kelly when win rate is 0; fee truth on new exits.
- **Worse (historical):** all recorded arbs lost money and paid fees; **do not** extrapolate to future without new data.

## 3. Pure prediction — over time

| Month | n | Sum PnL | Sum fees | Notes |
|-------|---|---------|----------|--------|
| 2026-03 | 30 | **−$46.42** | **$55.40** | Dominated by **Mar 19** fee-heavy closes + AI/trailing exits; many small **Mar 22+** wins with **$0** fees. |
| 2026-04 | 3 | **+$7.12** | **$0.00** | Small sample; aligns with **tighter spread bar**, **concurrency cap**, **news reserve**, **flat-leg suppression** for soft AI exits, and **maker fee truth**. |

**Better vs worse**

- **Better:** April slice (early) shows **zero fee drag** and **positive** aggregate PnL; March **post-22nd** already shows **$0** fees on many `market_resolved` / `bracket_target` wins.
- **Worse (legacy):** March early window combines **taker-like fees**, **AI/time_decay/trailing** exits that the product has since **restricted or removed** from the happy path.

## 4. Recommended follow-ups

1. **Optional journal hygiene:** tag or migrate pre-2026-03-20 rows as `fee_model: legacy_taker` for dashboards so `fee_to_gross` milestones are evaluated on **post-cutover** closes only.
2. **Cross-arb:** wait for **N ≥ 10** new closes after #128; if fees appear on `market_resolved`, trace `sell_limit` → FOK fallback in `position_manager._place_limit_sell`.
3. **Re-run** `tools/metrics_snapshot.py --days 30` after more April volume for rolling KPIs.
