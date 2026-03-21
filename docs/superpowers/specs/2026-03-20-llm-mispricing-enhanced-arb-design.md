# LLM Mispricing Detection & Enhanced Cross-Platform Arb — Design Spec

## Goal

Add two independent trading improvements: (1) a 2-model LLM consensus estimator that detects mispriced markets when cross-platform price disagreement is flagged, and (2) enhanced cross-platform arbitrage with dynamic Polymarket fees, resolution criteria comparison, and volume filtering. All execution remains paper-only.

## Strategy 2: LLM Mispricing Detection

### Architecture

New module `src/positions/llm_estimator.py` — a stateless probability estimator. Does not trade. Returns a structured estimate that the auto-trader uses for score boosting.

### Data Flow

```
Auto-trader scan cycle (every 5 min)
        ↓
probability_model flags market with >10% cross-platform deviation
        ↓
llm_estimator.estimate(title, resolution_text, platform_prices, news_context)
        ↓ (Claude + Gemini queried in parallel)
        ↓
Consensus = mean of two model estimates
        ↓
Agreement check: |claude_prob - gemini_prob| < 0.15
        ↓
Edge check: |consensus_prob - market_price| > 0.05 (post-fees)
        ↓
If agree AND edge > 5%: score boost 2.0x
If disagree (>15%): no boost, no penalty (skip estimation)
If one model fails: no boost, log only
        ↓
Log estimate to eval_logger for calibration
```

### Components

#### 1. LLM Estimator (`src/positions/llm_estimator.py`)

**Class: `LLMEstimator`**

Constructor takes API keys for Anthropic and Google (read from env vars `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`). Creates async HTTP clients for both.

**Method: `async estimate(title, resolution_text, platform_prices, news_headlines) -> EstimateResult`**

- Builds a prompt asking for a probability estimate as JSON: `{"probability": 0.62, "confidence": "high", "reasoning": "..."}`
- Queries Claude and Gemini in parallel via `asyncio.gather()`
- Parses both responses, computes consensus (mean)
- Returns `EstimateResult` dataclass

**Prompt template:**
```
You are a calibrated probability estimator for prediction markets.

Market: {title}
Resolution criteria: {resolution_text}

Current prices across platforms:
{platform_prices_formatted}

Recent news (if any):
{news_headlines_formatted}

Estimate the true probability this market resolves YES.
Respond with JSON only: {"probability": 0.XX, "confidence": "high|medium|low", "reasoning": "one sentence"}
```

**EstimateResult dataclass:**
```python
@dataclass
class EstimateResult:
    consensus_prob: float        # mean of model estimates
    edge_pct: float              # |consensus - market_price| as percentage
    confidence: str              # "high" if models agree within 10%, "medium" within 15%, "low" otherwise
    models: dict[str, float]     # {"claude": 0.62, "gemini": 0.58}
    should_boost: bool           # True if confidence != "low" AND edge > 5%
    reasoning: str               # combined reasoning from models
```

**Rate limiting:** Max 10 estimates per scan cycle. Counter resets each cycle.

**Error handling:** If one model fails, return result with single model + confidence="low" + should_boost=False. If both fail, return None.

#### 2. Auto-Trader Integration (`src/positions/auto_trader.py`)

In the scoring section (around line 420), after checking `probability_model`:

```python
if self._llm_estimator and prob_deviation > 0.10:
    estimate = await self._llm_estimator.estimate(
        title=opp_title,
        resolution_text=opp.get("resolution_text", opp_title),
        platform_prices={...},
        news_headlines=self._news_scanner.get_recent_headlines(opp_title) if self._news_scanner else [],
    )
    if estimate and estimate.should_boost:
        score *= 2.0
        logger.info("LLM mispricing boost: %s (edge=%.1f%%, consensus=%.2f)",
                     opp_title, estimate.edge_pct, estimate.consensus_prob)
```

The estimator is optional — if `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` is missing, it's not initialized and all markets proceed without LLM estimation.

#### 3. Calibration Logging

Each estimate logged to eval_logger:
```python
{
    "type": "llm_estimate",
    "market_id": event_id,
    "title": title,
    "claude_prob": 0.62,
    "gemini_prob": 0.58,
    "consensus_prob": 0.60,
    "market_price": 0.50,
    "edge_pct": 10.0,
    "should_boost": True,
    "timestamp": "2026-03-20T...",
}
```

No calibration reports or Brier score computation in this iteration — just log the data for future analysis.

---

## Strategy 3: Enhanced Cross-Platform Arbitrage

### Architecture

Three extensions to existing modules. No new files except tests.

### Component 1: Dynamic Fee Model (`src/arbitrage_engine.py`)

**Replace** the flat `PLATFORM_FEES` dict with a `compute_taker_fee(platform, price, category)` function.

**Polymarket formula** (from official docs):
```python
effective_rate = fee_rate * (price * (1 - price)) ** exponent
```

**Parameters by market category:**

| Category | fee_rate | exponent | Max effective rate (at p=0.50) |
|----------|----------|----------|-------------------------------|
| Crypto | 0.25 | 2 | 1.5625% |
| Sports/Political | 0.0175 | 1 | 0.4375% |

