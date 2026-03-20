# Fee-Aware Arbitrage Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix false-positive arbitrage opportunities by adding fee-adjusted profit calculation, implied-probability discrepancy detection, and opportunity deduplication.

**Architecture:** Add a fee computation layer inside `find_arbitrage()` that calculates post-fee guaranteed profit using platform-specific taker fees and PredictIt's 10% profit tax. Add a confidence scorer that flags large implied-probability discrepancies as likely false matches. Dedup results by event-id pair. Expose `net_profit_pct` and `confidence` in the API response.

**Tech Stack:** Python 3.14, pytest, existing dataclass models

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/arbitrage_engine.py` | Modify (lines 1-515) | Add fee constants, `_compute_fee_adjusted_profit()`, `_match_confidence()`, update `find_arbitrage()` to filter/dedup/enrich |
| `src/adapters/models.py` | Modify (lines 66-112) | Add `net_profit_pct`, `confidence` fields to `ArbitrageOpportunity`, update `to_dict()` |
| `src/static/js/arbitrout.js` | Modify (lines 286-356) | Show net profit and confidence badge in opportunity list and detail view |
| `tests/test_arbitrage.py` | Modify | Add tests for fee-adjusted filtering, confidence scoring, dedup, PredictIt profit tax |

---

### Task 1: Add fee-adjusted profit and confidence to ArbitrageOpportunity model

**Files:**
- Modify: `src/adapters/models.py:66-112`
- Test: `tests/test_arbitrage.py`

- [ ] **Step 1: Write failing tests for new model fields**

```python
# In tests/test_arbitrage.py, add to TestArbitrageOpportunity:

def test_to_dict_includes_net_profit_and_confidence(self):
    """to_dict should include net_profit_pct and confidence."""
    ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.65)
    ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55)
    matched = _make_matched([ev_a, ev_b])
    opps = find_arbitrage([matched])
    d = opps[0].to_dict()
    assert "net_profit_pct" in d
    assert "confidence" in d
    assert d["confidence"] in ("high", "medium", "low", "very_low")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout && python -m pytest tests/test_arbitrage.py::TestArbitrageOpportunity::test_to_dict_includes_net_profit_and_confidence -v`
Expected: FAIL — `net_profit_pct` and `confidence` not in dict

- [ ] **Step 3: Add fields to ArbitrageOpportunity**

In `src/adapters/models.py`, add to the `ArbitrageOpportunity` dataclass:
```python
net_profit_pct: float = 0.0       # Guaranteed profit % after all platform fees
confidence: str = "medium"         # "high", "medium", "low", "very_low"
```

Update `to_dict()` to include them:
```python
d["net_profit_pct"] = round(self.net_profit_pct, 2)
d["confidence"] = self.confidence
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout && python -m pytest tests/test_arbitrage.py::TestArbitrageOpportunity -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/adapters/models.py tests/test_arbitrage.py
git commit -m "feat(models): add net_profit_pct and confidence fields to ArbitrageOpportunity"
```

---

### Task 2: Add fee computation and confidence scoring to arbitrage engine

**Files:**
- Modify: `src/arbitrage_engine.py:1-27` (add constants + helpers before the calculator section)
- Test: `tests/test_arbitrage.py`

- [ ] **Step 1: Write failing tests for fee-adjusted profit**

```python
# In tests/test_arbitrage.py, add new class:

class TestFeeAdjustedProfit:
    """Tests for fee-aware arbitrage filtering."""

    def test_small_spread_with_predictit_filtered_out(self):
        """0.6% spread with PredictIt NO at 99c should be filtered (fees > profit)."""
        ev_a = _make_event("polymarket", "p1", "Peru Election: Forsyth", yes=0.004, no=0.996)
        ev_b = _make_event("predictit", "pi1", "Peru Election: Forsyth", yes=0.01, no=0.99)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        # After PM 2% taker + PI 10% profit tax, this is a loss
        assert len(opps) == 0

    def test_large_spread_survives_fees(self):
        """16.8% spread with Polymarket+Limitless should survive after fees."""
        ev_a = _make_event("polymarket", "p1", "Corners 7+", yes=0.536, no=0.464)
        ev_b = _make_event("limitless", "l1", "Corners 7+", yes=0.704, no=0.296)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        assert len(opps) == 1
        opp = opps[0]
        # Net profit should be less than gross but still positive
        assert opp.net_profit_pct > 0
        assert opp.net_profit_pct < opp.profit_pct

    def test_predictit_profit_tax_modeled(self):
        """PredictIt 10% profit tax should reduce net payout on cheap contracts."""
        # Buy YES on PI at 1c, NO on Kalshi at 9c = 90% gross spread
        # PI profit tax: 10% of (1.00 - 0.01) = 0.099, net payout = 0.901
        ev_a = _make_event("predictit", "pi1", "CA Gov: Padilla", yes=0.01, no=0.99)
        ev_b = _make_event("kalshi", "k1", "CA Gov: Padilla", yes=0.90, no=0.09)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        if opps:  # May be filtered by confidence
            assert opps[0].net_profit_pct < opps[0].profit_pct

    def test_kalshi_1pct_spread_with_predictit_5pct_filtered(self):
        """1% spread Kalshi+PredictIt should be negative after fees."""
        ev_a = _make_event("predictit", "pi1", "Cabinet: Loeffler", yes=0.01, no=0.99)
        ev_b = _make_event("kalshi", "k1", "Cabinet: Loeffler", yes=0.99, no=0.98)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        assert len(opps) == 0  # Filtered out


