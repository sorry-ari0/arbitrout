"""Tests for limit order support across the executor layer."""
import pytest
import asyncio
from execution.base_executor import BaseExecutor, ExecutionResult, BalanceResult, PositionInfo
from execution.paper_executor import PaperExecutor, MAKER_FEE_RATES, TAKER_FEE_RATES


def _run(coro):
    return asyncio.run(coro)


# --- Stub executor for testing BaseExecutor defaults ---

class StubExecutor(BaseExecutor):
    """Minimal concrete executor — inherits default limit order methods."""
    async def buy(self, asset_id, amount_usd):
        return ExecutionResult(True, "market_buy", 0.60, amount_usd / 0.60, 0.04, None)
    async def sell(self, asset_id, quantity):
        return ExecutionResult(True, "market_sell", 0.70, quantity, 0.03, None)
    async def get_balance(self):
        return BalanceResult(1000, 1000)
    async def get_positions(self):
        return []
    async def get_current_price(self, asset_id):
        return 0.65
    def is_configured(self):
        return True


class TestBaseExecutorDefaults:
    """BaseExecutor default limit order methods fall back to market orders."""

    def test_buy_limit_falls_back_to_buy(self):
        ex = StubExecutor()
        r = _run(ex.buy_limit("tok:YES", 100.0, 0.60))
        assert r.success
        assert r.tx_id == "market_buy"  # fell back to market buy

    def test_sell_limit_falls_back_to_sell(self):
        ex = StubExecutor()
        r = _run(ex.sell_limit("tok:YES", 50.0, 0.70))
        assert r.success
        assert r.tx_id == "market_sell"  # fell back to market sell

    def test_check_order_status_default(self):
        ex = StubExecutor()
        status = _run(ex.check_order_status("order_123"))
        assert status["status"] == "UNKNOWN"

    def test_cancel_order_default(self):
        ex = StubExecutor()
        assert _run(ex.cancel_order("order_123")) is False


# --- Fake executor for PaperExecutor testing ---

class FakePolymarketExecutor(BaseExecutor):
    """Mimics PolymarketExecutor for paper testing — class name contains 'polymarket'."""
    def __init__(self):
        self._prices = {"tok:YES": 0.65, "tok:NO": 0.35}
    async def buy(self, a, amt):
        return ExecutionResult(True, "r", self._prices.get(a, 1.0), amt / self._prices.get(a, 1.0), 0, None)
    async def sell(self, a, q):
        return ExecutionResult(True, "r", self._prices.get(a, 1.0), q, 0, None)
    async def get_balance(self):
        return BalanceResult(1000, 1000)
    async def get_positions(self):
        return []
    async def get_current_price(self, a):
        return self._prices.get(a, 1.0)
    def is_configured(self):
        return True


class TestPaperBuyLimit:
    """PaperExecutor.buy_limit uses maker fee rate (0% for Polymarket)."""

    def test_buy_limit_zero_fees_polymarket(self):
        paper = PaperExecutor(FakePolymarketExecutor(), starting_balance=1000.0)
        r = _run(paper.buy_limit("tok:YES", 100.0, 0.65))
        assert r.success
        assert r.fees == 0.0  # 0% maker fee for polymarket
        assert r.filled_price == 0.65
        assert r.filled_quantity == pytest.approx(100.0 / 0.65)
        # Balance should be exactly $900 (no fees)
        assert paper.balance == pytest.approx(900.0)

    def test_buy_limit_uses_limit_price_not_market(self):
        paper = PaperExecutor(FakePolymarketExecutor(), starting_balance=1000.0)
        # Limit price different from market price (0.65)
        r = _run(paper.buy_limit("tok:YES", 100.0, 0.60))
        assert r.success
        assert r.filled_price == 0.60  # used limit price
        assert r.filled_quantity == pytest.approx(100.0 / 0.60)

    def test_buy_limit_insufficient_balance(self):
        paper = PaperExecutor(FakePolymarketExecutor(), starting_balance=50.0)
        r = _run(paper.buy_limit("tok:YES", 100.0, 0.65))
        assert not r.success
        assert "Insufficient" in r.error

    def test_buy_limit_invalid_price(self):
        paper = PaperExecutor(FakePolymarketExecutor(), starting_balance=1000.0)
        r = _run(paper.buy_limit("tok:YES", 100.0, 0.0))
        assert not r.success
        assert "Invalid" in r.error

    def test_buy_limit_records_trade(self):
        paper = PaperExecutor(FakePolymarketExecutor(), starting_balance=1000.0)
        _run(paper.buy_limit("tok:YES", 100.0, 0.65))
        assert len(paper.trade_history) == 1
        assert paper.trade_history[0]["action"] == "buy_limit"
        assert paper.trade_history[0]["fee"] == 0.0

    def test_buy_limit_does_not_mutate_buy_fee_rate(self):
        paper = PaperExecutor(FakePolymarketExecutor(), starting_balance=1000.0)
        original_rate = paper.buy_fee_rate
        _run(paper.buy_limit("tok:YES", 100.0, 0.65))
        assert paper.buy_fee_rate == original_rate

    def test_buy_limit_accumulates_position(self):
        paper = PaperExecutor(FakePolymarketExecutor(), starting_balance=1000.0)
        _run(paper.buy_limit("tok:YES", 100.0, 0.60))
        _run(paper.buy_limit("tok:YES", 100.0, 0.70))
        pos = paper.positions["tok:YES"]
        # Weighted average: (0.60 * (100/0.60) + 0.70 * (100/0.70)) / (100/0.60 + 100/0.70)
        expected_qty = 100.0 / 0.60 + 100.0 / 0.70
        assert pos["quantity"] == pytest.approx(expected_qty)


class TestPaperSellLimit:
    """PaperExecutor.sell_limit uses maker fee rate (0% for Polymarket) and limit price."""

    def test_sell_limit_uses_maker_fee(self):
        paper = PaperExecutor(FakePolymarketExecutor(), starting_balance=1000.0)
        _run(paper.buy("tok:YES", 100.0))
        qty = paper.positions["tok:YES"]["quantity"]
        r = _run(paper.sell_limit("tok:YES", qty, 0.75))
        assert r.success
        # Should use maker fee rate (0% for Polymarket), not taker fee
        assert r.fees == 0.0  # 0% maker fee for polymarket
        assert r.filled_price == 0.75  # uses limit price, not market price


class TestFeeRateComparison:
    """Verify that limit orders save fees vs market orders."""

    def test_limit_vs_market_fee_savings(self):
        paper_limit = PaperExecutor(FakePolymarketExecutor(), starting_balance=1000.0)
        paper_market = PaperExecutor(FakePolymarketExecutor(), starting_balance=1000.0, use_limit_orders=False)

        # Limit buy: 0% fee
        r_limit = _run(paper_limit.buy_limit("tok:YES", 200.0, 0.65))
        # Market buy: 2% fee
        r_market = _run(paper_market.buy("tok:YES", 200.0))

        assert r_limit.fees == 0.0
        assert r_market.fees == pytest.approx(200.0 * 0.02, abs=0.01)
        # Limit order saves $4 on a $200 trade
        savings = r_market.fees - r_limit.fees
        assert savings == pytest.approx(4.0, abs=0.01)
