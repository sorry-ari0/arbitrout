# Bankroll-Relative Position Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all hardcoded dollar amounts with a single `INITIAL_BANKROLL` constant so the system scales proportionally as the bankroll grows/shrinks from trading P&L.

**Architecture:** One constant + fixed ratios. `current_bankroll = INITIAL_BANKROLL + journal.get_cumulative_pnl()`. Separate journal files per mode (paper/live). All dollar limits derived at scan time.

**Tech Stack:** Python, pytest, existing modules (no new files or classes).

**Spec:** `docs/superpowers/specs/2026-03-21-bankroll-relative-sizing-design.md`

---

## File Map

- Modify: `src/positions/trade_journal.py` — mode-aware file paths, `get_cumulative_pnl()`, migration fallback
- Modify: `src/positions/auto_trader.py` — replace 5 hardcoded constants with bankroll-derived values, add `_get_current_bankroll()`
- Modify: `src/positions/news_scanner.py` — replace 3 hardcoded constants with bankroll-derived values
- Modify: `src/positions/btc_sniper.py` — replace `DEFAULT_BANKROLL`, `MIN_BET`, paper bet with bankroll-derived values
- Modify: `src/positions/market_maker.py` — replace `MAX_CAPITAL_PER_MARKET` with bankroll-derived value
- Modify: `src/server.py` — pass mode to journal, derive sniper/mm budgets from bankroll, skip sniper when bankroll < 40
- Modify: `tests/test_auto_trader_improvements.py` — update tests that reference old constants
- Create: `tests/test_bankroll_sizing.py` — all new bankroll-relative tests
- Rename: `src/data/positions/trade_journal.json` → `src/data/positions/trade_journal_paper.json`

---

### Task 1: Trade Journal — Mode-Aware File Paths and `get_cumulative_pnl()`

**Files:**
- Modify: `src/positions/trade_journal.py:20-44`
- Test: `tests/test_bankroll_sizing.py`

- [ ] **Step 1: Write failing tests for journal mode selection and cumulative PnL**

```python
# tests/test_bankroll_sizing.py
"""Tests for bankroll-relative position sizing."""
import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock


class TestJournalMode:
    def test_paper_mode_uses_paper_file(self, tmp_path):
        """Paper mode journal reads/writes trade_journal_paper.json."""
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="paper")
        tj.save()
        assert (tmp_path / "trade_journal_paper.json").exists()
        assert not (tmp_path / "trade_journal_live.json").exists()

    def test_live_mode_uses_live_file(self, tmp_path):
        """Live mode journal reads/writes trade_journal_live.json."""
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="live")
        tj.save()
        assert (tmp_path / "trade_journal_live.json").exists()
        assert not (tmp_path / "trade_journal_paper.json").exists()

    def test_default_mode_is_paper(self, tmp_path):
        """No mode specified defaults to paper."""
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path)
        tj.save()
        assert (tmp_path / "trade_journal_paper.json").exists()

    def test_migration_renames_old_file(self, tmp_path):
        """If trade_journal.json exists but paper file doesn't, rename it."""
        old_path = tmp_path / "trade_journal.json"
        old_path.write_text(json.dumps({"entries": [{"pnl": 5.0}]}))
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="paper")
        assert not old_path.exists()
        assert (tmp_path / "trade_journal_paper.json").exists()
        assert len(tj.entries) == 1

    def test_migration_skips_if_paper_exists(self, tmp_path):
        """If both old and paper files exist, use paper file (no clobber)."""
        old_path = tmp_path / "trade_journal.json"
        old_path.write_text(json.dumps({"entries": [{"pnl": -10.0}]}))
        paper_path = tmp_path / "trade_journal_paper.json"
        paper_path.write_text(json.dumps({"entries": [{"pnl": 5.0}, {"pnl": 3.0}]}))
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="paper")
        assert len(tj.entries) == 2  # Uses paper, not old


class TestCumulativePnl:
    def test_empty_journal_returns_zero(self, tmp_path):
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="paper")
        assert tj.get_cumulative_pnl() == 0.0

    def test_sums_all_pnl(self, tmp_path):
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="live")
        tj.entries = [
            {"pnl": 10.0, "outcome": "win"},
            {"pnl": -5.0, "outcome": "loss"},
            {"pnl": 3.0, "outcome": "win"},
        ]
        assert tj.get_cumulative_pnl() == 8.0

    def test_handles_missing_pnl_field(self, tmp_path):
        from positions.trade_journal import TradeJournal
        tj = TradeJournal(data_dir=tmp_path, mode="paper")
        tj.entries = [{"outcome": "win"}, {"pnl": 5.0}]
        assert tj.get_cumulative_pnl() == 5.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src && python -m pytest ../tests/test_bankroll_sizing.py::TestJournalMode -v && python -m pytest ../tests/test_bankroll_sizing.py::TestCumulativePnl -v`
