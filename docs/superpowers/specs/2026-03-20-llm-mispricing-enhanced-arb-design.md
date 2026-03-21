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
probability_model.get_consensus() flags market with max_deviation > 10%
        ↓
llm_estimator.estimate(title, platform_prices, news_headlines)
        ↓ (Claude + Gemini queried in parallel)
        ↓
Consensus = mean of two model estimates
        ↓
Agreement check: |claude_prob - gemini_prob| < 0.15
        ↓
Edge check: |consensus_prob - best_market_price| > 0.05 (post-fees)
        ↓
If agree AND edge > 5%: score boost 2.0x (replaces existing 1.3x cross-platform boost)
If disagree (>15%): no boost, no penalty (skip estimation)
If one model fails: no boost, log only
        ↓
Log estimate to eval_logger for calibration
```

### Components

#### 1. LLM Estimator (`src/positions/llm_estimator.py`)

**Class: `LLMEstimator`**

Constructor takes API keys for Anthropic and Google (read from env vars `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`). Creates async HTTP clients for both.

**Method: `async estimate(title, platform_prices, news_headlines) -> EstimateResult | None`**

- Builds a prompt asking for a probability estimate as JSON: `{"probability": 0.62, "confidence": "high", "reasoning": "..."}`
- Queries Claude and Gemini in parallel via `asyncio.gather(return_exceptions=True)`
- Parses both responses, computes consensus (mean)
- Returns `EstimateResult` dataclass

The `title` serves as both the market description and resolution context. No separate `resolution_text` field — adapters don't provide it. If resolution criteria are added to `NormalizedEvent` in the future, the estimator can accept it as an optional parameter.

**Prompt template:**
```
You are a calibrated probability estimator for prediction markets.

Market: {title}

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
    edge_pct: float              # |consensus - best_market_price| as percentage
    confidence: str              # "high" if models agree within 10%, "medium" within 15%, "low" otherwise
    models: dict[str, float]     # {"claude": 0.62, "gemini": 0.58}
    should_boost: bool           # True if confidence != "low" AND edge > 5%
    reasoning: str               # combined reasoning from models
```

`best_market_price` is the price on the platform where the trade would execute — the cheapest YES or NO price from the opportunity dict (`buy_yes_price` or `buy_no_price`).

**Rate limiting:** Max 10 estimates per scan cycle. Counter resets each cycle.

**Error handling:** If one model fails, return result with single model + confidence="low" + should_boost=False. If both fail, return None.

#### 2. Auto-Trader Integration (`src/positions/auto_trader.py`)

**Constructor change:** Add `llm_estimator=None` parameter to `AutoTrader.__init__()`. Store as `self._llm_estimator`. Wire in `server.py` alongside the existing `probability_model` initialization.

**News scanner access:** Add `set_news_scanner(scanner)` method to `AutoTrader` (same pattern as existing `set_political_analyzer()` and `set_weather_scanner()`). This is distinct from the existing `add_news_opportunity()` queue — the queue feeds news-triggered opportunities, while the setter provides headline lookup for LLM context. Wire in `server.py` after news_scanner initialization. The news scanner's existing `get_recent_headlines(condition_id, hours=24)` method is called with the opportunity's condition_id.

In the scoring section (around line 420), after checking `probability_model`:

```python
# LLM mispricing detection: only when cross-platform price disagreement detected
consensus = self.probability_model.get_consensus(opp_title) if self.probability_model else None
prob_deviation = consensus.get("max_deviation", 0) if consensus else 0
if self._llm_estimator and prob_deviation > 0.10:
    condition_id = opp.get("buy_yes_market_id", "").split(":")[0]
    news = []
    if self._news_scanner and condition_id:
        try:
            news = self._news_scanner.get_recent_headlines(condition_id, hours=24)
        except Exception:
            pass
    estimate = await self._llm_estimator.estimate(
        title=opp_title,
        platform_prices={"yes": opp.get("buy_yes_price", 0), "no": opp.get("buy_no_price", 0)},
        news_headlines=news,
    )
    if estimate and estimate.should_boost:
        score *= 2.0  # replaces the existing 1.3x cross-platform boost, does not stack
        logger.info("LLM mispricing boost: %s (edge=%.1f%%, consensus=%.2f)",
                     opp_title, estimate.edge_pct, estimate.consensus_prob)
