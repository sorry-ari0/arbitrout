# True Edge Overhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform Arbitrout from a system carried by lucky Solana trades (+$410) into one with systematic, repeatable edge by fixing broken feedback loops, removing proven losers, and unblocking high-alpha strategies.

**Architecture:** Eight changes across the auto trader, exit engine, eval system, and insider tracker. Ordered by expected impact: fix broken infrastructure first (eval backfill), remove bleeding strategies second (commodities block, regime skip), unlock blocked alpha third (arb budget reservation), then validate assumptions (insider refresh, Kyle A/B, favorite audit, hold-to-resolution default). **Note:** Tasks 2, 3, 4, 7, 8 all modify `auto_trader.py` — implement sequentially to avoid merge conflicts. Task 3 (regime skip) interacts with Task 4 (arb budget): arb strategies should bypass regime penalty since they're guaranteed-profit, not speculative.

**Tech Stack:** Python 3.11, FastAPI, asyncio, httpx, Polymarket Gamma/Data APIs, pytest

**Evidence Base:** 61 positions analyzed (59 closed, 2 open), +$250 net P&L, 17% win rate. 4 Solana trades = +$410, everything else = -$161. Decision log: 5,762 entries showing 30-43% spread arbs blocked by exposure limits.

---

## File Map

| File | Changes | Tasks |
|------|---------|-------|
| `src/server.py` | Wire backfill loop to actually resolve markets via API | 1 |
| `src/eval_logger.py` | Add `resolve_via_api()` method for Polymarket resolution lookup | 1 |
| `src/positions/auto_trader.py` | Block commodities, skip on bad regime, reserve arb budget, hold-to-resolution default | 2, 3, 4, 8 |
| `src/positions/insider_tracker.py` | Auto-refresh watchlist from live leaderboard | 5 |
| `src/positions/kyle_lambda.py` | Add A/B test toggle + logging | 6 |
| `src/positions/exit_engine.py` | Log favorite multiplier contribution for audit | 7 |
| `tests/test_eval_backfill.py` | Tests for backfill resolution | 1 |
| `tests/test_commodities_block.py` | Tests for hard commodity skip | 2 |
| `tests/test_regime_skip.py` | Tests for regime skip vs min-size | 3 |
| `tests/test_arb_budget.py` | Tests for reserved arb budget | 4 |
| `tests/test_insider_refresh.py` | Tests for watchlist auto-refresh | 5 |
| `tests/test_kyle_ab.py` | Tests for A/B toggle | 6 |

---

### Task 1: Wire Up Eval Logger Backfill (Fix Broken Feedback Loop)

**Why:** The backfill loop in `server.py:540-549` runs hourly but only calls `get_unresolved_skips()` and logs the count — it never actually resolves any markets. Without backfill data, `get_calibration()` and `get_missed_opportunities()` return empty results. The calibration engine is blind.

**Files:**
- Modify: `src/eval_logger.py:78-96` (add resolution lookup method)
- Modify: `src/server.py:540-549` (wire backfill to resolve markets)
- Create: `tests/test_eval_backfill.py`

- [ ] **Step 1: Write failing test for `resolve_opportunity()`**

```python
# tests/test_eval_backfill.py
"""Tests for eval logger backfill — resolving skipped opportunities."""
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from eval_logger import EvalLogger


class TestResolveOpportunity:
    def test_resolve_writes_backfill_entry(self):
        """resolve_opportunity should append a backfill entry to the log."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
            # Log a skipped opportunity
            logger.log_opportunity(
                strategy_type="pure_prediction",
                opportunity_id="opp_btc_100k",
                action="skipped",
                action_reason="low_score",
                markets=[{"condition_id": "0xabc123", "platform": "polymarket"}],
                prices_at_decision={"yes": 0.35, "no": 0.65},
            )

            # Resolve it
            logger.backfill_outcome(
                opportunity_id="opp_btc_100k",
                actual_pnl_pct=15.0,
                actual_outcome="win",
                resolution_date="2026-03-22",
                prices_at_resolution={"yes": 1.0, "no": 0.0},
            )

            # Verify backfill entry exists
            entries = logger._read_all()
            backfills = [e for e in entries if e.get("type") == "backfill"]
            assert len(backfills) == 1
            assert backfills[0]["opportunity_id"] == "opp_btc_100k"
            assert backfills[0]["actual_pnl_pct"] == 15.0
            assert backfills[0]["actual_outcome"] == "win"

    def test_missed_opportunity_detected_after_backfill(self):
        """After backfill, get_missed_opportunities should find profitable skips."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
            logger.log_opportunity(
                strategy_type="pure_prediction",
                opportunity_id="opp_missed",
                action="skipped",
                action_reason="low_score",
            )
            logger.backfill_outcome(
                opportunity_id="opp_missed",
                actual_pnl_pct=25.0,
                actual_outcome="win",
                resolution_date="2026-03-22",
            )

            missed = logger.get_missed_opportunities()
            assert len(missed) == 1
            assert missed[0]["opportunity_id"] == "opp_missed"
            assert missed[0]["actual_pnl_pct"] == 25.0

    def test_correct_skip_not_in_missed(self):
        """Skips that resolved at a loss should NOT appear in missed."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
            logger.log_opportunity(
                strategy_type="pure_prediction",
                opportunity_id="opp_good_skip",
                action="skipped",
                action_reason="low_score",
            )
            logger.backfill_outcome(
                opportunity_id="opp_good_skip",
                actual_pnl_pct=-30.0,
                actual_outcome="loss",
                resolution_date="2026-03-22",
            )

            missed = logger.get_missed_opportunities()
            assert len(missed) == 0

    def test_calibration_with_backfills(self):
        """Calibration should reflect backfilled data."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
            # 2 skips for same reason, 1 correct, 1 missed
            logger.log_opportunity("pure_prediction", "opp_1", "skipped", "low_score")
            logger.log_opportunity("pure_prediction", "opp_2", "skipped", "low_score")
            logger.backfill_outcome("opp_1", -10.0, "loss", "2026-03-22")
            logger.backfill_outcome("opp_2", 20.0, "win", "2026-03-22")

            cal = logger.get_calibration()
            assert "low_score" in cal
            assert cal["low_score"]["resolved"] == 2
            assert cal["low_score"]["correct_skips"] == 1
            assert cal["low_score"]["missed_opportunities"] == 1
            assert cal["low_score"]["correct_skip_rate"] == 0.5
```

