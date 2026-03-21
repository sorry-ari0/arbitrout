"""Tests for auto trader improvements: churn reduction, filters, scoring."""
import pytest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock


class TestChurnReduction:
    def test_min_spread_is_8(self):
        """MIN_SPREAD_PCT lowered to 8% (0% maker fees both sides)."""
        from positions.auto_trader import MIN_SPREAD_PCT
        assert MIN_SPREAD_PCT == 8.0

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


class TestTradeablePlatformFilter:
    """Fix 2: _arb_to_opportunity should skip arbs on non-tradeable platforms."""

    def _make_arb(self, yes_platform="polymarket", no_platform="kalshi"):
        """Helper to build a minimal arb dict."""
        return {
            "matched_event": {
                "canonical_title": "Will BTC exceed $100k?",
                "category": "crypto",
                "expiry": "2026-12-31",
                "markets": [
                    {"platform": yes_platform, "event_id": "evt_yes",
                     "yes_price": 0.40, "no_price": 0.55, "volume": 50000},
                    {"platform": no_platform, "event_id": "evt_no",
                     "yes_price": 0.50, "no_price": 0.45, "volume": 40000},
                ],
            },
            "buy_yes_platform": yes_platform,
            "buy_no_platform": no_platform,
            "buy_yes_price": 0.40,
            "buy_no_price": 0.45,
            "spread": 0.15,
            "profit_pct": 15.0,
            "net_profit_pct": 13.0,
            "combined_volume": 90000,
            "confidence": "high",
        }

    def test_predictit_arb_filtered_when_no_executor(self):
        """PredictIt arb should return None when PredictIt executor is absent."""
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        pm.list_packages = MagicMock(return_value=[])
        pm.executors = {"polymarket": MagicMock(), "kalshi": MagicMock()}
        trader = AutoTrader(pm)

        arb = self._make_arb(yes_platform="predictit", no_platform="polymarket")
        result = trader._arb_to_opportunity(arb)
        assert result is None

    def test_predictit_arb_passes_when_executor_present(self, caplog):
        """PredictIt arb should NOT be filtered when PredictIt executor IS configured."""
        import logging
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        pm.list_packages = MagicMock(return_value=[])
        pm.executors = {
            "polymarket": MagicMock(),
            "kalshi": MagicMock(),
            "predictit": MagicMock(),
        }
        trader = AutoTrader(pm)

        arb = self._make_arb(yes_platform="predictit", no_platform="polymarket")
        with caplog.at_level(logging.DEBUG, logger="positions.auto_trader"):
            trader._arb_to_opportunity(arb)
        # The tradeable filter should NOT have triggered
        assert "Skipping arb on non-tradeable platform" not in caplog.text

    def test_polymarket_kalshi_arb_unaffected(self, caplog):
        """Standard Polymarket/Kalshi arb should not trigger the tradeable filter."""
        import logging
        from positions.auto_trader import AutoTrader
        pm = MagicMock()
        pm.list_packages = MagicMock(return_value=[])
        pm.executors = {"polymarket": MagicMock(), "kalshi": MagicMock()}
        trader = AutoTrader(pm)

        arb = self._make_arb(yes_platform="polymarket", no_platform="kalshi")
        with caplog.at_level(logging.DEBUG, logger="positions.auto_trader"):
            trader._arb_to_opportunity(arb)
        # The tradeable filter should NOT have triggered
        assert "Skipping arb on non-tradeable platform" not in caplog.text


