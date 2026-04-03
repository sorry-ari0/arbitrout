# Spec: Paper trading optimizations (2026-04-03)

Execution order matches implementation PR.

## 1. Fee truth

**Goal:** Paper closes reflect **0% maker** policy (see `PaperExecutor`).

**Actions:**
- Runtime: `PaperExecutor` forces `buy_fee_rate = sell_fee_rate = 0` when `use_limit_orders=True` (done prior).
- **Audit:** `tools/audit_maker_fee_compliance.py` flags journal rows with `total_fees` above epsilon (post–fee-fix rows).
- **Metrics:** `metrics_snapshot.py` emits `fee_anomaly_count` and milestone checks vs targets.

## 2. Stop funding negative gross strategies

### 2a Cross-platform arb

- **Pre-trade:** Re-fetch YES/NO mid via executors; require gross edge ≥ `CROSS_ARB_MIN_EDGE_REQUOTE_PCT` after re-quote.
- **Staleness:** Reject if `time.time() - scanner._last_scan_time > CROSS_ARB_MAX_QUOTE_AGE_SEC`.
- **Cluster integrity:** Both leg condition IDs must appear under `matched_event.markets`.
- **Kelly:** If ≥ `CROSS_PLATFORM_KELLY_GATE_MIN_CLOSES` journal closes and **0 wins**, cap Kelly fraction at `CROSS_PLATFORM_KELLY_CAP_UNTIL_WIN`.

### 2b Pure prediction

- **Higher spread bar:** `MIN_SPREAD_PCT_DIRECTIONAL = 12%` for opportunities without news/insider/reference backing; keep `MIN_SPREAD_PCT_SIGNAL = 8%` for signal-backed.
- **Concurrency:** Cap open `pure_prediction` packages at `MAX_CONCURRENT_PURE_PREDICTION`.
- **Scoring:** Apply `net_profit_pct / profit_pct` multiplier to score when both present (fee-truthful edge).

## 3. Allocate bankroll to proven sleeves

- **News reserve:** `NEWS_BUDGET_RESERVE_PCT` of max exposure reserved for `_news_driven` packages (mirror arb reserve).
- Non-news directional trades draw from `directional_budget - news_headroom` only.
- **Synthetics:** Remain disabled unless `SYNTHETIC_DERIVATIVE_EXPERIMENT=true` (optional tiny experiment hook).

## 4. Exit policy vs data

- **AI soft exits:** Suppress `time_decay` / `negative_drift` from AI batch when all open legs are **flat** (|Δprice/entry| < `FLAT_MOVE_THRESHOLD_AI_SOFT`).
- **`market_resolved`:** Unchanged; `_snap_resolution_prices` remains authoritative.

## 5. Measurement

- **`metrics_snapshot.py`:** Adds `milestones` (`fee_to_gross_ok`, `pure_fee_to_gross_ok`) and `fee_anomaly_count`.
- **Weekly:** `--days N` optional window for rolling stats (same script).

## Acceptance

- `pytest` passes for `tests/test_limit_orders.py`, `tests/test_limit_exits.py`, `tests/test_paper_executor.py`.
- `python tools/metrics_snapshot.py` runs without error.
- `python tools/audit_maker_fee_compliance.py` runs (informational exit 0).
