"""Tests for Kalshi executor executable-quote handling."""
import pytest


class _FakeMarket:
    yes_ask_dollars = 0.63
    no_ask_dollars = 0.41
    yes_bid_dollars = 0.61
    no_bid_dollars = 0.39


class _FakeResult:
    market = _FakeMarket()


class _FakeClient:
    def get_market(self, ticker):
        return _FakeResult()


@pytest.mark.asyncio
async def test_get_executable_price_prefers_fixed_point_market_fields():
    from execution.kalshi_executor import KalshiExecutor

    ex = KalshiExecutor()
    ex._get_client = lambda: _FakeClient()

    async def _run_sync(func, *args, **kwargs):
        return func(*args, **kwargs)

    ex._run_sync = _run_sync

    ask = await ex.get_executable_price("TICKER:YES", side="buy")
    bid = await ex.get_executable_price("TICKER:NO", side="sell")

    assert ask == 0.63
    assert bid == 0.39
    stats = ex.get_quote_stats()
    assert stats["market_fields"] >= 2
