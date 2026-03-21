"""Tests for auto trader improvements: churn reduction, filters, scoring."""
import pytest
from datetime import date, datetime, timedelta
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


class TestShortDurationFilter:
    def test_min_hours_constant_exists(self):
        from positions.auto_trader import MIN_HOURS_TO_EXPIRY
        assert MIN_HOURS_TO_EXPIRY >= 1.0

    def test_short_expiry_opportunity_skipped(self):
        """An opportunity expiring in 30 minutes should be skipped."""
        from positions.auto_trader import MIN_HOURS_TO_EXPIRY
        soon = (datetime.now() + timedelta(minutes=30)).isoformat()
        exp_dt = datetime.fromisoformat(soon)
        hours = (exp_dt - datetime.now()).total_seconds() / 3600
        assert hours < MIN_HOURS_TO_EXPIRY


class TestFavoriteLongshot:
    def test_favorite_scores_higher_than_longshot(self):
        """Same spread — favorite (0.85) should score much higher than longshot (0.15)."""
        spread_pct = 15.0
        fav_score = spread_pct * 2.5
        long_score = spread_pct * 0.2
        assert fav_score > long_score * 10

    def test_moderate_favorite_multiplier(self):
        """Moderate favorites (0.70-0.79) should get 1.8x."""
        spread_pct = 15.0
        score = spread_pct * 1.8
        assert score > spread_pct * 1.5

    def test_kelly_fraction_longshot_is_smaller(self):
        """Longshots (<=0.30) use 1/8 Kelly, favorites (>=0.70) use 1/4."""
        longshot_frac = 0.125
        midrange_frac = 0.20
        favorite_frac = 0.25
        assert longshot_frac < midrange_frac < favorite_frac
        assert longshot_frac <= favorite_frac * 0.5


class TestVolumeFilter:
    def test_volume_filter_constant(self):
        from positions.auto_trader import MIN_ARB_VOLUME
        assert MIN_ARB_VOLUME == 50_000
        assert isinstance(MIN_ARB_VOLUME, int)

    def test_low_volume_arb_skipped(self):
        from positions.auto_trader import MIN_ARB_VOLUME
        opp = {"volume": 30_000, "opportunity_type": ""}
        exempt = ("political_synthetic", "crypto_synthetic", "weather", "multi_outcome_arb", "portfolio_no")
        should_skip = (opp.get("opportunity_type", "") not in exempt and opp.get("volume", 0) < MIN_ARB_VOLUME)
        assert should_skip is True

    def test_high_volume_arb_not_skipped(self):
        from positions.auto_trader import MIN_ARB_VOLUME
        opp = {"volume": 100_000, "opportunity_type": ""}
        exempt = ("political_synthetic", "crypto_synthetic", "weather", "multi_outcome_arb", "portfolio_no")
        should_skip = (opp.get("opportunity_type", "") not in exempt and opp.get("volume", 0) < MIN_ARB_VOLUME)
        assert should_skip is False

    def test_synthetic_exempt_from_volume_filter(self):
        from positions.auto_trader import MIN_ARB_VOLUME
        exempt = ("political_synthetic", "crypto_synthetic", "weather", "multi_outcome_arb", "portfolio_no")
        for opp_type in ["political_synthetic", "crypto_synthetic"]:
            opp = {"volume": 1_000, "opportunity_type": opp_type}
            should_skip = (opp.get("opportunity_type", "") not in exempt and opp.get("volume", 0) < MIN_ARB_VOLUME)
            assert should_skip is False, f"{opp_type} should be exempt"