- [ ] **Step 2: Run tests to verify they pass (these test existing backfill_outcome)**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_eval_backfill.py -v
```

Expected: All PASS (backfill_outcome already exists, tests validate it works)

- [ ] **Step 3: Add `resolve_via_polymarket()` to eval_logger**

Add a helper method that takes an unresolved skip entry and checks Polymarket's API for resolution:

```python
# In eval_logger.py, add after backfill_outcome():

async def resolve_via_polymarket(self, entry: dict, http_client) -> bool:
    """Check if a skipped opportunity's market has resolved on Polymarket.

    Queries Polymarket Gamma API for the condition. If resolved, calls
    backfill_outcome with the actual result.

    Returns True if resolved and backfilled, False otherwise.
    """
    markets = entry.get("markets") or []
    if not markets:
        return False

    # Find the Polymarket condition_id
    condition_id = None
    for m in markets:
        if m.get("platform") == "polymarket":
            condition_id = m.get("condition_id") or m.get("asset_id", "").split(":")[0]
            break
    if not condition_id:
        return False

    try:
        resp = await http_client.get(
            f"https://gamma-api.polymarket.com/markets",
            params={"condition_id": condition_id},
            timeout=10,
        )
        if resp.status_code != 200:
            return False

        data = resp.json()
        if not data:
            return False

        market = data[0] if isinstance(data, list) else data
        if not market.get("closed"):
            return False

        # Market resolved — compute what P&L would have been
        # outcomePrices is a JSON string like '["0.95","0.05"]'
        try:
            outcome_prices = json.loads(market.get("outcomePrices", "[0,0]"))
            resolution_price = float(outcome_prices[0])
        except (json.JSONDecodeError, IndexError, TypeError):
            return False
        prices_at_decision = entry.get("prices_at_decision", {})
        entry_yes = prices_at_decision.get("yes", 0.5)

        # If we would have bought YES: pnl = (resolution - entry) / entry
        # If we would have bought NO: pnl = ((1-resolution) - (1-entry)) / (1-entry)
        if entry_yes < 0.5:
            # Would have bought YES side
            pnl_pct = round((resolution_price - entry_yes) / entry_yes * 100, 2) if entry_yes > 0 else 0
        else:
            # Would have bought NO side
            entry_no = 1 - entry_yes
            resolution_no = 1 - resolution_price
            pnl_pct = round((resolution_no - entry_no) / entry_no * 100, 2) if entry_no > 0 else 0

        outcome = "win" if pnl_pct > 0 else ("loss" if pnl_pct < 0 else "flat")

        self.backfill_outcome(
            opportunity_id=entry["opportunity_id"],
            actual_pnl_pct=pnl_pct,
            actual_outcome=outcome,
            resolution_date=market.get("endDate", ""),
            prices_at_resolution={"resolution_price": resolution_price},
        )
        return True
    except Exception as e:
        logger.debug("Polymarket resolution check failed for %s: %s",
                     entry.get("opportunity_id"), e)
        return False
```

- [ ] **Step 4: Wire backfill loop in server.py to call resolve_via_polymarket**

Replace `server.py:540-549`:

```python
        # Start hourly backfill task for skipped opportunity resolution checks
        async def _backfill_loop():
            while True:
                await asyncio.sleep(3600)  # 1 hour
                try:
                    unresolved = _eval_log.get_unresolved_skips()
                    if unresolved:
                        resolved_count = 0
                        async with httpx.AsyncClient() as client:
                            for entry in unresolved[:50]:  # Batch limit: 50 per hour
                                try:
                                    if await _eval_log.resolve_via_polymarket(entry, client):
                                        resolved_count += 1
                                    await asyncio.sleep(0.5)  # Rate limit: 2 req/s
                                except Exception:
                                    pass
                        logger.info("Eval backfill: checked %d/%d unresolved, resolved %d",
                                     min(50, len(unresolved)), len(unresolved), resolved_count)
                except Exception as e:
                    logger.warning("Eval backfill error: %s", e)
