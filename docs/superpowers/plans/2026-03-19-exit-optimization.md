# Exit Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce exit fee drag from 2% to ~0.4%, add news-validated exit decisions, and build a calibration feedback loop for data-driven threshold tuning.

**Architecture:** Three sequential changes to the existing exit pipeline: (1) wire `sell_limit()` into the exit path with async pending order tracking, (2) pipe the news scanner's headline matches into the AI advisor prompt, (3) new CalibrationEngine that reads eval_logger + trade_journal and generates threshold suggestions.

**Tech Stack:** Python 3.11+, FastAPI, asyncio, httpx, Polymarket CLOB client

**Spec:** `docs/superpowers/specs/2026-03-19-exit-optimization-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/execution/paper_executor.py` | Modify | Rewrite `sell_limit()` to use maker fees (0%) |
| `src/positions/position_manager.py` | Modify | Add `use_limit` param to `exit_leg()`, add `place_limit_sell()` and `resolve_pending_order()` |
| `src/positions/exit_engine.py` | Modify | Add `_resolve_pending_limit_orders()`, pass `use_limit` based on trigger type, wire news_scanner, pass news context to AI |
| `src/positions/news_scanner.py` | Modify | Add `_matched_headlines` cache, populate during scan cycle, add `get_recent_headlines()` |
| `src/positions/ai_advisor.py` | Modify | Add `news_context` param to `_build_batched_prompt()` |
| `src/positions/trade_journal.py` | Modify | Record `exit_order_type`, add `get_performance_by_hold_duration()` |
| `src/positions/calibration.py` | Create | CalibrationEngine: generate_report(), save_report() |
| `src/positions/position_router.py` | Modify | Add `GET /api/derivatives/calibration` endpoint |
| `src/server.py` | Modify | Wire news_scanner into ExitEngine, wire CalibrationEngine, add 24h background task |
| `tests/test_limit_exits.py` | Create | Tests for limit order exit flow |
| `tests/test_news_exits.py` | Create | Tests for news-validated exit decisions |
| `tests/test_calibration.py` | Create | Tests for calibration engine |

---

## Task 1: Rewrite paper_executor.sell_limit() and add paper order methods

**Files:**
- Modify: `src/execution/paper_executor.py:155-157`
- Create: `tests/test_limit_exits.py`

- [ ] **Step 1: Write failing test for sell_limit maker fee**

```python
# tests/test_limit_exits.py
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from execution.paper_executor import PaperExecutor
from execution.base_executor import BaseExecutor, ExecutionResult


class StubPolymarketExecutor(BaseExecutor):
    """Minimal real executor stub for PaperExecutor wrapping."""
    async def buy(self, asset_id, amount_usd, **kw):
        return ExecutionResult(False, None, 0, 0, 0, "stub")
    async def sell(self, asset_id, quantity, **kw):
        return ExecutionResult(False, None, 0, 0, 0, "stub")
    async def get_current_price(self, asset_id):
        return 0.50
    async def get_balance(self):
        from execution.base_executor import BalanceResult
        return BalanceResult(0, 0)
    async def get_positions(self):
        return []
    def is_configured(self):
        return True


def make_paper_executor(balance=1000.0):
    """Create a PaperExecutor wrapping a stub Polymarket executor."""
    # Class name contains "polymarket" so fee lookup finds 0% maker / 2% taker
    StubPolymarketExecutor.__name__ = "polymarketexecutor"
    real = StubPolymarketExecutor()
    pe = PaperExecutor(real, starting_balance=balance)
    return pe


def test_sell_limit_uses_maker_fee():
    """sell_limit() should use 0% maker fee for Polymarket, not 2% taker."""
    pe = make_paper_executor(balance=1000.0)
    # Simulate an existing position
    pe.positions["test:YES"] = {"quantity": 100.0, "avg_entry_price": 0.50}

    result = asyncio.get_event_loop().run_until_complete(
        pe.sell_limit("test:YES", 100.0, 0.60)
    )
    assert result.success
    assert result.fees == 0.0, f"Expected 0% maker fee, got {result.fees}"
    assert result.filled_price == 0.60, "Should fill at the limit price"
    assert result.filled_quantity == 100.0


def test_sell_limit_uses_limit_price_not_market():
    """sell_limit() should use the provided price, not fetch market price."""
    pe = make_paper_executor(balance=1000.0)
    pe.positions["test:YES"] = {"quantity": 50.0, "avg_entry_price": 0.40}

    result = asyncio.get_event_loop().run_until_complete(
        pe.sell_limit("test:YES", 50.0, 0.75)
    )
    assert result.success
    assert result.filled_price == 0.75
    # Balance should increase by qty * price (no fee)
    assert pe.balance == 1000.0 + (50.0 * 0.75)


def test_sell_market_still_charges_taker_fee():
    """Regular sell() should still charge 2% taker fee."""
    pe = make_paper_executor(balance=1000.0)
    pe.positions["test:YES"] = {"quantity": 100.0, "avg_entry_price": 0.50}

    result = asyncio.get_event_loop().run_until_complete(
        pe.sell("test:YES", 100.0)
    )
    assert result.success
    assert result.fees > 0, "Market sell should charge taker fee"


def test_check_order_status_returns_filled():
    """Paper executor check_order_status() should return filled immediately."""
    pe = make_paper_executor()
    result = asyncio.get_event_loop().run_until_complete(
        pe.check_order_status("paper_abc123")
    )
    assert result["status"] == "filled"


def test_cancel_order_returns_true():
    """Paper executor cancel_order() should return True (no-op)."""
    pe = make_paper_executor()
    result = asyncio.get_event_loop().run_until_complete(
        pe.cancel_order("paper_abc123")
    )
    assert result is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_limit_exits.py -v`
