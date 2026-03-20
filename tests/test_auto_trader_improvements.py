"""Tests for auto trader improvements: churn reduction, filters, scoring."""
import pytest
from datetime import date
from unittest.mock import MagicMock


class TestChurnReduction:
    def test_min_spread_is_12(self):
        """MIN_SPREAD_PCT should be raised from 8% to 12%."""
        from positions.auto_trader import MIN_SPREAD_PCT
        assert MIN_SPREAD_PCT == 12.0

    def test_max_trades_per_day_is_3(self):
        from positions.auto_trader import MAX_NEW_TRADES_PER_DAY
        assert MAX_NEW_TRADES_PER_DAY == 3

    def test_cooldown_is_48h(self):
        from positions.auto_trader import MARKET_COOLDOWN_SECONDS
        assert MARKET_COOLDOWN_SECONDS == 172800

    def test_daily_limit_blocks_after_3(self):
        """_check_daily_limit should return False after 3 trades."""
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        pm.list_packages = MagicMock(return_value=[])
        trader = AutoTrader(pm)
        assert trader._check_daily_limit() is True
        trader._daily_trade_count = 1
        assert trader._check_daily_limit() is True
        trader._daily_trade_count = 2
        assert trader._check_daily_limit() is True
        trader._daily_trade_count = 3
        assert trader._check_daily_limit() is False

    def test_daily_limit_resets_on_new_day(self):
        """Counter should reset when the date changes."""
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        pm.list_packages = MagicMock(return_value=[])
        trader = AutoTrader(pm)
        trader._daily_trade_count = 3
        trader._daily_trade_date = "2020-01-01"
        assert trader._check_daily_limit() is True
        assert trader._daily_trade_count == 0