```

- [ ] **Step 5: Write integration test for resolve_via_polymarket**

```python
# Add to tests/test_eval_backfill.py

@pytest.mark.asyncio
async def test_resolve_via_polymarket_closed_market():
    """resolve_via_polymarket should backfill when market is closed."""
    with tempfile.TemporaryDirectory() as tmp:
        logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
        logger.log_opportunity(
            strategy_type="pure_prediction",
            opportunity_id="opp_resolved",
            action="skipped",
            action_reason="low_score",
            markets=[{"condition_id": "0xabc", "platform": "polymarket"}],
            prices_at_decision={"yes": 0.30, "no": 0.70},
        )

        entry = logger.get_unresolved_skips()[0]

        mock_client = AsyncMock()
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: [{"closed": True, "outcomePrices": "[1.0, 0.0]", "endDate": "2026-03-22"}],
        )

        result = await logger.resolve_via_polymarket(entry, mock_client)
        assert result is True

        missed = logger.get_missed_opportunities()
        assert len(missed) == 1
        assert missed[0]["actual_outcome"] == "win"


@pytest.mark.asyncio
async def test_resolve_via_polymarket_still_open():
    """resolve_via_polymarket should return False for open markets."""
    with tempfile.TemporaryDirectory() as tmp:
        logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
        logger.log_opportunity(
            strategy_type="pure_prediction",
            opportunity_id="opp_open",
            action="skipped",
            action_reason="low_score",
            markets=[{"condition_id": "0xdef", "platform": "polymarket"}],
        )

        entry = logger.get_unresolved_skips()[0]

        mock_client = AsyncMock()
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: [{"closed": False}],
        )

        result = await logger.resolve_via_polymarket(entry, mock_client)
        assert result is False

        # No backfill should have been written
        assert len(logger.get_unresolved_skips()) == 1
```

- [ ] **Step 6: Run all tests**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_eval_backfill.py -v
```

- [ ] **Step 7: Commit**

```bash
git add src/eval_logger.py src/server.py tests/test_eval_backfill.py
git commit -m "feat: wire eval logger backfill to resolve markets via Polymarket API

The backfill loop previously only counted unresolved skips but never
actually checked if markets had resolved. Now queries Polymarket Gamma
API hourly (batch of 50) and writes backfill entries with actual P&L.
Enables calibration engine to produce real data for threshold tuning."
```

---

### Task 2: Block Commodities Entirely

**Why:** 3 trades, 0% win rate, -$46. The 0.4x penalty still lets them through on high-spread markets. Block them like exact-score sports.

**Files:**
- Modify: `src/positions/auto_trader.py:632-635`
- Create: `tests/test_commodities_block.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_commodities_block.py
"""Tests for commodities hard block in auto trader scoring."""
import re


class TestCommoditiesBlock:
    def test_commodities_keyword_detected(self):
        """COMMODITIES_KEYWORDS should identify commodity markets."""
        from positions.auto_trader import COMMODITIES_KEYWORDS
        title = "crude oil settle over $76"
        assert any(kw in title.lower() for kw in COMMODITIES_KEYWORDS)

    def test_non_commodity_not_blocked(self):
        """Non-commodity markets should pass through the filter."""
        from positions.auto_trader import COMMODITIES_KEYWORDS
        title = "will btc hit 100k by june"
        is_commodities = any(kw in title.lower() for kw in COMMODITIES_KEYWORDS)
        assert not is_commodities

    def test_commodities_code_uses_continue_not_multiply(self):
        """The auto_trader source should `continue` on commodities, not `score *= 0.4`.

        This is a source-code assertion that verifies the behavioral change:
        after the fix, the code block for is_commodities should contain 'continue'
        and NOT contain 'score *= 0.4'.
        """
        import inspect
        from positions.auto_trader import AutoTrader
        source = inspect.getsource(AutoTrader._scan_and_trade)
        # Find the commodities block: from "if is_commodities" to the next unindented line
        # After fix: should have 'continue' in the commodities block
        # After fix: should NOT have 'score *= 0.4' anywhere near commodities
        assert "commodities_market" in source, "Should log skip reason 'commodities_market'"
        # The old penalty pattern should be gone
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "is_commodities" in line and "score *=" in line:
                raise AssertionError(
                    f"Line {i}: commodities should be hard-skipped, not penalized: {line.strip()}"
                )
```

- [ ] **Step 2: Run tests**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_commodities_block.py -v
```

- [ ] **Step 3: Change commodities from penalty to hard skip**

In `src/positions/auto_trader.py`, replace lines 632-635:

```python
            # OLD: score *= 0.4
            # NEW: hard skip — 0% WR across 3 trades, -$46
            if is_commodities:
                self._trades_skipped += 1
                if self.dlog:
                    self.dlog.log_opportunity_skip(opp_title, "commodities_market")
                continue
```

- [ ] **Step 4: Run full test suite**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add src/positions/auto_trader.py tests/test_commodities_block.py
git commit -m "fix: block commodities entirely instead of 0.4x penalty

Journal data: 3 trades, 0% win rate, -\$46. The 0.4x penalty still
allowed entry on high-spread commodity markets. Now hard-skipped
like exact-score sports bets."
```

---

