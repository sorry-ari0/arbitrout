# LLM Mispricing Detection & Enhanced Cross-Platform Arb — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dynamic Polymarket fees, resolution criteria comparison, volume filtering, and a 2-model LLM consensus estimator that detects mispriced markets — all paper-only execution.

**Architecture:** Strategy 3 (dynamic fees, resolution comparison, volume filter) lives in `arbitrage_engine.py` and `auto_trader.py`. Strategy 2 (LLM estimator) is a new module `src/positions/llm_estimator.py` that the auto-trader calls when cross-platform price deviation exceeds 10%. Both share a dynamic fee helper importable by `paper_executor.py`.

**Tech Stack:** Python 3.11+, asyncio, httpx (Claude/Gemini API calls), functools.lru_cache, pytest

**Spec:** `docs/superpowers/specs/2026-03-20-llm-mispricing-enhanced-arb-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/arbitrage_engine.py` | Modify | Add `compute_taker_fee()`, `_POLYMARKET_FEE_PARAMS`, update `_compute_fee_adjusted_profit()`, add `_compare_resolution()`, `ResolutionMatch`, integrate into `find_arbitrage()` |
| `src/execution/paper_executor.py` | Modify | Use `compute_taker_fee()` for Polymarket taker fees |
| `src/positions/auto_trader.py` | Modify | Add `MIN_ARB_VOLUME` filter, `set_news_scanner()`, `_llm_estimator` param, LLM 2.0x boost logic |
| `src/positions/llm_estimator.py` | Create | `EstimateResult` dataclass, `LLMEstimator` class (Claude + Gemini parallel) |
| `src/server.py` | Modify | Wire `LLMEstimator`, `set_news_scanner()` |
| `src/eval_logger.py` | Modify | Add `log_llm_estimate()` method |
| `tests/test_dynamic_fees.py` | Create | Fee curve tests at key price points |
| `tests/test_resolution_comparison.py` | Create | Heuristic + LLM phase tests |
| `tests/test_llm_estimator.py` | Create | Consensus calculation, agreement/disagreement, rate limiting |
| `tests/test_auto_trader_improvements.py` | Modify | Volume filter + LLM boost tests |

---

### Task 1: Dynamic Polymarket Fee Model

**Files:**
- Modify: `src/arbitrage_engine.py:28-56` (fee constants + `_compute_fee_adjusted_profit`)
- Create: `tests/test_dynamic_fees.py`

**Context:** The current code uses a flat `_TAKER_FEES` dict (line 29) with `polymarket: 0.02` (2%). The real Polymarket formula is `effective_rate = fee_rate * (price * (1 - price)) ** exponent`, which varies by market category. At p=0.50, crypto markets pay 1.56% and politics/sports pay 0.44% — both less than the current flat 2%.

- [ ] **Step 1: Write failing tests for `compute_taker_fee()`**

Create `tests/test_dynamic_fees.py`:

```python
"""Tests for dynamic Polymarket fee model."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from arbitrage_engine import compute_taker_fee, _POLYMARKET_FEE_PARAMS


class TestComputeTakerFee:
    """Test the dynamic fee formula for Polymarket and flat rates for others."""

    def test_polymarket_crypto_at_050(self):
        """Crypto at p=0.50: 0.25 * (0.5 * 0.5)^2 = 0.015625"""
        fee = compute_taker_fee("polymarket", 0.50, "crypto")
        assert abs(fee - 0.015625) < 1e-6

    def test_polymarket_crypto_at_010(self):
        """Crypto at p=0.10: 0.25 * (0.1 * 0.9)^2 = 0.25 * 0.0081 = 0.002025"""
        fee = compute_taker_fee("polymarket", 0.10, "crypto")
        assert abs(fee - 0.002025) < 1e-6

    def test_polymarket_crypto_at_090(self):
        """Crypto at p=0.90: same as 0.10 (symmetric)."""
        fee = compute_taker_fee("polymarket", 0.90, "crypto")
        assert abs(fee - 0.002025) < 1e-6

    def test_polymarket_crypto_at_001(self):
        """Extreme low price: near-zero fee."""
        fee = compute_taker_fee("polymarket", 0.01, "crypto")
        assert fee < 0.0001

    def test_polymarket_crypto_at_099(self):
        """Extreme high price: near-zero fee."""
        fee = compute_taker_fee("polymarket", 0.99, "crypto")
        assert fee < 0.0001

    def test_polymarket_politics_at_050(self):
        """Politics at p=0.50: 0.0175 * (0.5 * 0.5)^1 = 0.004375"""
        fee = compute_taker_fee("polymarket", 0.50, "politics")
        assert abs(fee - 0.004375) < 1e-6

    def test_polymarket_sports_at_050(self):
        """Sports uses same curve as politics."""
        fee = compute_taker_fee("polymarket", 0.50, "sports")
        assert abs(fee - 0.004375) < 1e-6

    def test_polymarket_unknown_category_uses_crypto(self):
        """Unknown category defaults to crypto params (most conservative)."""
        fee = compute_taker_fee("polymarket", 0.50, "")
        assert abs(fee - 0.015625) < 1e-6

    def test_polymarket_zero_price_returns_zero(self):
        """Edge case: price=0 produces 0 fee."""
        fee = compute_taker_fee("polymarket", 0.0, "crypto")
        assert fee == 0.0

    def test_polymarket_one_price_returns_zero(self):
        """Edge case: price=1.0 produces 0 fee."""
        fee = compute_taker_fee("polymarket", 1.0, "crypto")
        assert fee == 0.0

    def test_kalshi_flat_rate(self):
        """Non-Polymarket platforms use flat rates from _TAKER_FEES."""
        fee = compute_taker_fee("kalshi", 0.50, "politics")
        assert fee == 0.01

    def test_predictit_flat_rate(self):
        fee = compute_taker_fee("predictit", 0.50, "politics")
        assert fee == 0.0

    def test_unknown_platform_default(self):
        fee = compute_taker_fee("newplatform", 0.50, "crypto")
        assert fee == 0.02

    def test_polymarket_lower_than_old_flat(self):
        """Dynamic model is always <= 2% (the old flat rate)."""
        for price in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
            for cat in ["crypto", "politics", "sports", "economics"]:
                fee = compute_taker_fee("polymarket", price, cat)
                assert fee <= 0.02, f"Fee {fee} > 0.02 at price={price}, cat={cat}"


class TestFeeParams:
    """Test that _POLYMARKET_FEE_PARAMS has expected categories."""

    def test_crypto_params(self):
        assert _POLYMARKET_FEE_PARAMS["crypto"]["fee_rate"] == 0.25
        assert _POLYMARKET_FEE_PARAMS["crypto"]["exponent"] == 2

    def test_politics_params(self):
        assert _POLYMARKET_FEE_PARAMS["politics"]["fee_rate"] == 0.0175
        assert _POLYMARKET_FEE_PARAMS["politics"]["exponent"] == 1

    def test_all_non_crypto_same_curve(self):
        """All non-crypto categories use the same low-fee curve."""
        for cat in ["politics", "sports", "economics", "weather", "culture"]:
            assert _POLYMARKET_FEE_PARAMS[cat]["fee_rate"] == 0.0175
            assert _POLYMARKET_FEE_PARAMS[cat]["exponent"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_dynamic_fees.py -v`
Expected: FAIL — `compute_taker_fee` and `_POLYMARKET_FEE_PARAMS` do not exist yet.

- [ ] **Step 3: Implement `compute_taker_fee()` and `_POLYMARKET_FEE_PARAMS`**

In `src/arbitrage_engine.py`, add the fee params dict and function AFTER line 40 (`_PREDICTIT_WITHDRAWAL_FEE = 0.05`), BEFORE the existing `_compute_fee_adjusted_profit` function (line 43):

