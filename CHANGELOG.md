# Changelog

Notable changes to **arbitrout** are listed here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **NASA EONET** (`src/positions/eonet_client.py`): fetch open natural events from [EONET API v3](https://eonet.gsfc.nasa.gov/) and blend conservative precipitation adjustments into NWS forecasts in `WeatherScanner` when hazards are near the city and forecast date. Toggle with `EONET_WEATHER_ENABLED`. Unit tests in `tests/test_eonet_client.py`.

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