### Task 3: Skip Trades During Bad Regime Instead of Min-Sizing

**Why:** When `_regime_penalty = 0.5` (5+ consecutive losses), the Kelly sizing drops 50% but then `max(self._min_trade_size, ...)` floors it back to $10. These zombie $10 bets waste a concurrent slot and exposure budget, can't produce meaningful returns.

**Files:**
- Modify: `src/positions/auto_trader.py:283-287` (`_kelly_size` method)
- Create: `tests/test_regime_skip.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_regime_skip.py
"""Tests for regime penalty — skip trades entirely instead of min-sizing."""
import tempfile
from pathlib import Path
from positions.auto_trader import AutoTrader
from positions.position_manager import PositionManager


class TestRegimeSkip:
    def _make_trader(self, tmp_path):
        pm = PositionManager(data_dir=tmp_path, executors={})
        trader = AutoTrader(pm, scanner=None)
        return trader

    def test_normal_regime_returns_positive_size(self, tmp_path):
        """With regime_penalty=1.0, _kelly_size should return a trade size."""
        trader = self._make_trader(tmp_path)
        trader._regime_penalty = 1.0
        size = trader._kelly_size("pure_prediction", remaining_budget=500.0,
                                   implied_prob=0.70, spread_pct=15.0)
        assert size > 0

    def test_bad_regime_returns_zero(self, tmp_path):
        """With regime_penalty<1.0, _kelly_size should return 0 (skip the trade)."""
        trader = self._make_trader(tmp_path)
        trader._regime_penalty = 0.5
        size = trader._kelly_size("pure_prediction", remaining_budget=500.0,
                                   implied_prob=0.70, spread_pct=15.0)
        assert size == 0

    def test_regime_recovery_resumes_trading(self, tmp_path):
        """After regime_penalty returns to 1.0, trades should resume."""
        trader = self._make_trader(tmp_path)
        trader._regime_penalty = 0.5
        assert trader._kelly_size("pure_prediction", 500.0, 0.70, 15.0) == 0

        trader._regime_penalty = 1.0
        assert trader._kelly_size("pure_prediction", 500.0, 0.70, 15.0) > 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_regime_skip.py -v
```

Expected: `test_bad_regime_returns_zero` FAILS (currently returns `_min_trade_size`)

- [ ] **Step 3: Modify `_kelly_size` to return 0 during bad regimes**

In `src/positions/auto_trader.py:283-287`, replace:

```python
        kelly_sized = max(0.0, kelly_full * frac)
        sized = round(min(self._max_trade_size, remaining_budget * kelly_sized), 2)
        # Apply regime penalty (5-loss rule: reduce by 50% during bad streaks)
        sized = round(sized * self._regime_penalty, 2)
        return max(self._min_trade_size, min(sized, self._max_trade_size))
```

With:

```python
        kelly_sized = max(0.0, kelly_full * frac)
        sized = round(min(self._max_trade_size, remaining_budget * kelly_sized), 2)
        # Bad regime (5+ consecutive losses): skip entirely instead of limping
        # in with min-size bets that waste concurrent slots and can't produce
        # meaningful returns. Resume trading when streak breaks.
        if self._regime_penalty < 1.0:
            return 0
        return max(self._min_trade_size, min(sized, self._max_trade_size))
```

- [ ] **Step 4: Add `<= 0` guards at ALL 6 call sites of `_kelly_size`**

**CRITICAL:** None of the call sites currently check for `trade_size <= 0`. Without guards, returning 0 will create zero-cost legs or cause division-by-zero errors. Add a guard after EACH call:

```python
# Line 856 (multi_outcome_arb):
trade_size = self._kelly_size("multi_outcome_arb", remaining_budget, spread_pct=spread_pct)
if trade_size <= 0:
    continue

# Line 932 (portfolio_no):
trade_size = self._kelly_size("portfolio_no", remaining_budget, spread_pct=spread_pct)
if trade_size <= 0:
    continue

# Line 1012 (weather_forecast):
trade_size = self._kelly_size("weather_forecast", remaining_budget, ...)
if trade_size <= 0:
    continue

# Line 1078 (political_synthetic):
trade_size = self._kelly_size("political_synthetic", remaining_budget, ...)
if trade_size <= 0:
    continue

# Line 1157 (crypto_synthetic):
trade_size = self._kelly_size("crypto_synthetic", remaining_budget, ...)
if trade_size <= 0:
    continue

# Line 1251 (cross_platform_arb / synthetic_derivative):
trade_size = self._kelly_size(strategy, remaining_budget, spread_pct=spread_pct)
if trade_size <= 0:
    continue
```

**IMPORTANT exception:** For guaranteed-profit strategies (`multi_outcome_arb`, `portfolio_no`, `cross_platform_arb`), the regime penalty should NOT apply — these are risk-free. Modify `_kelly_size` to accept a `bypass_regime` parameter:

```python
    def _kelly_size(self, strategy: str, remaining_budget: float,
                    implied_prob: float = 0.0, spread_pct: float = 0.0,
                    bypass_regime: bool = False) -> float:
        # ... existing Kelly calculation ...
        kelly_sized = max(0.0, kelly_full * frac)
        sized = round(min(self._max_trade_size, remaining_budget * kelly_sized), 2)
        # Bad regime: skip speculative trades entirely (not arbs)
        if self._regime_penalty < 1.0 and not bypass_regime:
            return 0
        return max(self._min_trade_size, min(sized, self._max_trade_size))
```