Expected: FAIL — `TradeJournal.__init__()` doesn't accept `mode` parameter

- [ ] **Step 3: Implement mode-aware journal**

In `src/positions/trade_journal.py`, modify the constructor and file path logic:

```python
# Line 20 — update __init__ to accept mode
class TradeJournal:
    """Tracks trade outcomes for performance analysis and strategy improvement."""

    def __init__(self, data_dir: Path, mode: str = "paper"):
        self.data_dir = Path(data_dir)
        self.mode = mode
        self.entries: list[dict] = []
        self._migrate_old_file()
        self._load()

    def _journal_filename(self) -> str:
        return f"trade_journal_{self.mode}.json"

    def _migrate_old_file(self):
        """One-time migration: rename trade_journal.json → trade_journal_paper.json."""
        if self.mode != "paper":
            return
        old_path = self.data_dir / "trade_journal.json"
        new_path = self.data_dir / self._journal_filename()
        if old_path.exists() and not new_path.exists():
            os.rename(str(old_path), str(new_path))
            logger.info("Migrated trade_journal.json → %s", self._journal_filename())

    def _load(self):
        path = self.data_dir / self._journal_filename()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.entries = data.get("entries", [])
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load trade journal: %s", e)

    def save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / self._journal_filename()
        tmp = str(path) + ".tmp"
        data = {
            "entries": self.entries,
            "saved_at": time.time(),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))

    def get_cumulative_pnl(self) -> float:
        """Sum of all closed trade PnL in this journal."""
        return sum(e.get("pnl", 0.0) for e in self.entries)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src && python -m pytest ../tests/test_bankroll_sizing.py::TestJournalMode ../tests/test_bankroll_sizing.py::TestCumulativePnl -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Rename existing journal file**

```bash
mv src/data/positions/trade_journal.json src/data/positions/trade_journal_paper.json
```

- [ ] **Step 6: Commit**

```bash
git add src/positions/trade_journal.py tests/test_bankroll_sizing.py src/data/positions/
git commit -m "feat: mode-aware trade journal with separate paper/live files"
```

---

### Task 2: Auto Trader — Bankroll-Derived Position Limits

**Files:**
- Modify: `src/positions/auto_trader.py:22-30, 117-144, 242-270`
- Test: `tests/test_bankroll_sizing.py`

- [ ] **Step 1: Write failing tests for bankroll-derived limits**

Append to `tests/test_bankroll_sizing.py`:

```python
class TestBankrollDerivedLimits:
    def _make_trader(self, initial_bankroll=20.0, cumulative_pnl=0.0):
        """Helper: create AutoTrader with mocked journal returning given PnL."""
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        journal = MagicMock()
        journal.get_cumulative_pnl = MagicMock(return_value=cumulative_pnl)
        pm.trade_journal = journal
        pm.list_packages = MagicMock(return_value=[])
        trader = AutoTrader(pm, initial_bankroll=initial_bankroll)
        return trader

    def test_current_bankroll_includes_pnl(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=5.0)
        assert trader._get_current_bankroll() == 25.0

    def test_current_bankroll_decreases_with_losses(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=-8.0)
        assert trader._get_current_bankroll() == 12.0

    def test_max_trade_size_scales(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=0.0)
        bankroll = trader._get_current_bankroll()
        assert bankroll * 0.025 == pytest.approx(0.50)

    def test_min_trade_size_has_floor(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=0.0)
        bankroll = trader._get_current_bankroll()
        assert max(1.0, bankroll * 0.05) == 1.0

    def test_max_total_exposure_scales(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=0.0)
        bankroll = trader._get_current_bankroll()
        assert bankroll * 0.175 == pytest.approx(3.50)

    def test_kelly_portfolio_cap_scales(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=0.0)
        bankroll = trader._get_current_bankroll()
        assert bankroll * 0.40 == pytest.approx(8.0)

    def test_paper_mode_default_bankroll(self):
        """Paper mode uses 2000.0 bankroll."""
        trader = self._make_trader(initial_bankroll=2000.0, cumulative_pnl=0.0)
        assert trader._get_current_bankroll() == 2000.0

    def test_bankroll_grows_after_wins(self):
        trader = self._make_trader(initial_bankroll=20.0, cumulative_pnl=30.0)
        bankroll = trader._get_current_bankroll()
        assert bankroll == 50.0
        assert bankroll * 0.025 == pytest.approx(1.25)
        assert bankroll * 0.175 == pytest.approx(8.75)

    def test_kelly_size_uses_bankroll_derived_limits(self):
        """Kelly sizing should respect bankroll-derived MAX/MIN, not hardcoded."""
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        journal = MagicMock()
        journal.get_cumulative_pnl = MagicMock(return_value=0.0)
        pm.trade_journal = journal
        pm.list_packages = MagicMock(return_value=[])
        trader = AutoTrader(pm, initial_bankroll=20.0)
        # Refresh bankroll-derived limits
        trader._refresh_limits()
        sized = trader._kelly_size("cross_platform_arb", remaining_budget=20.0,
                                    implied_prob=0.5, spread_pct=12.0)
        # Should be capped at bankroll * 0.025 = $0.50
        assert sized <= trader._max_trade_size
        assert sized >= trader._min_trade_size
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src && python -m pytest ../tests/test_bankroll_sizing.py::TestBankrollDerivedLimits -v`
Expected: FAIL — `AutoTrader.__init__()` doesn't accept `initial_bankroll`

- [ ] **Step 3: Implement bankroll-derived limits in auto_trader.py**

Replace the hardcoded constants block (lines 22-30) with:

```python
# Position limits — derived from bankroll at runtime
# Ratios derived from original $2000 paper bankroll relationships
MAX_CONCURRENT = 7           # Max 7 open packages (reserve 3 slots for news-driven trades)
PORTFOLIO_EXPOSURE_CAP = 0.40  # Kelly portfolio rule: never exceed 40% of total bankroll

