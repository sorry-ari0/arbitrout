# Derivative Position Manager Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a derivative position manager with AI-driven auto-exit for prediction market arbitrage packages

**Architecture:** Layered engine — executors (buy/sell per platform) → position manager (CRUD, rollback) → exit engine (30s loop, 18 heuristic triggers) → AI advisor (Claude API review) → FastAPI + WebSocket dashboard. Paper trading mode by default.

**Tech Stack:** Python 3.11+, FastAPI, anthropic SDK, py-clob-client, kalshi-python, coinbase-advanced-py, httpx, pytest

---

## File Structure

### Create:
- `src/execution/base_executor.py` — ABC + ExecutionResult/BalanceResult/PositionInfo dataclasses
- `src/execution/paper_executor.py` — simulation wrapper using real prices
- `src/execution/kalshi_executor.py` — Kalshi RSA auth buy/sell
- `src/execution/coinbase_spot_executor.py` — Coinbase Advanced Trade spot crypto
- `src/execution/predictit_executor.py` — PredictIt session auth, 850-share cap
- `src/execution/robinhood_advisor.py` — advisory only, no execution
- `src/positions/__init__.py` — package init
- `src/positions/wallet_config.py` — env var loading + platform availability
- `src/positions/position_manager.py` — data models + CRUD + balance + rollback
- `src/positions/exit_engine.py` — 30s loop + 18 heuristic triggers + exit execution
- `src/positions/ai_advisor.py` — Claude API + batching + guardrails
- `src/positions/position_router.py` — FastAPI router + WebSocket
- `tests/conftest.py` — shared path setup
- `tests/test_base_executor.py`
- `tests/test_paper_executor.py`
- `tests/test_position_manager.py`
- `tests/test_exit_engine.py`
- `tests/test_ai_advisor.py`

### Modify:
- `src/execution/polymarket_executor.py` — full async rewrite
- `src/server.py` — include position_router, CORS PATCH, exit_engine lifespan
- `src/static/js/arbitrout.js` — positions dashboard tab
- `src/static/css/arbitrout.css` — positions styles
- `src/requirements.txt` — add new dependencies

### Delete:
- `src/execution/wallet_config.py` — empty file, functionality moved to src/positions/

---

## Chunk 1: Execution Foundation

### Task 1: Execution Models and Base Executor

**Files:**
- Create: `src/execution/base_executor.py`
- Create: `tests/conftest.py`
- Create: `tests/test_base_executor.py`

- [ ] **Step 1: Create test conftest and write failing test**

`tests/conftest.py`:
```python
"""Shared test configuration."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
```

`tests/test_base_executor.py`:
```python
"""Tests for execution models and BaseExecutor ABC."""
import pytest
from execution.base_executor import (
    ExecutionResult, BalanceResult, PositionInfo, BaseExecutor,
)


class TestExecutionResult:
    def test_success_result(self):
        r = ExecutionResult(
            success=True, tx_id="tx_123", filled_price=0.65,
            filled_quantity=10.0, fees=0.02, error=None,
        )
        assert r.success is True
        assert r.tx_id == "tx_123"
        assert r.filled_price == 0.65
        assert r.fees == 0.02

    def test_failure_result(self):
        r = ExecutionResult(
            success=False, tx_id=None, filled_price=0.0,
            filled_quantity=0.0, fees=0.0, error="Insufficient balance",
        )
        assert r.success is False
        assert r.error == "Insufficient balance"

    def test_to_dict(self):
        r = ExecutionResult(
            success=True, tx_id="tx_1", filled_price=0.5,
            filled_quantity=5.0, fees=0.01, error=None,
        )
        d = r.to_dict()
        assert d["success"] is True
        assert d["tx_id"] == "tx_1"


class TestBalanceResult:
    def test_balance(self):
        b = BalanceResult(available=100.0, total=150.0)
        assert b.available == 100.0
        assert b.total == 150.0


class TestPositionInfo:
    def test_position(self):
        p = PositionInfo(
            asset_id="BTC", quantity=0.001, avg_entry_price=97000.0,
            current_price=99000.0, unrealized_pnl=2.0,
        )
        assert p.asset_id == "BTC"
        assert p.unrealized_pnl == 2.0


class TestBaseExecutor:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            BaseExecutor()

    def test_subclass_must_implement_all(self):
        class Incomplete(BaseExecutor):
            pass
        with pytest.raises(TypeError):
            Incomplete()

    def test_valid_subclass(self):
        class Stub(BaseExecutor):
            async def buy(self, asset_id, amount_usd):
                return ExecutionResult(True, "t", 1.0, 1.0, 0.0, None)
            async def sell(self, asset_id, quantity):
                return ExecutionResult(True, "t", 1.0, 1.0, 0.0, None)
            async def get_balance(self):
                return BalanceResult(100.0, 100.0)
            async def get_positions(self):
                return []
            async def get_current_price(self, asset_id):
                return 1.0
            def is_configured(self):
                return True
        s = Stub()
        assert s.is_configured()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout && python -m pytest tests/test_base_executor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.base_executor'`