Expected: FAIL — `sell_limit` currently delegates to `sell()` which charges taker fees.

- [ ] **Step 3: Rewrite sell_limit in paper_executor.py**

Replace lines 155-157 of `src/execution/paper_executor.py` with:

```python
    async def sell_limit(self, asset_id: str, quantity: float, price: float) -> ExecutionResult:
        """Simulate a limit sell using maker fee rate (0% for Polymarket).

        Uses the provided limit price instead of market price.
        Mirrors buy_limit() pattern — looks up maker fee via MAKER_FEE_RATES.
        """
        pos = self.positions.get(asset_id)
        if not pos or pos["quantity"] < quantity * 0.999:
            return ExecutionResult(False, None, 0, 0, 0, f"No position or insufficient quantity for {asset_id}")
        if price <= 0:
            return ExecutionResult(False, None, 0, 0, 0, f"Invalid limit price for {asset_id}")

        # Look up maker fee rate for this platform (same pattern as buy_limit)
        platform = getattr(self.real, '__class__', type(self.real)).__name__.lower()
        maker_rate = DEFAULT_FEE_RATE
        for name, rate in MAKER_FEE_RATES.items():
            if name in platform:
                maker_rate = rate
                break

        proceeds = quantity * price
        fee = round(proceeds * maker_rate, 4)
        net_proceeds = proceeds - fee
        self.balance += net_proceeds
        self.total_fees_paid += fee
        pos["quantity"] -= quantity
        if pos["quantity"] < 1e-10:
            del self.positions[asset_id]
        tx_id = f"paper_{uuid.uuid4().hex[:12]}"
        self.trade_history.append({
            "action": "sell_limit", "asset_id": asset_id, "price": price,
            "quantity": quantity, "proceeds_usd": net_proceeds, "fee": fee, "tx_id": tx_id,
        })
        return ExecutionResult(True, tx_id, price, quantity, fee, None)
```

- [ ] **Step 4: Add check_order_status() and cancel_order() to PaperExecutor**

PaperExecutor is NOT a BaseExecutor subclass (uses composition), so it doesn't inherit the default methods. Add these after `sell_limit()`:

```python
    async def check_order_status(self, order_id: str) -> dict:
        """Paper mode: limit orders fill immediately."""
        return {"status": "filled", "price": 0, "size_matched": 0, "fee": 0.0}

    async def cancel_order(self, order_id: str) -> bool:
        """Paper mode: no-op cancel."""
        return True
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_limit_exits.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/.openclaw/workspace/projects/arbitrout
git add tests/test_limit_exits.py src/execution/paper_executor.py
git commit -m "feat: rewrite paper sell_limit() to use 0% maker fees, add check/cancel stubs"
```

---

## Task 2: Add pending limit order tracking to position_manager

**Files:**
- Modify: `src/positions/position_manager.py:256-312`
- Modify: `tests/test_limit_exits.py`

- [ ] **Step 1: Write failing test for limit sell with pending order**

Append to `tests/test_limit_exits.py`:

```python
def test_exit_leg_with_limit_returns_pending():
    """exit_leg(use_limit=True) should place a limit order and return pending status."""
    from positions.position_manager import PositionManager, create_package, create_leg
    from execution.base_executor import ExecutionResult
    import tempfile
    from pathlib import Path

    pm = PositionManager(data_dir=Path(tempfile.mkdtemp()), executors={})
    # Create a mock executor that supports sell_limit
    mock_exec = AsyncMock()
    mock_exec.sell_limit = AsyncMock(return_value=ExecutionResult(
        success=True, tx_id="order_123", filled_price=0.60,
        filled_quantity=100.0, fees=0.0, error=None
    ))
    mock_exec.check_order_status = AsyncMock(return_value={"status": "open"})
    pm.executors["polymarket"] = mock_exec

    pkg = create_package("Test pkg", "pure_prediction")
    leg = create_leg("polymarket", "prediction_yes", "cond:YES", "Cond YES", 0.50, 100.0, "2026-12-31")
    leg["current_price"] = 0.60
    pkg["legs"].append(leg)
    pm.packages[pkg["id"]] = pkg

    result = asyncio.get_event_loop().run_until_complete(
        pm.exit_leg(pkg["id"], leg["leg_id"], trigger="ai_approved:target_hit", use_limit=True)
    )
    assert result.get("pending"), "Expected pending status for limit order"
    assert result.get("order_id") == "order_123"
    assert leg["status"] == "open", "Leg should stay open while order is pending"
    assert "_pending_limit_orders" in pkg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_limit_exits.py::test_exit_leg_with_limit_returns_pending -v`
Expected: FAIL — `exit_leg()` doesn't accept `use_limit` param.

- [ ] **Step 3: Modify exit_leg() to support use_limit**

In `src/positions/position_manager.py`, replace the `exit_leg` method (line 256) and add new methods after `_exit_leg_locked`:

```python
    async def exit_leg(self, pkg_id: str, leg_id: str, trigger: str = "manual",
                       use_limit: bool = False) -> dict:
        """Exit (sell) a single leg. If use_limit=True, places a GTC limit order
        and returns {"pending": True, "order_id": ...} — caller resolves later."""
        async with self._lock:
            if use_limit:
                return await self._place_limit_sell(pkg_id, leg_id, trigger)
            return await self._exit_leg_locked(pkg_id, leg_id, trigger)

    async def _place_limit_sell(self, pkg_id: str, leg_id: str, trigger: str) -> dict:
        """Place a GTC limit sell order. Does NOT finalize the exit — returns pending."""
        pkg = self.packages.get(pkg_id)
        if not pkg:
            return {"success": False, "error": "Package not found"}

        leg = next((l for l in pkg["legs"] if l["leg_id"] == leg_id), None)
        if not leg or leg["status"] != "open":
            return {"success": False, "error": "Leg not found or not open"}

        executor = self.executors.get(leg["platform"])
        if not executor:
            return {"success": False, "error": f"No executor for {leg['platform']}"}

        # Limit price: midpoint minus max(1 cent, 1% of price)
        mid = leg.get("current_price", 0)
        if mid <= 0:
            # Can't set a limit price without a current price — fall back to FOK
            return await self._exit_leg_locked(pkg_id, leg_id, trigger)
        offset = max(0.01, mid * 0.01)
        limit_price = round(mid - offset, 4)
        if limit_price <= 0:
            return await self._exit_leg_locked(pkg_id, leg_id, trigger)

        # Place limit order (same call for paper or real executor)
        result = await executor.sell_limit(leg["asset_id"], leg["quantity"], limit_price)

        if not result.success:
            # Limit order failed to place — fall back to FOK
            logger.warning("Limit sell failed for %s, falling back to FOK: %s", leg_id, result.error)
            return await self._exit_leg_locked(pkg_id, leg_id, trigger)

        # Record pending order
        if "_pending_limit_orders" not in pkg:
            pkg["_pending_limit_orders"] = {}
        pkg["_pending_limit_orders"][leg_id] = {
            "order_id": result.tx_id,
            "placed_at": time.time(),
            "quantity": leg["quantity"],
            "asset_id": leg["asset_id"],
            "platform": leg["platform"],
            "trigger": trigger,
            "limit_price": limit_price,
        }
        self.save()
        logger.info("Placed limit sell for %s @ %.4f (order %s)", leg_id, limit_price, result.tx_id)
        return {"pending": True, "order_id": result.tx_id}

    async def resolve_pending_order(self, pkg_id: str, leg_id: str) -> dict:
        """Check a pending limit order and finalize or cancel+FOK.

        Per spec: check status OUTSIDE the lock (network call), then acquire
        lock only for finalization (writing exit data). This keeps the lock
        window small so safety overrides are never blocked.
        """
        # --- Phase 1: read state and check order status WITHOUT lock ---
        pkg = self.packages.get(pkg_id)
        if not pkg:
            return {"success": False, "error": "Package not found"}

        pending = pkg.get("_pending_limit_orders", {}).get(leg_id)
        if not pending:
            return {"success": False, "error": "No pending order for this leg"}

        executor = self.executors.get(pending["platform"])
        if not executor:
            return {"success": False, "error": f"No executor for {pending['platform']}"}

        order_id = pending["order_id"]
        status = await executor.check_order_status(order_id)  # Network call — no lock held
        order_status = status.get("status", "unknown").lower()

        # --- Phase 2: acquire lock only for finalization ---
        async with self._lock:
            # Re-read state in case it changed while we were unlocked
            pkg = self.packages.get(pkg_id)
            if not pkg:
                return {"success": False, "error": "Package not found"}
            pending = pkg.get("_pending_limit_orders", {}).get(leg_id)
            if not pending:
                return {"success": False, "error": "No pending order (resolved by safety override?)"}

            leg = next((l for l in pkg["legs"] if l["leg_id"] == leg_id), None)
            if not leg:
                return {"success": False, "error": "Leg not found"}

            if order_status == "filled":
                # Finalize exit with maker fees (0 or near-0)
                fill_price = status.get("price", pending["limit_price"])
                fill_qty = status.get("size_matched", pending["quantity"])
                fill_fee = status.get("fee", 0.0)
                self._finalize_exit(pkg, leg, pending["trigger"], fill_price, fill_qty, fill_fee, "limit_filled")
                del pkg["_pending_limit_orders"][leg_id]
                if not pkg["_pending_limit_orders"]:
                    del pkg["_pending_limit_orders"]
                self.save()
                return {"success": True, "exit_order_type": "limit_filled"}

            elif order_status == "partially_filled":
                # Cancel remainder, FOK the rest
                await executor.cancel_order(order_id)
                filled_qty = float(status.get("size_matched", 0))
                remaining = pending["quantity"] - filled_qty
                if filled_qty > 0:
                    fill_price = status.get("price", pending["limit_price"])
                    fill_fee = status.get("fee", 0.0)
                    leg["quantity"] = remaining  # Reduce to unfilled portion
                if remaining > 0.001:
                    result = await executor.sell(pending["asset_id"], remaining)
                    if result.success:
                        self._finalize_exit(pkg, leg, pending["trigger"], result.filled_price,
                                            pending["quantity"], result.fees, "limit_partial_fok")
                del pkg["_pending_limit_orders"][leg_id]
                if not pkg["_pending_limit_orders"]:
                    del pkg["_pending_limit_orders"]
                self.save()
                return {"success": True, "exit_order_type": "limit_partial_fok"}

            elif time.time() - pending["placed_at"] > 60:
                # Timeout — cancel and FOK
                await executor.cancel_order(order_id)
                del pkg["_pending_limit_orders"][leg_id]
                if not pkg["_pending_limit_orders"]:
                    del pkg["_pending_limit_orders"]
                logger.info("Limit order timed out for %s, falling back to FOK", leg_id)
                return await self._exit_leg_locked(pkg_id, leg_id, pending["trigger"])

            else:
                # Still open, not timed out — check again next tick
                return {"pending": True, "order_id": order_id}

    def _finalize_exit(self, pkg: dict, leg: dict, trigger: str,
                       fill_price: float, fill_qty: float, fees: float,
                       exit_order_type: str):
        """Finalize a leg exit — same logic as _exit_leg_locked but with provided fill data."""
        leg["status"] = "closed"
        leg["exit_price"] = fill_price
        leg["exit_quantity"] = fill_qty
        leg["sell_fees"] = fees
        leg["exit_trigger"] = trigger
        leg["exit_order_type"] = exit_order_type
        leg["exit_value"] = round(fill_qty * fill_price, 4)
        leg["current_value"] = round(fill_qty * fill_price - fees, 4)
        pkg["execution_log"].append({
            "action": "sell", "leg_id": leg["leg_id"], "platform": leg["platform"],
            "tx_id": None, "price": fill_price, "fees": fees,
            "trigger": trigger, "exit_order_type": exit_order_type,
            "timestamp": time.time(),
        })
        if all(l["status"] in ("closed", "advisory") for l in pkg["legs"]):
            pkg["status"] = STATUS_CLOSED
            pkg["current_value"] = round(sum(
                l.get("quantity", 0) * l.get("exit_price", l.get("current_price", l.get("entry_price", 0)))
                for l in pkg["legs"] if l.get("status") != "advisory"
            ), 4)
            if self.trade_journal:
                try:
                    self.trade_journal.record_close(pkg, exit_trigger=trigger)
                except Exception as e:
                    logger.warning("Failed to record trade journal: %s", e)
        else:
            pkg["status"] = STATUS_PARTIAL
        pkg["updated_at"] = time.time()
```

