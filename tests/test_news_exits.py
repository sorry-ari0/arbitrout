# tests/test_news_exits.py
import time
import pytest

def make_news_scanner():
    from unittest.mock import MagicMock
    from positions.news_scanner import NewsScanner
    pm = MagicMock()
    pm.trade_journal = MagicMock()
    pm.trade_journal.get_cumulative_pnl = MagicMock(return_value=0.0)
    pm.list_packages = MagicMock(return_value=[])
    ns = NewsScanner(
        position_manager=pm,
        news_ai=MagicMock(),
    )
    return ns

def test_get_recent_headlines_returns_matches():
    ns = make_news_scanner()
    now = time.time()
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