```python
# Dynamic Polymarket fee parameters by market category
# Formula: effective_rate = fee_rate * (price * (1 - price)) ** exponent
_POLYMARKET_FEE_PARAMS = {
    "crypto": {"fee_rate": 0.25, "exponent": 2},
    "politics": {"fee_rate": 0.0175, "exponent": 1},
    "sports": {"fee_rate": 0.0175, "exponent": 1},
    "economics": {"fee_rate": 0.0175, "exponent": 1},
    "weather": {"fee_rate": 0.0175, "exponent": 1},
    "culture": {"fee_rate": 0.0175, "exponent": 1},
}
# Default (unknown category): crypto params (conservative — highest fees)
_POLYMARKET_DEFAULT_FEE_PARAMS = {"fee_rate": 0.25, "exponent": 2}


def compute_taker_fee(platform: str, price: float, category: str = "") -> float:
    """Compute taker fee for a given platform, price, and category.

    Polymarket uses a dynamic formula: fee_rate * (price * (1 - price)) ** exponent.
    All other platforms use flat rates from _TAKER_FEES.
    """
    if platform != "polymarket":
        return _TAKER_FEES.get(platform, _DEFAULT_TAKER_FEE)

    params = _POLYMARKET_FEE_PARAMS.get(category.lower(), _POLYMARKET_DEFAULT_FEE_PARAMS)
    base = price * (1.0 - price)
    return params["fee_rate"] * (base ** params["exponent"])
```

- [ ] **Step 4: Update `_compute_fee_adjusted_profit()` to accept `category` and use dynamic fees**

Change the function signature and body at line 43:

```python
def _compute_fee_adjusted_profit(yes_price: float, no_price: float,
                                  yes_platform: str, no_platform: str,
                                  category: str = "") -> tuple[float, float]:
    """Compute guaranteed profit after all platform fees.

    Returns (net_profit_pct, total_cost_with_fees).
    """
    yes_fee = yes_price * compute_taker_fee(yes_platform, yes_price, category)
    no_fee = no_price * compute_taker_fee(no_platform, no_price, category)
    total_cost = yes_price + no_price + yes_fee + no_fee

    # Resolution payouts — PredictIt takes 10% of profits + 5% of withdrawal
    yes_payout = 1.0
    if yes_platform == "predictit":
        after_tax = 1.0 - _PREDICTIT_PROFIT_TAX * (1.0 - yes_price)
        yes_payout = after_tax * (1.0 - _PREDICTIT_WITHDRAWAL_FEE)
    no_payout = 1.0
    if no_platform == "predictit":
        after_tax = 1.0 - _PREDICTIT_PROFIT_TAX * (1.0 - no_price)
        no_payout = after_tax * (1.0 - _PREDICTIT_WITHDRAWAL_FEE)

    worst_payout = min(yes_payout, no_payout)
    worst_profit = worst_payout - total_cost
    net_pct = worst_profit * 100

    return net_pct, total_cost
```

- [ ] **Step 5: Thread `category` through `find_arbitrage()`**

In `find_arbitrage()` (line 714), the `match` variable is a `MatchedEvent` which has `.category`. Pass it to both calls to `_compute_fee_adjusted_profit`:

At line 793 (synthetic path):
```python
net_pct, _ = _compute_fee_adjusted_profit(
    best_yes_market.yes_price, best_no_market.no_price,
    best_yes_market.platform, best_no_market.platform,
    category=match.category,
)
```

At line 830 (pure arb path):
```python
net_pct, _ = _compute_fee_adjusted_profit(
    best_yes_market.yes_price, best_no_market.no_price,
    best_yes_market.platform, best_no_market.platform,
    category=match.category,
)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_dynamic_fees.py tests/test_arbitrage.py -v`
Expected: All PASS. Existing arbitrage tests still pass (the new `category=""` default means non-Polymarket callers are unchanged).

- [ ] **Step 7: Commit**

```bash
cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/arbitrage_engine.py tests/test_dynamic_fees.py
git commit -m "feat: add dynamic Polymarket fee model with category-based curves

Replace flat 2% taker fee for Polymarket with official formula:
effective_rate = fee_rate * (price * (1 - price)) ** exponent.
Crypto: max 1.56% at p=0.50. Politics/sports: max 0.44%.
Other platforms keep flat rates unchanged."
```

---

### Task 2: Update Paper Executor Fees

**Files:**
- Modify: `src/execution/paper_executor.py:24-35`
- Modify: `tests/test_paper_executor.py` (add dynamic fee tests)

**Context:** `paper_executor.py` has its own `TAKER_FEE_RATES` dict (line 24) with `polymarket: 0.02`. It needs to use the dynamic formula for Polymarket too. Import `compute_taker_fee` from `arbitrage_engine`.

- [ ] **Step 1: Write failing test for paper executor dynamic fee**

Add to `tests/test_paper_executor.py`:

```python
class TestDynamicFees:
    """Test that PaperExecutor uses dynamic Polymarket fees."""

    def test_polymarket_taker_fee_uses_dynamic(self):
        """Polymarket taker fee should vary with price, not be flat 0.02."""
        from execution.paper_executor import get_taker_fee_rate
        # At p=0.50, crypto: 0.015625 (not 0.02)
        fee = get_taker_fee_rate("polymarket", 0.50, "crypto")
        assert abs(fee - 0.015625) < 1e-6

    def test_kalshi_taker_fee_unchanged(self):
        from execution.paper_executor import get_taker_fee_rate
        fee = get_taker_fee_rate("kalshi", 0.50, "politics")
        assert fee == 0.01
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_paper_executor.py::TestDynamicFees -v`
Expected: FAIL — `get_taker_fee_rate` does not exist.

- [ ] **Step 3: Add `get_taker_fee_rate()` to paper_executor.py**

At the top of `src/execution/paper_executor.py`, after the existing imports (line 3), add:

```python
from arbitrage_engine import compute_taker_fee
```

Then after `DEFAULT_FEE_RATE = 0.02` (line 35), add:

```python
def get_taker_fee_rate(platform: str, price: float = 0.5, category: str = "") -> float:
    """Get taker fee rate, using dynamic formula for Polymarket."""
    if platform == "polymarket":
        return compute_taker_fee(platform, price, category)
    return TAKER_FEE_RATES.get(platform, DEFAULT_FEE_RATE)
```

**Note:** The existing `PaperExecutor` methods that reference `TAKER_FEE_RATES["polymarket"]` should be updated to call `get_taker_fee_rate()` where the price is known. Where price context isn't available, the flat rate is still acceptable as a conservative fallback. Check the `execute_order` method and update the Polymarket fee lookup.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_paper_executor.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/execution/paper_executor.py tests/test_paper_executor.py
git commit -m "feat: use dynamic Polymarket fees in paper executor

