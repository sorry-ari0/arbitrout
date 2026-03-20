# Exit Optimization: Limit Orders, News-Validated Exits, Calibration Loop

**Date:** 2026-03-19
**Status:** Draft
**Scope:** exit_engine.py, position_manager.py, polymarket_executor.py, ai_advisor.py, news_scanner.py, news_ai.py, eval_logger.py, trade_journal.py, server.py

## Problem

Three systemic issues are destroying trading performance:

1. **2% taker fee on every exit.** Entries use maker limit orders (0% fee) but exits use FOK market orders (2% taker). On $5.7K deployed, that's ~$114 in avoidable exit fees. The executor already has `sell_limit()`, `check_order_status()`, and `cancel_order()` — they're just not wired into the exit path.

2. **Exits based on price action, not fundamentals.** The AI advisor reviews triggers like trailing_stop and negative_drift by looking at P&L numbers. It has zero awareness of whether a price drop is caused by real news (injury, regulation, delay) or just a liquidity gap / single large market order. The news scanner already monitors 12 RSS feeds every 150s — but the exit engine never sees this data.

3. **No feedback loop.** The eval_logger has `get_calibration()` which tracks whether each skip reason and exit trigger is performing well or poorly. Nothing reads this data. Thresholds are manually tuned from 31 trades — they should adapt as the dataset grows.

---

## Change 1: Limit Order Exits

### Current Flow
```
ExitEngine._tick() → trigger fires → AI approves →
  PositionManager.exit_leg(pkg_id, leg_id, trigger) →
    executor.sell(asset_id, quantity)  ← FOK market order, 2% taker fee
```

### New Flow (Async Pending Order Pattern)
```
ExitEngine._tick() → trigger fires → AI approves →
  PositionManager.exit_leg(pkg_id, leg_id, trigger, use_limit=True) →
    IF safety override OR stop_loss OR use_limit=False:
      executor.sell(asset_id, quantity)  ← FOK, immediate fill
    ELSE:
      executor.sell_limit(asset_id, quantity, price)  ← GTC, 0% maker fee
      Record order_id in pkg["_pending_limit_orders"][leg_id]
      Release lock, return "pending"

ExitEngine._tick() (next cycle, 60s later) →
  _resolve_pending_limit_orders() →
    FOR each pending order:
      status = check_order_status(order_id)
      IF "filled" → finalize exit (sell_fees=0, exit_order_type="limit_filled")
      IF "partially_filled" → cancel remainder, FOK the rest
      IF "open" AND elapsed > 60s → cancel, FOK full quantity (exit_order_type="fok_fallback")
      IF "open" AND elapsed <= 60s → keep waiting (check next tick)
```

### Design Decisions

**Why async pending orders instead of synchronous polling:**
The position_manager's `_lock` serializes all exits. If a limit order polls inside the lock for 60 seconds, a safety override (#7 spread_inversion) arriving during that window would be blocked — potentially losing money while waiting. By releasing the lock immediately after placing the limit order and resolving it on the next tick, safety overrides can always execute without delay.