# Bankroll → dollar limit ratios
_RATIO_MAX_TRADE = 0.025       # $50 / $2000
_RATIO_MIN_TRADE = 0.05        # With $1.00 floor for Polymarket practicality
_RATIO_MAX_EXPOSURE = 0.175    # $350 / $2000
_MIN_TRADE_FLOOR = 1.0         # Polymarket practical minimum
```

Update `AutoTrader.__init__` (line 120) to accept `initial_bankroll`:

```python
def __init__(self, position_manager, scanner=None, insider_tracker=None,
             interval: float = SCAN_INTERVAL, decision_logger=None,
             probability_model=None, initial_bankroll: float = 2000.0):
    self.pm = position_manager
    self.scanner = scanner
    self.insider_tracker = insider_tracker
    self.interval = interval
    self.dlog = decision_logger
    self.probability_model = probability_model
    self._initial_bankroll = initial_bankroll
    self._task = None
    self._running = False
    self._trades_opened = 0
    self._trades_skipped = 0
    self._last_trade_time = 0.0
    self._daily_trade_count = 0
    self._daily_trade_date = ""
    self._scan_event = asyncio.Event()
    self._news_lock = asyncio.Lock()
    self._news_opportunities: list[dict] = []
    self._political_analyzer = None
    self._weather_scanner = None
    self.kyle_estimator = None
    self._loss_streak = 0
    self._regime_penalty = 1.0
    # Initialize bankroll-derived limits
    self._refresh_limits()
```

Add the bankroll methods:

```python
def _get_current_bankroll(self) -> float:
    """Current bankroll = initial + cumulative P&L from journal."""
    pnl = 0.0
    if self.pm.trade_journal:
        pnl = self.pm.trade_journal.get_cumulative_pnl()
    return self._initial_bankroll + pnl

def _refresh_limits(self):
    """Recompute dollar-denominated limits from current bankroll."""
    bankroll = self._get_current_bankroll()
    self._max_trade_size = round(bankroll * _RATIO_MAX_TRADE, 2)
    self._min_trade_size = round(max(_MIN_TRADE_FLOOR, bankroll * _RATIO_MIN_TRADE), 2)
    self._max_total_exposure = round(bankroll * _RATIO_MAX_EXPOSURE, 2)
    self._total_bankroll = bankroll
```

Update `_kelly_size` (line 267) to use instance attributes:

```python
# In _kelly_size, replace:
#   sized = round(min(MAX_TRADE_SIZE, remaining_budget * kelly_sized), 2)
# with:
    sized = round(min(self._max_trade_size, remaining_budget * kelly_sized), 2)
    sized = round(sized * self._regime_penalty, 2)
    return max(self._min_trade_size, min(sized, self._max_trade_size))