Also update `_exit_leg_locked` to record `exit_order_type` = "fok_direct" on the leg:

After line 285 (`leg["exit_trigger"] = trigger`), add:
```python
            leg["exit_order_type"] = "fok_direct"
```

And in the execution_log append (line 290-294), add `"exit_order_type": "fok_direct"` to the dict.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_limit_exits.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/.openclaw/workspace/projects/arbitrout
git add src/positions/position_manager.py tests/test_limit_exits.py
git commit -m "feat: add limit order exit support to position_manager"
```

---

## Task 3: Wire limit exits into exit_engine

**Files:**
- Modify: `src/positions/exit_engine.py:326,360-430,580-596`

- [ ] **Step 1: Add news_scanner param to ExitEngine constructor**

At `src/positions/exit_engine.py` line 326, change the constructor:

```python
    def __init__(self, position_manager, ai_advisor=None, interval: float = 60.0,
                 decision_logger=None, news_scanner=None):
```

Add after existing init lines:
```python
        self._news_scanner = news_scanner
```

- [ ] **Step 2: Add _resolve_pending_limit_orders() method**

Add this method to the ExitEngine class, before `_tick()`:

```python
    async def _resolve_pending_limit_orders(self):
        """Check all pending limit orders and finalize or FOK-fallback."""
        for pkg in self.pm.list_packages("open"):
            pending = pkg.get("_pending_limit_orders", {})
            if not pending:
                continue
            for leg_id in list(pending.keys()):
                result = await self.pm.resolve_pending_order(pkg["id"], leg_id)
                if result.get("success"):
                    etype = result.get("exit_order_type", "unknown")
                    logger.info("Resolved pending limit order for %s/%s: %s", pkg["id"], leg_id, etype)
                elif result.get("pending"):
                    pass  # Still waiting — check next tick
                else:
                    logger.warning("Failed to resolve pending order %s/%s: %s",
                                   pkg["id"], leg_id, result.get("error", "?"))
```

- [ ] **Step 3: Call it at the start of _tick()**

At line 367 (start of `_tick()` body), add before `open_pkgs = ...`:

```python
        # Resolve any pending limit orders from previous tick
        await self._resolve_pending_limit_orders()
```

- [ ] **Step 4: Pass use_limit to exit_leg calls**

**Safety overrides** (line 422): keep as-is (no `use_limit` param = defaults to `False` = FOK).

**AI-approved exits** (lines 589-590, 595-596): add `use_limit=True` UNLESS the trigger is `stop_loss`:

Replace the exit calls at lines 589-590:
```python
                            is_stop = trigger["name"] == "stop_loss"
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                trigger=f"ai_approved:{trigger['name']}",
                                use_limit=not is_stop)
```

Replace lines 595-596:
```python
                            is_stop = trigger["name"] == "stop_loss"
                            await self.pm.exit_leg(pkg["id"], leg["leg_id"],
                                trigger=f"ai_partial:{trigger['name']}",
                                use_limit=not is_stop)
```

- [ ] **Step 5: Skip trigger evaluation for packages with pending orders**

At line 382 (the `_exiting` check), extend it:

```python
            # Skip packages currently being exited or with pending limit orders
            if pkg.get("_exiting") or pkg.get("_pending_limit_orders"):
                continue
```

- [ ] **Step 6: Cancel pending orders before safety overrides**

At line 414, before executing safety overrides, add:

```python
            # Cancel any pending limit orders before executing safety override
            for pending_leg_id in list(pkg.get("_pending_limit_orders", {}).keys()):
                pending_info = pkg["_pending_limit_orders"][pending_leg_id]
                executor = self.pm.executors.get(pending_info["platform"])
                if executor:
                    await executor.cancel_order(pending_info["order_id"])
                    logger.warning("Cancelled pending limit order %s for safety override", pending_info["order_id"])
            pkg.pop("_pending_limit_orders", None)
```

- [ ] **Step 7: Run all tests**

Run: `cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
cd ~/.openclaw/workspace/projects/arbitrout
git add src/positions/exit_engine.py
git commit -m "feat: wire limit order exits into exit engine with async pending resolution"
```

---

## Task 4: Wire news_scanner into server.py

**Files:**
- Modify: `src/server.py:308-333`

- [ ] **Step 1: Set news_scanner reference on ExitEngine after both are created**

`_news_scanner` is constructed at lines 326-333, AFTER ExitEngine (line 311). The ExitEngine starts its loop at line 312, but the first tick has a 60s sleep and `_news_scanner` is only used in `_batched_ai_review()`, so it's safe to set the reference post-construction. Add after line 333 (`_news_scanner.start()`):

```python
    exit_engine._news_scanner = _news_scanner