**Which exits get limit orders:**
- Safety overrides (#7 spread_inversion, #21 political_event_resolved): **Always FOK.** These are "get out now" — saving 2% doesn't matter if the position is guaranteed to lose.
- stop_loss (#4): **Always FOK.** Loss is accelerating — immediate fill more important than fee savings. Note: stop_loss is NOT a safety override in the current code (it goes through AI review), but its exit path should always be FOK regardless of AI routing.
- target_hit (#1): **Limit order.** We're in profit, no rush.
- trailing_stop (#2): **Limit order.** Drawdown from peak, but not a crisis.
- All other AI-approved soft triggers: **Limit order.**

**Limit price calculation:**
- `price = current_midpoint - max(0.01, current_price * 0.01)` — at least 1 cent below midpoint, or 1% of current price for low-priced contracts. This prevents a 20% discount on a $0.05 contract while remaining aggressive enough to fill as a maker order on liquid markets.

**Pending order tracking:**
- Each package gains a `_pending_limit_orders` dict: `{leg_id: {"order_id": str, "placed_at": float, "quantity": float, "asset_id": str}}`.
- At the start of each `_tick()`, before evaluating triggers, call `_resolve_pending_limit_orders()` to check and finalize any pending orders.
- Max wait: 60 seconds (1 tick cycle). If not filled after one full tick cycle, cancel and FOK.
- Pending legs are treated as "exiting" — the exit engine skips trigger evaluation for packages with pending orders.

**Concurrency safety:**
- `_resolve_pending_limit_orders()` acquires the `_lock` only for the finalization step (writing exit data), not during the status check. This keeps the lock window small.
- Safety overrides that fire while a limit order is pending: the safety override takes priority. Cancel the pending limit order, then FOK the full quantity.

### Files Changed

| File | Change |
|------|--------|
| `position_manager.py` | `exit_leg()` gains `use_limit: bool = False` param. New `_place_limit_sell()` method places GTC and records pending order. New `_resolve_pending_order()` method checks status and finalizes or falls back. |
| `exit_engine.py` | Add `_resolve_pending_limit_orders()` at start of `_tick()`. Safety overrides pass `use_limit=False`. AI-approved exits pass `use_limit=True` (except stop_loss → `use_limit=False`). Skip trigger evaluation for packages with pending orders. Cancel pending orders before executing safety overrides on the same package. |
| `base_executor.py` | Add abstract `sell_limit()`, `check_order_status()`, `cancel_order()` to ABC with default `NotImplementedError` raises — platforms that don't support limit orders fall back to FOK via the position_manager logic. |
| `paper_executor.py` | **Rewrite** existing `sell_limit()` (currently delegates to `sell()` with taker fees). New implementation: simulate fill at the limit price with 0% maker fee. `check_order_status()` returns "filled" immediately (paper mode). |
| `trade_journal.py` | Record `exit_order_type` field ("limit_filled", "limit_partial_fok", "fok_fallback", "fok_direct") in journal entries. |

### Expected Impact
- **Fee savings:** ~$114 per $5.7K deployed (2% of exit value on ~70% of exits).
- **Fill rate estimate:** 70-80% of limit exits should fill within 60s on liquid Polymarket markets. The remaining 20-30% fall back to FOK with no worse outcome than today.
- **Zero risk increase:** Every limit order path has a guaranteed FOK fallback. Safety overrides are never blocked.

---

## Change 2: News-Validated Exit Decisions

### Current Flow
```
ExitEngine._tick() → soft trigger fires →
  AIAdvisor.review_proposals(triggers, package_context) →
    AI sees: P&L, prices, rules, performance history
    AI does NOT see: any news about the market
    AI decides: APPROVE/MODIFY/REJECT based purely on price action
```

### New Flow
```
ExitEngine._tick() → soft trigger fires →
  news_context = news_scanner.get_recent_headlines(condition_id, hours=24) →
  AIAdvisor.review_proposals(triggers, package_context, news_context) →
    AI sees: P&L, prices, rules, performance history, AND recent headlines
    AI rule: if no negative news found → strong bias toward REJECT
    AI rule: if negative news found → weigh fundamental shift vs noise
```

### Design Decisions

**New headline cache in NewsScanner:**
The existing dedup caches (`_seen_hashes`, `_recent_headlines`) do NOT store headline text, market matches, confidence, or sentiment — they only store hashes and word sets. A new cache structure is required.

During `_scan_cycle()`, after Pass 1 AI classification matches a headline to a market, store the match in a new `_matched_headlines` dict:

```python
# Keyed by condition_id for fast lookup by exit engine
_matched_headlines: dict[str, list[dict]] = {}

# Each entry:
{
    "headline": str,           # Full headline text
    "source": str,             # RSS feed name
    "timestamp": str,          # ISO UTC
    "confidence": int,         # 1-10 from Pass 1
    "sentiment": str,          # "positive"|"negative"|"neutral"
    "condition_id": str,       # Matched market
    "market_title": str        # Matched market title
}
```

Entries are pruned on each scan cycle: remove anything older than 48 hours. Cap at 500 entries total to bound memory.

**New method on NewsScanner:**
```python
def get_recent_headlines(self, condition_id: str, hours: int = 24) -> list[dict]:
    """Return cached headlines matching this market from the last N hours.
    Returns empty list if no matches or scanner not running.
    Thread-safe: reads from dict (GIL-protected for CPython).
    """
```

**Sentiment derivation (no new LLM call):**
The Pass 1 AI scan returns a `side` field (YES/NO) indicating which direction the news favors. Derive sentiment from this:
- `side == "NO"` → `sentiment = "negative"` (news is bad for the market's YES outcome)
- `side == "YES"` → `sentiment = "positive"`
- `side` missing or unclear → `sentiment = "neutral"`

This avoids an additional LLM call. It's a coarse mapping but sufficient for the exit engine's needs — the AI advisor gets the full headline text and can make its own nuanced judgment.

**Wiring the exit engine to the news scanner:**
The exit engine currently takes `ExitEngine(pm, ai_advisor, decision_logger)` — it has no reference to the news scanner. Add `news_scanner` as an optional constructor parameter:

```python
class ExitEngine:
    def __init__(self, pm, ai_advisor=None, decision_logger=None, news_scanner=None):
        self._news_scanner = news_scanner
```

In `server.py`, pass the news_scanner instance when constructing the exit engine.

**How news context enters the AI prompt:**
Integrate into the existing AI advisor guidelines rather than appending a separate section. Add to the per-package context in `_build_batched_prompt()`:

```
RECENT NEWS (last 24 hours):
- [2026-03-19 14:30] CoinDesk: "SEC delays BTC ETF decision to Q3" (confidence: 8/10, negative)
- (none found)

If NO negative news exists for this market, default to REJECT for trailing_stop,
negative_drift, and time_decay. Price drops without news are liquidity noise.
If negative news exists, assess whether it materially shifts the probability.
```

This is added inline with the existing package context, not as a separate prompt section, to avoid conflicting with the existing strong-REJECT defaults.

**Edge cases:**
- Market not in news scanner's top 200 cache: return empty list. AI sees "none found" → defaults to REJECT. This is the safe default.
- News scanner not running / `_news_scanner is None`: return empty, AI defaults to REJECT.
- Multiple conflicting headlines: include all. Let the AI weigh them.
- Concurrency: `_matched_headlines` is a dict read by the exit engine and written by the news scanner. Both are async coroutines in the same event loop — safe under CPython GIL. If the system moves to multiprocessing in the future, this will need a lock.

### Files Changed

| File | Change |
|------|--------|
| `news_scanner.py` | Add `_matched_headlines` dict. Populate during `_scan_cycle()` after Pass 1 matches. Add `get_recent_headlines(condition_id, hours)` method. Prune entries >48h on each cycle. |
| `news_ai.py` | Ensure Pass 1 `_parse_scan_response()` returns `side` field consistently (verify — it should already be there). |
| `exit_engine.py` | Constructor gains `news_scanner=None` param. In `_tick()`, before `_batched_ai_review()`, collect news context per package by iterating open legs' condition_ids. Pass news_context dict to AI advisor. |
| `ai_advisor.py` | `_build_batched_prompt()` accepts optional `news_context: dict[str, list]` (keyed by pkg_id). Appends news + fundamental exit guidance inline with existing package context. |
| `server.py` | Pass `news_scanner` to `ExitEngine()` constructor during lifespan init. |

### Expected Impact
- **Fewer premature exits on liquidity noise.** The 31-trade data showed all automated exits lost money. Most were triggered by temporary price dips with no fundamental cause.
- **Better exits when real news hits.** When a genuine probability shift occurs (injury, regulation, etc.), the AI has context to approve the exit.
- **No additional API costs.** Reuses existing news scanner pipeline — zero new LLM calls for the news lookup.

---

## Change 3: Calibration Feedback Loop

### Current State
The `eval_logger.py` has three analysis methods that nothing reads:
- `get_calibration()` — per-skip-reason correct_skip_rate (did we correctly avoid losers?)
- `get_missed_opportunities()` — skipped trades that would have been profitable
- `get_performance()` (trade_journal.py) — per-trigger win rate and P&L

Thresholds are hardcoded from manual analysis of 31 trades. As the dataset grows, these should adapt.

### New: Daily Calibration Report + Threshold Suggestions

**Approach:** A periodic calibration task (runs every 24 hours) that:
1. Reads eval_logger calibration data
2. Reads trade journal performance by trigger and hold duration
3. Identifies systematic biases
4. Writes a calibration report to `src/data/calibration/`
5. Suggests threshold adjustments (human-approved, not auto-applied)

**Why not auto-adjust?**
With <100 trades, statistical significance is low. Auto-adjusting thresholds on small samples causes overfitting. The system should surface recommendations that a human reviews. Once the dataset reaches 200+ trades, auto-adjustment becomes viable.

### Calibration Metrics

**Entry calibration (from eval_logger):**

| Metric | Source | What It Tells You |
|--------|--------|-------------------|
| `correct_skip_rate` per reason | `get_calibration()` | If `low_score` has 50% correct_skip_rate, the score threshold is too aggressive — half the skipped trades would have been profitable. |
| `missed_opportunity_count` | `get_missed_opportunities()` | Total profitable trades we passed on. |
| `missed_pnl` | Backfilled entries | Dollar value of missed profits — prioritize the most expensive misses. |

**Exit calibration (from trade_journal):**

| Metric | Source | What It Tells You |
|--------|--------|-------------------|
| `win_rate` per trigger | `get_performance().by_trigger` | If trailing_stop has 0% win rate across 10+ trades, it should never fire (or threshold needs widening). |
| `avg_pnl` per trigger | Same | Which triggers make money vs. destroy it. |
| `avg_hold_hours` by outcome | `get_performance_by_hold_duration()` (new) | If winners are held 48h+ and losers are exited at 4h, the hold period should increase. |
| `fee_drag_pct` | Aggregate | If fee drag is still >1.5%, limit order adoption isn't high enough. |
| `limit_fill_rate` | Journal entries with `exit_order_type` | Percentage of limit order exits that filled without FOK fallback. |

**Calibration report format:**
```json
{
  "generated_at": "2026-03-19T17:00:00Z",
  "trade_count": 45,
  "entry_calibration": {
    "low_score": {
      "correct_skip_rate": 0.75,
      "missed_count": 3,
      "missed_pnl": 45.20,
      "suggestion": "KEEP — 75% correct skip rate is healthy"
    },
    "max_concurrent": {
      "correct_skip_rate": 0.40,
      "missed_count": 8,
      "missed_pnl": 120.50,
      "suggestion": "REVIEW — 40% correct rate, missing $120. Consider raising MAX_CONCURRENT from 7 to 9"
    }
  },
  "exit_calibration": {
    "trailing_stop": {
      "trades": 8,
      "win_rate": 0.0,
      "total_pnl": -72.0,
      "avg_hold_hours": 3.2,
      "suggestion": "WIDEN — 0% win rate. Current 35% trail is too tight. Suggest 45%."
    },
    "target_hit": {
      "trades": 5,
      "win_rate": 0.80,
      "total_pnl": 65.0,
      "avg_hold_hours": 72.1,
      "suggestion": "KEEP — performing well at 50% target"
    }
  },
  "hold_duration_analysis": {
    "0-6h": {"trades": 8, "win_rate": 0.0, "avg_pnl": -12.3},
    "6-24h": {"trades": 5, "win_rate": 0.20, "avg_pnl": -4.1},
    "24h-3d": {"trades": 10, "win_rate": 0.40, "avg_pnl": 2.5},
    "3d-7d": {"trades": 6, "win_rate": 0.50, "avg_pnl": 8.2},
    "7d+": {"trades": 2, "win_rate": 1.0, "avg_pnl": 22.0}
  },
  "fee_analysis": {
    "total_fees": 89.20,
    "fee_drag_pct": 1.65,
    "limit_fill_rate": 0.73,
    "limit_saves": 42.50,
    "suggestion": "GOOD — 73% limit fill rate saving $42.50 in fees"
  }
}
```

### Implementation

**New file: `src/positions/calibration.py` (~150 lines)**

```python
class CalibrationEngine:
    def __init__(self, eval_logger, trade_journal):
        self.eval_logger = eval_logger
        self.journal = trade_journal
        self.data_dir = Path(__file__).parent.parent / "data" / "calibration"

    def generate_report(self) -> dict:
        """Generate calibration report from all available data."""
        # 1. Entry calibration from eval_logger.get_calibration()
        # 2. Exit calibration from trade_journal.get_performance()
        # 3. Hold duration analysis from trade_journal.get_performance_by_hold_duration()
        # 4. Fee analysis including limit fill rate from journal entries
        # 5. Generate suggestions based on thresholds

    def save_report(self):
        """Write report to src/data/calibration/YYYY-MM-DD.json"""
```

**Suggestion logic (conservative):**
- Skip reason with <60% correct_skip_rate AND >5 resolved trades → "REVIEW: threshold may be too aggressive"
- Exit trigger with 0% win rate AND >5 trades → "WIDEN: threshold too tight, suggest N%"
- Exit trigger with >70% win rate → "KEEP: performing well"
- Fee drag >2% → "HIGH: investigate execution quality"
- Limit fill rate <50% → "LOW: limit price may be too aggressive, consider widening offset"
- Insufficient data (<5 trades for a metric) → "INSUFFICIENT DATA: need N more trades"

**Integration:**
- Runs as a background task every 24 hours (alongside existing eval backfill)
- Reports saved to `src/data/calibration/` as dated JSON files
- New API endpoint: `GET /api/derivatives/calibration` — returns latest report
- No auto-adjustment. Report is informational. Human reads it, decides whether to change thresholds.

### Files Changed

| File | Change |
|------|--------|
| `positions/calibration.py` | New file. CalibrationEngine class with `generate_report()` and `save_report()`. |
| `server.py` | Add calibration engine to lifespan init, wire eval_logger + trade_journal. Add 24h background task. |
| `positions/position_router.py` | Add `GET /api/derivatives/calibration` endpoint returning latest report. |
| `trade_journal.py` | Add `get_performance_by_hold_duration()` method — buckets trades by hold time (0-6h, 6-24h, 24h-3d, 3d-7d, 7d+) with per-bucket win_rate, avg_pnl, trade_count. Also extend existing per-trigger breakdown to include `avg_hold_hours`. |
| `eval_logger.py` | No changes — existing `get_calibration()` and `get_missed_opportunities()` are sufficient. |

### Expected Impact
- **Data-driven threshold tuning** instead of manual guesswork from 31 trades.
- **Early warning** when a filter is too aggressive (missing profitable trades) or too loose (approving losing exits).
- **Fee tracking** for the new limit order exits — confirms whether the change is working.
- **Foundation for auto-adjustment** once the dataset reaches 200+ trades.

---

## Implementation Order

1. **Limit order exits** — Highest impact (direct fee savings), smallest risk (FOK fallback), existing code to wire up. 5 files changed.
2. **News-validated exits** — High impact (prevents premature exits on noise), moderate complexity (new cache + prompt changes). 5 files changed.
3. **Calibration loop** — Foundational for long-term optimization, lowest urgency (value grows with trade count). 1 new file + 4 files changed.

## Testing Strategy

- **Limit orders:** Paper mode `sell_limit()` must be rewritten to simulate 0% maker fee (currently delegates to `sell()` which charges taker fees). Verify: open a position, trigger target_hit, confirm limit order path taken and `sell_fees = 0` in journal. Verify: trigger spread_inversion while a limit order is pending → safety override executes immediately, pending order cancelled.
- **News validation:** Unit test `get_recent_headlines()` with mock `_matched_headlines` data. Integration test: inject a fake negative headline for a market, trigger negative_drift, verify headline appears in AI prompt context.
- **Calibration:** Unit test report generation with known trade data. Verify suggestions match expected output for edge cases (0% win rate, insufficient data, high fee drag, etc.).

All changes are backward-compatible. No schema migrations. No new dependencies.