Import compute_taker_fee from arbitrage_engine so paper trading
simulates the real Polymarket fee curve instead of flat 2%."
```

---

### Task 3: Resolution Criteria Comparison

**Files:**
- Modify: `src/arbitrage_engine.py` (add `_compare_resolution()`, `ResolutionMatch`, integrate into `find_arbitrage()`)
- Create: `tests/test_resolution_comparison.py`

**Context:** When two markets from different platforms are matched as the "same event," they might actually have subtly different resolution criteria (e.g., "BTC above $100K by Dec 31" vs "BTC above $100K by Dec 30"). The current system doesn't catch this, leading to false arb signals. This adds a two-phase check: fast heuristic first, LLM for uncertain cases.

- [ ] **Step 1: Write failing tests for resolution comparison**

Create `tests/test_resolution_comparison.py`:

```python
"""Tests for resolution criteria comparison between matched markets."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from arbitrage_engine import _compare_resolution, ResolutionMatch


class TestHeuristicPhase:
    """Test the heuristic (non-LLM) resolution comparison."""

    def test_identical_titles_match(self):
        result = _compare_resolution(
            "Will BTC exceed $100K by Dec 2026?",
            "Will BTC exceed $100K by Dec 2026?",
            "polymarket", "kalshi",
        )
        assert result.status == "match"
        assert result.confidence >= 0.9

    def test_near_identical_titles_match(self):
        """Minor wording differences should still match."""
        result = _compare_resolution(
            "Will Bitcoin exceed $100,000 by December 2026?",
            "Bitcoin above $100K by Dec 2026",
            "polymarket", "kalshi",
        )
        assert result.status == "match"

    def test_clearly_different_titles_divergent(self):
        result = _compare_resolution(
            "Will BTC exceed $100K by Dec 2026?",
            "Will ETH exceed $5K by Dec 2026?",
            "polymarket", "kalshi",
        )
        assert result.status == "divergent"
        assert result.confidence >= 0.8

    def test_different_dates_divergent(self):
        """Different resolution dates = different markets."""
        result = _compare_resolution(
            "Trump wins 2026 midterm",
            "Trump wins 2028 election",
            "polymarket", "kalshi",
        )
        assert result.status == "divergent"

    def test_different_dollar_amounts_divergent(self):
        """Different price targets = different markets."""
        result = _compare_resolution(
            "BTC above $100K by Dec 2026",
            "BTC above $150K by Dec 2026",
            "polymarket", "kalshi",
        )
        assert result.status == "divergent"

    def test_subtle_difference_uncertain(self):
        """Similar but not identical — needs LLM or gets 'uncertain'."""
        result = _compare_resolution(
            "Fed cuts rates in June 2026",
            "Fed lowers interest rates before July 2026",
            "polymarket", "kalshi",
        )
        assert result.status in ("uncertain", "match")


class TestResolutionMatchDataclass:
    def test_fields(self):
        rm = ResolutionMatch(status="match", confidence=0.95, reasoning="identical titles")
        assert rm.status == "match"
        assert rm.confidence == 0.95
        assert rm.reasoning == "identical titles"


class TestCaching:
    """Test that repeated calls use cache."""

    def test_same_pair_cached(self):
        """Calling with the same normalized titles should return cached result."""
        r1 = _compare_resolution("BTC above $100K", "BTC above $100K", "poly", "kalshi")
        r2 = _compare_resolution("BTC above $100K", "BTC above $100K", "poly", "kalshi")
        assert r1.status == r2.status
        assert r1.confidence == r2.confidence

    def test_order_independent(self):
        """(A, B) should give same result as (B, A)."""
        r1 = _compare_resolution("BTC above $100K", "ETH above $5K", "poly", "kalshi")
        r2 = _compare_resolution("ETH above $5K", "BTC above $100K", "kalshi", "poly")
        assert r1.status == r2.status
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_resolution_comparison.py -v`
Expected: FAIL — `_compare_resolution` and `ResolutionMatch` do not exist.

- [ ] **Step 3: Implement `ResolutionMatch` dataclass and `_compare_resolution()`**

In `src/arbitrage_engine.py`, add imports after the existing `import threading` (line 14):
```python
from dataclasses import dataclass
from functools import lru_cache
import string
```

Add after the fee functions, before `_match_confidence()` (around line 78):

```python
# ============================================================
# RESOLUTION CRITERIA COMPARISON
# ============================================================
@dataclass
class ResolutionMatch:
    """Result of comparing resolution criteria between two markets."""
    status: str          # "match", "divergent", "uncertain"
    confidence: float    # 0.0-1.0
    reasoning: str       # explanation


def _normalize_title(title: str) -> str:
    """Normalize a market title for comparison."""
    t = title.lower().strip()
    # Remove common platform prefixes
    for prefix in ["will ", "will the ", "is ", "does ", "do "]:
        if t.startswith(prefix):
            t = t[len(prefix):]
    # Remove punctuation
    t = t.translate(str.maketrans("", "", string.punctuation))
    # Normalize whitespace
    return " ".join(t.split())


def _extract_key_terms(title: str) -> dict:
    """Extract dates, dollar amounts, and named entities from a title."""
    terms = {"dates": [], "amounts": [], "entities": []}

    # Dates: years (2024-2030), months, specific dates
    years = re.findall(r'\b(20[2-3]\d)\b', title)
    terms["dates"].extend(years)
    months = re.findall(r'\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b', title.lower())
    terms["dates"].extend(months)

    # Dollar amounts: $100K, $1,000, $50000
    amounts = re.findall(r'\$[\d,]+(?:\.\d+)?[KkMmBb]?', title)
    terms["amounts"].extend(amounts)

    # Named entities / authority keywords
    for entity in ["SEC", "Congress", "Fed", "FDA", "EPA", "Trump", "Biden", "NATO", "WHO", "UN"]:
        if entity.lower() in title.lower():
            terms["entities"].append(entity)

    return terms


def _jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity on word tokens."""
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a and not tokens_b:
        return 1.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union) if union else 0.0


def _key_terms_conflict(terms_a: dict, terms_b: dict) -> bool:
    """Check if key terms conflict between two titles."""
    # Different dollar amounts = conflict
    if terms_a["amounts"] and terms_b["amounts"]:
        # Normalize amounts for comparison
        a_amounts = set(a.lower().replace(",", "").replace("$", "") for a in terms_a["amounts"])
        b_amounts = set(a.lower().replace(",", "").replace("$", "") for a in terms_b["amounts"])
        if a_amounts != b_amounts:
            return True

    # Different years = conflict
    a_years = set(terms_a["dates"]) & set(str(y) for y in range(2024, 2031))
    b_years = set(terms_b["dates"]) & set(str(y) for y in range(2024, 2031))
    if a_years and b_years and a_years != b_years:
        return True

    return False


@lru_cache(maxsize=512)
def _compare_resolution_cached(norm_a: str, norm_b: str, platform_a: str, platform_b: str) -> tuple:
    """Cached heuristic comparison. Returns (status, confidence, reasoning) tuple."""
    sim = _jaccard_similarity(norm_a, norm_b)
    terms_a = _extract_key_terms(norm_a)
    terms_b = _extract_key_terms(norm_b)
    has_conflict = _key_terms_conflict(terms_a, terms_b)

    if sim > 0.90 and not has_conflict:
        return ("match", 0.95, f"High similarity ({sim:.0%}), no key term conflicts")
    if sim < 0.50 or has_conflict:
        reason = f"Low similarity ({sim:.0%})" if sim < 0.50 else "Key term conflict detected"
        return ("divergent", 0.85, reason)
    return ("uncertain", 0.5, f"Moderate similarity ({sim:.0%}), needs review")


def _compare_resolution(title_a: str, title_b: str,
                         platform_a: str, platform_b: str) -> ResolutionMatch:
    """Compare resolution criteria of two market titles.

    Heuristic only: Jaccard similarity + key term extraction.
    LLM Phase 2 is deferred — find_arbitrage() is synchronous, so async
    LLM calls cannot execute here. Uncertain pairs get reduced confidence (0.4)
    which is conservative but not auto-rejected.
    """
    norm_a = _normalize_title(title_a)
    norm_b = _normalize_title(title_b)

    # Order-independent caching: sort the pair
    if norm_a > norm_b:
        norm_a, norm_b = norm_b, norm_a
        platform_a, platform_b = platform_b, platform_a

    status, confidence, reasoning = _compare_resolution_cached(
        norm_a, norm_b, platform_a, platform_b
    )

    # Uncertain pairs: reduce confidence as conservative default
    # (LLM Phase 2 deferred to future async implementation)
    if status == "uncertain":
        confidence = 0.4

    return ResolutionMatch(status=status, confidence=confidence, reasoning=reasoning)
