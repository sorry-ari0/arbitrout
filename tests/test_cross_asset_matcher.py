import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from adapters.models import NormalizedEvent
from cross_asset_matcher import CrossAssetMatcher


def _event(platform: str, title: str, yes_price: float, expiry: str = "2026-12-31", spot_price: float = 0.0):
    return NormalizedEvent(
        platform=platform,
        event_id=f"{platform}-1",
        title=title,
        category="crypto",
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 4),
        volume=1000,
        expiry=expiry,
        url=f"https://{platform}.example.com/1",
        spot_price=spot_price,
    )


def test_cross_asset_matcher_finds_crypto_hedge():
    matcher = CrossAssetMatcher(registry=None)
    prediction_events = [
        _event("kalshi", "Will BTC exceed $100,000 by 2026-12-31?", 0.30),
    ]
    reference_events = [
        _event("crypto_spot", "Will BTC exceed $100,000 by end of 2026?", 0.55, spot_price=72000),
    ]

    opportunities = matcher._match_events(
        prediction_events=prediction_events,
        reference_events=reference_events,
        min_profit=0.02,
        max_expiry_gap_days=200,
    )

    assert opportunities
    best = opportunities[0]
    assert best["prediction_side"] == "YES"
    assert best["reference_side"] == "NO"
    assert best["asset_class"] == "crypto"
    assert best["guaranteed_profit_pct"] == 25.0
    assert best["prediction_volume"] == 1000
    assert best["combined_volume"] == 2000


def test_cross_asset_matcher_ignores_large_expiry_gap():
    matcher = CrossAssetMatcher(registry=None)
    prediction_events = [
        _event("kalshi", "Will BTC exceed $100,000 by 2026-12-31?", 0.30, expiry="2026-12-31"),
    ]
    reference_events = [
        _event("crypto_spot", "Will BTC exceed $100,000 by end of 2026?", 0.55, expiry="2028-12-31", spot_price=72000),
    ]

    opportunities = matcher._match_events(
        prediction_events=prediction_events,
        reference_events=reference_events,
        min_profit=0.02,
        max_expiry_gap_days=30,
    )

    assert opportunities == []