- [ ] **Step 3: Write base_executor.py**

`src/execution/base_executor.py`:
```python
"""Base executor ABC and shared dataclasses for all platform executors."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict


@dataclass
class ExecutionResult:
    """Result of a buy or sell operation."""
    success: bool
    tx_id: str | None
    filled_price: float
    filled_quantity: float
    fees: float
    error: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BalanceResult:
    """Platform account balance."""
    available: float
    total: float


@dataclass
class PositionInfo:
    """A single position on a platform."""
    asset_id: str
    quantity: float
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float


class BaseExecutor(ABC):
    """Abstract base for all platform executors. All trade methods are async."""

    @abstractmethod
    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        ...

    @abstractmethod
    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        ...

    @abstractmethod
    async def get_balance(self) -> BalanceResult:
        ...

    @abstractmethod
    async def get_positions(self) -> list[PositionInfo]:
        ...

    @abstractmethod
    async def get_current_price(self, asset_id: str) -> float:
        ...

    @abstractmethod
    def is_configured(self) -> bool:
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout && python -m pytest tests/test_base_executor.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout
git add src/execution/base_executor.py tests/conftest.py tests/test_base_executor.py
git commit -m "feat: add BaseExecutor ABC and execution dataclasses"
```

---

### Task 2: Wallet Config

**Files:**
- Create: `src/positions/__init__.py`
- Create: `src/positions/wallet_config.py`
- Create: `tests/test_wallet_config.py`
- Delete: `src/execution/wallet_config.py`

- [ ] **Step 1: Write failing test**

`tests/test_wallet_config.py`:
```python
"""Tests for wallet configuration and platform detection."""
import os
import pytest
from positions.wallet_config import get_configured_platforms, is_paper_mode, get_paper_balance


class TestPaperMode:
    def test_paper_mode_default_true(self, monkeypatch):
        monkeypatch.delenv("PAPER_TRADING", raising=False)
        assert is_paper_mode() is True

    def test_paper_mode_explicit_true(self, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "true")
        assert is_paper_mode() is True

    def test_paper_mode_false(self, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "false")
        assert is_paper_mode() is False

    def test_paper_balance_default(self, monkeypatch):
        monkeypatch.delenv("PAPER_STARTING_BALANCE", raising=False)
        assert get_paper_balance() == 10000.0

    def test_paper_balance_custom(self, monkeypatch):
        monkeypatch.setenv("PAPER_STARTING_BALANCE", "5000")
        assert get_paper_balance() == 5000.0


class TestConfiguredPlatforms:
    def test_no_keys_set(self, monkeypatch):
        for key in ["POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS",
                     "KALSHI_API_KEY", "KALSHI_RSA_PRIVATE_KEY",
                     "COINBASE_ADV_API_KEY", "COINBASE_ADV_API_SECRET",
                     "PREDICTIT_SESSION", "ANTHROPIC_API_KEY"]:
            monkeypatch.delenv(key, raising=False)
        platforms = get_configured_platforms()
        assert platforms == {}

    def test_polymarket_configured(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
        monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", "0xdef")
        platforms = get_configured_platforms()
        assert "polymarket" in platforms
        assert platforms["polymarket"] is True

    def test_polymarket_partial(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
        monkeypatch.delenv("POLYMARKET_FUNDER_ADDRESS", raising=False)
        platforms = get_configured_platforms()
        assert "polymarket" not in platforms
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_wallet_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'positions'`

- [ ] **Step 3: Create positions package and wallet_config**

`src/positions/__init__.py`:
```python
"""Positions package — derivative position management and auto-exit."""
```

`src/positions/wallet_config.py`:
```python
"""Wallet configuration — env var loading and platform availability detection."""
import os


# Platform credential requirements: {platform_id: [required_env_vars]}
PLATFORM_CREDENTIALS = {
    "polymarket": ["POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS"],
    "kalshi": ["KALSHI_API_KEY", "KALSHI_RSA_PRIVATE_KEY"],
    "coinbase_spot": ["COINBASE_ADV_API_KEY", "COINBASE_ADV_API_SECRET"],
    "predictit": ["PREDICTIT_SESSION"],
}


def is_paper_mode() -> bool:
    """Check if paper trading mode is active (default: True)."""
    return os.environ.get("PAPER_TRADING", "true").lower() != "false"


def get_paper_balance() -> float:
    """Get starting balance for paper trading."""
    try:
        return float(os.environ.get("PAPER_STARTING_BALANCE", "10000"))
    except ValueError:
        return 10000.0


def get_configured_platforms() -> dict[str, bool]:
    """Return dict of platform_id -> True for platforms with all credentials set."""
    configured = {}
    for platform, keys in PLATFORM_CREDENTIALS.items():
        if all(os.environ.get(k, "") for k in keys):
            configured[platform] = True
    return configured


def has_anthropic_key() -> bool:
    """Check if Anthropic API key is set for AI advisor."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", ""))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_wallet_config.py -v`
Expected: 6 passed

- [ ] **Step 5: Delete empty wallet_config from execution package**

```bash
rm src/execution/wallet_config.py
```