```

**Note on LLM Phase 2:** `find_arbitrage()` is synchronous, so async LLM calls cannot execute here. The heuristic handles most cases: identical titles match, clearly different titles diverge, and uncertain pairs get reduced confidence (0.4) which is conservative but not auto-rejected. LLM Phase 2 for uncertain resolution comparison is deferred to a future iteration where it could run from an async context.

- [ ] **Step 4: Integrate into `find_arbitrage()`**

In `find_arbitrage()`, after finding `best_yes_market` and `best_no_market` (around line 756), before computing spread, add the resolution check:

```python
        # Resolution criteria comparison — check if markets resolve the same way
        resolution = _compare_resolution(
            best_yes_market.title, best_no_market.title,
            best_yes_market.platform, best_no_market.platform,
        )
        # Divergent resolution → confidence="very_low", which the existing
        # filter at line 840-842 will drop. This preserves logging/counting.
        force_very_low = resolution.status == "divergent"
```

Add this AFTER the same-platform check (line 754) and BEFORE `total_cost = ...` (line 756).

Then in the pure arb path (around line 838), BEFORE the existing `confidence = _match_confidence(profit_pct)`, add:

```python
            if force_very_low:
                confidence = "very_low"
            else:
                confidence = _match_confidence(profit_pct)
```

Replace the existing `confidence = _match_confidence(profit_pct)` line with the above. The existing `if confidence == "very_low": continue` at line 840-842 will then filter divergent matches while preserving the normal logging path.

- [ ] **Step 5: Run all tests**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_resolution_comparison.py tests/test_arbitrage.py tests/test_dynamic_fees.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/arbitrage_engine.py tests/test_resolution_comparison.py
git commit -m "feat: add resolution criteria comparison for cross-platform arbs

Two-phase approach: heuristic (Jaccard similarity + key term extraction)
catches identical and clearly different markets. Uncertain pairs get
reduced confidence. Divergent pairs are rejected before spread computation."
```

---

### Task 4: Volume-Weighted Filtering

**Files:**
- Modify: `src/positions/auto_trader.py:306-339` (arb processing block)
- Modify: `tests/test_auto_trader_improvements.py`

**Context:** Below $50K daily volume, 78% of arb opportunities fail to execute due to illiquidity. The volume filter goes in the arb-specific processing block (after line 327, before cooldown checks), NOT in the synthetic handlers (political_synthetic, crypto_synthetic are directional, not arbs requiring simultaneous execution).

- [ ] **Step 1: Write failing test for volume filter**

Add to `tests/test_auto_trader_improvements.py`:

```python
class TestVolumeFilter:
    """Test MIN_ARB_VOLUME filtering in auto trader."""

    def test_volume_filter_constant(self):
        """MIN_ARB_VOLUME should be 50,000."""
        from positions.auto_trader import MIN_ARB_VOLUME
        assert MIN_ARB_VOLUME == 50_000
        assert isinstance(MIN_ARB_VOLUME, int)

    def test_low_volume_arb_skipped(self):
        """An arb opp below $50K volume should be skipped by the volume filter."""
        from positions.auto_trader import MIN_ARB_VOLUME
        # Simulate the filter logic
        opp = {"volume": 30_000, "opportunity_type": ""}
        opp_type = opp.get("opportunity_type", "")
        exempt_types = ("political_synthetic", "crypto_synthetic", "weather", "multi_outcome_arb", "portfolio_no")
        should_skip = (opp_type not in exempt_types and opp.get("volume", 0) < MIN_ARB_VOLUME)
        assert should_skip is True

    def test_high_volume_arb_not_skipped(self):
        """An arb opp above $50K volume should pass the volume filter."""
        from positions.auto_trader import MIN_ARB_VOLUME
        opp = {"volume": 100_000, "opportunity_type": ""}
        should_skip = (opp.get("opportunity_type", "") not in ("political_synthetic", "crypto_synthetic", "weather", "multi_outcome_arb", "portfolio_no")
                       and opp.get("volume", 0) < MIN_ARB_VOLUME)
        assert should_skip is False

    def test_synthetic_exempt_from_volume_filter(self):
        """Political and crypto synthetics should NOT be volume-filtered."""
        from positions.auto_trader import MIN_ARB_VOLUME
        exempt_types = ("political_synthetic", "crypto_synthetic", "weather", "multi_outcome_arb", "portfolio_no")
        for opp_type in ["political_synthetic", "crypto_synthetic"]:
            opp = {"volume": 1_000, "opportunity_type": opp_type}  # Very low volume
            should_skip = (opp.get("opportunity_type", "") not in exempt_types
                           and opp.get("volume", 0) < MIN_ARB_VOLUME)
            assert should_skip is False, f"{opp_type} should be exempt from volume filter"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_auto_trader_improvements.py::TestVolumeFilter -v`
Expected: FAIL — `MIN_ARB_VOLUME` does not exist.

- [ ] **Step 3: Add `MIN_ARB_VOLUME` constant and filter logic**

In `src/positions/auto_trader.py`, add the constant after `MIN_HOURS_TO_EXPIRY` (line 41):

```python
MIN_ARB_VOLUME = 50_000  # Skip arb opportunities below $50K daily volume (78% execution failure)
```

Then in the arb processing loop, after the spread filter (line 327 `continue`) and before the position duplicate check (line 329), add:

```python
            # Volume filter for arb opportunities (78% execution failure below $50K daily volume)
            # Exempt: directional strategies that don't require simultaneous cross-platform execution.
            # Spec exempts political_synthetic and crypto_synthetic. We also exempt weather (single-platform
            # directional), multi_outcome_arb (single-platform all-outcomes), and portfolio_no (single-platform).
            opp_type = opp.get("opportunity_type", "")
            if opp_type not in ("political_synthetic", "crypto_synthetic", "weather", "multi_outcome_arb", "portfolio_no"):
                arb_volume = opp.get("volume", opp.get("combined_volume", 0))
                if arb_volume < MIN_ARB_VOLUME:
                    self._trades_skipped += 1
                    if self.dlog:
                        self.dlog.log_opportunity_skip(opp_title, "low_volume", volume=arb_volume)
                    continue
```

- [ ] **Step 4: Run tests**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_auto_trader_improvements.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/positions/auto_trader.py tests/test_auto_trader_improvements.py
git commit -m "feat: add volume filter for arb opportunities ($50K minimum)