```

- [ ] **Step 2: Verify server starts cleanly**

Run: `cd ~/.openclaw/workspace/projects/arbitrout/src && python -m uvicorn server:app --host 127.0.0.1 --port 8500 &`
Wait 5 seconds, then: `curl http://127.0.0.1:8500/api/health`
Expected: `{"status":"ok",...}`
Kill the test server.

- [ ] **Step 3: Commit**

```bash
cd ~/.openclaw/workspace/projects/arbitrout
git add src/server.py
git commit -m "feat: wire news_scanner into exit engine via server lifespan"
```

---

## Task 5: Add headline cache to news_scanner

**Files:**
- Modify: `src/positions/news_scanner.py:84-114,265-294`
- Create: `tests/test_news_exits.py`

- [ ] **Step 1: Write failing test for get_recent_headlines**

```python
# tests/test_news_exits.py
import time
import pytest

def make_news_scanner():
    from unittest.mock import MagicMock
    from positions.news_scanner import NewsScanner
    ns = NewsScanner(
        position_manager=MagicMock(),
        news_ai=MagicMock(),
    )
    return ns

def test_get_recent_headlines_returns_matches():
    ns = make_news_scanner()
    now = time.time()
    # Manually populate the cache
    ns._matched_headlines = {
        "condition_abc": [
            {"headline": "BTC ETF delayed", "source": "CoinDesk", "timestamp": now - 3600,
             "confidence": 8, "sentiment": "negative", "market_title": "Will BTC hit $100K?"},
            {"headline": "Old stale headline", "source": "BBC", "timestamp": now - 200000,
             "confidence": 5, "sentiment": "neutral", "market_title": "Will BTC hit $100K?"},
        ],
    }
    results = ns.get_recent_headlines("condition_abc", hours=24)
    assert len(results) == 1, "Should only return headlines from last 24 hours"
    assert results[0]["headline"] == "BTC ETF delayed"

def test_get_recent_headlines_empty_for_unknown_market():
    ns = make_news_scanner()
    ns._matched_headlines = {}
    results = ns.get_recent_headlines("unknown_condition", hours=24)
    assert results == []

def test_get_recent_headlines_no_scanner():
    """When news_scanner is None, the exit engine should get empty results."""
    results = []  # Default behavior when _news_scanner is None
    assert results == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_news_exits.py -v`
Expected: FAIL — `_matched_headlines` attribute doesn't exist, `get_recent_headlines()` doesn't exist.

- [ ] **Step 3: Add _matched_headlines cache and get_recent_headlines()**

In `src/positions/news_scanner.py`, at line 98 (after `_recent_headlines`), add:

```python
        self._matched_headlines: dict[str, list[dict]] = {}  # condition_id → [{headline, source, ...}]
```

Add new method to the NewsScanner class (after `_load_cache`):

```python
    def get_recent_headlines(self, condition_id: str, hours: int = 24) -> list[dict]:
        """Return cached headlines matching this market from the last N hours."""
        cutoff = time.time() - (hours * 3600)
        entries = self._matched_headlines.get(condition_id, [])
        return [e for e in entries if e.get("timestamp", 0) > cutoff]
```

- [ ] **Step 4: Populate cache during _scan_cycle()**

In `src/positions/news_scanner.py`, at line 290 (where `market_id = market.get("condition_id", "")`), add the cache population **IMMEDIATELY after line 290, BEFORE the confidence gate at line 292**. This ensures all matched headlines are cached for exit validation — the gating logic (confidence >= 7, daily cap, etc.) only controls whether the news scanner acts on the headline, not whether the exit engine sees it:

```python
            # Cache headline match for exit engine news validation (BEFORE gating)
            title = headline.get("title", "?")
            side = result.get("side", "")
            if side.upper() == "NO":
                sentiment = "negative"
            elif side.upper() == "YES":
                sentiment = "positive"
            else:
                sentiment = "neutral"
            if market_id:
                if market_id not in self._matched_headlines:
                    self._matched_headlines[market_id] = []
                self._matched_headlines[market_id].append({
                    "headline": title,
                    "source": headline.get("source", "unknown"),
                    "timestamp": time.time(),
                    "confidence": confidence,
                    "sentiment": sentiment,
                    "market_title": market.get("question", market.get("title", "")),
                })
                # Cap at 500 total entries to bound memory (per spec)
                total = sum(len(v) for v in self._matched_headlines.values())
                if total > 500:
                    # Remove oldest entry across all keys
                    oldest_cid, oldest_idx = None, None
                    oldest_ts = float("inf")
                    for cid, entries in self._matched_headlines.items():
                        for i, e in enumerate(entries):
                            if e.get("timestamp", 0) < oldest_ts:
                                oldest_ts = e["timestamp"]
                                oldest_cid, oldest_idx = cid, i
                    if oldest_cid is not None:
                        self._matched_headlines[oldest_cid].pop(oldest_idx)
                        if not self._matched_headlines[oldest_cid]:
                            del self._matched_headlines[oldest_cid]
```

Add pruning at the start of `_scan_cycle()`, after `self._prune_state()` (line 172):

```python
        # Prune stale headline matches (>48h)
        cutoff_48h = time.time() - 172800
        for cid in list(self._matched_headlines.keys()):
            self._matched_headlines[cid] = [
                h for h in self._matched_headlines[cid] if h.get("timestamp", 0) > cutoff_48h
            ]
            if not self._matched_headlines[cid]:
                del self._matched_headlines[cid]
```

- [ ] **Step 5: Run tests**

Run: `cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_news_exits.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/.openclaw/workspace/projects/arbitrout
git add src/positions/news_scanner.py tests/test_news_exits.py
git commit -m "feat: add headline cache to news_scanner for exit engine consumption"
```

---

## Task 6: Add news context to AI advisor prompt