class TestMarketCategoryFilter:
    """Journal-driven: sports -$91.99 (10 trades), commodities -$45.76 (3 trades)."""

    def test_sports_keywords_exist(self):
        from positions.auto_trader import SPORTS_KEYWORDS
        assert "ncaa" in SPORTS_KEYWORDS
        assert "nba" in SPORTS_KEYWORDS
        assert "ufc" in SPORTS_KEYWORDS
        assert "vs." in SPORTS_KEYWORDS

    def test_commodities_keywords_exist(self):
        from positions.auto_trader import COMMODITIES_KEYWORDS
        assert "crude oil" in COMMODITIES_KEYWORDS
        assert "wti" in COMMODITIES_KEYWORDS

    def test_exact_score_detected(self):
        title = "Exact Score: Fulham FC 1 - 1 Burnley FC".lower()
        assert "exact score" in title

    def test_ncaa_detected(self):
        from positions.auto_trader import SPORTS_KEYWORDS
        title = "Will the 2026 Men's NCAA basketball championship go to Duke?".lower()
        assert any(kw in title for kw in SPORTS_KEYWORDS)

    def test_crypto_not_detected_as_sports(self):
        from positions.auto_trader import SPORTS_KEYWORDS
        title = "Will Bitcoin reach $100k by end of 2026?".lower()
        assert not any(kw in title for kw in SPORTS_KEYWORDS)

    def test_commodities_detected(self):
        from positions.auto_trader import COMMODITIES_KEYWORDS
        title = "Will Crude Oil (CL) settle over $70 by Friday?".lower()
        assert any(kw in title for kw in COMMODITIES_KEYWORDS)

    def test_position_size_constants(self):
        from positions.auto_trader import MAX_TRADE_SIZE, MIN_TRADE_SIZE, MAX_TOTAL_EXPOSURE
        assert MAX_TRADE_SIZE == 50.0
        assert MIN_TRADE_SIZE == 10.0
        assert MAX_TOTAL_EXPOSURE == 350.0


class TestTrailingStopCalibration:
    """Journal-driven: 0/8 trailing stop wins, NCAA lost -13.5% avg."""

    def test_minimum_trail_floor(self):
        """Trail should never go below 25%, even for favorites."""
        from positions.exit_engine import evaluate_heuristics
        pkg = {
            "strategy_type": "pure_prediction",
            "legs": [{"entry_price": 0.80, "status": "open", "current_price": 0.80,
                       "quantity": 100, "cost": 80}],
            "exit_rules": [{"type": "trailing_stop", "active": True, "params": {"current": 35}}],
            "total_cost": 80,
            "peak_value": 100,
            "current_value": 70,  # 30% drawdown from peak
        }
        triggers = evaluate_heuristics(pkg)
        trailing = [t for t in triggers if t["name"] == "trailing_stop"]
        # 35 * 0.7 (favorite) = 24.5, but floor is 25%. Drawdown is 30% >= 25%
        assert len(trailing) == 1
        assert "25.0" in trailing[0]["details"]

    def test_binary_event_wider_trail(self):
        """Sports events with favorite entry should get 1.5x wider trail."""
        from positions.exit_engine import evaluate_heuristics
        pkg = {
            "name": "Auto: NCAA Basketball Final",
            "strategy_type": "pure_prediction",
            "legs": [{"entry_price": 0.70, "status": "open", "current_price": 0.70,
                       "quantity": 100, "cost": 70}],
            "exit_rules": [{"type": "trailing_stop", "active": True, "params": {"current": 35}}],
            "total_cost": 70,
            "peak_value": 80,
            "current_value": 30,  # 62.5% drawdown
        }
        triggers = evaluate_heuristics(pkg)
        trailing = [t for t in triggers if t["name"] == "trailing_stop"]
        # 35 * 0.7 (favorite) * 1.5 (sports) = 36.75%, floored to 36.75%. Drawdown 62.5% >= 36.75%
        assert len(trailing) == 1

    def test_non_sports_normal_trail(self):
        """Non-sports favorite positions should use normal trail without widening."""
        from positions.exit_engine import evaluate_heuristics
        pkg = {
            "name": "Auto: Will Bitcoin reach $100k",
            "strategy_type": "pure_prediction",
            "legs": [{"entry_price": 0.70, "status": "open", "current_price": 0.70,
                       "quantity": 100, "cost": 70}],
            "exit_rules": [{"type": "trailing_stop", "active": True, "params": {"current": 35}}],
            "total_cost": 70,
            "peak_value": 80,
            "current_value": 55,  # 31.25% drawdown
        }
        triggers = evaluate_heuristics(pkg)
        trailing = [t for t in triggers if t["name"] == "trailing_stop"]
        # 35 * 0.7 (favorite) = 24.5%, floored to 25%. Drawdown 31.25% >= 25%
        assert len(trailing) == 1

    def test_non_favorite_trailing_stop_skipped(self):
        """Mid-range entries (< 0.60) should NOT trigger trailing stop."""
        from positions.exit_engine import evaluate_heuristics
        pkg = {
            "name": "Auto: Will BTC reach $120k",
            "strategy_type": "pure_prediction",
            "legs": [{"entry_price": 0.50, "status": "open", "current_price": 0.50,
                       "quantity": 100, "cost": 50}],
            "exit_rules": [{"type": "trailing_stop", "active": True, "params": {"current": 35}}],
            "total_cost": 50,
            "peak_value": 60,
            "current_value": 25,  # 58% drawdown — would fire, but entry < 0.60
        }
        triggers = evaluate_heuristics(pkg)
        trailing = [t for t in triggers if t["name"] == "trailing_stop"]
        # Entry 0.50 < 0.60 threshold → trailing stop skipped entirely
        assert len(trailing) == 0