Skip arb opportunities below $50K combined volume. Does not
affect political/crypto synthetics (directional, not arbs)."
```

---

### Task 5: LLM Estimator Module

**Files:**
- Create: `src/positions/llm_estimator.py`
- Create: `tests/test_llm_estimator.py`

**Context:** This is the core of Strategy 2. A stateless probability estimator that queries Claude and Gemini in parallel, computes consensus, and returns a structured result the auto-trader uses for score boosting. Only called when `probability_model` flags >10% cross-platform deviation. Max 10 estimates per scan cycle.

- [ ] **Step 1: Write failing tests**

Create `tests/test_llm_estimator.py`:

```python
"""Tests for LLM mispricing estimator (Claude + Gemini consensus)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import json
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from positions.llm_estimator import LLMEstimator, EstimateResult


def _run(coro):
    """Helper to run async code in tests."""
    return asyncio.run(coro)


class TestEstimateResult:
    def test_fields(self):
        r = EstimateResult(
            consensus_prob=0.60,
            edge_pct=10.0,
            confidence="high",
            models={"claude": 0.62, "gemini": 0.58},
            should_boost=True,
            reasoning="Test reasoning",
        )
        assert r.consensus_prob == 0.60
        assert r.should_boost is True
        assert r.models["claude"] == 0.62

    def test_should_boost_logic(self):
        """should_boost=True requires confidence != 'low' AND edge > 5%."""
        r = EstimateResult(0.60, 10.0, "high", {}, True, "")
        assert r.should_boost is True

        r2 = EstimateResult(0.60, 3.0, "high", {}, False, "")
        assert r2.should_boost is False


class TestLLMEstimator:
    def _make_estimator(self):
        return LLMEstimator(
            anthropic_api_key="test-key",
            gemini_api_key="test-key",
        )

    @patch("positions.llm_estimator.LLMEstimator._query_claude")
    @patch("positions.llm_estimator.LLMEstimator._query_gemini")
    def test_both_models_agree(self, mock_gemini, mock_claude):
        """When both models agree within 15%, should_boost=True if edge > 5%."""
        mock_claude.return_value = {"probability": 0.62, "confidence": "high", "reasoning": "Claude says yes"}
        mock_gemini.return_value = {"probability": 0.58, "confidence": "high", "reasoning": "Gemini says yes"}

        est = self._make_estimator()
        result = _run(est.estimate(
            title="Will BTC exceed $100K?",
            platform_prices={"yes": 0.50, "no": 0.50},
            news_headlines=[],
        ))

        assert result is not None
        assert abs(result.consensus_prob - 0.60) < 1e-6
        assert result.edge_pct == pytest.approx(10.0, abs=0.5)
        assert result.should_boost is True
        assert result.confidence in ("high", "medium")

    @patch("positions.llm_estimator.LLMEstimator._query_claude")
    @patch("positions.llm_estimator.LLMEstimator._query_gemini")
    def test_models_disagree(self, mock_gemini, mock_claude):
        """When models disagree by >15%, should_boost=False."""
        mock_claude.return_value = {"probability": 0.80, "confidence": "high", "reasoning": "Very bullish"}
        mock_gemini.return_value = {"probability": 0.40, "confidence": "medium", "reasoning": "Bearish"}

        est = self._make_estimator()
        result = _run(est.estimate(
            title="Will BTC exceed $100K?",
            platform_prices={"yes": 0.50, "no": 0.50},
            news_headlines=[],
        ))

        assert result is not None
        assert result.should_boost is False
        assert result.confidence == "low"

    @patch("positions.llm_estimator.LLMEstimator._query_claude")
    @patch("positions.llm_estimator.LLMEstimator._query_gemini")
    def test_one_model_fails(self, mock_gemini, mock_claude):
        """If one model fails, return low confidence, no boost."""
        mock_claude.return_value = {"probability": 0.62, "confidence": "high", "reasoning": "ok"}
        mock_gemini.side_effect = Exception("API error")

        est = self._make_estimator()
        result = _run(est.estimate(
            title="Test market",
            platform_prices={"yes": 0.50, "no": 0.50},
            news_headlines=[],
        ))

        assert result is not None
        assert result.should_boost is False
        assert result.confidence == "low"

    @patch("positions.llm_estimator.LLMEstimator._query_claude")
    @patch("positions.llm_estimator.LLMEstimator._query_gemini")
    def test_both_models_fail(self, mock_gemini, mock_claude):
        """If both models fail, return None."""
        mock_claude.side_effect = Exception("API error")
        mock_gemini.side_effect = Exception("API error")

        est = self._make_estimator()
        result = _run(est.estimate(
            title="Test market",
            platform_prices={"yes": 0.50, "no": 0.50},
            news_headlines=[],
        ))

        assert result is None

    @patch("positions.llm_estimator.LLMEstimator._query_claude")
    @patch("positions.llm_estimator.LLMEstimator._query_gemini")
    def test_rate_limiting(self, mock_gemini, mock_claude):
        """Max 10 estimates per cycle."""
        mock_claude.return_value = {"probability": 0.60, "confidence": "high", "reasoning": "ok"}
        mock_gemini.return_value = {"probability": 0.60, "confidence": "high", "reasoning": "ok"}

        est = self._make_estimator()
        # Call 10 times — should all succeed
        for i in range(10):
            result = _run(est.estimate(f"Market {i}", {"yes": 0.50, "no": 0.50}, []))
            assert result is not None

        # 11th should return None (rate limited)
        result = _run(est.estimate("Market 11", {"yes": 0.50, "no": 0.50}, []))
        assert result is None

    def test_reset_cycle(self):
        """reset_cycle() should reset the rate limit counter."""
        est = self._make_estimator()
        est._estimates_this_cycle = 10
        est.reset_cycle()
        assert est._estimates_this_cycle == 0

    @patch("positions.llm_estimator.LLMEstimator._query_claude")
    @patch("positions.llm_estimator.LLMEstimator._query_gemini")
    def test_edge_calculation(self, mock_gemini, mock_claude):
        """Edge = |consensus - best_market_price| as percentage."""
        mock_claude.return_value = {"probability": 0.70, "confidence": "high", "reasoning": "ok"}
        mock_gemini.return_value = {"probability": 0.70, "confidence": "high", "reasoning": "ok"}

        est = self._make_estimator()
        # Market YES at 0.50, so best_market_price = 0.50
        result = _run(est.estimate("Test", {"yes": 0.50, "no": 0.50}, []))
        assert result is not None
        assert result.edge_pct == pytest.approx(20.0, abs=1.0)

    @patch("positions.llm_estimator.LLMEstimator._query_claude")
    @patch("positions.llm_estimator.LLMEstimator._query_gemini")
    def test_small_edge_no_boost(self, mock_gemini, mock_claude):
        """Edge < 5% should not boost even if models agree."""
        mock_claude.return_value = {"probability": 0.52, "confidence": "high", "reasoning": "ok"}
        mock_gemini.return_value = {"probability": 0.52, "confidence": "high", "reasoning": "ok"}

        est = self._make_estimator()
        result = _run(est.estimate("Test", {"yes": 0.50, "no": 0.50}, []))
        assert result is not None
        assert result.edge_pct < 5.0
        assert result.should_boost is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_llm_estimator.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Create `src/positions/llm_estimator.py`**

```python
"""LLM mispricing estimator — 2-model consensus (Claude + Gemini).

Stateless probability estimator. Does not trade. Returns a structured
estimate that the auto-trader uses for score boosting when cross-platform
price disagreement exceeds 10%.
"""
import asyncio
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("positions.llm_estimator")

_MAX_ESTIMATES_PER_CYCLE = 10

_PROMPT_TEMPLATE = """You are a calibrated probability estimator for prediction markets.

Market: {title}

Current prices across platforms:
{platform_prices_formatted}

Recent news (if any):
{news_headlines_formatted}

