import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from adapters.models import MatchedEvent, NormalizedEvent
from theta_scanner import ThetaScanner


def _event(platform: str, yes_price: float, *, volume: int = 1000) -> NormalizedEvent:
    expiry = (date.today() + timedelta(days=2)).isoformat()
    return NormalizedEvent(
        platform=platform,
        event_id=f"{platform}-1",
        title="Will BTC exceed $100,000 by Friday?",
        category="crypto",
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 4),
        volume=volume,
        expiry=expiry,
        url=f"https://{platform}.example.com/market",
    )


def test_theta_scanner_flags_outlier_market():
    matched = MatchedEvent(
        match_id="btc-100k",
        canonical_title="Will BTC exceed $100,000 by Friday?",
        category="crypto",
        expiry=(date.today() + timedelta(days=2)).isoformat(),
        markets=[
            _event("polymarket", 0.30),
            _event("kalshi", 0.52),
            _event("predictit", 0.50),
        ],
    )

    scanner = ThetaScanner(registry=None)
    opportunities = scanner._scan_matched_events(
        [matched],
        as_of=date.today(),
        max_days_to_expiry=7,
        min_edge=0.08,
        min_volume=0,
    )

    assert opportunities
    best = opportunities[0]
    assert best["platform"] == "polymarket"
    assert best["buy_side"] == "YES"
    assert best["days_to_expiry"] == 2
    assert best["edge_pct"] >= 20


def test_theta_scanner_ignores_far_expiry():
    matched = MatchedEvent(
        match_id="btc-100k",
        canonical_title="Will BTC exceed $100,000 by Friday?",
        category="crypto",
        expiry=(date.today() + timedelta(days=30)).isoformat(),
        markets=[_event("polymarket", 0.30), _event("kalshi", 0.50)],
    )

    scanner = ThetaScanner(registry=None)
    opportunities = scanner._scan_matched_events(
        [matched],
        as_of=date.today(),
        max_days_to_expiry=7,
        min_edge=0.08,
        min_volume=0,
    )

    assert opportunities == []