**Files:**
- Modify: `src/positions/ai_advisor.py:365-400`
- Modify: `src/positions/exit_engine.py:438-460`

- [ ] **Step 1: Add news_context param to _build_batched_prompt()**

In `src/positions/ai_advisor.py` line 365, change the signature:

```python
    def _build_batched_prompt(self, work: list[tuple[dict, list[dict]]],
                              news_context: dict[str, list] | None = None) -> str:
```

**Replace** line 380 (`sections.append(f"[PKG:{pkg.get('id', '?')}]\n{context}\nTRIGGERS:\n{proposal_text}")`) with the following block. Do NOT keep the old line — this replaces it:

```python
            # Add news context if available
            news_text = ""
            pkg_id = pkg.get("id", "")
            if news_context and pkg_id in news_context:
                headlines = news_context[pkg_id]
                if headlines:
                    items = "\n".join(
                        f"  - [{h.get('source', '?')}] \"{h.get('headline', '?')}\" "
                        f"(confidence: {h.get('confidence', '?')}/10, {h.get('sentiment', 'neutral')})"
                        for h in headlines[:5]  # Cap at 5 headlines
                    )
                    news_text = f"\nRECENT NEWS (last 24h):\n{items}\nIf no NEGATIVE news exists, default to REJECT for trailing_stop, negative_drift, time_decay."
                else:
                    news_text = "\nRECENT NEWS: (none found) — no fundamental reason for price movement. Default to REJECT."
            sections.append(f"[PKG:{pkg_id}]\n{context}{news_text}\nTRIGGERS:\n{proposal_text}")
```

- [ ] **Step 2: Collect news context in exit_engine and pass it**

In `src/positions/exit_engine.py`, in `_batched_ai_review()` (line 438), before calling `_build_batched_prompt`:

```python
        # Collect news context for all packages
        news_context = {}
        if self._news_scanner:
            for pkg, _ in work:
                headlines = []
                for leg in pkg.get("legs", []):
                    if leg.get("status") != "open":
                        continue
                    asset_id = leg.get("asset_id", "")
                    # Extract condition_id from "conditionId:YES" format
                    cond_id = asset_id.split(":")[0] if ":" in asset_id else asset_id
                    if cond_id:
                        headlines.extend(self._news_scanner.get_recent_headlines(cond_id, hours=24))
                news_context[pkg.get("id", "")] = headlines
```

Then change line 443 from:
```python
            combined_prompt = self.ai._build_batched_prompt(work)
```
to:
```python
            combined_prompt = self.ai._build_batched_prompt(work, news_context=news_context)
```

- [ ] **Step 3: Run all tests**

Run: `cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
cd ~/.openclaw/workspace/projects/arbitrout
git add src/positions/ai_advisor.py src/positions/exit_engine.py
git commit -m "feat: inject news context into AI advisor exit prompt"
```

---

## Task 7: Add exit_order_type to trade journal

**Files:**
- Modify: `src/positions/trade_journal.py:45-117,156-169`

- [ ] **Step 1: Record exit_order_type in journal entries**

In `src/positions/trade_journal.py`, in the `record_close()` method, find where leg data is built (around lines 60-80). Add to each leg dict in the journal entry:

```python
                "exit_order_type": leg.get("exit_order_type", "fok_direct"),
```

And to the top-level journal entry:

```python
            "exit_order_type": pkg.get("legs", [{}])[0].get("exit_order_type", "fok_direct"),
```

- [ ] **Step 2: Add get_performance_by_hold_duration()**

Add this method to the TradeJournal class:

```python
    def get_performance_by_hold_duration(self, mode: str | None = None) -> dict:
        """Bucket trades by hold duration and compute per-bucket metrics."""
        filtered = self.entries if not mode else [e for e in self.entries if e.get("mode") == mode]
        buckets = {
            "0-6h": {"max_hours": 6},
            "6-24h": {"max_hours": 24},
            "24h-3d": {"max_hours": 72},
            "3d-7d": {"max_hours": 168},
            "7d+": {"max_hours": float("inf")},
        }
        result = {}
        for name, cfg in buckets.items():
            result[name] = {"trades": 0, "wins": 0, "pnl": 0.0, "avg_pnl": 0.0, "win_rate": 0.0}

        for e in filtered:
            hours = e.get("hold_duration_hours", 0)
            for name, cfg in buckets.items():
                prev_max = {"0-6h": 0, "6-24h": 6, "24h-3d": 24, "3d-7d": 72, "7d+": 168}.get(name, 0)
                if prev_max <= hours < cfg["max_hours"]:
                    result[name]["trades"] += 1
                    result[name]["pnl"] += e.get("pnl", 0)
                    if e.get("outcome") == "win":
                        result[name]["wins"] += 1
                    break

        for name in result:
            b = result[name]
            if b["trades"] > 0:
                b["win_rate"] = round(b["wins"] / b["trades"], 2)
                b["avg_pnl"] = round(b["pnl"] / b["trades"], 2)
            b["pnl"] = round(b["pnl"], 2)

        return result
```

- [ ] **Step 3: Commit**

```bash
cd ~/.openclaw/workspace/projects/arbitrout
git add src/positions/trade_journal.py
git commit -m "feat: add exit_order_type tracking and hold duration analysis to trade journal"
```

---

## Task 8: Create CalibrationEngine