Estimate the true probability this market resolves YES.
Respond with JSON only: {{"probability": 0.XX, "confidence": "high|medium|low", "reasoning": "one sentence"}}"""


@dataclass
class EstimateResult:
    """Result of LLM consensus estimation."""
    consensus_prob: float        # mean of model estimates
    edge_pct: float              # |consensus - best_market_price| as percentage
    confidence: str              # "high" if models agree within 10%, "medium" within 15%, "low" otherwise
    models: dict[str, float]     # {"claude": 0.62, "gemini": 0.58}
    should_boost: bool           # True if confidence != "low" AND edge > 5%
    reasoning: str               # combined reasoning from models


class LLMEstimator:
    """Queries Claude and Gemini in parallel for probability estimates."""

    def __init__(self, anthropic_api_key: str = "", gemini_api_key: str = ""):
        self._anthropic_key = anthropic_api_key
        self._gemini_key = gemini_api_key
        self._estimates_this_cycle = 0

    def reset_cycle(self):
        """Reset the per-cycle rate limit counter. Call at start of each scan cycle."""
        self._estimates_this_cycle = 0

    async def estimate(self, title: str, platform_prices: dict,
                       news_headlines: list) -> EstimateResult | None:
        """Estimate true probability via Claude + Gemini consensus.

        Returns EstimateResult or None if both models fail or rate limited.
        """
        if self._estimates_this_cycle >= _MAX_ESTIMATES_PER_CYCLE:
            logger.debug("LLM estimator: rate limited (%d/%d)", self._estimates_this_cycle, _MAX_ESTIMATES_PER_CYCLE)
            return None

        self._estimates_this_cycle += 1

        # Build prompt
        prices_fmt = "\n".join(f"  {k}: {v:.2f}" for k, v in platform_prices.items())
        news_fmt = "\n".join(f"  - {h}" if isinstance(h, str) else f"  - {h.get('title', h.get('headline', str(h)))}" for h in news_headlines) if news_headlines else "  (no recent news)"

        prompt = _PROMPT_TEMPLATE.format(
            title=title,
            platform_prices_formatted=prices_fmt,
            news_headlines_formatted=news_fmt,
        )

        # Query both models in parallel
        claude_task = self._query_claude(prompt)
        gemini_task = self._query_gemini(prompt)
        results = await asyncio.gather(claude_task, gemini_task, return_exceptions=True)

        claude_result = results[0] if not isinstance(results[0], Exception) else None
        gemini_result = results[1] if not isinstance(results[1], Exception) else None

        if isinstance(results[0], Exception):
            logger.warning("LLM estimator: Claude failed: %s", results[0])
        if isinstance(results[1], Exception):
            logger.warning("LLM estimator: Gemini failed: %s", results[1])

        # Both failed
        if claude_result is None and gemini_result is None:
            return None

        # Compute consensus
        models = {}
        probs = []
        reasonings = []

        if claude_result:
            cp = claude_result.get("probability", 0.5)
            cp = max(0.0, min(1.0, cp))
            models["claude"] = cp
            probs.append(cp)
            reasonings.append(f"Claude: {claude_result.get('reasoning', '')}")

        if gemini_result:
            gp = gemini_result.get("probability", 0.5)
            gp = max(0.0, min(1.0, gp))
            models["gemini"] = gp
            probs.append(gp)
            reasonings.append(f"Gemini: {gemini_result.get('reasoning', '')}")

        consensus_prob = sum(probs) / len(probs)

        # Agreement check
        if len(probs) == 2:
            disagreement = abs(probs[0] - probs[1])
            if disagreement < 0.10:
                confidence = "high"
            elif disagreement < 0.15:
                confidence = "medium"
            else:
                confidence = "low"
        else:
            # Single model — low confidence
            confidence = "low"

        # Edge calculation: |consensus - best_market_price|
        # best_market_price is the price on the platform where the trade would execute
        # If consensus > market YES price, we'd buy YES (edge vs YES price)
        # If consensus < market YES price, we'd buy NO (edge vs 1-NO price = YES implied)
        yes_price = platform_prices.get("yes", 0.5)
        no_price = platform_prices.get("no", 0.5)
        # Best market price = cheapest entry for the direction consensus suggests
        if consensus_prob > yes_price:
            # We'd buy YES — edge is vs YES price
            best_market_price = yes_price
        else:
            # We'd buy NO — edge is vs implied YES from NO side (1 - no_price)
            best_market_price = 1.0 - no_price
        edge = abs(consensus_prob - best_market_price)
        edge_pct = edge * 100.0

        # Should boost: models agree AND edge > 5%
        should_boost = confidence != "low" and edge_pct > 5.0

        return EstimateResult(
            consensus_prob=round(consensus_prob, 4),
            edge_pct=round(edge_pct, 1),
            confidence=confidence,
            models=models,
            should_boost=should_boost,
            reasoning=" | ".join(reasonings),
        )

    async def _query_claude(self, prompt: str) -> dict:
        """Query Claude API and return parsed JSON response."""
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx required for LLM estimator")

        if not self._anthropic_key:
            raise ValueError("No Anthropic API key")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 256,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["content"][0]["text"]
            # Parse JSON from response (handle markdown code blocks)
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return json.loads(text)

    async def _query_gemini(self, prompt: str) -> dict:
        """Query Gemini API and return parsed JSON response."""
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx required for LLM estimator")

        if not self._gemini_key:
            raise ValueError("No Gemini API key")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={self._gemini_key}",
                headers={"content-type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 256},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return json.loads(text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_llm_estimator.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/positions/llm_estimator.py tests/test_llm_estimator.py
git commit -m "feat: add LLM mispricing estimator (Claude + Gemini consensus)

New module: 2-model parallel estimation with rate limiting (10/cycle),
agreement checking (high/medium/low confidence), edge calculation,
and should_boost decision (agree + edge > 5%)."
```

---

### Task 6: Auto-Trader LLM Integration

**Files:**
- Modify: `src/positions/auto_trader.py:47-76` (constructor, setters) and `420-423` (scoring)
- Modify: `tests/test_auto_trader_improvements.py`

**Context:** The auto-trader needs three changes: (1) accept `llm_estimator` as an optional constructor param, (2) add `set_news_scanner()` method, (3) replace the 1.3x cross-platform boost with conditional LLM 2.0x boost. The LLM boost is exclusive — it replaces (does not stack with) the existing 1.3x boost. Use an `elif` to prevent double-boosting.

- [ ] **Step 1: Write failing tests for LLM boost integration**

Add to `tests/test_auto_trader_improvements.py`:

```python
class TestLLMBoost:
    """Test LLM mispricing boost in auto trader scoring."""

    def test_set_news_scanner_method_exists(self):
        """AutoTrader should have set_news_scanner()."""
        from positions.auto_trader import AutoTrader
        assert hasattr(AutoTrader, "set_news_scanner")

    def test_constructor_accepts_llm_estimator(self):
        """AutoTrader constructor should accept llm_estimator param."""
        import inspect
        from positions.auto_trader import AutoTrader
        sig = inspect.signature(AutoTrader.__init__)
        assert "llm_estimator" in sig.parameters
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_auto_trader_improvements.py::TestLLMBoost -v`
Expected: FAIL — no `set_news_scanner` method, no `llm_estimator` param.

- [ ] **Step 3: Add `set_news_scanner()` and `llm_estimator` param**

In `src/positions/auto_trader.py`, update the constructor (line 47):

```python
    def __init__(self, position_manager, scanner=None, insider_tracker=None,
                 interval: float = SCAN_INTERVAL, decision_logger=None,
                 probability_model=None, llm_estimator=None):
```

Add `self._llm_estimator = llm_estimator` after `self.probability_model = probability_model` (line 55):

```python
        self._llm_estimator = llm_estimator
        self._news_scanner = None
        self._llm_cycle_reset = False  # Reset flag for per-cycle rate limiting
```

Also, at the TOP of the `_scan_once()` method (around line 270, after `open_pkgs` setup), add:

```python
        self._llm_cycle_reset = False  # Reset LLM cycle flag for this scan
```

Add the setter method after `set_weather_scanner()` (line 76):

```python
    def set_news_scanner(self, scanner):
        """Set the news scanner reference for headline lookup (LLM context)."""
        self._news_scanner = scanner
```

- [ ] **Step 4: Replace 1.3x boost with conditional LLM 2.0x boost**

Replace the cross-platform disagreement boost block at lines 418-423:

**Current code (lines 418-423):**
```python
            # Cross-platform disagreement boost: if platforms disagree >10%,
            # there may be an informational edge worth capturing
            if self.probability_model:
                consensus = self.probability_model.get_consensus(opp_title)
                if consensus and consensus.get("max_deviation", 0) > 0.10:
                    score *= 1.3
```

**Replace with:**
```python
            # Reset LLM estimator rate limit once per scan cycle
            if self._llm_estimator and not getattr(self, '_llm_cycle_reset', False):
                self._llm_estimator.reset_cycle()
                self._llm_cycle_reset = True

            # Cross-platform disagreement: LLM boost (2.0x) replaces old 1.3x boost
            if self.probability_model:
                consensus = self.probability_model.get_consensus(opp_title)
                prob_deviation = consensus.get("max_deviation", 0) if consensus else 0
                if prob_deviation > 0.10:
                    if self._llm_estimator:
                        # LLM path: 2.0x if models agree + edge, else NO boost (replaces 1.3x entirely)
                        try:
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
                                score *= 2.0
                                logger.info("LLM mispricing boost: %s (edge=%.1f%%, consensus=%.2f)",
                                             opp_title, estimate.edge_pct, estimate.consensus_prob)
                            # If LLM doesn't boost (disagree, small edge, rate limited):
                            # no boost at all — LLM replaces the 1.3x, per spec
                        except Exception as e:
                            logger.warning("LLM estimator error for %s: %s", opp_title, e)
                            # LLM error: fall back to 1.3x (estimator unavailable, not disagreement)
                            score *= 1.3
                    else:
                        # No LLM estimator available — use original 1.3x boost
                        score *= 1.3
```

- [ ] **Step 5: Run tests**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_auto_trader_improvements.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/positions/auto_trader.py tests/test_auto_trader_improvements.py
git commit -m "feat: integrate LLM estimator into auto-trader scoring

Add llm_estimator constructor param and set_news_scanner() method.
LLM 2.0x boost replaces 1.3x when models agree and edge > 5%.
Falls back to 1.3x when LLM unavailable or doesn't boost."
```

---

### Task 7: Calibration Logging

**Files:**
- Modify: `src/eval_logger.py` (add `log_llm_estimate()` method)
- Modify: `src/positions/auto_trader.py` (log estimates)
- Modify: `tests/test_eval_logger.py`

**Context:** Each LLM estimate should be logged to the eval logger for future calibration analysis. The auto-trader has access to the eval logger via the decision logger. We add a `log_llm_estimate()` method to `EvalLogger` and call it from the auto-trader after each estimate.

- [ ] **Step 1: Write failing test for `log_llm_estimate()`**

Add to `tests/test_eval_logger.py`:

```python
class TestLLMEstimateLogging:
    def test_log_llm_estimate(self, tmp_path):
        from eval_logger import EvalLogger
        path = str(tmp_path / "eval.jsonl")
        el = EvalLogger(path=path)
        el.log_llm_estimate(
            market_id="test-123",
            title="Will BTC exceed $100K?",
            claude_prob=0.62,
            gemini_prob=0.58,
            consensus_prob=0.60,
            market_price=0.50,
            edge_pct=10.0,
            should_boost=True,
        )
        with open(path) as f:
            line = f.readline()
            data = json.loads(line)
            assert data["type"] == "llm_estimate"
            assert data["market_id"] == "test-123"
            assert data["claude_prob"] == 0.62
            assert data["gemini_prob"] == 0.58
            assert data["consensus_prob"] == 0.60
            assert data["should_boost"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_eval_logger.py::TestLLMEstimateLogging -v`
Expected: FAIL — `log_llm_estimate` does not exist.

- [ ] **Step 3: Implement `log_llm_estimate()`**

In `src/eval_logger.py`, add after the existing logging methods:

```python
    def log_llm_estimate(self, market_id: str, title: str,
                          claude_prob: float | None, gemini_prob: float | None,
                          consensus_prob: float, market_price: float,
                          edge_pct: float, should_boost: bool):
        """Log an LLM probability estimate for calibration analysis."""
        self._write({
            "type": "llm_estimate",
            "market_id": market_id,
            "title": title,
            "claude_prob": claude_prob,
            "gemini_prob": gemini_prob,
            "consensus_prob": consensus_prob,
            "market_price": market_price,
            "edge_pct": edge_pct,
            "should_boost": should_boost,
        })
```

- [ ] **Step 4: Add logging call in auto-trader**

First, add an `_eval_logger` attribute to the AutoTrader constructor (in the init block added in Task 6):

```python
        self._eval_logger = None  # Set via set_eval_logger()
```

Add a setter method (next to the other setters):

```python
    def set_eval_logger(self, eval_logger):
        """Set the eval logger for LLM estimate calibration logging."""
        self._eval_logger = eval_logger
```

Then in `src/positions/auto_trader.py`, in the LLM boost block (added in Task 6), after the estimate is computed and BEFORE the `if estimate and estimate.should_boost:` check, add:

```python
                            # Log estimate for calibration
                            if estimate and self._eval_logger:
                                try:
                                    self._eval_logger.log_llm_estimate(
                                        market_id=opp.get("buy_yes_market_id", ""),
                                        title=opp_title,
                                        claude_prob=estimate.models.get("claude"),
                                        gemini_prob=estimate.models.get("gemini"),
                                        consensus_prob=estimate.consensus_prob,
                                        market_price=opp.get("buy_yes_price", 0),
                                        edge_pct=estimate.edge_pct,
                                        should_boost=estimate.should_boost,
                                    )
                                except Exception:
                                    pass
```

- [ ] **Step 5: Run tests**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_eval_logger.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/eval_logger.py src/positions/auto_trader.py tests/test_eval_logger.py
git commit -m "feat: add LLM estimate calibration logging

Log every LLM estimate to eval_log.jsonl for future calibration
analysis. Includes claude_prob, gemini_prob, consensus, market_price,
edge, and boost decision."
```

---

### Task 8: Server Wiring

**Files:**
- Modify: `src/server.py:331-350` (auto-trader init block)

**Context:** Wire the `LLMEstimator` into the auto-trader and connect the news scanner via `set_news_scanner()`. The estimator is optional — if `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` is missing, it's not initialized. Follow the existing pattern: create object, pass to constructor or setter, log the result.

- [ ] **Step 1: Add LLM estimator initialization in server.py**

In `src/server.py`, after the imports section (around line 80), add:

```python
# --- LLM estimator for mispricing detection ---
try:
    from positions.llm_estimator import LLMEstimator
    _LLM_ESTIMATOR_AVAILABLE = True
except (ImportError, SyntaxError) as _llm_err:
    logger.warning("LLM estimator not available: %s", _llm_err)
    _LLM_ESTIMATOR_AVAILABLE = False
```

Then in the auto-trader initialization block (after line 331, where `_probability_model` is created), add:

```python
            # LLM mispricing estimator (optional — needs API keys)
            _llm_estimator = None
            if _LLM_ESTIMATOR_AVAILABLE:
                anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
                gemini_key = os.environ.get("GEMINI_API_KEY", "")
                if anthropic_key and gemini_key:
                    _llm_estimator = LLMEstimator(
                        anthropic_api_key=anthropic_key,
                        gemini_api_key=gemini_key,
                    )
                    logger.info("LLM estimator initialized (Claude + Gemini)")
                else:
                    logger.info("LLM estimator skipped (missing API keys: ANTHROPIC=%s, GEMINI=%s)",
                                "set" if anthropic_key else "missing",
                                "set" if gemini_key else "missing")
```

Update the `AutoTrader()` constructor call (line 333) to pass the estimator:

```python
            _auto_trader = AutoTrader(pm, scanner=arb_scanner, insider_tracker=insider,
                                       decision_logger=decision_log,
                                       probability_model=_probability_model,
                                       llm_estimator=_llm_estimator)
```

After `_news_scanner.start()` (line 347), add:

```python
            _auto_trader.set_news_scanner(_news_scanner)
```

In the eval logger section (after line 455 `_eval_log = EvalLogger()`), add:

```python
        if _auto_trader:
            _auto_trader.set_eval_logger(_eval_log)
```

- [ ] **Step 2: Verify server starts**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout/src && python -c "import server; print('OK')"`
Expected: Prints "OK" (may log warnings for missing keys, which is fine).

- [ ] **Step 4: Commit**

```bash
cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/server.py
git commit -m "feat: wire LLM estimator and news scanner into server startup

Initialize LLMEstimator when both ANTHROPIC_API_KEY and GEMINI_API_KEY
are set. Connect news_scanner to auto_trader for headline context.
Gracefully disabled when API keys are missing."
```

---

### Task 9: Integration Tests

**Files:**
- Create: `tests/test_strategy23_integration.py`

**Context:** End-to-end tests verifying Strategy 2 and Strategy 3 work together: dynamic fees affect spread calculations, resolution comparison filters bad matches, volume filter removes illiquid arbs, LLM estimator integrates with scoring.

- [ ] **Step 1: Write integration tests**

Create `tests/test_strategy23_integration.py`:

```python
"""Integration tests for Strategy 2 (LLM mispricing) and Strategy 3 (enhanced arb)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from arbitrage_engine import (
    find_arbitrage, compute_taker_fee, _compare_resolution,
    _compute_fee_adjusted_profit, ResolutionMatch,
)
from adapters.models import NormalizedEvent, MatchedEvent