Then pass `bypass_regime=True` at arb call sites (lines 856, 932, 1251 for cross_platform_arb).

Also update the direct Kelly sizing in the main scoring loop at line 1392:
```python
                if self._regime_penalty < 1.0:
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "bad_regime")
                    continue
                sized_trade = round(min(self._max_trade_size, remaining_budget * kelly_quarter), 2)
```

- [ ] **Step 5: Run tests**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_regime_skip.py tests/ -x -q
```

- [ ] **Step 6: Commit**

```bash
git add src/positions/auto_trader.py tests/test_regime_skip.py
git commit -m "fix: skip trades entirely during bad regime instead of min-sizing

Previously, regime_penalty=0.5 halved position size but max() floored
it to min_trade_size (\$10). These zombie bets consumed concurrent
slots and exposure budget without meaningful return potential. Now
returns 0 from _kelly_size during bad regimes, causing callers to
skip the opportunity. Trading resumes when the loss streak breaks."
```

---

### Task 4: Reserve Budget for Cross-Platform Arb

**Why:** Decision log shows 30-43% spread arb opportunities repeatedly blocked by max_exposure limits consumed by speculative directional bets. Arbs are the highest-conviction strategy (mathematically guaranteed profit) but get starved of capital.

**Files:**
- Modify: `src/positions/auto_trader.py:380-406` (budget calculation)
- Create: `tests/test_arb_budget.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_arb_budget.py
"""Tests for reserved arb budget — arbs should always have capital access."""
import tempfile
from pathlib import Path
from positions.auto_trader import AutoTrader
from positions.position_manager import PositionManager, create_package, create_leg


class TestArbBudget:
    def _make_trader_at_capacity(self, tmp_path, exposure_pct=0.95):
        """Create a trader near max exposure with directional bets."""
        pm = PositionManager(data_dir=tmp_path, executors={})
        trader = AutoTrader(pm, scanner=None, initial_bankroll=2000.0)
        # Fill up with directional bets
        total_to_fill = trader._max_total_exposure * exposure_pct
        pkg = create_package("Filler Trade", "pure_prediction")
        pkg["legs"].append(create_leg("polymarket", "prediction_yes",
                                       "filler:YES", "Filler", 0.5, total_to_fill))
        pm.add_package(pkg)
        return trader, pm

    def test_arb_budget_reserved_when_near_capacity(self, tmp_path):
        """At 95% exposure from directional bets, arb budget should still exist."""
        trader, pm = self._make_trader_at_capacity(tmp_path, exposure_pct=0.95)
        open_pkgs = pm.list_packages("open")
        total_exposure = sum(p.get("total_cost", 0) for p in open_pkgs)

        # Directional budget should be exhausted
        directional_budget = trader._max_total_exposure * (1 - 0.40) - total_exposure
        assert directional_budget < trader._min_trade_size

        # Arb budget (40% reserved) should still have room
        arb_budget = trader._max_total_exposure - total_exposure
        assert arb_budget > trader._min_trade_size
```

- [ ] **Step 2: Run test to verify behavior**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_arb_budget.py -v
```

- [ ] **Step 3: Add ARB_BUDGET_RESERVE constant and split budget calculation**

At the top of `auto_trader.py`, near the other constants (around line 35-40), add:

```python
# Reserve 40% of max exposure for cross-platform arb (highest conviction, guaranteed profit).
# Directional bets can only consume the remaining 60%. This prevents arb starvation
# that the decision log showed: 30-43% spread arbs repeatedly blocked.
ARB_BUDGET_RESERVE_PCT = 0.40
```

In the `_scan_and_trade` method (around line 380-406), after computing `remaining_budget`, add budget splitting logic:

```python
        remaining_budget = min(self._max_total_exposure - total_exposure, kelly_cap - total_exposure)
        remaining_slots = MAX_CONCURRENT - len(open_pkgs)

        # Split budget: reserve ARB_BUDGET_RESERVE_PCT for cross-platform arbs.
        # Directional bets can only use the unreserved portion.
        arb_reserve = self._max_total_exposure * ARB_BUDGET_RESERVE_PCT
        # How much of the reserve is already consumed by existing arb packages?
        arb_exposure = sum(p.get("total_cost", 0) for p in open_pkgs
                          if p.get("strategy_type") in ("cross_platform_arb", "multi_outcome_arb"))
        arb_remaining_reserve = max(0, arb_reserve - arb_exposure)
        # Directional budget = total remaining MINUS the unfilled arb reserve
        # If arb reserve is full (arb_remaining_reserve=0), directional gets everything
        # If arb reserve is empty, directional is capped at (1-ARB_BUDGET_RESERVE_PCT) of exposure
        directional_budget = max(0, remaining_budget - arb_remaining_reserve)
        # Arb budget = full remaining (arbs can use both their reserve AND any leftover)
        arb_budget = remaining_budget
```

Then pass the appropriate budget to each loop section. Specifically:

```python
# Line ~523 (crypto directional scoring): use directional_budget
# Line ~856 (multi_outcome_arb loop): use arb_budget
# Line ~932 (portfolio_no loop): use arb_budget
# Line ~1012 (weather loop): use directional_budget
# Line ~1078 (political loop): use directional_budget
# Line ~1157 (crypto synthetic loop): use directional_budget
# Line ~1251 (cross_platform_arb/synthetic): use arb_budget
# Line ~1380 (main scoring loop): use directional_budget
```

For each loop, replace the `remaining_budget` variable name in the loop header and `remaining_budget -= trade_size` with the appropriate budget variable. Also update the early-exit check:

```python
        if directional_budget < self._min_trade_size and arb_budget < self._min_trade_size:
            # Skip logging — already logged at the top
            pass  # let individual loops handle their own budget checks
```

- [ ] **Step 4: Run tests**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_arb_budget.py tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add src/positions/auto_trader.py tests/test_arb_budget.py
git commit -m "feat: reserve 40% of exposure budget for cross-platform arbs

Decision log showed 30-43% spread arbs (guaranteed profit) repeatedly
blocked by exposure limits consumed by speculative directional bets.
Now reserves 40% of max_total_exposure exclusively for cross-platform
and multi-outcome arb. Directional bets can only use the remaining 60%."
```

---

### Task 5: Auto-Refresh Insider Watchlist

**Why:** `HIGH_CONVICTION_WATCHLIST` in `insider_tracker.py:45-54` is hardcoded from March 20 data. These 8 wallets may have changed strategy, retired, or been liquidated. The 5x signal weight is based on stale data.

**Files:**
- Modify: `src/positions/insider_tracker.py:45-54` (make watchlist refreshable)
- Create: `tests/test_insider_refresh.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_insider_refresh.py
"""Tests for insider tracker watchlist auto-refresh."""
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from positions.insider_tracker import InsiderTracker, HIGH_CONVICTION_WATCHLIST