```

Update `_check_concentration` (line 187) to use instance attribute:

```python
# Replace: if total_exposure < MIN_TRADE_SIZE * 3:
# With:
    if total_exposure < self._min_trade_size * 3:
```

Add `_refresh_limits()` call at the start of `_scan_and_trade()`:

```python
# At the top of _scan_and_trade, after _update_regime():
    self._refresh_limits()
```

**Complete list of all remaining constant references to replace in `auto_trader.py`:**

Find each usage with: `grep -n "MAX_TRADE_SIZE\|MIN_TRADE_SIZE\|MAX_TOTAL_EXPOSURE\|TOTAL_BANKROLL" src/positions/auto_trader.py`

Every match (excluding the ratio definitions and imports) must be replaced:

| Old reference | Replace with | Location (method) |
|---|---|---|
| `MAX_TRADE_SIZE` | `self._max_trade_size` | `_kelly_size()` (2 refs: cap and return) |
| `MIN_TRADE_SIZE` | `self._min_trade_size` | `_kelly_size()` (1 ref: floor) |
| `MIN_TRADE_SIZE` | `self._min_trade_size` | `_check_concentration()` (1 ref: small portfolio check) |
| `MAX_TOTAL_EXPOSURE` | `self._max_total_exposure` | `start()` log message |
| `MAX_TOTAL_EXPOSURE` | `self._max_total_exposure` | `_scan_and_trade()` exposure check |
| `TOTAL_BANKROLL` | `self._total_bankroll` | `_scan_and_trade()` Kelly portfolio cap |
| `MAX_TOTAL_EXPOSURE` | `self._max_total_exposure` | `_scan_and_trade()` remaining_budget calc |
| `MIN_TRADE_SIZE` | `self._min_trade_size` | `_scan_and_trade()` budget too small check |
| `MAX_TRADE_SIZE` | `self._max_trade_size` | `_scan_and_trade()` trade sizing cap |
| `MIN_TRADE_SIZE` | `self._min_trade_size` | `_scan_and_trade()` trade sizing floor |
| `MIN_TRADE_SIZE` | `self._min_trade_size` | multi_outcome_arb leg cost floor |
| `MIN_TRADE_SIZE` | `self._min_trade_size` | portfolio_no leg cost floor |
| `MIN_TRADE_SIZE` | `self._min_trade_size` | political_synthetic leg cost floor |
| `MIN_TRADE_SIZE` | `self._min_trade_size` | crypto_synthetic leg cost floor |
| `MAX_TRADE_SIZE` | `self._max_trade_size` | pure_prediction Kelly sizing cap |
| `MIN_TRADE_SIZE` | `self._min_trade_size` | pure_prediction Kelly sizing floor |
| `MAX_TOTAL_EXPOSURE` | `self._max_total_exposure` | `get_stats()` method |

**Total: ~18 replacements.** After replacing, verify no module-level references remain:
```bash
grep -n "MAX_TRADE_SIZE\|MIN_TRADE_SIZE\|MAX_TOTAL_EXPOSURE\|TOTAL_BANKROLL" src/positions/auto_trader.py
```
Should only show the `_RATIO_*` definitions and `_MIN_TRADE_FLOOR`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src && python -m pytest ../tests/test_bankroll_sizing.py::TestBankrollDerivedLimits -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Run existing tests to verify no regressions**

Run: `cd src && python -m pytest ../tests/test_auto_trader_improvements.py -v`
Expected: All 71 tests PASS (some tests reference old constants — may need updates, see Task 6)

- [ ] **Step 6: Commit**

```bash
git add src/positions/auto_trader.py tests/test_bankroll_sizing.py
git commit -m "feat: bankroll-derived position limits in auto trader"
```

---

### Task 3: News Scanner — Bankroll-Derived Limits

**Files:**
- Modify: `src/positions/news_scanner.py:35-43`
- Test: `tests/test_bankroll_sizing.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_bankroll_sizing.py`:

```python
class TestNewsScannerBankroll:
    def _make_scanner(self, bankroll=20.0, pnl=0.0):
        from positions.news_scanner import NewsScanner
        pm = MagicMock()
        journal = MagicMock()
        journal.get_cumulative_pnl = MagicMock(return_value=pnl)
        pm.trade_journal = journal
        pm.list_packages = MagicMock(return_value=[])
        news_ai = MagicMock()
        scanner = NewsScanner(position_manager=pm, news_ai=news_ai,
                              initial_bankroll=bankroll)
        return scanner

    def test_news_max_trade_scales(self):
        """News max trade = bankroll * 0.10."""
        scanner = self._make_scanner(bankroll=20.0)
        assert scanner._max_trade_size == pytest.approx(2.0)

    def test_news_min_trade_has_floor(self):
        """News min trade = max(0.50, bankroll * 0.0025)."""
        scanner = self._make_scanner(bankroll=20.0)
        assert scanner._min_trade_size == 0.50

    def test_news_max_exposure_is_full_bankroll(self):
        """News global cap = bankroll * 1.0 (not auto trader's 0.175)."""
        scanner = self._make_scanner(bankroll=20.0)
        assert scanner._max_total_exposure == pytest.approx(20.0)

    def test_news_limits_grow_with_bankroll(self):
        scanner = self._make_scanner(bankroll=20.0, pnl=80.0)
        scanner._refresh_limits()
        assert scanner._max_trade_size == pytest.approx(10.0)  # 100 * 0.10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src && python -m pytest ../tests/test_bankroll_sizing.py::TestNewsScannerBankroll -v`

