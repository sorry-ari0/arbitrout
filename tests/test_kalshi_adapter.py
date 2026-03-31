import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from adapters.kalshi import KalshiAdapter


def test_kalshi_normalize_market_prefers_dollar_fields():
    adapter = KalshiAdapter()

    event = adapter._normalize_market(
        {
            "ticker": "KXBTC-YES",
            "title": "Will BTC exceed $100k?",
            "yes_ask_dollars": 0.57,
            "no_ask_dollars": 0.45,
            "volume": 1234,
            "expiration_time": "2026-12-31T00:00:00Z",
        }
    )

    assert event.yes_price == 0.57
    assert event.no_price == 0.45
    assert event.expiry == "2026-12-31"
    assert event.event_id == "KXBTC-YES"


def test_kalshi_normalize_market_falls_back_to_cents():
    adapter = KalshiAdapter()

    event = adapter._normalize_market(
        {
            "ticker": "KXBTC-NO",
            "title": "Will BTC exceed $100k?",
            "yes_bid": 54,
            "no_bid": 44,
            "volume": 1234,
            "close_time": "2026-12-31T00:00:00Z",
        }
    )

    assert event.yes_price == 0.54
    assert event.no_price == 0.44