class TestConfidenceScoring:
    """Tests for match confidence scoring."""

    def test_huge_spread_gets_very_low_confidence(self):
        """91% spread = almost certainly a false match."""
        ev_a = _make_event("polymarket", "p1", "WV Senate: Republican", yes=0.015, no=0.985)
        ev_b = _make_event("predictit", "pi1", "WV Senate: Republican", yes=0.95, no=0.07)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        # Should either be filtered entirely or marked very_low confidence
        for opp in opps:
            assert opp.confidence == "very_low"

    def test_moderate_spread_gets_medium_confidence(self):
        """16% spread gets medium confidence."""
        ev_a = _make_event("polymarket", "p1", "Corners 7+", yes=0.536, no=0.464)
        ev_b = _make_event("limitless", "l1", "Corners 7+", yes=0.704, no=0.296)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        assert len(opps) == 1
        assert opps[0].confidence in ("medium", "high")

    def test_small_spread_gets_high_confidence(self):
        """5% spread between two agreeing platforms = high confidence."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.65)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55)
        matched = _make_matched([ev_a, ev_b])
        opps = find_arbitrage([matched])
        assert len(opps) == 1
        assert opps[0].confidence == "high"


class TestDeduplication:
    """Tests for opportunity deduplication."""

    def test_duplicate_event_ids_deduped(self):
        """Same event_id pair should only appear once."""
        ev_a = _make_event("polymarket", "p1", "Event X", yes=0.40, no=0.65)
        ev_b = _make_event("kalshi", "k1", "Event X", yes=0.50, no=0.55)
        # Create two matched events that would produce the same opportunity
        match1 = _make_matched([ev_a, ev_b], "Event X")
        match1.match_id = "m1"
        match2 = _make_matched([ev_a, ev_b], "Event X duplicate")
        match2.match_id = "m2"
        opps = find_arbitrage([match1, match2])
        # Should only have 1, not 2
        assert len(opps) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout && python -m pytest tests/test_arbitrage.py::TestFeeAdjustedProfit tests/test_arbitrage.py::TestConfidenceScoring tests/test_arbitrage.py::TestDeduplication -v`
Expected: Multiple FAILs

- [ ] **Step 3: Add fee constants and helper functions to arbitrage_engine.py**

Add after line 17 (after imports, before `DATA_DIR`):

```python
# ============================================================
# PLATFORM FEE RATES (for opportunity filtering)
# ============================================================
# Taker fees (market orders) applied at entry
_TAKER_FEES = {
    "polymarket": 0.02,
    "kalshi": 0.01,
    "predictit": 0.0,       # No entry fee; profit taxed at resolution
    "limitless": 0.01,
    "robinhood": 0.0,
    "coinbase_spot": 0.006,
    "kraken": 0.0026,
}
_DEFAULT_TAKER_FEE = 0.02
_PREDICTIT_PROFIT_TAX = 0.10  # 10% of profits at contract resolution


def _compute_fee_adjusted_profit(yes_price: float, no_price: float,
                                  yes_platform: str, no_platform: str) -> tuple[float, float]:
    """Compute guaranteed profit after all platform fees.

    Returns (net_profit_pct, total_cost_with_fees).

    For PredictIt: 10% tax on profits at resolution (payout < $1.00).
    For others: taker_fee_rate * price at entry.
    """
    yes_fee = yes_price * _TAKER_FEES.get(yes_platform, _DEFAULT_TAKER_FEE)
    no_fee = no_price * _TAKER_FEES.get(no_platform, _DEFAULT_TAKER_FEE)
    total_cost = yes_price + no_price + yes_fee + no_fee

    # Resolution payouts — PredictIt takes 10% of profits on the winning leg
    yes_payout = 1.0
    if yes_platform == "predictit":
        yes_payout = 1.0 - _PREDICTIT_PROFIT_TAX * (1.0 - yes_price)
    no_payout = 1.0
    if no_platform == "predictit":
        no_payout = 1.0 - _PREDICTIT_PROFIT_TAX * (1.0 - no_price)

    # Guaranteed profit = worst-case scenario
    worst_payout = min(yes_payout, no_payout)
    worst_profit = worst_payout - total_cost
    net_pct = (worst_profit / total_cost * 100) if total_cost > 0 else 0.0

    return net_pct, total_cost


def _match_confidence(profit_pct: float) -> str:
    """Estimate confidence that a detected spread is a real arbitrage.

    Huge spreads (>30%) on prediction markets almost always indicate
    a false match (different contracts matched as the same event),
    not a genuine arbitrage opportunity.
    """
    if profit_pct > 50:
        return "very_low"
    if profit_pct > 30:
        return "low"
    if profit_pct > 15:
        return "medium"
    return "high"
```

- [ ] **Step 4: Update find_arbitrage() to use fee filtering, confidence, and dedup**

In `find_arbitrage()`, after computing `spread` and `profit_pct` in the pure-arb branch (line ~494), add:

```python
# Fee-adjusted profit
net_pct, _ = _compute_fee_adjusted_profit(
    best_yes_market.yes_price, best_no_market.no_price,
    best_yes_market.platform, best_no_market.platform,
)
# Skip if guaranteed loss after fees
if net_pct <= 0:
    continue

confidence = _match_confidence(profit_pct)
```

And pass `net_profit_pct=round(net_pct, 2)` and `confidence=confidence` to the `ArbitrageOpportunity` constructor.

For the synthetic branch, do the same fee computation and assign `confidence`.

At the end, before `opportunities.sort(...)`, add deduplication:

```python
# Deduplicate by (yes_event_id, no_event_id) pair
seen = set()
deduped = []
for opp in opportunities:
    key = (opp.buy_yes_event_id, opp.buy_no_event_id)
    if key not in seen:
        seen.add(key)
        deduped.append(opp)
opportunities = deduped
```

- [ ] **Step 5: Run all arbitrage tests**

Run: `cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout && python -m pytest tests/test_arbitrage.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add src/arbitrage_engine.py src/adapters/models.py tests/test_arbitrage.py
git commit -m "feat: fee-adjusted profit filtering, confidence scoring, dedup in arbitrage engine"
```

---

### Task 3: Update frontend to display net profit and confidence

**Files:**
- Modify: `src/static/js/arbitrout.js:286-400`

- [ ] **Step 1: Update opportunity list to show net profit**

In `renderOpportunities()`, change the spread display to show net profit and add a confidence badge. Replace the `spreadEl.textContent = pctText;` block:

```javascript
// Show net profit if available, otherwise fall back to gross
var netPct = opp.net_profit_pct || opp.profit_pct || opp.spread * 100;
var grossPct = opp.profit_pct || opp.spread * 100;
var pctText = '+' + netPct.toFixed(1) + '%';
if (opp.is_synthetic) {
    spreadEl.style.color = '#e040fb';
    pctText += ' \u2726';
}
spreadEl.textContent = pctText;

// Confidence badge
if (opp.confidence && opp.confidence !== 'high') {
    var badge = document.createElement('span');
    badge.style.cssText = 'font-size:9px;padding:1px 4px;border-radius:3px;margin-left:6px;';
    if (opp.confidence === 'very_low') {
        badge.style.background = '#d32f2f';
        badge.style.color = '#fff';
        badge.textContent = 'LIKELY FALSE';
    } else if (opp.confidence === 'low') {
        badge.style.background = '#f57c00';
        badge.style.color = '#fff';
        badge.textContent = 'LOW CONF';
    } else {
        badge.style.background = '#ffd54f';
        badge.style.color = '#333';
        badge.textContent = 'MED';
    }
    spreadEl.appendChild(badge);
}
```

- [ ] **Step 2: Update detail view to show fee breakdown**

In `showEventDetail()`, add a fee info section after the "HOW TO TRADE" block showing:
- Gross profit %
- Net profit % (after fees)
- Confidence level

- [ ] **Step 3: Test manually in browser**

Start server, open UI, verify:
1. Low-confidence opportunities show badges
2. Net profit is displayed instead of gross
3. Fee-negative opportunities no longer appear

- [ ] **Step 4: Commit**

```bash
git add src/static/js/arbitrout.js
git commit -m "feat(ui): show net profit after fees and confidence badges"
```

---

### Task 4: Run full test suite and push

- [ ] **Step 1: Run full test suite**

Run: `cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: Push and restart server**

```bash
cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout && git push origin main
```

Kill existing server, restart with `python -m uvicorn server:app --host 127.0.0.1 --port 8500`.
