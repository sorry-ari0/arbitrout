# Changelog

Notable changes to **arbitrout** are listed here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Maker round-trip fee helper** (`src/execution/fee_model.py:compute_maker_round_trip_fee_frac`): returns modeled entry+exit maker fees as a fraction of stake for Polymarket (0%), Kalshi (`2·0.0175·(1−p)`), PredictIt (profit tax + withdrawal drag), and other platforms via `PLATFORM_MAKER_FEE_FRAC_PER_FILL`. Used to pre-subtract fee drag from Kelly edge.
- **Hard NO-cap gate** (`src/positions/auto_trader.py` directional path): rejects `prediction_no` entries at `side_price > 0.85` unless a NAMED edge source (`insider_follow`, `reference`, `calibration`) exceeds `round_trip_fee + 3%`. Whelan 2026 (Kalshi favorite-longshot bias paper) shows the prior on this side is negative — a hardcoded `+0.02` assumed edge has no empirical basis. Skip reason `no_side_high_price_insufficient_edge`.
- Tests: `TestKellyFeeSubtractionAndNoCap` in `tests/test_auto_trader_improvements.py` covering default-edge=0 fallback, fee>edge→0, fee shrinks sizing, Kalshi/Polymarket maker fee math, and gate accept/reject scenarios.
- **NASA EONET** (`src/positions/eonet_client.py`): fetch open natural events from [EONET API v3](https://eonet.gsfc.nasa.gov/) and blend conservative precipitation adjustments into NWS forecasts in `WeatherScanner` when hazards are near the city and forecast date. Toggle with `EONET_WEATHER_ENABLED`. Unit tests in `tests/test_eonet_client.py`.
- Spec: `docs/specs/2026-04-08-journal-audit-followups.md` for audited follow-up fixes covering structural-arb settlement, insider feedback wiring, and fee-aware cross-arb gating.
- Spec: `docs/specs/2026-04-08-arb-depth-insider-risk-followups.md` for shared fee modeling, executable quote rechecks, insider-exit reviews, and risk-capped directional sizing.
- Spec: `docs/specs/2026-04-10-true-arb-and-kelly-hardening.md` for validated directional Kelly sizing, vetted true-arb gating, and paper resolution settlement fixes.

### Changed

- **Exit engine:** `multi_outcome_arb` no longer force-exits on a single losing leg hitting ~0; package-level `market_resolved` now waits for a visible winner or fully binary legs to avoid stale-mid settlement journaling.
- **Auto trader:** cross-platform arb re-quote gate now recomputes fee-adjusted net edge and rejects gross-positive / net-negative spreads after fresh quotes.
- **Position manager / trade journal:** closing Polymarket legs now feed resolved outcomes back into `InsiderTracker`, and journal rows preserve insider-backed sleeve flags for later audit slices.
- **Execution / arb screening:** fee-model math is now centralized in `src/execution/fee_model.py`, and cross-platform arb rechecks prefer executor-provided executable quotes over scalar midpoint-only prices.
- **Exit engine:** recent conviction-trader exits from `InsiderTracker` now emit `insider_exit` review triggers when they match the package bet direction.
- **Auto trader sizing:** directional Kelly sizing now uses a stressed-probability helper plus explicit bankroll-at-risk caps for speculative entries.
- **Auto trader / execution:** `pure_prediction` sizing now requires validated directional edge inputs instead of a favorite-bonus heuristic, and `cross_platform_arb` is now restricted to vetted executable-quote pairs with no midpoint fallback or dust legs.
- **Position manager / paper executor:** resolved binary paper legs now settle directly from snapped resolution payout values, so hold-to-resolution packages no longer depend on stale live quote mechanics during closeout.
- **Auto trader Kelly sizing:** `_kelly_size` now subtracts round-trip maker fees from edge BEFORE applying the Kelly fraction (Hausch & Ziemba 1985, *Management Science* 31(4):381–394). Default per-strategy edge fallback dropped from `0.02` to `0.0` — unknown strategies with no spread no longer assume a 2% edge that has no empirical basis. Returns 0 sizing when fees exceed edge. Arb/synthetic and weather call sites pass weighted `round_trip_fee_frac`.
- **Auto trader directional path:** `p_true` now adjusted downward by `fee_pp = fee_frac × side_price` before `_risk_capped_directional_fraction`, floored at `side_price` so fee drag never flips the edge sign.

### Fixed

- **Stop-loss / trailing-stop full suppression** (regression cleanup):
  - `src/positions/bracket_manager.py`: `place_brackets` no longer reads `stop_loss` rules or sets `stop_price`; `check_brackets` stop-monitoring path removed (this was the silent leak that re-introduced `auto:trailing_stop` and `ai_approved:stop_loss` entries in the journal); `adjust_stop` and `_compute_trail_price` are now no-ops.
  - `src/positions/news_scanner.py`: news packages no longer append `stop_loss` or `trailing_stop` exit rules — only `target_profit`.
  - `src/server.py`: legacy migration force-deactivates `stop_loss` / `trailing_stop` rules instead of adjusting their params.
  - `src/positions/ai_advisor.py`: `_KNOWN_TRIGGERS` no longer recognizes `stop_loss` or `trailing_stop`, so a hallucinated LLM verdict can never reach a downstream execution path.

## [2026-04-05]

### Added

- **Local LLM backup:** `src/positions/llm_ollama.py` — optional Ollama (OpenAI-compatible chat, e.g. Gemma 4) tried **after** Groq / Gemini / OpenRouter / Anthropic in `AIAdvisor` and `NewsAI` when `OLLAMA_API_KEY` is set. Documented in `src/.env.example`.
- **`tools/journal_kelly_bucket_analysis.py`** — paper journal summary by `side_price` bucket and before/after cutoff (Kelly regime analysis).

### Changed

- **Auto trader (pure prediction Kelly):** shrink assumed edge toward market price via `KELLY_EDGE_SHRINK` (default `0.35`, env override). Skip opens when Kelly fraction is zero after shrink (`kelly_no_edge_after_shrink`). Same shrink applied to `_kelly_size` when `implied_prob` is set.
- **Position manager:** when `_use_limit_orders` is true, entry uses **`buy_limit` only** at leg `entry_price` (0% maker path); no `buy_and_confirm` / `buy` fallback. Executors without `buy_limit` fail closed with a clear error. Documented in `src/.env.example` (`KELLY_EDGE_SHRINK`).

## [2026-04-03]

### Added

- Spec: `docs/specs/2026-04-03-paper-trading-optimization.md` for paper-trading optimization workstreams.
- `tools/audit_maker_fee_compliance.py` — flags journal closes with material `total_fees` under maker-only paper policy.
- `tools/metrics_snapshot.py` — portfolio/strategy fee–gross KPIs, `fee_anomaly_count`, milestone booleans, optional `--days` rolling window.
- `docs/pr-metrics-workflow.md` — how to snapshot and compare metrics for PRs.

### Changed

- **Auto trader:** News exposure reserve (mirror arb reserve); non-news directional sleeve; cross-platform pre-trade re-quote + staleness + cluster checks; Kelly cap for cross-arb until first journal win; higher minimum spread for scanner-only directional flow; cap on concurrent `pure_prediction`; optional synthetic experiment via `SYNTHETIC_DERIVATIVE_EXPERIMENT`; score weighting with net vs gross when both exist.
- **Paper executor:** With `use_limit_orders=True`, buy/sell fee rates forced to 0% (maker-only sim).
- **Exit engine:** `time_decay` and `negative_drift` are not sent to AI when open legs are flat vs entry (`FLAT_MOVE_THRESHOLD_AI_SOFT`).
- **AI advisor:** Prompts aligned with tightened heuristic gates for soft exits (news-aware approve vs reject).
- **News scanner:** `MAX_CONCURRENT` aligned with auto-trader hard cap; `DAILY_TRADE_CAP` aligned with `MAX_NEW_TRADES_PER_DAY` (12).
- **Position router:** Distinguish permanently banned triggers from soft AI triggers when approving alerts.

### Fixed

- **Tests:** `AutoTrader` unit tests use `_stub_paper_executor()` and default empty `pm.executors` so `_get_current_bankroll()` stays numeric; daily-cap assertions match current limits (12).