- [ ] **Step 6: Commit**

```bash
git add src/positions/__init__.py src/positions/wallet_config.py tests/test_wallet_config.py
git rm src/execution/wallet_config.py
git commit -m "feat: add wallet_config with platform detection and paper mode"
```

---

### Task 3: Paper Executor

**Files:**
- Create: `src/execution/paper_executor.py`
- Create: `tests/test_paper_executor.py`

- [ ] **Step 1: Write failing test**

`tests/test_paper_executor.py`:
```python
"""Tests for PaperExecutor — simulated trading with real prices."""
import pytest
import asyncio
from execution.base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo
from execution.paper_executor import PaperExecutor


class FakeExecutor(BaseExecutor):
    """Fake executor returning fixed prices for testing."""
    def __init__(self, prices: dict[str, float] | None = None):
        self._prices = prices or {"BTC": 97000.0, "token_yes_abc": 0.65}

    async def buy(self, asset_id, amount_usd):
        return ExecutionResult(True, "real_tx", self._prices.get(asset_id, 1.0), 1.0, 0.01, None)

    async def sell(self, asset_id, quantity):
        return ExecutionResult(True, "real_tx", self._prices.get(asset_id, 1.0), quantity, 0.01, None)

    async def get_balance(self):
        return BalanceResult(1000.0, 1000.0)

    async def get_positions(self):
        return []

    async def get_current_price(self, asset_id):
        return self._prices.get(asset_id, 1.0)

    def is_configured(self):
        return True


@pytest.fixture
def paper():
    return PaperExecutor(FakeExecutor(), starting_balance=1000.0)


class TestPaperBuy:
    def test_buy_deducts_balance(self, paper):
        result = asyncio.get_event_loop().run_until_complete(
            paper.buy("token_yes_abc", 100.0)
        )
        assert result.success is True
        assert result.filled_price == 0.65
        assert result.fees == 0.0
        assert result.tx_id.startswith("paper_")
        bal = asyncio.get_event_loop().run_until_complete(paper.get_balance())
        assert bal.available == pytest.approx(900.0)

    def test_buy_insufficient_balance(self, paper):
        result = asyncio.get_event_loop().run_until_complete(
            paper.buy("BTC", 2000.0)
        )
        assert result.success is False
        assert "Insufficient" in result.error


class TestPaperSell:
    def test_sell_after_buy(self, paper):
        asyncio.get_event_loop().run_until_complete(
            paper.buy("token_yes_abc", 100.0)
        )
        # quantity bought = 100 / 0.65 = ~153.85
        result = asyncio.get_event_loop().run_until_complete(
            paper.sell("token_yes_abc", 50.0)
        )
        assert result.success is True
        assert result.filled_price == 0.65
        # proceeds = 50 * 0.65 = 32.50, balance = 900 + 32.50 = 932.50
        bal = asyncio.get_event_loop().run_until_complete(paper.get_balance())
        assert bal.available == pytest.approx(932.5)

    def test_sell_no_position(self, paper):
        result = asyncio.get_event_loop().run_until_complete(
            paper.sell("BTC", 1.0)
        )
        assert result.success is False
        assert "No position" in result.error


class TestPaperPrice:
    def test_uses_real_prices(self, paper):
        price = asyncio.get_event_loop().run_until_complete(
            paper.get_current_price("BTC")
        )
        assert price == 97000.0


class TestPaperConfig:
    def test_always_configured(self, paper):
        assert paper.is_configured() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_paper_executor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.paper_executor'`

- [ ] **Step 3: Write paper_executor.py**

