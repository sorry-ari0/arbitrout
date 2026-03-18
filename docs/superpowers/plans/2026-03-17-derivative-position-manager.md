# Derivative Position Manager Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a derivative position manager with AI-driven auto-exit for prediction market arbitrage packages

**Architecture:** Layered engine — executors (buy/sell per platform) → position manager (CRUD, rollback) → exit engine (30s loop, 18 heuristic triggers) → AI advisor (Claude API review) → FastAPI + WebSocket dashboard. Paper trading mode by default.

**Tech Stack:** Python 3.11+, FastAPI, anthropic SDK, py-clob-client, kalshi-python, coinbase-advanced-py, httpx, pytest

**Spec:** `docs/superpowers/specs/2026-03-17-derivative-position-manager-design.md`

---

## File Structure

### Create:
- `src/execution/base_executor.py` — ABC + dataclasses (~60 lines)
- `src/execution/paper_executor.py` — simulation wrapper (~120 lines)
- `src/execution/kalshi_executor.py` — Kalshi RSA auth (~130 lines)
- `src/execution/coinbase_spot_executor.py` — Coinbase Advanced Trade (~140 lines)
- `src/execution/predictit_executor.py` — PredictIt session auth (~120 lines)
- `src/execution/robinhood_advisor.py` — advisory only (~40 lines)
- `src/positions/__init__.py`
- `src/positions/wallet_config.py` — env vars + platform detection (~40 lines)
- `src/positions/position_manager.py` — CRUD + persistence + rollback (~250 lines)
- `src/positions/exit_engine.py` — 30s loop + 18 triggers (~400 lines)
- `src/positions/ai_advisor.py` — Claude API + batching (~250 lines)
- `src/positions/position_router.py` — FastAPI router + WebSocket (~200 lines)
- `tests/conftest.py`, `tests/test_base_executor.py`, `tests/test_paper_executor.py`
- `tests/test_position_manager.py`, `tests/test_exit_engine.py`, `tests/test_ai_advisor.py`

### Modify:
- `src/execution/polymarket_executor.py` — full async rewrite
- `src/server.py` — include position_router, CORS PATCH, exit_engine lifespan
- `src/static/js/arbitrout.js` — positions dashboard tab
- `src/static/css/arbitrout.css` — positions styles
- `src/requirements.txt` — add new dependencies

### Delete:
- `src/execution/wallet_config.py` — empty file (if it exists; skip if already removed)

---

## Chunk 1: Execution Foundation

### Task 1: Base Executor + Models

**Files:** Create `src/execution/base_executor.py`, `tests/conftest.py`, `tests/test_base_executor.py`

- [ ] **Step 1: Write conftest + failing test**

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
from execution.base_executor import ExecutionResult, BalanceResult, PositionInfo, BaseExecutor

class TestExecutionResult:
    def test_success(self):
        r = ExecutionResult(success=True, tx_id="tx_1", filled_price=0.65,
                           filled_quantity=10.0, fees=0.02, error=None)
        assert r.success and r.tx_id == "tx_1" and r.fees == 0.02

    def test_failure(self):
        r = ExecutionResult(success=False, tx_id=None, filled_price=0.0,
                           filled_quantity=0.0, fees=0.0, error="Insufficient balance")
        assert not r.success and r.error == "Insufficient balance"

    def test_to_dict(self):
        r = ExecutionResult(True, "tx_1", 0.5, 5.0, 0.01, None)
        assert r.to_dict()["success"] is True

class TestBaseExecutor:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            BaseExecutor()

    def test_valid_subclass(self):
        class Stub(BaseExecutor):
            async def buy(self, asset_id, amount_usd): return ExecutionResult(True,"t",1,1,0,None)
            async def sell(self, asset_id, quantity): return ExecutionResult(True,"t",1,1,0,None)
            async def get_balance(self): return BalanceResult(100, 100)
            async def get_positions(self): return []
            async def get_current_price(self, asset_id): return 1.0
            def is_configured(self): return True
        assert Stub().is_configured()