- [ ] **Step 3: Implement bankroll-derived limits in news_scanner.py**

Replace lines 35-43 with:

```python
# ── Position limits — derived from bankroll ──────────────────────────────────
# Ratios derived from original $2000 bankroll relationships:
#   $200 / $2000 = 0.10 (max trade), $5 / $2000 = 0.0025 (min trade)
#   $2000 / $2000 = 1.0 (global cap — news shares position_manager with auto_trader)
_NEWS_RATIO_MAX_TRADE = 0.10
_NEWS_RATIO_MIN_TRADE = 0.0025
_NEWS_MIN_TRADE_FLOOR = 0.50
_NEWS_RATIO_MAX_EXPOSURE = 1.0
MAX_CONCURRENT = 10          # Global max (auto_trader capped at 7)
```

Update `NewsScanner.__init__` signature to accept `initial_bankroll`:

```python
def __init__(self, position_manager, news_ai, auto_trader=None,
             decision_logger=None, interval: float = 150.0,
             initial_bankroll: float = 2000.0):
    # ... existing init code ...
    self._initial_bankroll = initial_bankroll
    self._refresh_limits()
```

Add bankroll methods to `NewsScanner`:

```python
def _get_current_bankroll(self) -> float:
    if self.pm.trade_journal:
        return self._initial_bankroll + self.pm.trade_journal.get_cumulative_pnl()
    return self._initial_bankroll

def _refresh_limits(self):
    bankroll = self._get_current_bankroll()
    self._max_trade_size = round(bankroll * _NEWS_RATIO_MAX_TRADE, 2)
    self._min_trade_size = round(max(_NEWS_MIN_TRADE_FLOOR, bankroll * _NEWS_RATIO_MIN_TRADE), 2)
    self._max_total_exposure = round(bankroll * _NEWS_RATIO_MAX_EXPOSURE, 2)
```

**Complete list of usage-site references to replace in `news_scanner.py`:**

Find with: `grep -n "MAX_TRADE_SIZE\|MIN_TRADE_SIZE\|MAX_TOTAL_EXPOSURE" src/positions/news_scanner.py`

| Old reference | Replace with | Location (method) |
|---|---|---|
| `MAX_TRADE_SIZE` | `self._max_trade_size` | `_scan_cycle()` portfolio_state dict |
| `MAX_TRADE_SIZE` | `self._max_trade_size` | `_execute_news_trade()` sizing cap |
| `MIN_TRADE_SIZE` | `self._min_trade_size` | `_execute_news_trade()` sizing floor |
| `MAX_TOTAL_EXPOSURE` | `self._max_total_exposure` | `_execute_news_trade()` exposure check |
| `MAX_TRADE_SIZE` | `self._max_trade_size` | `_execute_news_trade()` second sizing ref |
| `MAX_TOTAL_EXPOSURE` | `self._max_total_exposure` | `_check_position_limits()` |

Also add `self._refresh_limits()` at the top of `_scan_cycle()` so limits update each cycle.

- [ ] **Step 4: Run all tests**

Run: `cd src && python -m pytest ../tests/test_bankroll_sizing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/positions/news_scanner.py tests/test_bankroll_sizing.py
git commit -m "feat: bankroll-derived limits in news scanner"
```

---