`src/execution/paper_executor.py`:
```python
"""Paper executor — wraps a real executor for simulated trading.

Uses real market prices from the underlying executor but simulates
all buy/sell operations with a fake balance. Zero fees in paper mode.
"""
import logging
import uuid

from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.paper")


class PaperExecutor(BaseExecutor):
    """Simulated executor for paper trading. Real prices, fake money."""

    def __init__(self, real_executor: BaseExecutor, starting_balance: float = 10000.0):
        self.real = real_executor
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.positions: dict[str, dict] = {}  # asset_id -> {quantity, avg_entry_price}
        self.trade_history: list[dict] = []

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        if amount_usd > self.balance:
            return ExecutionResult(
                success=False, tx_id=None, filled_price=0.0,
                filled_quantity=0.0, fees=0.0,
                error=f"Insufficient paper balance: {self.balance:.2f} < {amount_usd:.2f}",
            )
        price = await self.real.get_current_price(asset_id)
        if price <= 0:
            return ExecutionResult(
                success=False, tx_id=None, filled_price=0.0,
                filled_quantity=0.0, fees=0.0, error=f"Invalid price for {asset_id}",
            )
        quantity = amount_usd / price
        self.balance -= amount_usd

        # Update position
        pos = self.positions.get(asset_id)
        if pos:
            total_qty = pos["quantity"] + quantity
            pos["avg_entry_price"] = (
                (pos["avg_entry_price"] * pos["quantity"] + price * quantity) / total_qty
            )
            pos["quantity"] = total_qty
        else:
            self.positions[asset_id] = {"quantity": quantity, "avg_entry_price": price}

        tx_id = f"paper_{uuid.uuid4().hex[:12]}"
        self.trade_history.append({
            "action": "buy", "asset_id": asset_id, "price": price,
            "quantity": quantity, "amount_usd": amount_usd, "tx_id": tx_id,
        })
        logger.info("Paper BUY: %s qty=%.6f @ %.4f ($%.2f)", asset_id, quantity, price, amount_usd)
        return ExecutionResult(
            success=True, tx_id=tx_id, filled_price=price,
            filled_quantity=quantity, fees=0.0, error=None,
        )

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        pos = self.positions.get(asset_id)
        if not pos or pos["quantity"] < quantity * 0.999:  # 0.1% tolerance for float
            return ExecutionResult(
                success=False, tx_id=None, filled_price=0.0,
                filled_quantity=0.0, fees=0.0,
                error=f"No position or insufficient quantity for {asset_id}",
            )
        price = await self.real.get_current_price(asset_id)
        proceeds = quantity * price
        self.balance += proceeds
        pos["quantity"] -= quantity
        if pos["quantity"] < 1e-10:
            del self.positions[asset_id]

        tx_id = f"paper_{uuid.uuid4().hex[:12]}"
        self.trade_history.append({
            "action": "sell", "asset_id": asset_id, "price": price,
            "quantity": quantity, "proceeds_usd": proceeds, "tx_id": tx_id,
        })
        logger.info("Paper SELL: %s qty=%.6f @ %.4f ($%.2f)", asset_id, quantity, price, proceeds)
        return ExecutionResult(
            success=True, tx_id=tx_id, filled_price=price,
            filled_quantity=quantity, fees=0.0, error=None,
        )

    async def get_balance(self) -> BalanceResult:
        # Total includes value of open positions
        position_value = 0.0
        for asset_id, pos in self.positions.items():
            try:
                price = await self.real.get_current_price(asset_id)
                position_value += pos["quantity"] * price
            except Exception:
                position_value += pos["quantity"] * pos["avg_entry_price"]
        return BalanceResult(available=self.balance, total=self.balance + position_value)

    async def get_positions(self) -> list[PositionInfo]:
        result = []
        for asset_id, pos in self.positions.items():
            try:
                price = await self.real.get_current_price(asset_id)
            except Exception:
                price = pos["avg_entry_price"]
            pnl = (price - pos["avg_entry_price"]) * pos["quantity"]
            result.append(PositionInfo(
                asset_id=asset_id, quantity=pos["quantity"],
                avg_entry_price=pos["avg_entry_price"],
                current_price=price, unrealized_pnl=pnl,
            ))
        return result

    async def get_current_price(self, asset_id: str) -> float:
        return await self.real.get_current_price(asset_id)

    def is_configured(self) -> bool:
        return True

    def get_stats(self) -> dict:
        """Paper trading performance stats."""
        total_pnl = self.balance - self.starting_balance
        for pos in self.positions.values():
            total_pnl += pos["quantity"] * pos["avg_entry_price"]  # approximate
        wins = sum(1 for t in self.trade_history if t["action"] == "sell"
                   and t.get("proceeds_usd", 0) > 0)
        sells = sum(1 for t in self.trade_history if t["action"] == "sell")
        return {
            "mode": "paper",
            "starting_balance": self.starting_balance,
            "current_balance": self.balance,
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / self.starting_balance * 100, 2) if self.starting_balance else 0,
            "total_trades": len(self.trade_history),
            "win_rate": round(wins / sells, 2) if sells else 0,
            "open_positions": len(self.positions),
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_paper_executor.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/execution/paper_executor.py tests/test_paper_executor.py
git commit -m "feat: add PaperExecutor for simulated paper trading"
```

---

### Task 4: Polymarket Executor Rewrite

**Files:**
- Modify: `src/execution/polymarket_executor.py` (full rewrite)

- [ ] **Step 1: Write the full async executor**

