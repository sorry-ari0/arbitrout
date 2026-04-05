# Changelog

Notable changes to **arbitrout** are listed here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [2026-04-05]

### Added

- **Local LLM backup:** `src/positions/llm_ollama.py` — optional Ollama (OpenAI-compatible chat, e.g. Gemma 4) tried **after** Groq / Gemini / OpenRouter / Anthropic in `AIAdvisor` and `NewsAI` when `OLLAMA_API_KEY` is set. Documented in `src/.env.example`.

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