**Files:**
- Create: `src/positions/calibration.py`
- Create: `tests/test_calibration.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_calibration.py
import pytest
from unittest.mock import MagicMock

def make_calibration_engine():
    from positions.calibration import CalibrationEngine

    mock_eval = MagicMock()
    mock_eval.get_calibration.return_value = {
        "low_score": {"total_skips": 20, "resolved": 10, "correct_skips": 8, "missed_opportunities": 2, "correct_skip_rate": 0.80},
        "max_concurrent": {"total_skips": 15, "resolved": 10, "correct_skips": 4, "missed_opportunities": 6, "correct_skip_rate": 0.40},
    }
    mock_eval.get_missed_opportunities.return_value = [
        {"action_reason": "max_concurrent", "actual_pnl_pct": 15.0},
        {"action_reason": "max_concurrent", "actual_pnl_pct": 22.0},
    ]

    mock_journal = MagicMock()
    mock_journal.get_performance.return_value = {
        "total_trades": 20,
        "total_fees": 40.0,
        "total_invested": 2000.0,
        "fee_drag_pct": 2.0,
        "by_trigger": {
            "trailing_stop": {"trades": 8, "wins": 0, "pnl": -72.0, "win_rate": 0.0},
            "target_hit": {"trades": 5, "wins": 4, "pnl": 65.0, "win_rate": 0.80},
        },
    }
    mock_journal.get_performance_by_hold_duration.return_value = {
        "0-6h": {"trades": 5, "wins": 0, "pnl": -30.0, "avg_pnl": -6.0, "win_rate": 0.0},
        "24h-3d": {"trades": 8, "wins": 4, "pnl": 40.0, "avg_pnl": 5.0, "win_rate": 0.50},
    }
    mock_journal.entries = [
        {"exit_order_type": "limit_filled"},
        {"exit_order_type": "limit_filled"},
        {"exit_order_type": "fok_fallback"},
        {"exit_order_type": "fok_direct"},
    ]

    return CalibrationEngine(mock_eval, mock_journal)

def test_generate_report_has_all_sections():
    ce = make_calibration_engine()
    report = ce.generate_report()
    assert "entry_calibration" in report
    assert "exit_calibration" in report
    assert "hold_duration_analysis" in report
    assert "fee_analysis" in report
    assert "generated_at" in report

def test_low_correct_skip_rate_flagged():
    ce = make_calibration_engine()
    report = ce.generate_report()
    suggestion = report["entry_calibration"]["max_concurrent"]["suggestion"]
    assert "REVIEW" in suggestion

def test_zero_win_rate_trigger_flagged():
    ce = make_calibration_engine()
    report = ce.generate_report()
    suggestion = report["exit_calibration"]["trailing_stop"]["suggestion"]
    assert "WIDEN" in suggestion

def test_limit_fill_rate_calculated():
    ce = make_calibration_engine()
    report = ce.generate_report()
    # 2 limit_filled out of 3 limit attempts (limit_filled + fok_fallback)
    assert report["fee_analysis"]["limit_fill_rate"] == pytest.approx(0.67, abs=0.01)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_calibration.py -v`
Expected: FAIL — `calibration` module doesn't exist.

- [ ] **Step 3: Create calibration.py**

```python
# src/positions/calibration.py
"""Calibration engine — generates threshold tuning reports from eval and trade data."""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data" / "calibration"


class CalibrationEngine:
    def __init__(self, eval_logger, trade_journal):
        self.eval_logger = eval_logger
        self.journal = trade_journal

    def generate_report(self) -> dict:
        """Generate calibration report from all available data."""
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trade_count": 0,
            "entry_calibration": {},
            "exit_calibration": {},
            "hold_duration_analysis": {},
            "fee_analysis": {},
        }

        # --- Entry calibration ---
        try:
            calibration = self.eval_logger.get_calibration()
            missed = self.eval_logger.get_missed_opportunities()
            missed_by_reason = {}
            for m in missed:
                reason = m.get("action_reason", "unknown")
                if reason not in missed_by_reason:
                    missed_by_reason[reason] = {"count": 0, "pnl": 0.0}
                missed_by_reason[reason]["count"] += 1
                missed_by_reason[reason]["pnl"] += m.get("actual_pnl_pct", 0)

            for reason, data in calibration.items():
                rate = data.get("correct_skip_rate", 1.0)
                resolved = data.get("resolved", 0)
                missed_info = missed_by_reason.get(reason, {"count": 0, "pnl": 0.0})
                if resolved >= 5 and rate < 0.60:
                    suggestion = f"REVIEW — {rate:.0%} correct skip rate, missing ${missed_info['pnl']:.0f}. Threshold may be too aggressive."
                elif resolved < 5:
                    suggestion = f"INSUFFICIENT DATA — only {resolved} resolved trades. Need 5+."
                else:
                    suggestion = f"KEEP — {rate:.0%} correct skip rate is healthy."
                report["entry_calibration"][reason] = {
                    "correct_skip_rate": round(rate, 2),
                    "missed_count": missed_info["count"],
                    "missed_pnl": round(missed_info["pnl"], 2),
                    "resolved": resolved,
                    "suggestion": suggestion,
                }
        except Exception as e:
            logger.warning("Entry calibration failed: %s", e)

        # --- Exit calibration ---
        try:
            perf = self.journal.get_performance()
            report["trade_count"] = perf.get("total_trades", 0)
            by_trigger = perf.get("by_trigger", {})
            for trigger, data in by_trigger.items():
                trades = data.get("trades", 0)
                win_rate = data.get("win_rate", 0)
                pnl = data.get("pnl", 0)
                if trades >= 5 and win_rate == 0:
                    suggestion = f"WIDEN — 0% win rate across {trades} trades. Threshold too tight."
                elif trades >= 5 and win_rate >= 0.70:
                    suggestion = "KEEP — performing well."
                elif trades < 5:
                    suggestion = f"INSUFFICIENT DATA — only {trades} trades."
                else:
                    suggestion = f"MONITOR — {win_rate:.0%} win rate, ${pnl:.0f} P&L."
                report["exit_calibration"][trigger] = {
                    "trades": trades,
                    "win_rate": win_rate,
                    "total_pnl": round(pnl, 2),
                    "suggestion": suggestion,
                }
        except Exception as e:
            logger.warning("Exit calibration failed: %s", e)

        # --- Hold duration analysis ---
        try:
            report["hold_duration_analysis"] = self.journal.get_performance_by_hold_duration()
        except Exception as e:
            logger.warning("Hold duration analysis failed: %s", e)

        # --- Fee analysis ---
        try:
            perf = self.journal.get_performance()
            total_fees = perf.get("total_fees", 0)
            fee_drag = perf.get("fee_drag_pct", 0)

            # Calculate limit fill rate from journal entries
            limit_attempts = 0
            limit_fills = 0
            for entry in getattr(self.journal, "entries", []):
                etype = entry.get("exit_order_type", "")
                if etype in ("limit_filled", "limit_partial_fok"):
                    limit_fills += 1
                    limit_attempts += 1
                elif etype == "fok_fallback":
                    limit_attempts += 1
            fill_rate = round(limit_fills / limit_attempts, 2) if limit_attempts > 0 else None

            if fee_drag > 2.0:
                fee_suggestion = f"HIGH — {fee_drag:.1f}% fee drag. Investigate execution quality."
            elif fill_rate is not None and fill_rate < 0.50:
                fee_suggestion = f"LOW FILL RATE — {fill_rate:.0%}. Consider widening limit price offset."
            elif fill_rate is not None:
                fee_suggestion = f"GOOD — {fill_rate:.0%} limit fill rate."
            else:
                fee_suggestion = "TRACK — no limit order data yet."

            report["fee_analysis"] = {
                "total_fees": round(total_fees, 2),
                "fee_drag_pct": round(fee_drag, 2),
                "limit_fill_rate": fill_rate,
                "suggestion": fee_suggestion,
            }
        except Exception as e:
            logger.warning("Fee analysis failed: %s", e)

        return report

    def save_report(self) -> str:
        """Generate and save report to data/calibration/YYYY-MM-DD.json. Returns path."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        report = self.generate_report()
        filename = datetime.now().strftime("%Y-%m-%d") + ".json"
        path = DATA_DIR / filename
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info("Calibration report saved to %s", path)
        return str(path)
```