`src/execution/polymarket_executor.py`:
```python
"""Polymarket CLOB executor — async buy/sell via Polygon chain.

Uses py-clob-client for CLOB interaction. Prefers maker (limit) orders
for zero fees + rebates. Falls back to market (taker) orders for urgent exits.
Dynamic taker fee up to ~1.56% at 50% probability.
"""
import logging
import os

import httpx

from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.polymarket")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


class PolymarketExecutor(BaseExecutor):
    """Polymarket prediction market executor with CLOB buy+sell."""

    def __init__(self):
        self._private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        self._funder_address = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
        self._client = None
        self._http = None

    def is_configured(self) -> bool:
        return bool(self._private_key and self._funder_address)

    def _get_clob_client(self):
        """Lazy-init the CLOB client. Requires py-clob-client package."""
        if not self.is_configured():
            raise RuntimeError("Polymarket not configured")
        if self._client is None:
            from py_clob_client.client import ClobClient
            self._client = ClobClient(
                self._private_key, self._funder_address,
                CLOB_API, chain_id=137,
            )
        return self._client

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                headers={"User-Agent": "Arbitrout/1.0"},
            )
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        """Buy a YES or NO token on Polymarket CLOB.

        asset_id format: "{token_id}:{side}" e.g. "0xabc123:YES" or "0xabc123:NO"
        """
        try:
            token_id, side = asset_id.rsplit(":", 1)
            client = self._get_clob_client()
            # Place limit order at current price (maker = zero fees)
            order = client.create_and_post_order({
                "token_id": token_id,
                "side": side.upper(),
                "size": amount_usd,
                "price": None,  # market order
                "type": "FOK",
            })
            return ExecutionResult(
                success=True,
                tx_id=order.get("id", order.get("orderID", "")),
                filled_price=float(order.get("price", 0)),
                filled_quantity=float(order.get("size", amount_usd)),
                fees=float(order.get("fee", 0)),
                error=None,
            )
        except Exception as exc:
            logger.error("Polymarket buy failed: %s", exc)
            return ExecutionResult(False, None, 0.0, 0.0, 0.0, str(exc))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        """Sell tokens on Polymarket CLOB. Prefers limit (maker) order."""
        try:
            token_id, side = asset_id.rsplit(":", 1)
            client = self._get_clob_client()
            # Post sell-side limit order
            order = client.create_and_post_order({
                "token_id": token_id,
                "side": "SELL",
                "size": quantity,
                "price": None,
                "type": "FOK",
            })
            return ExecutionResult(
                success=True,
                tx_id=order.get("id", order.get("orderID", "")),
                filled_price=float(order.get("price", 0)),
                filled_quantity=float(order.get("size", quantity)),
                fees=float(order.get("fee", 0)),
                error=None,
            )
        except Exception as exc:
            logger.error("Polymarket sell failed: %s", exc)
            return ExecutionResult(False, None, 0.0, 0.0, 0.0, str(exc))

    async def get_balance(self) -> BalanceResult:
        try:
            client = self._get_clob_client()
            bal = client.get_balance()
            return BalanceResult(
                available=float(bal.get("available", 0)),
                total=float(bal.get("total", bal.get("available", 0))),
            )
        except Exception as exc:
            logger.error("Polymarket balance failed: %s", exc)
            return BalanceResult(0.0, 0.0)

    async def get_positions(self) -> list[PositionInfo]:
        try:
            client = self._get_clob_client()
            positions = client.get_positions()
            return [
                PositionInfo(
                    asset_id=p.get("asset_id", ""),
                    quantity=float(p.get("size", 0)),
                    avg_entry_price=float(p.get("avg_price", 0)),
                    current_price=float(p.get("cur_price", p.get("avg_price", 0))),
                    unrealized_pnl=float(p.get("pnl", 0)),
                )
                for p in (positions if isinstance(positions, list) else [])
            ]
        except Exception as exc:
            logger.error("Polymarket positions failed: %s", exc)
            return []

    async def get_current_price(self, asset_id: str) -> float:
        """Get current price from Gamma API (no auth required)."""
        try:
            token_id = asset_id.split(":")[0] if ":" in asset_id else asset_id
            http = await self._get_http()
            resp = await http.get(f"{GAMMA_API}/markets/{token_id}")
            if resp.status_code == 200:
                data = resp.json()
                return float(data.get("outcomePrices", [0.5, 0.5])[0])
        except Exception as exc:
            logger.warning("Polymarket price fetch failed: %s", exc)
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
```

- [ ] **Step 2: Verify the module loads without errors**

Run: `cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout\src && python -c "from execution.polymarket_executor import PolymarketExecutor; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/execution/polymarket_executor.py
git commit -m "feat: rewrite PolymarketExecutor with async buy/sell and BaseExecutor"
```

---

## Chunk 2: Platform Executors

### Task 5: Kalshi Executor

**Files:**
- Create: `src/execution/kalshi_executor.py`

- [ ] **Step 1: Write kalshi_executor.py**