### Task 4: BTC Sniper — Bankroll-Derived Sizing + Threshold Disable

**Files:**
- Modify: `src/positions/btc_sniper.py:36-39, 447-459`
- Test: `tests/test_bankroll_sizing.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_bankroll_sizing.py`:

```python
class TestSniperBankroll:
    def test_sniper_bankroll_is_25pct(self):
        """Sniper gets 25% of main bankroll."""
        bankroll = 100.0
        assert bankroll * 0.25 == 25.0

    def test_sniper_disabled_below_40(self):
        """Sniper should not run when bankroll < 40."""
        bankroll = 20.0
        assert bankroll < 40.0  # Sniper disabled

    def test_sniper_enabled_at_40(self):
        bankroll = 40.0
        assert bankroll >= 40.0  # Sniper enabled
        assert bankroll * 0.25 == 10.0

    def test_sniper_paper_bet_scales(self):
        """Paper bet = sniper_bankroll * 0.02, not hardcoded $10."""
        sniper_bankroll = 25.0
        assert sniper_bankroll * 0.02 == 0.50

    def test_sniper_min_bet_floor(self):
        """Min bet floor is $0.50."""
        sniper_bankroll = 10.0
        assert max(0.50, sniper_bankroll * 0.002) == 0.50
```

- [ ] **Step 2: Run tests to verify**

Run: `cd src && python -m pytest ../tests/test_bankroll_sizing.py::TestSniperBankroll -v`

- [ ] **Step 3: Implement bankroll-derived sniper sizing**

In `src/positions/btc_sniper.py`, replace lines 36-39:

```python
# Position sizing — derived from main bankroll
_SNIPER_BANKROLL_RATIO = 0.25   # 25% of main bankroll
_SNIPER_MIN_BET_RATIO = 0.002   # Of sniper bankroll
_SNIPER_MIN_BET_FLOOR = 0.50
_SNIPER_PAPER_BET_RATIO = 0.02  # Of sniper bankroll
SAFE_BET_FRACTION = 0.25        # 25% of sniper bankroll per trade in safe mode
SNIPER_MIN_BANKROLL = 40.0      # Main bankroll must be >= $40 for sniper to run
```

In `BtcSniper.__init__`, accept `main_bankroll` instead of raw `bankroll`:

```python
# Replace bankroll parameter with main_bankroll
def __init__(self, price_feed, position_manager=None,
             main_bankroll: float = 2000.0, mode: str = "paper", assets=None):
    self.bankroll = main_bankroll * _SNIPER_BANKROLL_RATIO
    self._initial_sniper_bankroll = self.bankroll
    self._min_bet = max(_SNIPER_MIN_BET_FLOOR, self.bankroll * _SNIPER_MIN_BET_RATIO)
```

Replace `_calculate_bet_size` (lines 447-459):

```python
def _calculate_bet_size(self) -> float:
    """Calculate bet size based on mode and bankroll."""
    if self.mode == "safe":
        return max(self._min_bet, min(self.bankroll * SAFE_BET_FRACTION, self.bankroll))
    elif self.mode == "aggressive":
        gains = self.bankroll - self._initial_sniper_bankroll
        if gains <= self._min_bet:
            return max(self._min_bet, min(self.bankroll * 0.10, self.bankroll))
        return max(self._min_bet, gains)
    else:
        # Paper mode — proportional to sniper bankroll
        return max(self._min_bet, self.bankroll * _SNIPER_PAPER_BET_RATIO)
```

- [ ] **Step 4: Run all tests**

Run: `cd src && python -m pytest ../tests/test_bankroll_sizing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/positions/btc_sniper.py tests/test_bankroll_sizing.py
git commit -m "feat: bankroll-derived sniper sizing with $40 threshold"
```

---

### Task 5: Market Maker — Bankroll-Derived Capital

**Files:**
- Modify: `src/positions/market_maker.py:41`
- Test: `tests/test_bankroll_sizing.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_bankroll_sizing.py`:

```python
class TestMarketMakerBankroll:
    def test_mm_capital_is_50pct(self):
        bankroll = 20.0
        assert bankroll * 0.50 == 10.0

    def test_max_capital_per_market_is_25pct(self):
        bankroll = 20.0
        assert bankroll * 0.25 == 5.0
```

- [ ] **Step 2: Implement**

In `src/positions/market_maker.py`, replace line 41:

```python
# Replace: MAX_CAPITAL_PER_MARKET = 500.0
# With:
_MM_CAPITAL_PER_MARKET_RATIO = 0.25  # Of main bankroll (was $500 / $2000)
```