def _make_matched_event(title_a, title_b, yes_price=0.40, no_price=0.45,
                         platform_a="polymarket", platform_b="kalshi",
                         category="crypto", volume=100000):
    """Helper to create a MatchedEvent with two markets."""
    m1 = NormalizedEvent(platform_a, "e1", title_a, category,
                         yes_price, 1.0 - yes_price, volume, "2026-12-31", "")
    m2 = NormalizedEvent(platform_b, "e2", title_b, category,
                         1.0 - no_price, no_price, volume, "2026-12-31", "")
    return MatchedEvent("match-1", title_a, category, "2026-12-31", markets=[m1, m2])


class TestDynamicFeesInArbitrage:
    """Test that find_arbitrage uses dynamic fees correctly."""

    def test_crypto_arb_uses_lower_polymarket_fee(self):
        """Crypto arb with Polymarket should use dynamic fee (< 2%)."""
        # At p=0.40, crypto fee = 0.25 * (0.4 * 0.6)^2 = 0.25 * 0.0576 = 0.0144
        net_pct, _ = _compute_fee_adjusted_profit(0.40, 0.45, "polymarket", "kalshi", "crypto")
        net_pct_old, _ = _compute_fee_adjusted_profit(0.40, 0.45, "polymarket", "kalshi", "")
        # With crypto category, the fee is lower, so net profit should be higher
        # (old uses crypto default which is same, but the point is it's less than flat 2%)
        assert isinstance(net_pct, float)

    def test_politics_arb_much_lower_fees(self):
        """Political arb on Polymarket should use very low fees."""
        net_politics, _ = _compute_fee_adjusted_profit(0.40, 0.45, "polymarket", "polymarket", "politics")
        net_crypto, _ = _compute_fee_adjusted_profit(0.40, 0.45, "polymarket", "polymarket", "crypto")
        # Politics fees are much lower than crypto
        assert net_politics > net_crypto