`src/execution/kalshi_executor.py`:
```python
"""Kalshi exchange executor — RSA keypair auth, buy/sell YES/NO contracts."""
import logging
import os

import httpx

from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.kalshi")

KALSHI_API = "https://trading-api.kalshi.com/trade-api/v2"


class KalshiExecutor(BaseExecutor):
    """Kalshi prediction market executor. 0% fees currently."""

    def __init__(self):
        self._api_key = os.environ.get("KALSHI_API_KEY", "")
        self._rsa_key = os.environ.get("KALSHI_RSA_PRIVATE_KEY", "")
        self._http = None
        self._token = None

    def is_configured(self) -> bool:
        return bool(self._api_key and self._rsa_key)

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                base_url=KALSHI_API,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        """Buy YES or NO on Kalshi. asset_id format: 'ticker:YES' or 'ticker:NO'."""
        try:
            ticker, side = asset_id.rsplit(":", 1)
            http = await self._get_http()
            # Kalshi prices are in cents (1-99)
            resp = await http.post("/portfolio/orders", json={
                "ticker": ticker,
                "action": "buy",
                "side": side.lower(),
                "type": "market",
                "count": int(amount_usd * 100),  # convert to cents
            })
            resp.raise_for_status()
            data = resp.json().get("order", {})
            return ExecutionResult(
                success=True,
                tx_id=data.get("order_id", ""),
                filled_price=float(data.get("avg_price", 0)) / 100,
                filled_quantity=float(data.get("count", 0)),
                fees=0.0,
                error=None,
            )
        except Exception as exc:
            logger.error("Kalshi buy failed: %s", exc)
            return ExecutionResult(False, None, 0.0, 0.0, 0.0, str(exc))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        """Sell position on Kalshi by posting opposing order."""
        try:
            ticker, side = asset_id.rsplit(":", 1)
            http = await self._get_http()
            resp = await http.post("/portfolio/orders", json={
                "ticker": ticker,
                "action": "sell",
                "side": side.lower(),
                "type": "market",
                "count": int(quantity),
            })
            resp.raise_for_status()
            data = resp.json().get("order", {})
            return ExecutionResult(
                success=True,
                tx_id=data.get("order_id", ""),
                filled_price=float(data.get("avg_price", 0)) / 100,
                filled_quantity=float(data.get("count", 0)),
                fees=0.0,
                error=None,
            )
        except Exception as exc:
            logger.error("Kalshi sell failed: %s", exc)
            return ExecutionResult(False, None, 0.0, 0.0, 0.0, str(exc))

    async def get_balance(self) -> BalanceResult:
        try:
            http = await self._get_http()
            resp = await http.get("/portfolio/balance")
            resp.raise_for_status()
            data = resp.json()
            avail = float(data.get("available_balance", 0)) / 100
            total = float(data.get("portfolio_value", avail * 100)) / 100
            return BalanceResult(available=avail, total=total)
        except Exception as exc:
            logger.error("Kalshi balance failed: %s", exc)
            return BalanceResult(0.0, 0.0)

    async def get_positions(self) -> list[PositionInfo]:
        try:
            http = await self._get_http()
            resp = await http.get("/portfolio/positions")
            resp.raise_for_status()
            positions = resp.json().get("market_positions", [])
            return [
                PositionInfo(
                    asset_id=f"{p['ticker']}:{'YES' if p.get('position', 0) > 0 else 'NO'}",
                    quantity=abs(float(p.get("position", 0))),
                    avg_entry_price=float(p.get("average_price", 0)) / 100,
                    current_price=float(p.get("market_price", 0)) / 100,
                    unrealized_pnl=float(p.get("pnl", 0)) / 100,
                )
                for p in positions if p.get("position", 0) != 0
            ]
        except Exception as exc:
            logger.error("Kalshi positions failed: %s", exc)
            return []

    async def get_current_price(self, asset_id: str) -> float:
        """Get current market price from Kalshi public API."""
        try:
            ticker = asset_id.split(":")[0] if ":" in asset_id else asset_id
            http = await self._get_http()
            resp = await http.get(f"/markets/{ticker}")
            if resp.status_code == 200:
                data = resp.json().get("market", {})
                return float(data.get("last_price", data.get("yes_ask", 50))) / 100
        except Exception as exc:
            logger.warning("Kalshi price fetch failed: %s", exc)
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
```

- [ ] **Step 2: Verify module loads**

Run: `cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout\src && python -c "from execution.kalshi_executor import KalshiExecutor; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/execution/kalshi_executor.py
git commit -m "feat: add KalshiExecutor with RSA auth buy/sell"
```

---

### Task 6: Coinbase Spot Executor

**Files:**
- Create: `src/execution/coinbase_spot_executor.py`

- [ ] **Step 1: Write coinbase_spot_executor.py**

