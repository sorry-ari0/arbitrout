# Spec: Arb depth, insider exits, and risk-capped sizing (2026-04-08)

This follow-up closes the remaining gaps from the journal audit after PR #133.

## 1. Replace duplicated arb fee math with a shared model

### Problem

- Cross-platform arb screening still duplicates fee constants in multiple modules.
- The changelog review already flagged this as the wrong long-term shape.

### Changes

- Add a shared `execution.fee_model` module for:
  - per-platform entry fee rates,
  - PredictIt resolution drag,
  - cross-platform guaranteed net-edge computation.
- Switch both `arbitrage_engine.py` and `positions/auto_trader.py` to the shared helpers.

## 2. Prefer executable cross-arb quotes over scalar midpoint snapshots

### Problem

- The cross-arb recheck still accepted one scalar price per leg, which is too optimistic.
- Polymarket already exposes best bid/ask and order-book levels; Kalshi market payloads expose top-of-book fields when available.

### Changes

- Add an optional `get_executable_price(...)` hook on executors.
- Implement conservative buy-side executable quotes for:
  - `PolymarketExecutor`: shallow-depth ask-side estimate from the CLOB book, falling back to best ask, then midpoint.
  - `KalshiExecutor`: prefer explicit `yes_ask` / `no_ask` fields, then midpoint.
  - `PaperExecutor`: delegate through to the wrapped real executor.
- Update `_precheck_cross_platform_arb()` to use executable quotes and persist quote-source metadata on the opportunity for debugging.

## 3. Wire insider exits into exit review flow

### Problem

- `InsiderTracker.get_exit_signals()` exists, but the exit engine still ignores it.
- That means conviction-trader exits never surface as package review signals.

### Changes

- Extend `evaluate_heuristics()` with an optional `insider_tracker`.
- When recent Polymarket conviction-trader exits match the package’s `_bet_side`, emit a non-safety `insider_exit` review trigger.
- If AI is unavailable, route the signal to alerts rather than silently suppressing it.

## 4. Make directional sizing explicitly risk-capped

### Problem

- Directional sizing still relied on classic Kelly plus edge shrink, which does not explicitly encode probability uncertainty or bankroll-at-risk caps.
- The journal loss shape is driven by a few directional wipeouts, so the cap needs to be direct and mechanical.

### Changes

- Add a stressed-probability directional Kelly helper:
  - shrink toward the market as before,
  - subtract an uncertainty margin from the estimated probability,
  - then apply the Kelly fraction to the stressed probability.
- Cap the resulting directional bankroll fraction at:
  - `0.75%` by default,
  - `1.25%` for higher-conviction directional entries (insider-backed or extreme favorites).
- Use the helper in:
  - `_kelly_size()` when `implied_prob` is provided,
  - the `pure_prediction` sizing path.

## Acceptance

- Cross-arb precheck tests cover executable-quote preference and fee-model parity.
- Exit-engine tests cover insider-exit trigger generation.
- Focused test modules for the touched files pass.