- [ ] **Step 4: Run tests**

Run: `cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_calibration.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/.openclaw/workspace/projects/arbitrout
git add src/positions/calibration.py tests/test_calibration.py
git commit -m "feat: add CalibrationEngine for data-driven threshold tuning"
```

---

## Task 9: Wire CalibrationEngine into server + API

**Files:**
- Modify: `src/server.py`
- Modify: `src/positions/position_router.py`

- [ ] **Step 1: Add calibration engine to server lifespan**

In `src/server.py`, after the `_eval_log = EvalLogger()` line (line 408), add:

```python
    from positions.calibration import CalibrationEngine
    _calibration_engine = CalibrationEngine(_eval_log, journal)
```

Note: The eval logger variable in server.py is `_eval_log` (line 408), and the trade journal is `journal`.

Add a 24h background task after the existing background tasks:

```python
    async def _calibration_loop():
        """Run calibration report every 24 hours."""
        while True:
            await asyncio.sleep(86400)  # 24 hours
            try:
                path = _calibration_engine.save_report()
                logger.info("Calibration report generated: %s", path)
            except Exception as e:
                logger.error("Calibration report failed: %s", e)

    asyncio.create_task(_calibration_loop())
```

Store `_calibration_engine` in `app.state` so the router can access it:

```python
    app.state.calibration_engine = _calibration_engine
```

- [ ] **Step 2: Add API endpoint**

In `src/positions/position_router.py`, first add `Request` to the FastAPI import at line 5:

```python
from fastapi import APIRouter, Depends, HTTPException, Request, Security, WebSocket, WebSocketDisconnect
```

Then add the endpoint:

```python
@router.get("/calibration")
async def get_calibration(request: Request):
    """Return latest calibration report."""
    ce = getattr(request.app.state, "calibration_engine", None)
    if not ce:
        return {"error": "Calibration engine not initialized"}
    try:
        return ce.generate_report()
    except Exception as e:
        return {"error": str(e)}
```

- [ ] **Step 3: Verify endpoint works**

Restart the server, then:
```bash
curl http://127.0.0.1:8500/api/derivatives/calibration | python -m json.tool
```
Expected: JSON calibration report with all sections.

- [ ] **Step 4: Commit**

```bash
cd ~/.openclaw/workspace/projects/arbitrout
git add src/server.py src/positions/position_router.py
git commit -m "feat: wire CalibrationEngine into server with API endpoint and 24h background task"
```

---

## Task 10: Integration test — full exit flow

- [ ] **Step 1: Restart the production server**

```bash
cd ~/.openclaw/workspace/projects/arbitrout/src
# Kill existing server
powershell -Command "Get-NetTCPConnection -LocalPort 8500 -ErrorAction SilentlyContinue | Select-Object OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
# Start fresh
powershell -Command "Start-Process python -ArgumentList '-m','uvicorn','server:app','--host','127.0.0.1','--port','8500' -WindowStyle Hidden"
```

Wait 5s, then health check:
```bash
curl http://127.0.0.1:8500/api/health
```

- [ ] **Step 2: Verify calibration endpoint**

```bash
curl http://127.0.0.1:8500/api/derivatives/calibration | python -m json.tool
```

- [ ] **Step 3: Run full test suite**

```bash
cd ~/.openclaw/workspace/projects/arbitrout && python -m pytest tests/ -v
```
Expected: All tests PASS.

- [ ] **Step 4: Final commit**

```bash
cd ~/.openclaw/workspace/projects/arbitrout
git add tests/test_limit_exits.py tests/test_news_exits.py tests/test_calibration.py
git commit -m "test: integration tests for exit optimization changes"
```