`src/execution/coinbase_spot_executor.py`:
```python
"""Coinbase Advanced Trade executor — spot crypto buy/sell.

DISTINCT from src/adapters/coinbase.py which fetches prediction market events.
This executor uses the Coinbase Advanced Trade REST API for actual spot trading.
"""
import logging
import os

import httpx

from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.coinbase_spot")

COINBASE_API = "https://api.coinbase.com/api/v3/brokerage"


class CoinbaseSpotExecutor(BaseExecutor):
    """Coinbase Advanced Trade spot crypto executor."""

    def __init__(self):
        self._api_key = os.environ.get("COINBASE_ADV_API_KEY", "")
        self._api_secret = os.environ.get("COINBASE_ADV_API_SECRET", "")
        self._http = None

    def is_configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                base_url=COINBASE_API,
                headers={
                    "CB-ACCESS-KEY": self._api_key,
                    "CB-ACCESS-SIGN": self._api_secret,
                    "Content-Type": "application/json",
                },
            )
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        """Market buy crypto. asset_id = coin ID like 'BTC' or 'ETH'."""
        try:
            product_id = f"{asset_id.upper()}-USD"
            http = await self._get_http()
            resp = await http.post("/orders", json={
                "product_id": product_id,
                "side": "BUY",
                "order_configuration": {
                    "market_market_ioc": {"quote_size": str(amount_usd)}
                },
            })
            resp.raise_for_status()
            data = resp.json()
            order = data.get("success_response", data)
            return ExecutionResult(
                success=True,
                tx_id=order.get("order_id", ""),
                filled_price=float(order.get("average_filled_price", 0)),
                filled_quantity=float(order.get("filled_size", 0)),
                fees=float(order.get("total_fees", 0)),
                error=None,
            )
        except Exception as exc:
            logger.error("Coinbase buy failed: %s", exc)
            return ExecutionResult(False, None, 0.0, 0.0, 0.0, str(exc))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        """Market sell crypto."""
        try:
            product_id = f"{asset_id.upper()}-USD"
            http = await self._get_http()
            resp = await http.post("/orders", json={
                "product_id": product_id,
                "side": "SELL",
                "order_configuration": {
                    "market_market_ioc": {"base_size": str(quantity)}
                },
            })
            resp.raise_for_status()
            data = resp.json()
            order = data.get("success_response", data)
            return ExecutionResult(
                success=True,
                tx_id=order.get("order_id", ""),
                filled_price=float(order.get("average_filled_price", 0)),
                filled_quantity=float(order.get("filled_size", 0)),
                fees=float(order.get("total_fees", 0)),
                error=None,
            )
        except Exception as exc:
            logger.error("Coinbase sell failed: %s", exc)
            return ExecutionResult(False, None, 0.0, 0.0, 0.0, str(exc))

    async def get_balance(self) -> BalanceResult:
        try:
            http = await self._get_http()
            resp = await http.get("/accounts")
            resp.raise_for_status()
            accounts = resp.json().get("accounts", [])
            usd = next((a for a in accounts if a.get("currency") == "USD"), None)
            avail = float(usd["available_balance"]["value"]) if usd else 0.0
            total = float(usd["hold"]["value"]) + avail if usd else 0.0
            return BalanceResult(available=avail, total=total)
        except Exception as exc:
            logger.error("Coinbase balance failed: %s", exc)
            return BalanceResult(0.0, 0.0)

    async def get_positions(self) -> list[PositionInfo]:
        try:
            http = await self._get_http()
            resp = await http.get("/accounts")
            resp.raise_for_status()
            accounts = resp.json().get("accounts", [])
            positions = []
            for a in accounts:
                if a.get("currency") == "USD":
                    continue
                qty = float(a.get("available_balance", {}).get("value", 0))
                if qty <= 0:
                    continue
                price = await self.get_current_price(a["currency"])
                positions.append(PositionInfo(
                    asset_id=a["currency"],
                    quantity=qty,
                    avg_entry_price=0.0,  # Coinbase doesn't expose cost basis
                    current_price=price,
                    unrealized_pnl=0.0,
                ))
            return positions
        except Exception as exc:
            logger.error("Coinbase positions failed: %s", exc)
            return []

    async def get_current_price(self, asset_id: str) -> float:
        """Get spot price from CoinGecko (no auth required)."""
        try:
            coin_map = {
                "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
                "DOGE": "dogecoin", "XRP": "ripple", "ADA": "cardano",
                "AVAX": "avalanche-2", "LINK": "chainlink", "DOT": "polkadot",
            }
            coin_id = coin_map.get(asset_id.upper(), asset_id.lower())
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": coin_id, "vs_currencies": "usd"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return float(data.get(coin_id, {}).get("usd", 0))
        except Exception as exc:
            logger.warning("CoinGecko price fetch failed: %s", exc)
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
```

- [ ] **Step 2: Verify module loads**

Run: `cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout\src && python -c "from execution.coinbase_spot_executor import CoinbaseSpotExecutor; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/execution/coinbase_spot_executor.py
git commit -m "feat: add CoinbaseSpotExecutor for spot crypto trading"
```

---

### Task 7: PredictIt Executor and Robinhood Advisor

**Files:**
- Create: `src/execution/predictit_executor.py`
- Create: `src/execution/robinhood_advisor.py`

- [ ] **Step 1: Write predictit_executor.py**

