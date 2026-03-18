"""Tests for wallet configuration."""
import pytest
from positions.wallet_config import get_configured_platforms, is_paper_mode, get_paper_balance

class TestPaperMode:
    def test_default_true(self, monkeypatch):
        monkeypatch.delenv("PAPER_TRADING", raising=False)
        assert is_paper_mode() is True
    def test_false(self, monkeypatch):
        monkeypatch.setenv("PAPER_TRADING", "false")
        assert is_paper_mode() is False
    def test_balance_default(self, monkeypatch):
        monkeypatch.delenv("PAPER_STARTING_BALANCE", raising=False)
        assert get_paper_balance() == 10000.0

class TestConfiguredPlatforms:
    def test_none_set(self, monkeypatch):
        for k in ["POLYMARKET_PRIVATE_KEY","POLYMARKET_FUNDER_ADDRESS","KALSHI_API_KEY",
                   "KALSHI_RSA_PRIVATE_KEY","COINBASE_ADV_API_KEY","COINBASE_ADV_API_SECRET",
                   "PREDICTIT_SESSION"]:
            monkeypatch.delenv(k, raising=False)
        assert get_configured_platforms() == {}
    def test_polymarket(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
        monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", "0xdef")
        assert "polymarket" in get_configured_platforms()