class TestWatchlistRefresh:
    def test_initial_watchlist_loaded(self, tmp_path):
        """InsiderTracker should start with the hardcoded watchlist."""
        tracker = InsiderTracker(data_dir=tmp_path)
        assert len(tracker._conviction_watchlist) >= 8

    def test_watchlist_can_be_updated(self, tmp_path):
        """Calling refresh_watchlist should update the conviction set."""
        tracker = InsiderTracker(data_dir=tmp_path)
        new_wallets = {
            "0xnew_wallet_1": "NewTrader1",
            "0xnew_wallet_2": "NewTrader2",
        }
        tracker.update_watchlist(new_wallets)
        assert "0xnew_wallet_1" in tracker._conviction_watchlist
        assert "0xnew_wallet_2" in tracker._conviction_watchlist

    def test_watchlist_persists_to_disk(self, tmp_path):
        """Updated watchlist should survive restart."""
        t1 = InsiderTracker(data_dir=tmp_path)
        t1.update_watchlist({"0xpersist": "PersistTrader"})

        t2 = InsiderTracker(data_dir=tmp_path)
        assert "0xpersist" in t2._conviction_watchlist
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_insider_refresh.py -v
```

Expected: FAIL — `_conviction_watchlist` and `update_watchlist` don't exist yet.

- [ ] **Step 3: Add refreshable watchlist to InsiderTracker**

In `insider_tracker.py`, modify `__init__` to load the watchlist from disk (falling back to hardcoded):

```python
    def __init__(self, data_dir: Path, auto_trader=None):
        self.data_dir = Path(data_dir)
        # ... existing init ...
        self._conviction_watchlist = dict(HIGH_CONVICTION_WATCHLIST)
        self._load_watchlist()

    def _load_watchlist(self):
        """Load refreshed watchlist from disk, falling back to hardcoded."""
        path = self.data_dir / "conviction_watchlist.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("wallets") and data.get("updated_at", 0) > 0:
                    self._conviction_watchlist = data["wallets"]
                    logger.info("Loaded refreshed watchlist: %d wallets (updated %s)",
                                len(self._conviction_watchlist),
                                data.get("updated_at_str", "unknown"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load watchlist: %s", e)

    def update_watchlist(self, wallets: dict[str, str]):
        """Update the conviction watchlist and persist to disk.

        wallets: {address: display_name} dict
        Merges with hardcoded defaults — never shrinks below the base set.
        """
        merged = dict(HIGH_CONVICTION_WATCHLIST)
        merged.update(wallets)
        self._conviction_watchlist = merged

        path = self.data_dir / "conviction_watchlist.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "wallets": merged,
            "updated_at": time.time(),
            "updated_at_str": time.strftime("%Y-%m-%d %H:%M"),
        }
        with open(str(path), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Updated conviction watchlist: %d wallets", len(merged))
```

Also update the wallet classification check (line 218) to use `self._conviction_watchlist` instead of `HIGH_CONVICTION_WATCHLIST`:

```python
            if wallet.lower() in {k.lower() for k in self._conviction_watchlist}:
```

- [ ] **Step 4: Run tests**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_insider_refresh.py tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add src/positions/insider_tracker.py tests/test_insider_refresh.py
git commit -m "feat: make insider watchlist refreshable with disk persistence

HIGH_CONVICTION_WATCHLIST was hardcoded from March 20. Now loads from
conviction_watchlist.json on startup (falls back to hardcoded), and
can be updated via update_watchlist(). Merges with defaults so the
base set is never lost. Enables monthly auto-refresh from leaderboard."
```

---

### Task 6: Add Kyle's Lambda A/B Test Toggle

**Why:** Kyle's lambda multiplier (0.4-1.5x) is theoretically sound but unvalidated against real trades. Need to run 50+ trades with it on vs off to measure actual impact on win rate.

**Files:**
- Modify: `src/positions/kyle_lambda.py` (add toggle + logging)
- Create: `tests/test_kyle_ab.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_kyle_ab.py
"""Tests for Kyle's lambda A/B test toggle."""
from positions.kyle_lambda import KyleLambdaEstimator


class TestKyleABToggle:
    def test_ab_disabled_returns_neutral_multiplier(self):
        """When A/B test disables kyle, get_lambda_signal should return multiplier=1.0."""
        estimator = KyleLambdaEstimator()
        estimator.ab_test_enabled = False
        # Even if there's data, should return neutral
        result = estimator.get_lambda_signal("test_asset", "YES")
        assert result["multiplier"] == 1.0

    def test_ab_enabled_returns_dict(self):
        """When A/B test enables kyle, should return dict with multiplier key."""
        estimator = KyleLambdaEstimator()
        estimator.ab_test_enabled = True
        # Without data, returns neutral anyway
        result = estimator.get_lambda_signal("test_asset", "YES")
        assert isinstance(result, dict)
        assert "multiplier" in result
        assert isinstance(result["multiplier"], float)

    def test_ab_default_is_enabled(self):
        """Default A/B state should be enabled."""
        estimator = KyleLambdaEstimator()
        assert estimator.ab_test_enabled is True

    def test_ab_disabled_signal_includes_ab_flag(self):
        """When disabled, the returned signal should indicate A/B status."""
        estimator = KyleLambdaEstimator()
        estimator.ab_test_enabled = False
        result = estimator.get_lambda_signal("test_asset", "YES")
        assert result.get("ab_disabled") is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_kyle_ab.py -v
```

Expected: FAIL — `ab_test_enabled` doesn't exist yet.

- [ ] **Step 3: Add A/B toggle to KyleLambdaEstimator**

In `kyle_lambda.py`, add to `__init__`:

```python
        self.ab_test_enabled = True  # Set False to disable kyle scoring (A/B test)
```

In the `get_lambda_signal` method (line 138), add at the top (after the docstring):

```python
        if not self.ab_test_enabled:
            return {"multiplier": 1.0, "short_lambda": None, "long_lambda": None,
                    "lambda_ratio": None, "flow_direction": None, "ab_disabled": True}
```

- [ ] **Step 4: Run tests**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_kyle_ab.py tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add src/positions/kyle_lambda.py tests/test_kyle_ab.py
git commit -m "feat: add A/B test toggle to Kyle's lambda estimator

Allows disabling kyle scoring (returns neutral 1.0x) for controlled
comparison. Set estimator.ab_test_enabled = False to run without
kyle influence. After 50+ trades each, compare win rates to validate
whether kyle actually improves trade selection."
```

---

### Task 7: Log Favorite Multiplier Contribution for Audit

**Why:** The 3.0x favorite multiplier at >=0.80 entry is the largest single score driver. It powered the Solana wins (+$410) but also drove 17 XRP/ETH losses (0% WR). Need data to separate "multiplier picks good markets" from "multiplier picks high-confidence markets that lose to fee drag."

**Files:**
- Modify: `src/positions/auto_trader.py:637-650` (log multiplier components)

- [ ] **Step 1: Add score component logging to the eval logger call**

In `auto_trader.py`, after the scoring section (around the area where `log_opportunity` is called for entered trades), add metadata about score components:

```python
                    # Log score components for post-hoc analysis
                    score_metadata = {
                        "raw_spread": spread_pct,
                        "crypto_mult": crypto_mult,
                        "expiry_mult": expiry_mult,
                        "favorite_mult": favorite_mult,
                        "insider_mult": insider_mult,
                        "kyle_mult": kyle_mult,
                        "entry_price": favored_price,
                        "side": side,
                    }
```

This requires capturing the individual multiplier values into named variables during scoring. Refactor the scoring section to store each multiplier before applying it:

Find the scoring section (~lines 637-697) and ensure each multiplier step stores its value:

```python
            # Capture multipliers for logging
            crypto_mult = 2.0 if is_crypto else 1.0
            # ... (apply score *= crypto_mult)

            favorite_mult = 1.0
            if favored_price >= 0.80:
                favorite_mult = 3.0
            elif favored_price >= 0.70:
                favorite_mult = 2.2
            # ... etc
            # ... (apply score *= favorite_mult)
```

Then pass `score_metadata` in the eval logger and decision logger calls.

- [ ] **Step 2: Run full test suite to verify no regressions**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/ -x -q
```

- [ ] **Step 3: Commit**

```bash
git add src/positions/auto_trader.py
git commit -m "feat: log score multiplier components for favorite bias audit

Each trade now logs the individual multiplier values (crypto, expiry,
favorite, insider, kyle) in eval logger metadata. Enables post-hoc
analysis of whether the 3.0x favorite multiplier is selecting good
markets or just high-confidence markets that lose to other factors."
```

---

### Task 8: Default Hold-to-Resolution for Short-Expiry Prediction Markets

**Why:** Both open positions are profitable NO-side bets being held to expiry. The hold-to-resolution strategy avoids trailing stop losses (-$98 from 5 exits) and premature time-based exits (-$39). If Phase 2 data confirms, this should be the default for all <14 day expiry prediction markets.

**Files:**
- Modify: `src/positions/auto_trader.py` (~line 1460-1468, package construction)

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_auto_trader_improvements.py or create new file

import inspect

class TestHoldToResolutionDefault:
    def test_hold_to_resolution_threshold_exists_in_source(self):
        """The auto_trader source should reference a 14-day hold-to-resolution threshold."""
        from positions.auto_trader import AutoTrader
        source = inspect.getsource(AutoTrader._scan_and_trade)
        # Verify the threshold constant is used in the code
        assert "HOLD_TO_RESOLUTION_MAX_DAYS" in source or "hold_to_resolution" in source

    def test_short_expiry_logic_sets_hold_flag(self):
        """Package construction for <=14 day expiry should set _hold_to_resolution=True.

        Verify by reading the source: the conditional around _hold_to_resolution
        should include a days_to_expiry check, not just cross_platform_arb.
        """
        from positions.auto_trader import AutoTrader
        source = inspect.getsource(AutoTrader._scan_and_trade)
        # After the fix, _hold_to_resolution should be set based on days_to_expiry
        # Find the section that sets _hold_to_resolution
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "_hold_to_resolution" in line and "True" in line:
                # Check surrounding context includes expiry check
                context = "\n".join(lines[max(0, i-3):i+1])
                if "days_to_expiry" in context or "expiry" in context.lower():
                    return  # Found the expiry-based hold logic
        raise AssertionError(
            "_hold_to_resolution=True should be conditioned on days_to_expiry <= 14"
        )
```

- [ ] **Step 2: Modify package construction to default hold-to-resolution**

In `auto_trader.py`, around line 1460 where `_hold_to_resolution` is set conditionally, change the logic:

```python
            # Hold to resolution for short-expiry prediction markets.
            # Research: trailing stops (-$98) and time exits (-$39) destroyed value.
            # Markets with <14 days to expiry should resolve naturally.
            HOLD_TO_RESOLUTION_MAX_DAYS = 14
            if days_to_expiry <= HOLD_TO_RESOLUTION_MAX_DAYS:
                pkg["_hold_to_resolution"] = True
            # Also hold favorites (>$0.85) regardless of expiry
            if favored_price >= 0.85:
                pkg["_hold_to_resolution"] = True
```

Replace the existing conditional that only sets it for cross-platform arb and strong favorites.

- [ ] **Step 3: Run tests**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/ -x -q
```

- [ ] **Step 4: Commit**

```bash
git add src/positions/auto_trader.py
git commit -m "feat: default hold-to-resolution for <14 day expiry markets

Trailing stop exits lost -\$98, time-based exits lost -\$39. Both open
positions (hold-to-resolution) are profitable. Now all prediction
markets with <=14 days to expiry default to hold_to_resolution=True,
skipping trailing_stop, negative_drift, and stale_position triggers.
Markets resolve naturally at $0 or $1 — intermediate exits just
add fee drag and noise."
```

---

## Execution Order & Dependencies

```
Task 1 (Eval Backfill) ─────── independent (eval_logger.py + server.py)
Task 5 (Insider Refresh) ────── independent (insider_tracker.py)
Task 6 (Kyle A/B) ───────────── independent (kyle_lambda.py)

Tasks below ALL modify auto_trader.py — implement SEQUENTIALLY:
Task 2 (Commodities Block) ──── auto_trader.py:632-635
Task 3 (Regime Skip) ────────── auto_trader.py:283-287 + 6 call sites
Task 4 (Arb Budget) ─────────── auto_trader.py:380-406 + loop headers
Task 7 (Favorite Audit Log) ─── auto_trader.py:637-697
Task 8 (Hold-to-Resolution) ─── auto_trader.py:1460-1468
```

**Task 3 + Task 4 interaction:** Regime skip returns 0 from `_kelly_size`, but arb strategies (guaranteed profit) should bypass the regime penalty via `bypass_regime=True`. This is handled in Task 3 Step 4.

Recommended order: **1 → 5 → 6** (parallel, independent files) **→ 2 → 3 → 4 → 7 → 8** (sequential, all in auto_trader.py).

## Monitoring After Deployment

After all 8 tasks are deployed, track over the next 20+ trades:

| Metric | Phase 1 Baseline | Target |
|--------|-----------------|--------|
| Win rate | 17% (8/47) | >30% |
| Fee drag | ~2% (post-fix) | <1% |
| Net P&L per trade | -$3.43 (excl. Solana) | >$0 |
| Arb opportunities entered | 0 (all blocked) | >5 |
| Commodities trades | 3 (0% WR) | 0 |
| Zombie regime trades | unknown | 0 |
| Eval backfill rate | 0% | >50% resolved |
| Calibration data points | 0 | >20 |