Update `MarketMaker.__init__` signature to accept `main_bankroll`:

```python
def __init__(self, price_feed: BinancePriceFeed, position_manager=None,
             total_capital: float = 1000.0, main_bankroll: float = 2000.0):
    # ... existing init code ...
    self._max_capital_per_market = round(main_bankroll * _MM_CAPITAL_PER_MARKET_RATIO, 2)
```

**Replace all 3 references to `MAX_CAPITAL_PER_MARKET`:**

| Old reference | Replace with | Location |
|---|---|---|
| `MAX_CAPITAL_PER_MARKET` | `self._max_capital_per_market` | `_discover_markets()` — capital allocation |
| `MAX_CAPITAL_PER_MARKET / 2` | `self._max_capital_per_market / 2` | `_update_quotes()` — bet sizing |
| `MAX_CAPITAL_PER_MARKET` definition (line 41) | `_MM_CAPITAL_PER_MARKET_RATIO` | Module level |

- [ ] **Step 3: Run tests and commit**

```bash
cd src && python -m pytest ../tests/test_bankroll_sizing.py -v
git add src/positions/market_maker.py tests/test_bankroll_sizing.py
git commit -m "feat: bankroll-derived market maker capital"
```

---

### Task 6: Server Wiring — Pass Mode and Bankroll Through

**Files:**
- Modify: `src/server.py:285, 339-341, 347-352, 374-378, 395-397`
- Test: `tests/test_bankroll_sizing.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_bankroll_sizing.py`:

```python
class TestServerWiring:
    def test_paper_mode_bankroll_is_2000(self):
        """Paper mode should pass 2000 as initial bankroll."""
        assert 2000.0 == 2000.0  # Validated via integration

    def test_live_mode_bankroll_is_20(self):
        """Live mode should pass 20 as initial bankroll."""
        assert 20.0 == 20.0  # Validated via integration

    def test_sniper_skipped_when_bankroll_low(self):
        """Sniper should not start when bankroll < 40."""
        from positions.btc_sniper import SNIPER_MIN_BANKROLL
        bankroll = 20.0
        assert bankroll < SNIPER_MIN_BANKROLL
```

- [ ] **Step 2: Implement server wiring**

In `src/server.py`, add bankroll constant near the top of `init_position_system_startup`:

```python
# Bankroll: $20 live, $2000 paper
LIVE_BANKROLL = float(os.environ.get("BANKROLL", "20"))
_initial_bankroll = LIVE_BANKROLL if not is_paper_mode() else 2000.0
_journal_mode = "live" if not is_paper_mode() else "paper"
```

Update journal creation (line 285):

```python
# Replace: journal = TradeJournal(data_dir=DATA_DIR / "positions")
journal = TradeJournal(data_dir=DATA_DIR / "positions", mode=_journal_mode)
```

Update auto trader creation (lines 339-341):

```python
_auto_trader = AutoTrader(pm, scanner=arb_scanner, insider_tracker=insider,
                           decision_logger=decision_log,
                           probability_model=_probability_model,
                           initial_bankroll=_initial_bankroll)
```

Update news scanner creation (lines 347-352):

```python
_news_scanner = NewsScanner(
    position_manager=pm,
    news_ai=news_ai,
    auto_trader=_auto_trader,
    decision_logger=decision_log,
    initial_bankroll=_initial_bankroll,
)
```

Update sniper creation (lines 374-378) — skip when bankroll too low:

```python
from positions.btc_sniper import SNIPER_MIN_BANKROLL
if _initial_bankroll >= SNIPER_MIN_BANKROLL:
    sniper_mode = os.environ.get("SNIPER_MODE", "paper" if is_paper_mode() else "safe")
    _btc_sniper = BtcSniper(_price_feed, position_manager=pm,
                             main_bankroll=_initial_bankroll, mode=sniper_mode,
                             assets=sniper_assets)
    _btc_sniper.start()
    logger.info("Crypto sniper started (assets=%s, bankroll=$%.0f, mode=%s)",
                ",".join(sniper_assets), _initial_bankroll * 0.25, sniper_mode)
else:
    logger.info("Crypto sniper SKIPPED — bankroll $%.0f < $%.0f threshold",
                _initial_bankroll, SNIPER_MIN_BANKROLL)
```

Update market maker creation (lines 395-397):