```

- [ ] **Step 2: Run — expect FAIL** `python -m pytest tests/test_base_executor.py -v`

- [ ] **Step 3: Implement `src/execution/base_executor.py`**

```python
"""Base executor ABC and shared dataclasses for all platform executors."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict

@dataclass
class ExecutionResult:
    success: bool
    tx_id: str | None
    filled_price: float
    filled_quantity: float
    fees: float
    error: str | None
    def to_dict(self) -> dict: return asdict(self)

@dataclass
class BalanceResult:
    available: float
    total: float

@dataclass
class PositionInfo:
    asset_id: str
    quantity: float
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float

class BaseExecutor(ABC):
    @abstractmethod
    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult: ...
    @abstractmethod
    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult: ...
    @abstractmethod
    async def get_balance(self) -> BalanceResult: ...
    @abstractmethod
    async def get_positions(self) -> list[PositionInfo]: ...
    @abstractmethod
    async def get_current_price(self, asset_id: str) -> float: ...
    @abstractmethod
    def is_configured(self) -> bool: ...
```

- [ ] **Step 4: Run — expect PASS** `python -m pytest tests/test_base_executor.py -v`
- [ ] **Step 5: Commit** `git add src/execution/base_executor.py tests/conftest.py tests/test_base_executor.py && git commit -m "feat: add BaseExecutor ABC and execution dataclasses"`

---

### Task 2: Wallet Config + Positions Package

**Files:** Create `src/positions/__init__.py`, `src/positions/wallet_config.py`, `tests/test_wallet_config.py`. Delete `src/execution/wallet_config.py`.

- [ ] **Step 1: Write failing test** `tests/test_wallet_config.py`:
```python
"""Tests for wallet configuration."""
import pytest
from positions.wallet_config import get_configured_platforms, is_paper_mode, get_paper_balance

class TestPaperMode:
    def test_default_true(self, monkeypatch):
        monkeypatch.delenv("PAPER_TRADING", raising=False)
        assert is_paper_mode() is True
    def test_false(self, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "false")
        assert is_paper_mode() is False
    def test_balance_default(self, monkeypatch):
        monkeypatch.delenv("PAPER_STARTING_BALANCE", raising=False)
        assert get_paper_balance() == 10000.0

class TestConfiguredPlatforms:
    def test_none_set(self, monkeypatch):
        for k in ["POLYMARKET_PRIVATE_KEY","POLYMARKET_FUNDER_ADDRESS","KALSHI_API_KEY",
                   "KALSHI_RSA_PRIVATE_KEY","COINBASE_ADV_API_KEY","COINBASE_ADV_API_SECRET",
                   "PREDICTIT_SESSION"]:
            monkeypatch.delenv(k, raising=False)
        assert get_configured_platforms() == {}
    def test_polymarket(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
        monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", "0xdef")
        assert "polymarket" in get_configured_platforms()
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement**

`src/positions/__init__.py`:
```python
"""Positions package — derivative position management and auto-exit."""
```

`src/positions/wallet_config.py`:
```python
"""Wallet configuration — env var loading and platform availability."""
import os

PLATFORM_CREDENTIALS = {
    "polymarket": ["POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS"],
    "kalshi": ["KALSHI_API_KEY", "KALSHI_RSA_PRIVATE_KEY"],
    "coinbase_spot": ["COINBASE_ADV_API_KEY", "COINBASE_ADV_API_SECRET"],
    "predictit": ["PREDICTIT_SESSION"],
}

def is_paper_mode() -> bool:
    return os.environ.get("PAPER_TRADING", "true").lower() != "false"

def get_paper_balance() -> float:
    try: return float(os.environ.get("PAPER_STARTING_BALANCE", "10000"))
    except ValueError: return 10000.0

def get_configured_platforms() -> dict[str, bool]:
    return {p: True for p, keys in PLATFORM_CREDENTIALS.items()
            if all(os.environ.get(k, "") for k in keys)}

def has_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", ""))
```

- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Delete old (if exists)** `rm -f src/execution/wallet_config.py` (skip if file doesn't exist)
- [ ] **Step 6: Commit** `git add src/positions/ tests/test_wallet_config.py && git commit -m "feat: add wallet_config with platform detection and paper mode"`

---

### Task 3: Paper Executor

**Files:** Create `src/execution/paper_executor.py`, `tests/test_paper_executor.py`

- [ ] **Step 1: Write failing test** `tests/test_paper_executor.py`:
```python
"""Tests for PaperExecutor."""
import pytest, asyncio
from execution.base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo
from execution.paper_executor import PaperExecutor

class FakeExecutor(BaseExecutor):
    def __init__(self): self._prices = {"BTC": 97000.0, "tok:YES": 0.65}
    async def buy(self, a, amt): return ExecutionResult(True,"r",1,1,0,None)
    async def sell(self, a, q): return ExecutionResult(True,"r",1,q,0,None)
    async def get_balance(self): return BalanceResult(1000,1000)
    async def get_positions(self): return []
    async def get_current_price(self, a): return self._prices.get(a, 1.0)
    def is_configured(self): return True

@pytest.fixture
def paper(): return PaperExecutor(FakeExecutor(), starting_balance=1000.0)

class TestPaperBuy:
    def test_buy_deducts(self, paper):
        r = asyncio.get_event_loop().run_until_complete(paper.buy("tok:YES", 100.0))
        assert r.success and r.filled_price == 0.65 and r.tx_id.startswith("paper_")
        b = asyncio.get_event_loop().run_until_complete(paper.get_balance())
        assert b.available == pytest.approx(900.0)
    def test_insufficient(self, paper):
        r = asyncio.get_event_loop().run_until_complete(paper.buy("BTC", 2000.0))
        assert not r.success

class TestPaperSell:
    def test_sell_after_buy(self, paper):
        asyncio.get_event_loop().run_until_complete(paper.buy("tok:YES", 100.0))
        r = asyncio.get_event_loop().run_until_complete(paper.sell("tok:YES", 50.0))
        assert r.success
    def test_sell_no_position(self, paper):
        r = asyncio.get_event_loop().run_until_complete(paper.sell("BTC", 1.0))
        assert not r.success
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement `src/execution/paper_executor.py`**

```python
"""Paper executor — wraps real executor for simulated trading. Real prices, fake money."""
import logging, uuid
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.paper")

class PaperExecutor:
    def __init__(self, real_executor: BaseExecutor, starting_balance: float = 10000.0):
        self.real = real_executor
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.positions: dict[str, dict] = {}
        self.trade_history: list[dict] = []

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        if amount_usd > self.balance:
            return ExecutionResult(False, None, 0, 0, 0, f"Insufficient paper balance: {self.balance:.2f} < {amount_usd:.2f}")
        price = await self.real.get_current_price(asset_id)
        if price <= 0:
            return ExecutionResult(False, None, 0, 0, 0, f"Invalid price for {asset_id}")
        qty = amount_usd / price
        self.balance -= amount_usd
        pos = self.positions.get(asset_id)
        if pos:
            total = pos["quantity"] + qty
            pos["avg_entry_price"] = (pos["avg_entry_price"] * pos["quantity"] + price * qty) / total
            pos["quantity"] = total
        else:
            self.positions[asset_id] = {"quantity": qty, "avg_entry_price": price}
        tx_id = f"paper_{uuid.uuid4().hex[:12]}"
        self.trade_history.append({"action":"buy","asset_id":asset_id,"price":price,"quantity":qty,"amount_usd":amount_usd,"tx_id":tx_id})
        return ExecutionResult(True, tx_id, price, qty, 0.0, None)

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        pos = self.positions.get(asset_id)
        if not pos or pos["quantity"] < quantity * 0.999:
            return ExecutionResult(False, None, 0, 0, 0, f"No position or insufficient quantity for {asset_id}")
        price = await self.real.get_current_price(asset_id)
        proceeds = quantity * price
        self.balance += proceeds
        pos["quantity"] -= quantity
        if pos["quantity"] < 1e-10: del self.positions[asset_id]
        tx_id = f"paper_{uuid.uuid4().hex[:12]}"
        self.trade_history.append({"action":"sell","asset_id":asset_id,"price":price,"quantity":quantity,"proceeds_usd":proceeds,"tx_id":tx_id})
        return ExecutionResult(True, tx_id, price, quantity, 0.0, None)

    async def get_balance(self) -> BalanceResult:
        pos_val = 0.0
        for aid, pos in self.positions.items():
            try: pos_val += pos["quantity"] * await self.real.get_current_price(aid)
            except: pos_val += pos["quantity"] * pos["avg_entry_price"]
        return BalanceResult(self.balance, self.balance + pos_val)

    async def get_positions(self) -> list[PositionInfo]:
        result = []
        for aid, pos in self.positions.items():
            try: price = await self.real.get_current_price(aid)
            except: price = pos["avg_entry_price"]
            result.append(PositionInfo(aid, pos["quantity"], pos["avg_entry_price"], price,
                                       (price - pos["avg_entry_price"]) * pos["quantity"]))
        return result

    async def get_current_price(self, asset_id: str) -> float:
        return await self.real.get_current_price(asset_id)

    def is_configured(self) -> bool: return True

    def get_stats(self) -> dict:
        pnl = self.balance - self.starting_balance
        sells = [t for t in self.trade_history if t["action"] == "sell"]
        # Win = sold at higher price than bought (compare against matching buy's price)
        wins = sum(1 for t in sells if t.get("price", 0) > 0)  # tracked at package level via position_manager
        return {"mode":"paper","starting_balance":self.starting_balance,"current_balance":self.balance,
                "total_pnl":round(pnl,2),"total_trades":len(self.trade_history),
                "win_rate":round(wins/len(sells),2) if sells else 0,"open_positions":len(self.positions)}
```

- [ ] **Step 4: Run — expect PASS** `python -m pytest tests/test_paper_executor.py -v`
- [ ] **Step 5: Commit** `git add src/execution/paper_executor.py tests/test_paper_executor.py && git commit -m "feat: add PaperExecutor for simulated paper trading"`

---

### Task 4: Polymarket Executor Rewrite

**Files:** Rewrite `src/execution/polymarket_executor.py`

- [ ] **Step 1: Full rewrite**

```python
"""Polymarket CLOB executor — async buy/sell via Polygon chain."""
import logging, os
import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.polymarket")
GAMMA_API = "https://gamma-api.polymarket.com"

class PolymarketExecutor(BaseExecutor):
    def __init__(self):
        self._private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        self._funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
        self._client = None
        self._http = None

    def is_configured(self) -> bool: return bool(self._private_key and self._funder)

    def _get_clob(self):
        if not self.is_configured(): raise RuntimeError("Polymarket not configured")
        if not self._client:
            from py_clob_client.client import ClobClient
            self._client = ClobClient(self._private_key, self._funder, "https://clob.polymarket.com", chain_id=137)
        return self._client

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=15.0, headers={"User-Agent":"Arbitrout/1.0"})
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        try:
            token_id, side = asset_id.rsplit(":", 1)
            order = self._get_clob().create_and_post_order({"token_id":token_id,"side":side.upper(),"size":amount_usd,"price":None,"type":"FOK"})
            return ExecutionResult(True, order.get("id",""), float(order.get("price",0)), float(order.get("size",amount_usd)), float(order.get("fee",0)), None)
        except Exception as e:
            logger.error("Polymarket buy failed: %s", e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        try:
            token_id, _ = asset_id.rsplit(":", 1)
            order = self._get_clob().create_and_post_order({"token_id":token_id,"side":"SELL","size":quantity,"price":None,"type":"FOK"})
            return ExecutionResult(True, order.get("id",""), float(order.get("price",0)), float(order.get("size",quantity)), float(order.get("fee",0)), None)
        except Exception as e:
            logger.error("Polymarket sell failed: %s", e)
            return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        try:
            b = self._get_clob().get_balance()
            return BalanceResult(float(b.get("available",0)), float(b.get("total",0)))
        except: return BalanceResult(0,0)

    async def get_positions(self) -> list[PositionInfo]:
        try:
            ps = self._get_clob().get_positions()
            return [PositionInfo(p.get("asset_id",""),float(p.get("size",0)),float(p.get("avg_price",0)),float(p.get("cur_price",0)),float(p.get("pnl",0))) for p in (ps if isinstance(ps,list) else [])]
        except: return []

    async def get_current_price(self, asset_id: str) -> float:
        try:
            tid = asset_id.split(":")[0] if ":" in asset_id else asset_id
            http = await self._get_http()
            r = await http.get(f"{GAMMA_API}/markets/{tid}")
            if r.status_code == 200: return float(r.json().get("outcomePrices",[0.5])[0])
        except Exception as e: logger.warning("Polymarket price failed: %s", e)
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed: await self._http.aclose()
```

- [ ] **Step 2: Verify loads** `cd src && python -c "from execution.polymarket_executor import PolymarketExecutor; print('OK')"`
- [ ] **Step 3: Commit** `git add src/execution/polymarket_executor.py && git commit -m "feat: rewrite PolymarketExecutor with async BaseExecutor"`

---

## Chunk 2: Remaining Executors + Dependencies

### Task 5: Kalshi Executor

**Files:** Create `src/execution/kalshi_executor.py`

- [ ] **Step 1: Write executor** (NOTE: The implementation below uses raw httpx for clarity. For production, replace with `kalshi-python` SDK which handles RSA request signing automatically. The raw httpx Bearer auth shown here works only for Kalshi's demo/sandbox environment.)

```python
"""Kalshi exchange executor — RSA keypair auth, 0% fees currently."""
import logging, os
import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.kalshi")
KALSHI_API = "https://trading-api.kalshi.com/trade-api/v2"

class KalshiExecutor(BaseExecutor):
    def __init__(self):
        self._api_key = os.environ.get("KALSHI_API_KEY", "")
        self._rsa_key = os.environ.get("KALSHI_RSA_PRIVATE_KEY", "")
        self._http = None

    def is_configured(self): return bool(self._api_key and self._rsa_key)

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=15.0, base_url=KALSHI_API,
                                           headers={"Authorization":f"Bearer {self._api_key}"})
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        try:
            ticker, side = asset_id.rsplit(":", 1)
            http = await self._get_http()
            r = await http.post("/portfolio/orders", json={"ticker":ticker,"action":"buy","side":side.lower(),"type":"market","count":int(amount_usd*100)})
            r.raise_for_status(); d = r.json().get("order",{})
            return ExecutionResult(True, d.get("order_id",""), float(d.get("avg_price",0))/100, float(d.get("count",0)), 0.0, None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        try:
            ticker, side = asset_id.rsplit(":", 1)
            http = await self._get_http()
            r = await http.post("/portfolio/orders", json={"ticker":ticker,"action":"sell","side":side.lower(),"type":"market","count":int(quantity)})
            r.raise_for_status(); d = r.json().get("order",{})
            return ExecutionResult(True, d.get("order_id",""), float(d.get("avg_price",0))/100, float(d.get("count",0)), 0.0, None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        try:
            r = await (await self._get_http()).get("/portfolio/balance"); r.raise_for_status()
            d = r.json(); return BalanceResult(float(d.get("available_balance",0))/100, float(d.get("portfolio_value",0))/100)
        except: return BalanceResult(0,0)

    async def get_positions(self) -> list[PositionInfo]: return []

    async def get_current_price(self, asset_id: str) -> float:
        try:
            ticker = asset_id.split(":")[0] if ":" in asset_id else asset_id
            r = await (await self._get_http()).get(f"/markets/{ticker}")
            if r.status_code == 200: return float(r.json().get("market",{}).get("last_price",50))/100
        except: pass
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed: await self._http.aclose()
```

- [ ] **Step 2: Verify + Commit** `git add src/execution/kalshi_executor.py && git commit -m "feat: add KalshiExecutor"`

---

### Task 6: Coinbase Spot Executor

**Files:** Create `src/execution/coinbase_spot_executor.py`

- [ ] **Step 1: Write executor**

```python
"""Coinbase Advanced Trade executor — spot crypto buy/sell. DISTINCT from adapters/coinbase.py.
NOTE: Production should use coinbase-advanced-py SDK for HMAC-SHA256 request signing.
The raw httpx implementation below is for development/paper trading only.
"""
import logging, os
import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.coinbase_spot")

class CoinbaseSpotExecutor(BaseExecutor):
    def __init__(self):
        self._api_key = os.environ.get("COINBASE_ADV_API_KEY", "")
        self._secret = os.environ.get("COINBASE_ADV_API_SECRET", "")
        self._http = None

    def is_configured(self): return bool(self._api_key and self._secret)

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=15.0, base_url="https://api.coinbase.com/api/v3/brokerage",
                                           headers={"CB-ACCESS-KEY":self._api_key,"Content-Type":"application/json"})
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        try:
            http = await self._get_http()
            r = await http.post("/orders", json={"product_id":f"{asset_id.upper()}-USD","side":"BUY",
                "order_configuration":{"market_market_ioc":{"quote_size":str(amount_usd)}}})
            r.raise_for_status(); d = r.json().get("success_response", r.json())
            return ExecutionResult(True, d.get("order_id",""), float(d.get("average_filled_price",0)),
                                  float(d.get("filled_size",0)), float(d.get("total_fees",0)), None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        try:
            http = await self._get_http()
            r = await http.post("/orders", json={"product_id":f"{asset_id.upper()}-USD","side":"SELL",
                "order_configuration":{"market_market_ioc":{"base_size":str(quantity)}}})
            r.raise_for_status(); d = r.json().get("success_response", r.json())
            return ExecutionResult(True, d.get("order_id",""), float(d.get("average_filled_price",0)),
                                  float(d.get("filled_size",0)), float(d.get("total_fees",0)), None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        try:
            r = await (await self._get_http()).get("/accounts"); r.raise_for_status()
            usd = next((a for a in r.json().get("accounts",[]) if a.get("currency")=="USD"), None)
            return BalanceResult(float(usd["available_balance"]["value"]) if usd else 0, 0)
        except: return BalanceResult(0,0)

    async def get_positions(self) -> list[PositionInfo]: return []

    async def get_current_price(self, asset_id: str) -> float:
        coin_map = {"BTC":"bitcoin","ETH":"ethereum","SOL":"solana","DOGE":"dogecoin","XRP":"ripple"}
        cid = coin_map.get(asset_id.upper(), asset_id.lower())
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://api.coingecko.com/api/v3/simple/price", params={"ids":cid,"vs_currencies":"usd"})
                if r.status_code == 200: return float(r.json().get(cid,{}).get("usd",0))
        except: pass
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed: await self._http.aclose()
```

- [ ] **Step 2: Verify + Commit** `git add src/execution/coinbase_spot_executor.py && git commit -m "feat: add CoinbaseSpotExecutor"`

---

### Task 7: PredictIt + Robinhood

**Files:** Create `src/execution/predictit_executor.py`, `src/execution/robinhood_advisor.py`

- [ ] **Step 1: Write predictit_executor.py**

```python
"""PredictIt executor — session auth, 850-share cap per contract."""
import logging, os
import httpx
from .base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo

logger = logging.getLogger("execution.predictit")
MAX_SHARES = 850

class PredictItExecutor(BaseExecutor):
    def __init__(self):
        self._session = os.environ.get("PREDICTIT_SESSION", "")
        self._http = None

    def is_configured(self): return bool(self._session)

    async def _get_http(self):
        if not self._http or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=15.0, base_url="https://www.predictit.org/api",
                                           cookies={"predictit_session": self._session})
        return self._http

    async def buy(self, asset_id: str, amount_usd: float) -> ExecutionResult:
        try:
            cid, side = asset_id.rsplit(":", 1)
            price = await self.get_current_price(asset_id)
            qty = min(int(amount_usd / price) if price > 0 else 0, MAX_SHARES)
            r = await (await self._get_http()).post("/Trade/SubmitTrade",
                json={"contractId":int(cid),"pricePerShare":price,"quantity":qty,"tradeType":1 if side.upper()=="YES" else 2})
            r.raise_for_status(); d = r.json()
            return ExecutionResult(True, str(d.get("tradeId","")), price, float(qty), round(qty*price*0.05,4), None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def sell(self, asset_id: str, quantity: float) -> ExecutionResult:
        try:
            cid, side = asset_id.rsplit(":", 1)
            price = await self.get_current_price(asset_id)
            r = await (await self._get_http()).post("/Trade/SubmitTrade",
                json={"contractId":int(cid),"pricePerShare":price,"quantity":int(quantity),"tradeType":3 if side.upper()=="YES" else 4})
            r.raise_for_status(); return ExecutionResult(True, "", price, float(int(quantity)), 0, None)
        except Exception as e: return ExecutionResult(False, None, 0, 0, 0, str(e))

    async def get_balance(self) -> BalanceResult:
        try:
            r = await (await self._get_http()).get("/Profile/Shares"); r.raise_for_status()
            return BalanceResult(float(r.json().get("availableBalance",0)), 0)
        except: return BalanceResult(0,0)

    async def get_positions(self) -> list[PositionInfo]: return []

    async def get_current_price(self, asset_id: str) -> float:
        try:
            cid = asset_id.split(":")[0]
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"https://www.predictit.org/api/marketdata/markets/{cid}")
                if r.status_code == 200:
                    contracts = r.json().get("contracts",[{}])
                    if contracts: return float(contracts[0].get("lastTradePrice",0.5))
        except: pass
        return 0.0

    async def close(self):
        if self._http and not self._http.is_closed: await self._http.aclose()
```

- [ ] **Step 2: Write robinhood_advisor.py**

```python
"""Robinhood advisor — stock price fetch + recommendations. NO execution. Not a BaseExecutor."""
import logging
logger = logging.getLogger("execution.robinhood")

class RobinhoodAdvisor:
    async def get_current_price(self, symbol: str) -> float:
        try:
            import asyncio, yfinance as yf
            t = await asyncio.get_running_loop().run_in_executor(None, lambda: yf.Ticker(symbol.upper()))
            return float(t.fast_info.last_price or 0)
        except Exception as e:
            logger.warning("Stock price failed for %s: %s", symbol, e); return 0.0

    def recommend(self, symbol: str, action: str, quantity: float, reason: str) -> dict:
        return {"type":"stock_advisory","symbol":symbol.upper(),"action":action,
                "quantity":quantity,"reason":reason,"manual_execution_required":True}
```

- [ ] **Step 3: Verify + Commit** `git add src/execution/predictit_executor.py src/execution/robinhood_advisor.py && git commit -m "feat: add PredictItExecutor and RobinhoodAdvisor"`

---

### Task 8: Update Dependencies

**Files:** Modify `src/requirements.txt`

- [ ] **Step 1: Append deps** — add to end of `src/requirements.txt`:
```
py-clob-client>=0.1.0
kalshi-python>=1.0.0
coinbase-advanced-py>=1.0.0
anthropic>=0.40.0
```

- [ ] **Step 2: Commit** `git add src/requirements.txt && git commit -m "feat: add trading SDK and anthropic dependencies"`

---

## Chunk 3: Position Management

### Task 9: Position Manager

**Files:** Create `src/positions/position_manager.py`, `tests/test_position_manager.py`

- [ ] **Step 1: Write failing test** — see `tests/test_position_manager.py` below:

```python
"""Tests for position manager."""
import json, pytest, asyncio
from positions.position_manager import (
    PositionManager, create_package, create_leg, create_exit_rule,
    STATUS_OPEN, STATUS_CLOSED,
)

@pytest.fixture
def manager(tmp_path):
    return PositionManager(data_dir=tmp_path, executors={})

class TestHelpers:
    def test_create_package(self):
        p = create_package("Test", "cross_platform_arb")
        assert p["id"].startswith("pkg_") and p["status"] == STATUS_OPEN
    def test_create_leg(self):
        l = create_leg("polymarket", "prediction_yes", "tok:YES", "BTC>100k", 0.65, 10.0, "2026-12-31")
        assert l["cost"] == 10.0 and l["quantity"] == pytest.approx(10/0.65, rel=0.01)
        assert l["expiry"] == "2026-12-31"

class TestPersistence:
    def test_save_load(self, manager):
        p = create_package("P", "pure_prediction")
        manager.packages[p["id"]] = p; manager.save()
        m2 = PositionManager(data_dir=manager.data_dir, executors={})
        assert p["id"] in m2.packages

class TestCRUD:
    def test_add_get(self, manager):
        p = create_package("G", "spot_plus_hedge"); manager.add_package(p)
        assert manager.get_package(p["id"])["name"] == "G"
    def test_close(self, manager):
        p = create_package("C", "pure_prediction"); manager.add_package(p)
        manager.close_package(p["id"])
        assert manager.packages[p["id"]]["status"] == STATUS_CLOSED

class TestPnL:
    def test_update_pnl(self, manager):
        p = create_package("PnL","pure_prediction")
        l = create_leg("polymarket","prediction_yes","tok:YES","T",0.50,10.0)
        p["legs"].append(l); manager.add_package(p)
        l["current_price"] = 0.70; l["current_value"] = l["quantity"] * 0.70
        manager.update_pnl(p["id"])
        assert manager.get_package(p["id"])["itm_status"] == "ITM"
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement** — full `src/positions/position_manager.py` with data model helpers, persistence, CRUD, P&L updates, execute_package with concurrent leg buying, exit_leg, and rollback. See spec section "Data Model" and "Data Flow > Creation" for exact behavior.

The implementation must include:
- `create_package(name, strategy_type)` — returns package dict with all spec fields
- `create_leg(platform, leg_type, asset_id, asset_label, entry_price, cost, expiry)` — derives quantity from cost/price
- `create_exit_rule(rule_type, params)` — returns rule dict
- `PositionManager.__init__(data_dir, executors)` — loads from positions.json on init
- `save()` — atomic write (temp + os.replace)
- `add_package()`, `get_package()`, `list_packages(status)`, `close_package()`
- `update_pnl(pkg_id)` — recalculates all leg/package ITM/OTM/ATM
- `execute_package(pkg)` — buys all non-advisory legs, rollback on failure
- `exit_leg(pkg_id, leg_id, trigger)` — sells leg, updates status
- `_rollback(executed_legs)` — attempts to sell already-bought legs

- [ ] **Step 4: Run — expect PASS** `python -m pytest tests/test_position_manager.py -v`
- [ ] **Step 5: Commit** `git add src/positions/position_manager.py tests/test_position_manager.py && git commit -m "feat: add PositionManager with CRUD, P&L, execution, rollback"`

---

## Chunk 4: Exit Engine

### Task 10: Exit Engine — 18 Heuristic Triggers + Loop

**Files:** Create `src/positions/exit_engine.py`, `tests/test_exit_engine.py`

- [ ] **Step 1: Write failing test** — `tests/test_exit_engine.py`:

```python
"""Tests for exit engine heuristics."""
import pytest
from positions.exit_engine import evaluate_heuristics
from positions.position_manager import create_package, create_leg, create_exit_rule

def _make_pkg(strategy="cross_platform_arb"):
    pkg = create_package("Test", strategy)
    l1 = create_leg("polymarket","prediction_yes","tok1:YES","BTC>100k",0.60,10.0,"2026-12-31")
    l2 = create_leg("kalshi","prediction_no","tick1:NO","BTC>100k",0.35,10.0,"2026-12-31")
    pkg["legs"] = [l1, l2]
    pkg["exit_rules"].append(create_exit_rule("trailing_stop", {"bound_min":5,"bound_max":25,"current":12,"peak_value":20.0}))
    return pkg

class TestHeuristics:
    def test_spread_inversion_is_safety(self):
        pkg = _make_pkg()
        pkg["legs"][0]["current_price"] = 0.75
        pkg["legs"][1]["current_price"] = 0.30  # 1.0 - (0.75+0.30) = -0.05
        triggers = evaluate_heuristics(pkg)
        assert any(t.get("safety_override") for t in triggers)

    def test_new_ath_detected(self):
        pkg = _make_pkg()
        pkg["current_value"] = 25.0  # > peak 20.0
        triggers = evaluate_heuristics(pkg)
        assert any(t["trigger_id"] == 5 for t in triggers)

    def test_time_24h_safety(self):
        pkg = _make_pkg()
        from datetime import datetime, timedelta
        tomorrow = (datetime.now() + timedelta(hours=20)).strftime("%Y-%m-%d")
        for l in pkg["legs"]: l["expiry"] = tomorrow
        triggers = evaluate_heuristics(pkg)
        assert any(t.get("safety_override") for t in triggers)
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement** — full `src/positions/exit_engine.py` with:
  - `evaluate_heuristics(pkg)` — evaluates all 18 triggers from spec section "18 Heuristic Triggers"
  - `ExitEngine` class with `start()`, `stop()`, `_loop()` (30s interval), `_tick()` (process each open package), `_update_prices()` (fetch via executors), `_process_triggers()` (safety=immediate, else=batch to AI)
  - Price history tracking for volatility calculation
  - Negative streak counter for sustained drift
  - Platform error counter for liquidity triggers
  - Strategy type filtering (spread triggers only for cross_platform_arb, hedge triggers only for spot_plus_hedge)

- [ ] **Step 4: Run — expect PASS** `python -m pytest tests/test_exit_engine.py -v`
- [ ] **Step 5: Commit** `git add src/positions/exit_engine.py tests/test_exit_engine.py && git commit -m "feat: add ExitEngine with 18 heuristic triggers and safety overrides"`

---

## Chunk 5: AI Advisor

### Task 11: AI Advisor — Claude API + Batching + Guardrails

**Files:** Create `src/positions/ai_advisor.py`, `tests/test_ai_advisor.py`

- [ ] **Step 1: Write failing test** — `tests/test_ai_advisor.py`:

```python
"""Tests for AI advisor."""
import pytest
from positions.ai_advisor import AIAdvisor

@pytest.fixture
def advisor(): return AIAdvisor(max_calls_per_min=10)

class TestPromptBuilding:
    def test_build_context(self, advisor):
        pkg = {"id":"p1","name":"T","strategy_type":"cross_platform_arb","legs":[
            {"leg_id":"l1","platform":"poly","type":"prediction_yes","asset_label":"BTC>100k",
             "entry_price":0.60,"current_price":0.70,"quantity":16.67,"cost":10,"current_value":11.67,
             "expiry":"2026-12-31","status":"open","leg_status":"ITM"}],
            "exit_rules":[{"rule_id":"r1","type":"trailing_stop","params":{"bound_min":5,"bound_max":25,"current":12},"active":True}],
            "unrealized_pnl":1.67,"unrealized_pnl_pct":16.7}
        ctx = advisor._build_context(pkg)
        assert "BTC>100k" in ctx

class TestParseResponse:
    def test_approve(self, advisor):
        v = advisor._parse_response("r1: APPROVE\n")
        assert v["r1"]["action"] == "APPROVE"
    def test_modify(self, advisor):
        v = advisor._parse_response("r1: MODIFY 8\n")
        assert v["r1"]["action"] == "MODIFY" and v["r1"]["value"] == 8.0
    def test_reject(self, advisor):
        v = advisor._parse_response("r1: REJECT too risky\n")
        assert v["r1"]["action"] == "REJECT"
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement** — full `src/positions/ai_advisor.py` with:
  - `AIAdvisor.__init__(max_calls_per_min)` — loads ANTHROPIC_API_KEY, lazy Anthropic client
  - `_build_context(pkg)` — formats package/legs/rules as text
  - `_build_prompt(pkg, proposals)` — structured Claude prompt per spec "Claude Prompt Structure"
  - `_parse_response(text)` — parses APPROVE/MODIFY/REJECT per rule
  - `review_proposals(pkg, proposals)` — batches proposals into 1 API call, rate limited to max_calls/min
  - `_apply_verdicts(pkg, proposals, verdicts)` — APPROVE=execute, MODIFY within bounds=adjust, MODIFY outside bounds=escalation alert
  - Uses `from anthropic import Anthropic`, model configurable via `CLAUDE_MODEL` env var (default `claude-sonnet-4-20250514`), max_tokens=500, 10s timeout
  - **Deferred:** Dexter/Financial Datasets API enrichment (spec lines 596-603) — AI advisor works without it; add as follow-up task

- [ ] **Step 4: Run — expect PASS** `python -m pytest tests/test_ai_advisor.py -v`
- [ ] **Step 5: Commit** `git add src/positions/ai_advisor.py tests/test_ai_advisor.py && git commit -m "feat: add AIAdvisor with Claude API batching and guardrails"`

---

## Chunk 6: API Layer + Server Integration

### Task 12: Position Router

**Files:** Create `src/positions/position_router.py`

- [ ] **Step 1: Write router** — FastAPI APIRouter under prefix `/api/derivatives` with:

**Endpoints (all from spec "API Endpoints" section):**
- `GET /packages` — list (filterable by status)
- `GET /packages/{id}` — full detail
- `POST /packages` — create + execute legs
- `PATCH /packages/{id}` — update name/rules
- `DELETE /packages/{id}` — force-close all legs
- `POST /packages/{id}/exit` — manual full exit
- `POST /packages/{id}/exit-leg/{leg_id}` — single leg exit
- `POST /packages/{id}/confirm-stock` — confirm stock advisory
- `GET/POST/PATCH/DELETE /packages/{id}/rules/{rule_id}` — rule CRUD (PATCH adjusts params/bounds)
- `GET /dashboard` — aggregate stats
- `GET /dashboard/alerts` — pending escalations
- `POST /dashboard/alerts/{id}/approve` — approve escalation
- `POST /dashboard/alerts/{id}/reject` — reject escalation
- `GET /balances` — per-platform balance
- `GET /config` — platform availability + paper mode status
- `WS /ws` — position_update, package_created, package_closed, escalation

Module-level `init_position_system(pm, exit_engine, ai_advisor)` function called by server.py.

- [ ] **Step 2: Verify loads** `cd src && python -c "from positions.position_router import router; print('OK')"`

- [ ] **Step 3: Write smoke test** `tests/test_position_router.py`:
```python
"""Smoke tests for position router."""
import pytest
from fastapi.testclient import TestClient
from positions.position_router import router
from fastapi import FastAPI

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)

class TestRouterMounts:
    def test_packages_endpoint_exists(self, client):
        r = client.get("/api/derivatives/packages")
        assert r.status_code in (200, 503)  # 503 if position system not init'd
    def test_config_endpoint(self, client):
        r = client.get("/api/derivatives/config")
        assert r.status_code == 200
    def test_dashboard_endpoint(self, client):
        r = client.get("/api/derivatives/dashboard")
        assert r.status_code in (200, 503)
```

- [ ] **Step 4: Run — expect PASS** `python -m pytest tests/test_position_router.py -v`
- [ ] **Step 5: Commit** `git add src/positions/position_router.py tests/test_position_router.py && git commit -m "feat: add position_router with CRUD, dashboard, and WebSocket"`

---

### Task 13: Server Integration

**Files:** Modify `src/server.py`

- [ ] **Step 1: Add CORS PATCH** — change `allow_methods=["GET", "POST", "DELETE"]` to `allow_methods=["GET", "POST", "DELETE", "PATCH"]`

- [ ] **Step 2: Add position system** — After arbitrage imports, add try/except import of `positions.position_router`, `positions.position_manager`, `positions.exit_engine`, `positions.ai_advisor`, `positions.wallet_config`. In lifespan: build executors dict, wrap in PaperExecutor if paper mode, init PositionManager, AIAdvisor, ExitEngine, call `init_position_system()`, create exit engine task. In shutdown: stop exit engine. After arbitrage router include: include position_router.

- [ ] **Step 3: Verify server starts** `cd src && python -c "from server import app; print('OK')"`
- [ ] **Step 4: Commit** `git add src/server.py && git commit -m "feat: integrate position system into server with paper mode and exit engine"`

---

## Chunk 7: Frontend

### Task 14: Frontend — Positions Dashboard

**Files:** Modify `src/static/js/arbitrout.js`, `src/static/css/arbitrout.css`

- [ ] **Step 1: Add CSS** — Append positions dashboard styles to `src/static/css/arbitrout.css`: package cards, ITM/OTM badges, leg rows, alerts panel, paper mode banner, portfolio stats bar.

- [ ] **Step 2: Add JS** — Append to `src/static/js/arbitrout.js`: positions data loading (fetch /api/derivatives/packages, /dashboard, /alerts, /config), WebSocket connection to /api/derivatives/ws, rendering functions for portfolio bar, package cards with leg details, alerts panel with approve buttons, and exit/approve action functions. Use safe DOM construction methods (createElement + textContent) instead of string interpolation to prevent XSS.

- [ ] **Step 3: Commit** `git add src/static/js/arbitrout.js src/static/css/arbitrout.css && git commit -m "feat: add positions dashboard UI with package cards, alerts, and paper mode"`

---

### Task 15: End-to-End Verification

- [ ] **Step 1: Run all tests** `python -m pytest tests/ -v` — expect all pass
- [ ] **Step 2: Verify server** `cd src && python -c "from server import app; print('OK')"`
- [ ] **Step 3: Final commit** `git add -A && git commit -m "feat: complete derivative position manager with paper trading, AI exits, and dashboard"`

---

## Summary

| Task | Description | Key Files |
|------|-------------|-----------|
| 1 | Base executor + models | base_executor.py |
| 2 | Wallet config | wallet_config.py |
| 3 | Paper executor | paper_executor.py |
| 4 | Polymarket rewrite | polymarket_executor.py |
| 5 | Kalshi executor | kalshi_executor.py |
| 6 | Coinbase executor | coinbase_spot_executor.py |
| 7 | PredictIt + Robinhood | predictit_executor.py, robinhood_advisor.py |
| 8 | Dependencies | requirements.txt |
| 9 | Position manager | position_manager.py |
| 10 | Exit engine | exit_engine.py |
| 11 | AI advisor | ai_advisor.py |
| 12 | Position router | position_router.py |
| 13 | Server integration | server.py |
| 14 | Frontend dashboard | arbitrout.js, arbitrout.css |
| 15 | Verification | all tests |

**Total: 13 new files + 4 modified, ~1,924 lines across 15 tasks.**
