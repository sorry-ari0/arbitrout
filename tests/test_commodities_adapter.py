import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd

from adapters.commodities import CommoditiesAdapter, _annualized_volatility, _rolling_expiries


class _FakeTicker:
    def history(self, period="6mo", interval="1d", auto_adjust=False):
        return pd.DataFrame(
            {
                "Close": [2300, 2310, 2325, 2315, 2330, 2345, 2360, 2350, 2375, 2390] * 4,
                "Volume": [1000] * 40,
            }
        )


class _FakeYFinance:
    def Ticker(self, ticker_symbol):
        return _FakeTicker()


def test_annualized_volatility_uses_fallback_for_short_series():
    assert _annualized_volatility([100, 101, 102], fallback=0.25) == 0.25


def test_rolling_expiries_returns_future_dates():
    expiries = _rolling_expiries()
    assert len(expiries) == 2
    assert expiries[0] < expiries[1]


def test_fetch_sync_builds_normalized_events(monkeypatch):
    monkeypatch.setattr("adapters.commodities.yf", _FakeYFinance())
    adapter = CommoditiesAdapter()

    events = adapter._fetch_sync()

    assert events
    sample = events[0]
    assert sample.platform == "commodities"
    assert sample.category == "economics"
    assert sample.expiry
    assert sample.url.startswith("https://finance.yahoo.com/quote/")
    assert 0.0 <= sample.yes_price <= 1.0
    assert round(sample.yes_price + sample.no_price, 4) == 1.0
    assert sample.spot_price > 0