```

When the LLM 2.0x boost is applied, it replaces (does not stack with) the existing 1.3x cross-platform disagreement boost at line 423. The implementation should use an `elif` or flag to prevent double-boosting.

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

**Replace** the flat `_TAKER_FEES` dict (line 29) with a `compute_taker_fee(platform, price, category)` function. The dict stays for non-Polymarket platforms; only Polymarket switches to the dynamic formula.

**Polymarket formula** (from official docs):
```python
effective_rate = fee_rate * (price * (1 - price)) ** exponent
```

**Parameters by market category:**

| Category | fee_rate | exponent | Max effective rate (at p=0.50) |
|----------|----------|----------|-------------------------------|
| Crypto | 0.25 | 2 | 1.5625% |
| Sports | 0.0175 | 1 | 0.4375% |

**Category mapping for Polymarket markets:**
```python
_POLYMARKET_FEE_PARAMS = {
    "crypto": {"fee_rate": 0.25, "exponent": 2},
    # All non-crypto categories use the sports/political curve
    "politics": {"fee_rate": 0.0175, "exponent": 1},
    "sports": {"fee_rate": 0.0175, "exponent": 1},
    "economics": {"fee_rate": 0.0175, "exponent": 1},
    "weather": {"fee_rate": 0.0175, "exponent": 1},
    "culture": {"fee_rate": 0.0175, "exponent": 1},
}
# Default (unknown category): use crypto params (conservative — highest fees)
```

**Other platforms** keep flat rates from `_TAKER_FEES`:
- Kalshi: 1% taker
- PredictIt: 0% taker (but 10% profit tax + 5% withdrawal fee)
- Limitless: ~1% estimated
- All others: current values unchanged

**Maker fees:** 0% for Polymarket (already correct in `paper_executor.py`). The `MAKER_FEE_RATES` dict in `paper_executor.py` stays as-is.

**Integration:** `_compute_fee_adjusted_profit()` signature changes to add `category: str = ""`:

Current:
```python
def _compute_fee_adjusted_profit(yes_price, no_price, yes_platform, no_platform):
    fee_yes = _TAKER_FEES.get(yes_platform, 0.02)
```
New:
```python
def _compute_fee_adjusted_profit(yes_price, no_price, yes_platform, no_platform, category=""):
    fee_yes = compute_taker_fee(yes_platform, yes_price, category)
```

The `category` is threaded from `find_arbitrage()` which has access to `match.category` on each `MatchedEvent`.

**Behavioral note:** The dynamic model is LESS conservative than the current flat 2%. At p=0.50, Polymarket crypto is 1.56% (was 2%); politics/sports is 0.44% (was 2%). This means some previously-rejected thin-spread opportunities will now pass the profitability filter. The existing `MIN_SPREAD_PCT = 12.0%` in auto_trader provides a safety buffer and does not need adjustment.

**Also update** `TAKER_FEE_RATES` in `paper_executor.py`: for Polymarket, use the dynamic formula instead of the flat 0.02. Add a `_compute_polymarket_taker_fee(price, category)` helper that both files can import, or duplicate the small formula. Other platforms stay flat.

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

**Rate limiting for resolution LLM:** Max 5 LLM calls per scan cycle for resolution comparison. Most pairs hit the heuristic cache after first check. The LRU cache prevents re-checking the same pair.

**ResolutionMatch dataclass:**
```python
@dataclass
class ResolutionMatch:
    status: str          # "match", "divergent", "uncertain"
    confidence: float    # 0.0-1.0
    reasoning: str       # why
