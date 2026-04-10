# Spec: True-arb gating and validated Kelly sizing (2026-04-10)

This follow-up hardens two paths that are still structurally unsafe in the paper engine.

## 1. Directional Kelly must use validated edge, not a favorite bonus

### Problem

- `pure_prediction` still uses a separate sizing path that adds a heuristic edge bonus based on price level.
- High-priced favorites are being treated as "high conviction" even when the system has no validated probability edge.
- That creates Kelly-positive sizes from selection bias instead of evidence.

### Changes

- Replace the pure-prediction favorite-bonus sizing path with a validated-probability helper.
- Estimated probability sources, in priority order:
  - calibration signal,
  - strong insider-follow signal on the chosen side,
  - reference-backed directional signal.
- If no validated source produces positive edge after uncertainty stress, skip the trade.
- Stop treating `price >= 0.85` as high conviction by itself.

## 2. Cross-platform arb must mean deterministic binary arbitrage

### Problem

- Historical `cross_platform_arb` losses are concentrated in platform mixes whose execution / fee / settlement semantics are not reliable enough for “guaranteed” profit inside this engine.
- The current precheck still allows dust-price legs and fallback quote paths that are not robust enough for true arbitrage.

### Changes

- Gate `cross_platform_arb` to vetted binary platforms only.
- Require:
  - both legs in the same matched cluster,
  - compatible expiries,
  - executable quotes on both legs,
  - no midpoint fallback,
  - no dust-price legs,
  - net edge above a conservative true-arb buffer after fees.
- Treat unvetted platform mixes as ineligible for the `cross_platform_arb` strategy.

## 3. Paper hold-to-resolution exits must settle from snapped resolution values

### Problem

- `market_resolved` correctly snaps binary legs to `$0/$1`, but paper exit flow can still fall back to live quote mechanics before finalization.
- That lets a resolved winning leg be monetized at a stale quote instead of deterministic settlement value.

### Changes

- Add a direct paper settlement path for resolved binary legs.
- Use snapped binary price for normal binary markets.
- Use the shared resolution payout model for platforms with non-trivial settlement drag (currently PredictIt).
- Persist a distinct `resolution_settlement` exit type for auditability.

## Acceptance

- Pure-prediction opens without validated edge are skipped.
- Cross-platform arb rejects unvetted platform mixes and midpoint-fallback quotes.
- Paper-resolved arbs close from snapped settlement values instead of stale quote fallback.
- Focused tests pass for auto trader, paper executor, and position manager behavior.
