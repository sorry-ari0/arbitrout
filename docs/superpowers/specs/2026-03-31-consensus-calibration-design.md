# Consensus Calibration Layer — Design Spec

## Goal

Improve directional trade quality by replacing the auto-trader's coarse "platform disagreement" boost with a measurable calibration layer. The new layer should:

1. derive a consensus probability from matched markets,
2. shrink raw market prices toward that consensus using platform/category/horizon context,
3. expose a calibration report through the existing `/api/derivatives/calibration` endpoint,
4. and feed calibrated edge and side selection back into `auto_trader.py`.

This is intentionally a **consensus-calibration** system, not a resolved-outcome calibration system. The repo does not yet have enough fresh realized closes to fit reliable outcome-based calibration curves per bucket. The design uses current cross-platform disagreement as the best available proxy while keeping the API and data model compatible with a later outcome-based upgrade.

## Problem Summary

| # | Problem | Current Behavior | Impact |
|---|---------|------------------|--------|
| 1 | Probability model is too thin | `ProbabilityModel` only stores consensus and a max deviation number | Auto-trader can only apply a generic 1.3x boost |
| 2 | Existing calibration endpoint is effectively dead | `position_router.py` expects `app.state.calibration_engine`, but nothing initializes it | No calibration observability |
| 3 | Trade side selection is mostly heuristic | Directional trades default to favorite/longshot thresholds | Platform/category/horizon context is ignored |
| 4 | No explicit calibrated-edge gate | A trade can have high payout-based "spread" but minimal consensus edge | Weak directional entries survive ranking |

## Design

### 1. Extend `ProbabilityModel` into a consensus-calibration engine

`src/positions/probability_model.py` will continue to own the consensus cache, but it will also maintain bucket-level disagreement statistics keyed by:

- `platform`
- `category`
- `horizon_bucket`

Where `horizon_bucket` is one of:
- `intraday` (`< 1` day)
- `near` (`1-7` days)
- `medium` (`8-30` days)
- `long` (`31+` days)
- `unknown`

Each bucket tracks:
- observation count,
- average absolute deviation from consensus,
- average signed deviation,
- last update timestamp.

These are **not** resolved forecasting scores. They are live disagreement descriptors used to decide how aggressively to shrink a raw market probability toward the cross-platform consensus.

### 2. Add per-opportunity calibration signals

Add a method on `ProbabilityModel`:

```python
get_calibration_signal(
    title: str,
    platform: str,
    raw_yes: float,
    category: str = "",
    days_to_expiry: float | None = None,
    volume: float = 0,
) -> dict | None
```

It returns:
- `consensus_yes`
- `calibrated_yes`
- `shift`
- `calibrated_edge_pct`
- `preferred_side`
- `confidence`
- `bucket`

### Calibration math

For a single market with raw probability `p_raw` and consensus `p_consensus`:

```text
shift = p_consensus - p_raw
calibrated_yes = p_raw + shrink * shift
```

Where `shrink` is a bounded function of:
- bucket disagreement magnitude,
- number of platforms in the matched event,
- local market volume.

The design uses bounded shrinkage rather than a full isotonic/Beta calibration fit because:
- there is not enough resolved-outcome data in the repo yet,
- and the system still needs to act online from current matched events.

The shrinkage factor must remain clipped so the engine never fully overwrites observed market prices.

### 3. Auto-trader integration

`src/positions/auto_trader.py` should consume the calibration signal for single-platform directional opportunities.

Behavior:

1. derive a calibration signal using title/platform/category/horizon/volume,
2. set `preferred_side` from the calibration signal if the opportunity did not already specify one,
3. scale score up/down by calibrated edge magnitude,
4. skip weak non-reference-backed directional trades when calibrated edge is below a low threshold.

This changes the directional entry logic from:
- "price implies payout is attractive"

to:
- "price is attractive **and** consensus-adjusted probability still supports the trade."

### 4. Reporting

Add `generate_report()` to `ProbabilityModel` so the existing calibration endpoint becomes live without additional API redesign.

The report should include:
- total tracked events,
- total calibration buckets,
- top unstable buckets by average absolute deviation,
- recent events with largest current disagreement.

### 5. Server wiring

`src/server.py` should set:

```python
app.state.calibration_engine = _probability_model
```

This preserves the current router contract and avoids another singleton.

## What This Design Does Not Try To Solve

- It does not claim true outcome calibration from consensus alone.
- It does not replace future outcome-based calibration curves.
- It does not add new external data sources.
- It does not alter arbitrage package math; it primarily targets directional trade quality.

## Testing

### `tests/test_probability_model.py`
- bucketed consensus still computes correctly
- calibration signal shrinks raw prices toward consensus
- low-consensus / single-market events still return no calibration
- report contains bucket summaries

### `tests/test_auto_trader_improvements.py`
- calibration signal sets `preferred_side` and produces a tradeable converted opportunity path
- weak calibrated directional edges can be filtered for non-reference-backed trades

### `tests/test_position_router.py`
- `/api/derivatives/calibration` returns a structured payload when a calibration engine is attached to `app.state`

## Expected Outcome

This should increase win rate by removing directional trades that only look good on raw payout math while preserving opportunities where cross-platform consensus materially disagrees with the quoted price. It also gives the app a measurable calibration artifact that can later be upgraded from consensus-based shrinkage to outcome-based calibration once enough resolved data accumulates.