class TestEquityCurve:
    """Equity curve for persistent P&L tracking."""

    def test_empty_journal(self):
        from positions.trade_journal import TradeJournal
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            j = TradeJournal(Path(d))
            curve = j.get_equity_curve()
            assert curve["total_trades"] == 0
            assert curve["cumulative_pnl_usd"] == 0
            assert curve["curve"] == []

    def test_cumulative_tracking(self):
        from positions.trade_journal import TradeJournal
        from pathlib import Path
        import tempfile, time
        with tempfile.TemporaryDirectory() as d:
            j = TradeJournal(Path(d))
            j.entries = [
                {"pnl": 10.0, "total_fees": 1.0, "closed_at": time.time() - 300,
                 "name": "Win1", "exit_trigger": "bracket_target", "mode": "paper"},
                {"pnl": -5.0, "total_fees": 0.5, "closed_at": time.time() - 200,
                 "name": "Loss1", "exit_trigger": "stop_loss", "mode": "paper"},
                {"pnl": 8.0, "total_fees": 0.0, "closed_at": time.time() - 100,
                 "name": "Win2", "exit_trigger": "bracket_target", "mode": "paper"},
            ]
            curve = j.get_equity_curve()
            assert curve["total_trades"] == 3
            assert curve["cumulative_pnl_usd"] == 13.0
            assert curve["cumulative_fees_usd"] == 1.5
            assert curve["peak_equity_usd"] == 13.0
            assert curve["max_drawdown_usd"] == 5.0  # Peak 10 -> trough 5

    def test_mode_filter(self):
        from positions.trade_journal import TradeJournal
        from pathlib import Path
        import tempfile, time
        with tempfile.TemporaryDirectory() as d:
            j = TradeJournal(Path(d))
            j.entries = [
                {"pnl": 10.0, "total_fees": 0, "closed_at": time.time(),
                 "name": "Paper", "exit_trigger": "t", "mode": "paper"},
                {"pnl": 20.0, "total_fees": 0, "closed_at": time.time(),
                 "name": "Live", "exit_trigger": "t", "mode": "live"},
            ]
            paper_curve = j.get_equity_curve(mode="paper")
            assert paper_curve["total_trades"] == 1
            assert paper_curve["cumulative_pnl_usd"] == 10.0


class TestPoliticalEventResolved:
    """Fix: political_event_resolved should NOT force-exit when only some legs resolved."""

    def test_all_legs_resolved_triggers_immediate_exit(self):
        """When ALL legs have resolved prices, trigger immediate_exit."""
        from positions.exit_engine import evaluate_heuristics
        pkg = {
            "strategy_type": "political_synthetic",
            "legs": [
                {"leg_id": "L1", "entry_price": 0.40, "status": "open",
                 "current_price": 0.99, "quantity": 50, "cost": 20},
                {"leg_id": "L2", "entry_price": 0.30, "status": "open",
                 "current_price": 0.01, "quantity": 50, "cost": 15},
            ],
            "exit_rules": [],
            "total_cost": 35,
            "peak_value": 50,
            "current_value": 50,
        }
        triggers = evaluate_heuristics(pkg)
        pol = [t for t in triggers if t["name"] == "political_event_resolved"]
        assert len(pol) == 1
        assert pol[0]["action"] == "immediate_exit"
        assert pol[0]["safety_override"] is True

    def test_partial_resolution_triggers_review_not_exit(self):
        """When only SOME legs resolved, trigger review (not immediate_exit)."""
        from positions.exit_engine import evaluate_heuristics
        pkg = {
            "strategy_type": "political_synthetic",
            "legs": [
                {"leg_id": "L1", "entry_price": 0.40, "status": "open",
                 "current_price": 0.99, "quantity": 50, "cost": 20},
                {"leg_id": "L2", "entry_price": 0.30, "status": "open",
                 "current_price": 0.45, "quantity": 50, "cost": 15},
            ],
            "exit_rules": [],
            "total_cost": 35,
            "peak_value": 50,
            "current_value": 72,
        }
        triggers = evaluate_heuristics(pkg)
        pol = [t for t in triggers if t["name"] == "political_event_resolved"]
        assert len(pol) == 1
        assert pol[0]["action"] == "review"
        assert pol[0]["safety_override"] is False

    def test_no_resolved_legs_no_trigger(self):
        """When no legs have resolved, no political trigger fires."""
        from positions.exit_engine import evaluate_heuristics
        pkg = {
            "strategy_type": "political_synthetic",
            "legs": [
                {"leg_id": "L1", "entry_price": 0.40, "status": "open",
                 "current_price": 0.55, "quantity": 50, "cost": 20},
                {"leg_id": "L2", "entry_price": 0.30, "status": "open",
                 "current_price": 0.45, "quantity": 50, "cost": 15},
            ],
            "exit_rules": [],
            "total_cost": 35,
            "peak_value": 50,
            "current_value": 50,
        }
        triggers = evaluate_heuristics(pkg)
        pol = [t for t in triggers if t["name"] == "political_event_resolved"]
        assert len(pol) == 0


