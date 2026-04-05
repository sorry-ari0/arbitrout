"""Paper journal vertical health → live pause list."""
import json
import time
from pathlib import Path

import pytest

from positions import journal_vertical_health as jvh


def _write_journal(tmp: Path, entries: list[dict]) -> Path:
    p = tmp / "trade_journal_paper.json"
    p.write_text(json.dumps({"entries": entries}, indent=2), encoding="utf-8")
    return p


def test_paused_vertical_negative_pnl_non_news(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_JOURNAL_PAPER_PATH", str(_write_journal(tmp_path, [
        {
            "strategy_type": "pure_prediction",
            "pnl": -5.0,
            "closed_at": time.time(),
            "mode": "paper",
            "news_sleeve": False,
        },
    ])))
    monkeypatch.setenv("JOURNAL_HEALTH_CACHE_SEC", "0")
    jvh.invalidate_paused_verticals_cache()
    assert "pure_prediction" in jvh.get_paused_non_news_verticals()


def test_news_sleeve_excluded_from_pause_rollup(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_JOURNAL_PAPER_PATH", str(_write_journal(tmp_path, [
        {
            "strategy_type": "pure_prediction",
            "pnl": -50.0,
            "closed_at": time.time(),
            "mode": "paper",
            "news_sleeve": True,
        },
        {
            "strategy_type": "pure_prediction",
            "pnl": 10.0,
            "closed_at": time.time(),
            "mode": "paper",
            "news_sleeve": False,
        },
    ])))
    monkeypatch.setenv("JOURNAL_HEALTH_CACHE_SEC", "0")
    jvh.invalidate_paused_verticals_cache()
    assert "pure_prediction" not in jvh.get_paused_non_news_verticals()


def test_live_allows_news_pkg_when_vertical_paused(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.setenv("LIVE_TRADE_ALL_STRATEGIES", "true")
    monkeypatch.setenv("TRADE_JOURNAL_PAPER_PATH", str(_write_journal(tmp_path, [
        {
            "strategy_type": "pure_prediction",
            "pnl": -1.0,
            "closed_at": time.time(),
            "mode": "paper",
            "news_sleeve": False,
        },
    ])))
    monkeypatch.setenv("JOURNAL_HEALTH_CACHE_SEC", "0")
    jvh.invalidate_paused_verticals_cache()
    ok, _ = jvh.live_journal_allows_package_open(
        {"strategy_type": "pure_prediction", "_news_driven": True}
    )
    assert ok is True
    ok2, err = jvh.live_journal_allows_package_open({"strategy_type": "pure_prediction"})
    assert ok2 is False
    assert "pure_prediction" in err


def test_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JOURNAL_HEALTH_DISABLE", "true")
    monkeypatch.setenv("TRADE_JOURNAL_PAPER_PATH", str(_write_journal(tmp_path, [
        {"strategy_type": "cross_platform_arb", "pnl": -99.0, "closed_at": time.time(), "mode": "paper"},
    ])))
    jvh.invalidate_paused_verticals_cache()
    assert jvh.get_paused_non_news_verticals() == frozenset()


def test_resolve_opportunity_vertical():
    opp = {
        "opportunity_type": "multi_outcome_arb",
        "buy_yes_platform": "polymarket",
        "buy_no_platform": "polymarket",
    }
    assert jvh.resolve_opportunity_vertical_strategy(opp) == "multi_outcome_arb"
    opp2 = {
        "buy_yes_platform": "polymarket",
        "buy_no_platform": "kalshi",
        "buy_yes_market_id": "a",
        "buy_no_market_id": "b",
    }
    assert jvh.resolve_opportunity_vertical_strategy(opp2) == "cross_platform_arb"