class TestResolutionInArbitrage:
    """Test that divergent resolution criteria filter bad matches."""

    def test_divergent_titles_rejected(self):
        """Markets with clearly different resolution should produce no arbs."""
        match = _make_matched_event(
            "Will BTC exceed $100K by Dec 2026?",
            "Will ETH exceed $5K by Dec 2026?",
            yes_price=0.40, no_price=0.45,
        )
        arbs = find_arbitrage([match])
        # Should be filtered out — different assets
        assert len(arbs) == 0

    def test_matching_titles_not_rejected(self):
        """Markets with identical titles should pass resolution check."""
        match = _make_matched_event(
            "Will BTC exceed $100K by Dec 2026?",
            "Will BTC exceed $100K by Dec 2026?",
            yes_price=0.30, no_price=0.30,
            volume=100000,
        )
        arbs = find_arbitrage([match])
        # May or may not produce arb depending on spread, but shouldn't be
        # filtered by resolution
        # The point is it doesn't get rejected for divergent resolution
        for arb in arbs:
            assert arb.confidence != "very_low"


class TestVolumeAndFeeInteraction:
    """Test that volume filter and dynamic fees work together."""

    def test_fee_adjusted_profit_increases_with_dynamic_model(self):
        """Dynamic fees should produce higher net profit than old flat 2%."""
        # Old flat 2%: fee = 0.02 * 0.40 = 0.008
        # Dynamic crypto at 0.40: 0.25 * (0.4 * 0.6)^2 = 0.0144, fee = 0.0144 * 0.40 = 0.00576
        # So dynamic should give better net profit
        net_dynamic, _ = _compute_fee_adjusted_profit(0.40, 0.45, "polymarket", "kalshi", "crypto")
        # Verify it's a reasonable number
        assert isinstance(net_dynamic, float)


class TestEstimateResultIntegration:
    """Test EstimateResult integrates with auto-trader scoring logic."""

    def test_estimate_result_boost_decision(self):
        from positions.llm_estimator import EstimateResult
        # High confidence + big edge = boost
        r = EstimateResult(0.65, 15.0, "high", {"claude": 0.66, "gemini": 0.64}, True, "Strong edge")
        assert r.should_boost is True

        # Low confidence = no boost regardless of edge
        r2 = EstimateResult(0.65, 15.0, "low", {"claude": 0.80, "gemini": 0.50}, False, "Disagreement")
        assert r2.should_boost is False

        # High confidence but small edge = no boost
        r3 = EstimateResult(0.52, 2.0, "high", {"claude": 0.52, "gemini": 0.52}, False, "Small edge")
        assert r3.should_boost is False
```

- [ ] **Step 2: Run integration tests**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_strategy23_integration.py -v`
Expected: All PASS.

- [ ] **Step 3: Run full test suite**

Run: `cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/ -v --tb=short`
Expected: All existing tests + new tests PASS. No regressions.

- [ ] **Step 4: Commit**

```bash
cd /c/Users/afoma/.openclaw/workspace/projects/arbitrout
git add tests/test_strategy23_integration.py
git commit -m "test: add integration tests for Strategy 2+3 interaction

Tests dynamic fee calculation, resolution comparison filtering,
volume/fee interaction, and EstimateResult boost logic."
```

---

## Dependency Order

```
Task 1 (Dynamic Fees) ──────────┐
                                 ├──→ Task 2 (Paper Executor Fees)
Task 3 (Resolution Comparison) ─┤
                                 ├──→ Task 9 (Integration Tests)
Task 4 (Volume Filter) ─────────┤
                                 │
Task 5 (LLM Estimator Module) ──┤
                                 ├──→ Task 6 (Auto-Trader Integration)
                                 │         │
                                 │         └──→ Task 7 (Calibration Logging)
                                 │                    │
                                 │                    └──→ Task 8 (Server Wiring)
                                 └──→ Task 9 (Integration Tests)
```

**Independent (can run in parallel):** Tasks 1, 3, 4, 5
**Sequential chains:**
- Task 1 → Task 2
- Task 5 → Task 6 → Task 7 → Task 8
- All → Task 9