```

**Cache:** LRU cache keyed by `(title_a_normalized, title_b_normalized)`. TTL: 1 hour. Same pair doesn't need re-checking within a cycle.

**Integration:** Called in `find_arbitrage()` before computing spread. If `status == "divergent"`, the opportunity confidence is set to `"very_low"`. The existing logic in `find_arbitrage()` already drops `very_low` confidence opportunities (lines 840-842: `if confidence == "very_low": continue`), so divergent-resolution arbs are filtered at the engine level before reaching the auto-trader.

**LLM dependency:** The resolution comparison needs an Anthropic client. Pass it via a module-level `set_llm_client()` function or accept it as a parameter to `find_arbitrage()`. If `ANTHROPIC_API_KEY` is not set, skip Phase 2 — heuristic only. Conservative: uncertain pairs get reduced confidence but are not auto-skipped.

### Component 3: Volume-Weighted Filtering (`src/positions/auto_trader.py`)

**New constant:** `MIN_ARB_VOLUME = 50_000`

**Integration point:** In the arb opportunity processing section (around line 313-327), before creating the package:

```python
# Volume filter for arb opportunities (78% execution failure below $50K daily volume)
arb_volume = opp.get("volume", opp.get("combined_volume", 0))
if arb_volume < MIN_ARB_VOLUME:
    logger.debug("Auto trader: skipping low-volume arb: %s (vol=%d)", opp_title, arb_volume)
    self._trades_skipped += 1
    continue
```

Uses `opp.get("volume")` which maps to `combined_volume` from `_arb_to_opportunity()`. This is the sum of volumes across both platforms — a reasonable proxy since both legs need liquidity.

For political synthetic and crypto synthetic opportunities, the volume filter does NOT apply (those are directional strategies, not arbs requiring simultaneous execution). The filter is placed inside the arb-specific processing block, before the synthetic handlers.

---

## Interaction Between Strategy 2 and Strategy 3

When both strategies apply to the same opportunity (e.g., a cross-platform arb with >10% price deviation):

1. **Strategy 3 runs first** (at the engine level): Dynamic fees compute the true spread, resolution comparison filters bad matches, volume filter removes illiquid markets.
2. **Strategy 2 runs second** (at the auto-trader level): If the opportunity survives Strategy 3's filters AND probability_model flags deviation, the LLM estimator runs.
3. **Score boost is exclusive:** The LLM 2.0x boost replaces (does not stack with) the existing 1.3x cross-platform disagreement boost. Use a flag: if LLM boost applied, skip the 1.3x boost.

---

## Error Handling

- **LLM estimator fails:** No boost applied. Conservative default. Warning logged.
- **Single model fails in estimator:** Return low-confidence result. No boost. Log.
- **Resolution comparison LLM fails:** Heuristic result stands. Uncertain pairs get reduced confidence.
- **Dynamic fee compute edge case (price=0 or price=1):** Return 0 fee (formula naturally produces 0 at extremes).
- **Missing API keys:** Estimator and resolution LLM phases gracefully disabled. System works without them, just without those enhancements.

## Testing

**Strategy 2 tests:**
- `test_llm_estimator.py`: Mock LLM responses, verify consensus calculation, agreement/disagreement handling, single-model fallback, rate limiting, prompt construction
- Auto-trader integration: verify score boost applied when estimator returns should_boost=True, not applied otherwise, verify LLM boost replaces (not stacks with) 1.3x boost

**Strategy 3 tests:**
- `test_dynamic_fees.py`: Verify Polymarket fee curve at key price points (0.50, 0.10, 0.90, 0.01, 0.99), verify category mapping, verify other platforms unchanged, verify fee is lower than old flat 2%
- `test_resolution_comparison.py`: Identical titles → match, clearly different → divergent, subtle difference → uncertain → LLM phase, cache hit test, rate limiting
- Auto-trader: verify volume filter skips low-volume arbs, does not affect synthetics

## What This Does NOT Include

- No live/real-money execution (paper only)
- No new API endpoints or UI changes
- No historical LLM backfill or Brier score reports
- No order book analysis or market making
- No HFT or sub-second latency optimization
- No changes to exit engine or position manager
- No new adapters or data sources
- No `resolution_text` field on events (uses title only — known limitation)
