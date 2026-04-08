# Spec: Journal-audit follow-ups (2026-04-08)

Execution order matches implementation PR.

## 1. Fix paper settlement accounting for structural arbs

### Problem

- The journal still shows negative `multi_outcome_arb` closes that look like stale-price exits rather than binary settlement.
- `market_resolved` currently fires for any open leg at `<= 0.01` or `>= 0.99`, which is correct for complementary YES/NO packages but too aggressive for mutually exclusive YES portfolios.
- `_snap_resolution_prices()` only fully normalizes `multi_outcome_arb` when a winner leg is already visible.

### Changes

- Tighten `evaluate_heuristics()` so `multi_outcome_arb` only emits `market_resolved` when:
  - a winning leg is already at `>= 0.99`, or
  - every open leg is already at a binary boundary.
- Extend `_snap_resolution_prices()` to normalize residual legs conservatively once package-level resolution is known.
- Add regression tests for:
  - `multi_outcome_arb` partial-loser state does **not** trigger package exit,
  - `multi_outcome_arb` winner-present state **does** trigger package exit,
  - cross-platform and portfolio-no behavior remains unchanged.

## 2. Make insider tracking part of the live feedback loop

### Problem

- `InsiderTracker.record_resolution()` and `get_exit_signals()` exist but are not wired into the closing path.
- The journal currently does not preserve whether a close was insider-backed, making audit slices incomplete.

### Changes

- Add an optional `insider_tracker` reference to `PositionManager`.
- When a package fully closes, infer resolved outcomes for Polymarket condition IDs from binary leg exits and call `insider_tracker.record_resolution(...)`.
- Persist insider evidence in journal rows:
  - `insider_sleeve`
  - `insider_signal`
  - `_insider_driven`
- Add regression tests for:
  - resolution feedback fires once per resolved condition,
  - journal rows preserve insider flags.

## 3. Tighten cross-platform arb execution gating

### Problem

- Cross-platform arb is still 0-for-4 in the journal.
- The current re-quote gate checks fresh prices, but it compares gross edge only and rewrites `net_profit_pct` to the same gross value.
- That can over-admit spreads whose executable edge disappears after platform economics are applied.

### Changes

- Make `_precheck_cross_platform_arb()` recompute both gross and fee-adjusted net edge from the fresh re-quote.
- Gate on fee-adjusted net edge instead of gross edge.
- Preserve the fresh gross edge separately for debugging / decision-log visibility.
- Add regression tests for:
  - a gross-positive but fee-negative PredictIt cross-arb is rejected,
  - a genuinely net-positive arb is accepted and updates the opportunity prices.

## Acceptance

- Added regression coverage for the new resolution and arb-precheck behavior.
- `python -m pytest` passes for the touched test modules when `pytest` is available.
- `python tools/metrics_snapshot.py` still runs without error.