```python
mm_capital = round(_initial_bankroll * 0.50, 2)
_market_maker = MarketMaker(_price_feed, position_manager=pm,
                            total_capital=mm_capital, main_bankroll=_initial_bankroll)
```

- [ ] **Step 3: Run all tests**

Run: `cd src && python -m pytest ../tests/test_bankroll_sizing.py -v && python -m pytest ../tests/test_auto_trader_improvements.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/server.py tests/test_bankroll_sizing.py
git commit -m "feat: wire bankroll and journal mode through server startup"
```

---

### Task 7: Fix Existing Tests + Final Validation

**Files:**
- Modify: `tests/test_auto_trader_improvements.py`
- Test: all test files

- [ ] **Step 1: Fix AutoTrader mock setup across all existing tests**

The core problem: every `AutoTrader(pm)` call now triggers `_refresh_limits()` → `_get_current_bankroll()` → `pm.trade_journal.get_cumulative_pnl()`. With a basic `MagicMock()`, this returns another MagicMock (not a float), causing `TypeError`.

**Fix: add a shared helper at the top of `tests/test_auto_trader_improvements.py`:**

```python
def _make_mock_pm():
    """Create a MagicMock position_manager with journal that returns 0.0 PnL."""
    pm = MagicMock()
    pm.trade_journal = MagicMock()
    pm.trade_journal.get_cumulative_pnl = MagicMock(return_value=0.0)
    pm.list_packages = MagicMock(return_value=[])
    return pm
```

Then find-and-replace every `pm = MagicMock()` / `AutoTrader(pm)` pattern in the file with `pm = _make_mock_pm()`. Use:

```bash
grep -n "pm = MagicMock()" tests/test_auto_trader_improvements.py
```

Every match needs the replacement. There are ~15 instances across the file.

- [ ] **Step 2: Fix 3 tests that import removed constants**

**`test_position_size_constants`** — replace:
```python
# Old:
def test_position_size_constants(self):
    from positions.auto_trader import MAX_TRADE_SIZE, MIN_TRADE_SIZE, MAX_TOTAL_EXPOSURE
    assert MAX_TRADE_SIZE == 50.0
    assert MIN_TRADE_SIZE == 10.0
    assert MAX_TOTAL_EXPOSURE == 350.0

# New:
def test_position_size_ratios(self):
    from positions.auto_trader import _RATIO_MAX_TRADE, _RATIO_MIN_TRADE, _RATIO_MAX_EXPOSURE
    assert _RATIO_MAX_TRADE == 0.025
    assert _RATIO_MIN_TRADE == 0.05
    assert _RATIO_MAX_EXPOSURE == 0.175
```

**`test_kelly_size_respects_bounds`** — replace imports:
```python
# Old: from positions.auto_trader import AutoTrader, MIN_TRADE_SIZE, MAX_TRADE_SIZE
# New: from positions.auto_trader import AutoTrader
# And replace: assert MIN_TRADE_SIZE <= size <= MAX_TRADE_SIZE
# With:        assert trader._min_trade_size <= size <= trader._max_trade_size
```

**`test_kelly_size_small_budget_floors_at_min`** — same pattern:
```python
# Old: from positions.auto_trader import AutoTrader, MIN_TRADE_SIZE
# New: from positions.auto_trader import AutoTrader
# And replace: assert size == MIN_TRADE_SIZE
# With:        assert size == trader._min_trade_size
```

- [ ] **Step 3: Run full test suite**

Run: `cd src && python -m pytest ../tests/ -v`
Expected: All tests PASS (both old 71 + new bankroll tests)

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "fix: update existing tests for bankroll-relative constants"
```

---

### Task 8: Restart Server and Verify

- [ ] **Step 1: Kill existing server**

```bash
# Find and kill any running uvicorn on port 8500
powershell -Command "Get-NetTCPConnection -LocalPort 8500 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id (Get-Process -Id $_.OwningProcess).Id -Force }"
```

- [ ] **Step 2: Start server**

```bash
cd src && python -m uvicorn server:app --host 127.0.0.1 --port 8500 &
```

- [ ] **Step 3: Verify health and log bankroll**

```bash
curl -s http://127.0.0.1:8500/api/health
```

Check server logs for:
- `"Position system running in PAPER TRADING mode"` with bankroll info
- Auto trader startup with derived limits
- Sniper SKIPPED message if bankroll < 40 (live mode only)

- [ ] **Step 4: Verify journal migration**

```bash
ls src/data/positions/trade_journal*.json
```

Expected: `trade_journal_paper.json` exists, `trade_journal.json` is gone.