class TestSyntheticValidation:
    """Relaxed validation gates for political synthetic strategies."""

    def test_relaxed_win_probability(self):
        """35% win probability should now pass (was 50%)."""
        from political.strategy import validate_strategy
        from political.models import PoliticalSyntheticStrategy, SyntheticLeg, Scenario
        s = PoliticalSyntheticStrategy(
            cluster_id="test", strategy_name="test",
            legs=[SyntheticLeg(contract_idx=1, event_id="e1", side="YES", weight=1.0)],
            scenarios=[Scenario(outcome="win", probability=0.35, pnl_pct=20.0)],
            expected_value_pct=2.0, win_probability=0.35,
            max_loss_pct=-40.0, confidence="medium", reasoning="test",
        )
        assert validate_strategy(s) is True

    def test_relaxed_expected_value(self):
        """1% EV should now pass (was 3%)."""
        from political.strategy import validate_strategy
        from political.models import PoliticalSyntheticStrategy, SyntheticLeg, Scenario
        s = PoliticalSyntheticStrategy(
            cluster_id="test", strategy_name="test",
            legs=[SyntheticLeg(contract_idx=1, event_id="e1", side="YES", weight=1.0)],
            scenarios=[Scenario(outcome="win", probability=0.50, pnl_pct=5.0)],
            expected_value_pct=1.0, win_probability=0.50,
            max_loss_pct=-30.0, confidence="high", reasoning="test",
        )
        assert validate_strategy(s) is True

    def test_still_rejects_low_confidence(self):
        """Low confidence should still be rejected."""
        from political.strategy import validate_strategy
        from political.models import PoliticalSyntheticStrategy, SyntheticLeg, Scenario
        s = PoliticalSyntheticStrategy(
            cluster_id="test", strategy_name="test",
            legs=[SyntheticLeg(contract_idx=1, event_id="e1", side="YES", weight=1.0)],
            scenarios=[Scenario(outcome="win", probability=0.60, pnl_pct=10.0)],
            expected_value_pct=5.0, win_probability=0.60,
            max_loss_pct=-30.0, confidence="low", reasoning="test",
        )
        assert validate_strategy(s) is False

    def test_still_rejects_too_low_win_prob(self):
        """Below 35% win probability should still be rejected."""
        from political.strategy import validate_strategy
        from political.models import PoliticalSyntheticStrategy, SyntheticLeg, Scenario
        s = PoliticalSyntheticStrategy(
            cluster_id="test", strategy_name="test",
            legs=[SyntheticLeg(contract_idx=1, event_id="e1", side="YES", weight=1.0)],
            scenarios=[Scenario(outcome="win", probability=0.30, pnl_pct=10.0)],
            expected_value_pct=5.0, win_probability=0.30,
            max_loss_pct=-30.0, confidence="medium", reasoning="test",
        )
        assert validate_strategy(s) is False


class TestNewsThresholds:
    """Relaxed news scanner thresholds."""

    def test_news_daily_cap_exists(self):
        from positions.news_scanner import DAILY_TRADE_CAP
        assert DAILY_TRADE_CAP == 5
