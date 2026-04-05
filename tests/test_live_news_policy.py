"""Live trading: news-only open policy (wallet_config + execute_package gate)."""
import pytest

from positions.wallet_config import (
    live_news_only_execution_active,
    live_package_open_allowed,
)


def test_live_news_only_off_in_paper(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    assert live_news_only_execution_active() is False
    ok, err = live_package_open_allowed({"strategy_type": "cross_platform_arb"})
    assert ok is True
    assert err == ""


def test_live_news_only_on_when_live(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.delenv("LIVE_TRADE_ALL_STRATEGIES", raising=False)
    assert live_news_only_execution_active() is True
    ok, err = live_package_open_allowed({"strategy_type": "pure_prediction"})
    assert ok is False
    assert "news" in err.lower()
    ok, err = live_package_open_allowed({"strategy_type": "news_driven"})
    assert ok is True
    ok, err = live_package_open_allowed({"strategy_type": "pure_prediction", "_news_driven": True})
    assert ok is True


def test_live_all_strategies_override(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.setenv("LIVE_TRADE_ALL_STRATEGIES", "true")
    assert live_news_only_execution_active() is False
    ok, err = live_package_open_allowed({"strategy_type": "cross_platform_arb"})
    assert ok is True
