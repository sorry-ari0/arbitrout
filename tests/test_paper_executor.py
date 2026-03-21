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

def _run(coro):
    return asyncio.run(coro)

class TestPaperBuy:
    def test_buy_deducts(self, paper):
        r = _run(paper.buy("tok:YES", 100.0))
        assert r.success and r.filled_price == 0.65 and r.tx_id.startswith("paper_")
        b = _run(paper.get_balance())
        # $100 buy + 2% taker fee ($2) = $102 deducted from $1000
        expected = 1000.0 - 100.0 - (100.0 * paper.fee_rate)
        assert b.available == pytest.approx(expected)
    def test_insufficient(self, paper):
        r = _run(paper.buy("BTC", 2000.0))
        assert not r.success

class TestPaperSell:
    def test_sell_after_buy(self, paper):
        _run(paper.buy("tok:YES", 100.0))
        r = _run(paper.sell("tok:YES", 50.0))
        assert r.success
    def test_sell_no_position(self, paper):
        r = _run(paper.sell("BTC", 1.0))
        assert not r.success


class TestDynamicFees:
    def test_polymarket_taker_fee_uses_dynamic(self):
        from execution.paper_executor import get_taker_fee_rate
        fee = get_taker_fee_rate("polymarket", 0.50, "crypto")
        assert abs(fee - 0.015625) < 1e-6

    def test_kalshi_taker_fee_unchanged(self):
        from execution.paper_executor import get_taker_fee_rate
        fee = get_taker_fee_rate("kalshi", 0.50, "politics")
        assert fee == 0.01