**Other platforms** keep flat rates:
- Kalshi: 1% taker
- PredictIt: 0% taker (but 10% profit tax + 5% withdrawal fee)
- Limitless: ~1% estimated
- All others: current values unchanged

**Maker fees:** 0% for Polymarket (already correct in `paper_executor.py`). The `MAKER_FEE_RATES` dict in `paper_executor.py` stays as-is.

**Integration:** `_compute_fee_adjusted_profit()` currently does:
```python
fee_yes = PLATFORM_FEES.get(yes_platform, 0.02)
```
Replace with:
```python
fee_yes = compute_taker_fee(yes_platform, yes_price, category)
```

The `category` comes from the `NormalizedEvent.category` field already available on matched events.

**Also update** `TAKER_FEE_RATES` in `paper_executor.py` to use `compute_taker_fee()` for Polymarket instead of the flat 0.02. Other platforms stay flat.

### Component 2: Resolution Criteria Comparison (`src/arbitrage_engine.py`)

**New function: `_compare_resolution(title_a, title_b, platform_a, platform_b) -> ResolutionMatch`**

Two-phase approach:

**Phase 1 — Heuristic:**
- Normalize both titles (lowercase, strip punctuation, remove platform-specific prefixes)
- Compute token overlap ratio (Jaccard similarity on word tokens)
- Extract key terms: dates (regex), dollar amounts, named entities, source authorities ("SEC", "Congress", etc.)
- Decision:
  - Similarity > 90% AND no key term conflicts → `"match"` (confidence high)
  - Similarity < 50% OR key term conflict detected → `"divergent"` (confidence high)
  - Otherwise → `"uncertain"`

**Phase 2 — LLM (only when heuristic returns "uncertain"):**
- Query Claude (single model, not consensus — this is a binary classification, not estimation):
  ```
  Do these two prediction markets resolve the same way?
  Market A ({platform_a}): {title_a}
  Market B ({platform_b}): {title_b}
  Respond JSON: {"same_resolution": true/false, "reasoning": "one sentence"}
  ```
- If Claude says `same_resolution: false` → downgrade to `"divergent"`
- If Claude call fails → treat as `"uncertain"`, reduce confidence

**ResolutionMatch dataclass:**
```python
@dataclass
class ResolutionMatch:
    status: str          # "match", "divergent", "uncertain"
    confidence: float    # 0.0-1.0
    reasoning: str       # why
```

**Cache:** LRU cache keyed by `(title_a_normalized, title_b_normalized)`. TTL: 1 hour. Same pair doesn't need re-checking within a cycle.

**Integration:** Called in `find_arbitrage()` before computing spread. If `status == "divergent"`, set opportunity confidence to `"very_low"` (auto-trader skips very_low).

**LLM dependency:** The resolution comparison needs an Anthropic client. If `ANTHROPIC_API_KEY` is not set, skip Phase 2 — heuristic only. Conservative: uncertain pairs get reduced confidence but are not auto-skipped.

### Component 3: Volume-Weighted Filtering (`src/positions/auto_trader.py`)

**New constant:** `MIN_ARB_VOLUME = 50_000`

**Integration point:** In the arb opportunity processing section (around line 313-327), before creating the package:

```python
# Volume filter for arb opportunities
yes_volume = opp.get("yes_volume", opp.get("combined_volume", 0))
no_volume = opp.get("no_volume", opp.get("combined_volume", 0))
if min(yes_volume, no_volume) < MIN_ARB_VOLUME:
    logger.debug("Auto trader: skipping low-volume arb: %s (vol=%d)", opp_title, min(yes_volume, no_volume))
    self._trades_skipped += 1
    continue
```

For political synthetic and crypto synthetic opportunities, the volume filter does NOT apply (those are directional strategies, not arbs requiring simultaneous execution).

---

## Error Handling

- **LLM estimator fails:** No boost applied. Conservative default. Warning logged.
- **Single model fails in estimator:** Return low-confidence result. No boost. Log.
- **Resolution LLM fails:** Heuristic result stands. Uncertain pairs get reduced confidence.
- **Dynamic fee compute edge case (price=0 or price=1):** Return 0 fee (formula naturally produces 0 at extremes).
- **Missing API keys:** Estimator and resolution LLM phases gracefully disabled. System works without them, just without those enhancements.

## Testing

**Strategy 2 tests:**
- `test_llm_estimator.py`: Mock LLM responses, verify consensus calculation, agreement/disagreement handling, single-model fallback, rate limiting, prompt construction
- Auto-trader integration: verify score boost applied when estimator returns should_boost=True, not applied otherwise

**Strategy 3 tests:**
- `test_dynamic_fees.py`: Verify Polymarket fee curve at key price points (0.50, 0.10, 0.90, 0.01, 0.99), verify other platforms unchanged, verify category-based parameters
- `test_resolution_comparison.py`: Identical titles → match, clearly different → divergent, subtle difference → uncertain → LLM phase, cache hit test
- Auto-trader: verify volume filter skips low-volume arbs, does not affect synthetics

## What This Does NOT Include

- No live/real-money execution (paper only)
- No new API endpoints or UI changes
- No historical LLM backfill or Brier score reports
- No order book analysis or market making
- No HFT or sub-second latency optimization
- No changes to exit engine or position manager
- No new adapters or data sources