`src/execution/predictit_executor.py`:
```python
"""PredictIt executor — session-based auth, 850-share cap per contract."""
import logging
import os

import httpx

from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.predictit")

PREDICTIT_API = "https://www.predictit.org/api"
MAX_SHARES = 850  # PredictIt hard cap per contract


class PredictItExecutor(BaseExecutor):
    """PredictIt prediction market executor with 850-share cap validation."""

    def __init__(self):
        self._session = os.environ.get("PREDICTIT_SESSION", "")
        self._http = None

    def is_configured(self) -> bool:
        return bool(self._session)

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                base_url=PREDICTIT_API,
                cookies={"predictit_session": self._session},
                headers={"User-Agent": "Arbitrout/1.0"},
            )
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        """Buy shares. asset_id format: 'contract_id:YES' or 'contract_id:NO'."""
        try:
            contract_id, side = asset_id.rsplit(":", 1)
            price = await self.get_current_price(asset_id)
            if price <= 0:
                return ExecutionResult(False, None, 0, 0, 0, "Cannot get price")
            quantity = int(amount_usd / price)
            if quantity > MAX_SHARES:
                quantity = MAX_SHARES
                logger.warning("PredictIt: capped to %d shares (850 limit)", MAX_SHARES)
            http = await self._get_http()
            resp = await http.post(f"/Trade/SubmitTrade", json={
                "contractId": int(contract_id),
                "pricePerShare": price,
                "quantity": quantity,
                "tradeType": 1 if side.upper() == "YES" else 2,  # 1=buy yes, 2=buy no
            })
            resp.raise_for_status()
            data = resp.json()
            return ExecutionResult(
                success=True,
                tx_id=str(data.get("tradeId", "")),
                filled_price=price,
                filled_quantity=float(quantity),
                fees=round(quantity * price * 0.05, 4),  # PredictIt 5% profit fee approx
                error=None,
            )
        except Exception as exc:
            logger.error("PredictIt buy failed: %s", exc)
            return ExecutionResult(False, None, 0.0, 0.0, 0.0, str(exc))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        """Sell shares on PredictIt."""
        try:
            contract_id, side = asset_id.rsplit(":", 1)
            price = await self.get_current_price(asset_id)
            http = await self._get_http()
            resp = await http.post(f"/Trade/SubmitTrade", json={
                "contractId": int(contract_id),
                "pricePerShare": price,
                "quantity": int(quantity),
                "tradeType": 3 if side.upper() == "YES" else 4,  # 3=sell yes, 4=sell no
            })
            resp.raise_for_status()
            data = resp.json()
            return ExecutionResult(
                success=True,
                tx_id=str(data.get("tradeId", "")),
                filled_price=price,
                filled_quantity=float(int(quantity)),
                fees=0.0,
                error=None,
            )
        except Exception as exc:
            logger.error("PredictIt sell failed: %s", exc)
            return ExecutionResult(False, None, 0.0, 0.0, 0.0, str(exc))

    async def get_balance(self) -> BalanceResult:
        try:
            http = await self._get_http()
            resp = await http.get("/Profile/Shares")
            resp.raise_for_status()
            data = resp.json()
            avail = float(data.get("availableBalance", 0))
            return BalanceResult(available=avail, total=avail)
        except Exception as exc:
            logger.error("PredictIt balance failed: %s", exc)
            return BalanceResult(0.0, 0.0)

    async def get_positions(self) -> list[PositionInfo]:
        return []  # PredictIt positions tracked via our position_manager

    async def get_current_price(self, asset_id: str) -> float:
        """Get price from PredictIt public API (no auth)."""
        try:
            contract_id = asset_id.split(":")[0] if ":" in asset_id else asset_id
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"https://www.predictit.org/api/marketdata/markets/{contract_id}")
                if resp.status_code == 200:
                    data = resp.json()
                    contracts = data.get("contracts", [{}])
                    if contracts:
                        return float(contracts[0].get("lastTradePrice", 0.5))
        except Exception as exc:
            logger.warning("PredictIt price fetch failed: %s", exc)
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
```

- [ ] **Step 2: Write robinhood_advisor.py**

`src/execution/robinhood_advisor.py`:
```python
"""Robinhood advisor — stock price fetching + recommendations only.

NO execution capability. Does NOT inherit BaseExecutor.
Uses yfinance for price data (Robinhood has no official retail API).
"""
import logging

logger = logging.getLogger("execution.robinhood")


class RobinhoodAdvisor:
    """Advisory-only stock interface. User manually executes trades."""

    async def get_current_price(self, symbol: str) -> float:
        """Get stock price via yfinance."""
        try:
            import asyncio
            import yfinance as yf
            loop = asyncio.get_running_loop()
            ticker = await loop.run_in_executor(None, lambda: yf.Ticker(symbol.upper()))
            info = ticker.fast_info
            return float(info.last_price or 0)
        except Exception as exc:
            logger.warning("Stock price fetch failed for %s: %s", symbol, exc)
            return 0.0

    def recommend(self, symbol: str, action: str, quantity: float, reason: str) -> dict:
        """Create a stock recommendation (user must execute manually)."""
        return {
            "type": "stock_advisory",
            "symbol": symbol.upper(),
            "action": action,  # "buy" or "sell"
            "quantity": quantity,
            "reason": reason,
            "manual_execution_required": True,
        }
```

- [ ] **Step 3: Verify both modules load**

Run: `cd C:\Users\afoma\.openclaw\workspace\projects\arbitrout\src && python -c "from execution.predictit_executor import PredictItExecutor; from execution.robinhood_advisor import RobinhoodAdvisor; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/execution/predictit_executor.py src/execution/robinhood_advisor.py
git commit -m "feat: add PredictItExecutor (850-share cap) and RobinhoodAdvisor"
```

---

### Task 8: Update Dependencies

**Files:**
- Modify: `src/requirements.txt`

- [ ] **Step 1: Add new dependencies**

Append to `src/requirements.txt`:
```
py-clob-client>=0.1.0
kalshi-python>=1.0.0
coinbase-advanced-py>=1.0.0
anthropic>=0.40.0
```

- [ ] **Step 2: Commit**

```bash
git add src/requirements.txt
git commit -m "feat: add py-clob-client, kalshi-python, coinbase-advanced-py, anthropic deps"
```

---
