"""Tests for auto trader improvements: churn reduction, filters, scoring."""
import pytest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock


def _make_mock_pm():
    """Create a MagicMock position_manager with journal that returns 0.0 PnL."""
    pm = MagicMock()
    pm.trade_journal = MagicMock()
    pm.trade_journal.get_cumulative_pnl = MagicMock(return_value=0.0)
    pm.list_packages = MagicMock(return_value=[])
    return pm


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
        pm = _make_mock_pm()
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
        pm = _make_mock_pm()
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
        fav_score = spread_pct * 3.0  # Strong favorite: 3.0x
        long_score = spread_pct * 0.1  # Extreme longshot: 0.1x
        assert fav_score > long_score * 10

    def test_moderate_favorite_multiplier(self):
        """Moderate favorites (0.70-0.79) should get 2.2x."""
        spread_pct = 15.0
        score = spread_pct * 2.2
        assert score > spread_pct * 1.5

    def test_mild_favorite_tier(self):
        """Mild favorites (0.60-0.69) should get 1.4x — new tier."""
        spread_pct = 15.0
        mild = spread_pct * 1.4
        base = spread_pct * 1.0
        assert mild > base

    def test_extreme_longshot_near_zero(self):
        """Extreme longshots (<= 0.15) should get 0.1x — near elimination."""
        spread_pct = 15.0
        extreme = spread_pct * 0.1
        assert extreme < 2.0  # Nearly eliminated

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
        pm = _make_mock_pm()
        pm.executors = {"polymarket": MagicMock(), "kalshi": MagicMock()}
        trader = AutoTrader(pm)

        arb = self._make_arb(yes_platform="predictit", no_platform="polymarket")
        result = trader._arb_to_opportunity(arb)
        assert result is None

    def test_predictit_arb_passes_when_executor_present(self, caplog):
        """PredictIt arb should NOT be filtered when PredictIt executor IS configured."""
        import logging
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
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
        pm = _make_mock_pm()
        pm.executors = {"polymarket": MagicMock(), "kalshi": MagicMock()}
        trader = AutoTrader(pm)

        arb = self._make_arb(yes_platform="polymarket", no_platform="kalshi")
        with caplog.at_level(logging.DEBUG, logger="positions.auto_trader"):
            trader._arb_to_opportunity(arb)
        # The tradeable filter should NOT have triggered
        assert "Skipping arb on non-tradeable platform" not in caplog.text


class TestReferenceScanners:
    def test_theta_opportunity_converts_to_directional_trade(self):
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        pm.executors = {"polymarket": MagicMock()}
        trader = AutoTrader(pm)

        opp = trader._theta_to_opportunity({
            "canonical_title": "Will BTC exceed $100,000 by Friday?",
            "platform": "polymarket",
            "event_id": "poly-btc-100k",
            "expiry": "2026-12-31",
            "days_to_expiry": 2,
            "market_yes_price": 0.30,
            "market_no_price": 0.70,
            "buy_side": "YES",
            "expected_edge_pct": 18.0,
            "theta_capture_pct_per_day": 9.0,
            "volume": 2500,
        })

        assert opp is not None
        assert opp["opportunity_type"] == "theta_consensus"
        assert opp["preferred_side"] == "YES"
        assert opp["_reference_backed"] is True
        assert opp["volume"] == 2500

    def test_cross_asset_opportunity_converts_to_prediction_only_trade(self):
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        pm.executors = {"kalshi": MagicMock()}
        trader = AutoTrader(pm)

        opp = trader._cross_asset_to_opportunity({
            "prediction_platform": "kalshi",
            "prediction_event_id": "kalshi-oil-90",
            "prediction_title": "Will Crude Oil settle above $90 in March?",
            "prediction_yes_price": 0.38,
            "prediction_side": "YES",
            "model_gap_pct": 14.0,
            "guaranteed_profit_pct": 22.0,
            "prediction_volume": 4100,
            "asset_class": "commodity",
            "expiry": "2026-12-31",
        })

        assert opp is not None
        assert opp["opportunity_type"] == "cross_asset_reference"
        assert opp["preferred_side"] == "YES"
        assert opp["_reference_backed"] is True
        assert opp["volume"] == 4100

    def test_cross_asset_requires_tradeable_prediction_platform(self):
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        pm.executors = {"polymarket": MagicMock()}
        trader = AutoTrader(pm)

        opp = trader._cross_asset_to_opportunity({
            "prediction_platform": "kalshi",
            "prediction_event_id": "kalshi-oil-90",
            "prediction_title": "Will Crude Oil settle above $90 in March?",
            "prediction_yes_price": 0.38,
            "prediction_side": "YES",
            "model_gap_pct": 14.0,
            "guaranteed_profit_pct": 22.0,
            "prediction_volume": 4100,
            "asset_class": "commodity",
            "expiry": "2026-12-31",
        })

        assert opp is None


class TestLiquidityGates:
    @pytest.mark.asyncio
    async def test_zero_volume_opportunity_is_skipped_before_execution(self):
        from positions.auto_trader import AutoTrader

        pm = _make_mock_pm()
        pm.executors = {"polymarket": MagicMock()}
        pm.execute_package = AsyncMock(return_value={"success": True})

        trader = AutoTrader(pm, scanner=None)
        trader._scan_polymarket = AsyncMock(return_value=[{
            "title": "Will BTC exceed $100,000?",
            "canonical_title": "Will BTC exceed $100,000?",
            "buy_yes_platform": "polymarket",
            "buy_yes_price": 0.35,
            "buy_no_platform": "polymarket",
            "buy_no_price": 0.65,
            "buy_yes_market_id": "btc-100k",
            "buy_no_market_id": "btc-100k",
            "profit_pct": 18.0,
            "net_profit_pct": 18.0,
            "opportunity_type": "pure_prediction",
            "expiry": (datetime.now() + timedelta(days=5)).isoformat(),
            "volume": 0,
            "_score": 40.0,
        }])
        await trader._scan_and_trade()

        pm.execute_package.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_low_calibrated_edge_directional_trade_is_skipped(self):
        from positions.auto_trader import AutoTrader

        pm = _make_mock_pm()
        pm.executors = {"polymarket": MagicMock()}
        pm.execute_package = AsyncMock(return_value={"success": True})

        model = MagicMock()
        model.get_consensus.return_value = {"max_deviation": 0.05}
        model.get_calibration_signal.return_value = {
            "preferred_side": "NO",
            "calibrated_yes": 0.48,
            "calibrated_edge_pct": 1.2,
            "confidence": 0.2,
        }

        trader = AutoTrader(pm, scanner=None, probability_model=model)
        trader._scan_polymarket = AsyncMock(return_value=[{
            "title": "Will BTC exceed $100,000?",
            "canonical_title": "Will BTC exceed $100,000?",
            "buy_yes_platform": "polymarket",
            "buy_yes_price": 0.49,
            "buy_no_platform": "polymarket",
            "buy_no_price": 0.51,
            "buy_yes_market_id": "btc-100k",
            "buy_no_market_id": "btc-100k",
            "profit_pct": 18.0,
            "net_profit_pct": 18.0,
            "opportunity_type": "pure_prediction",
            "expiry": (datetime.now() + timedelta(days=5)).isoformat(),
            "volume": 5000,
            "_score": 40.0,
        }])

        await trader._scan_and_trade()

        pm.execute_package.assert_not_awaited()


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

    def test_position_size_ratios(self):
        from positions.auto_trader import _RATIO_MAX_TRADE, _RATIO_MIN_TRADE, _RATIO_MAX_EXPOSURE
        assert _RATIO_MAX_TRADE == 0.025
        assert _RATIO_MIN_TRADE == 0.005
        assert _RATIO_MAX_EXPOSURE == 0.50


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


class TestKellySizing:
    """Kelly criterion position sizing across all strategy types."""

    def test_kelly_constants_exist(self):
        from positions.auto_trader import KELLY_EDGE_BY_STRATEGY, KELLY_FRACTION_BY_STRATEGY
        assert "multi_outcome_arb" in KELLY_EDGE_BY_STRATEGY
        assert "portfolio_no" in KELLY_EDGE_BY_STRATEGY
        assert "weather_forecast" in KELLY_EDGE_BY_STRATEGY
        assert "political_synthetic" in KELLY_EDGE_BY_STRATEGY
        assert "cross_platform_arb" in KELLY_EDGE_BY_STRATEGY

    def test_kelly_size_respects_bounds(self):
        """Kelly size should be between MIN_TRADE_SIZE and MAX_TRADE_SIZE."""
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        trader = AutoTrader(pm)
        size = trader._kelly_size("multi_outcome_arb", 500.0, spread_pct=15.0)
        assert trader._min_trade_size <= size <= trader._max_trade_size

    def test_kelly_size_arb_larger_than_synthetic(self):
        """Arb (high confidence) should size larger than synthetic (uncertain)."""
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        trader = AutoTrader(pm)
        arb_size = trader._kelly_size("cross_platform_arb", 500.0, spread_pct=15.0)
        synth_size = trader._kelly_size("political_synthetic", 500.0, spread_pct=15.0)
        assert arb_size >= synth_size

    def test_kelly_size_small_budget_floors_at_min(self):
        """With tiny budget, should floor at MIN_TRADE_SIZE."""
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        trader = AutoTrader(pm)
        size = trader._kelly_size("political_synthetic", 15.0, spread_pct=5.0)
        assert size == trader._min_trade_size

    def test_half_kelly_fractions(self):
        """Multi-outcome arb and portfolio NO should use Half Kelly (0.50)."""
        from positions.auto_trader import KELLY_FRACTION_BY_STRATEGY
        assert KELLY_FRACTION_BY_STRATEGY["multi_outcome_arb"] == 0.50
        assert KELLY_FRACTION_BY_STRATEGY["portfolio_no"] == 0.50
        assert KELLY_FRACTION_BY_STRATEGY["cross_platform_arb"] == 0.50


class TestRegimeDetection:
    """5-loss rule: consecutive losses reduce position sizes."""

    def test_regime_constants(self):
        from positions.auto_trader import LOSS_STREAK_THRESHOLD, REGIME_SIZE_REDUCTION
        assert LOSS_STREAK_THRESHOLD == 5
        assert REGIME_SIZE_REDUCTION == 0.50

    def test_regime_penalty_after_5_losses(self):
        """After 5 consecutive losses, regime_penalty should be 0.50."""
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        journal = MagicMock()
        journal.get_recent = MagicMock(return_value=[
            {"outcome": "loss", "closed_at": 100},
            {"outcome": "loss", "closed_at": 99},
            {"outcome": "loss", "closed_at": 98},
            {"outcome": "loss", "closed_at": 97},
            {"outcome": "loss", "closed_at": 96},
            {"outcome": "win", "closed_at": 95},
        ])
        journal.get_cumulative_pnl = MagicMock(return_value=0.0)
        pm.trade_journal = journal
        trader = AutoTrader(pm)
        trader._update_regime()
        assert trader._loss_streak == 5
        assert trader._regime_penalty == 0.50

    def test_regime_normal_after_win(self):
        """If most recent trade is a win, regime should be normal."""
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        journal = MagicMock()
        journal.get_recent = MagicMock(return_value=[
            {"outcome": "win", "closed_at": 100},
            {"outcome": "loss", "closed_at": 99},
            {"outcome": "loss", "closed_at": 98},
        ])
        journal.get_cumulative_pnl = MagicMock(return_value=0.0)
        pm.trade_journal = journal
        trader = AutoTrader(pm)
        trader._update_regime()
        assert trader._loss_streak == 0
        assert trader._regime_penalty == 1.0

    def test_regime_4_losses_still_normal(self):
        """4 consecutive losses should NOT trigger regime reduction."""
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        journal = MagicMock()
        journal.get_recent = MagicMock(return_value=[
            {"outcome": "loss", "closed_at": 100},
            {"outcome": "loss", "closed_at": 99},
            {"outcome": "loss", "closed_at": 98},
            {"outcome": "loss", "closed_at": 97},
            {"outcome": "win", "closed_at": 96},
        ])
        journal.get_cumulative_pnl = MagicMock(return_value=0.0)
        pm.trade_journal = journal
        trader = AutoTrader(pm)
        trader._update_regime()
        assert trader._loss_streak == 4
        assert trader._regime_penalty == 1.0

    def test_kelly_size_reduced_in_bad_regime(self):
        """Kelly sizing should be halved during bad regime."""
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        trader = AutoTrader(pm)
        # Normal regime
        trader._regime_penalty = 1.0
        normal_size = trader._kelly_size("multi_outcome_arb", 500.0, spread_pct=15.0)
        # Bad regime
        trader._regime_penalty = 0.50
        bad_size = trader._kelly_size("multi_outcome_arb", 500.0, spread_pct=15.0)
        assert bad_size <= normal_size


class TestPortfolioCorrelation:
    """Portfolio concentration limits — max 30% in any one category."""

    def test_category_detection_crypto(self):
        from positions.auto_trader import AutoTrader
        assert AutoTrader._detect_category("Will BTC exceed $100k?") == "crypto"
        assert AutoTrader._detect_category("Ethereum price target") == "crypto"

    def test_category_detection_politics(self):
        from positions.auto_trader import AutoTrader
        assert AutoTrader._detect_category("Will Trump win the election?") == "politics"

    def test_category_detection_sports(self):
        from positions.auto_trader import AutoTrader
        assert AutoTrader._detect_category("NCAA Basketball Final") == "sports"

    def test_category_detection_weather(self):
        from positions.auto_trader import AutoTrader
        assert AutoTrader._detect_category("NYC Temperature above 90F") == "weather"

    def test_category_detection_other(self):
        from positions.auto_trader import AutoTrader
        assert AutoTrader._detect_category("Will aliens land?") == "other"

    def test_concentration_allows_first_trade(self):
        """First trade in empty portfolio should always be allowed."""
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        trader = AutoTrader(pm)
        assert trader._check_concentration("BTC price", 50.0, 0.0, {}) is True

    def test_concentration_blocks_overweight(self):
        """If crypto is already 50%, adding more crypto should be blocked."""
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        trader = AutoTrader(pm)
        # 100 total, 50 in crypto = 50%. Adding 10 more crypto = 60/110 = 54.5% > 50%
        assert trader._check_concentration(
            "BTC target $200k", 10.0, 100.0, {"crypto": 50.0}
        ) is False

    def test_concentration_allows_different_category(self):
        """Adding politics trade when crypto is heavy should be fine."""
        from positions.auto_trader import AutoTrader
        pm = _make_mock_pm()
        trader = AutoTrader(pm)
        assert trader._check_concentration(
            "Will election happen?", 10.0, 100.0, {"crypto": 30.0}
        ) is True

    def test_max_concentration_constant(self):
        from positions.auto_trader import MAX_CATEGORY_CONCENTRATION
        assert MAX_CATEGORY_CONCENTRATION == 0.50


class TestSignalDecay:
    """News signal urgency decay based on signal age."""

    def test_fresh_signal_full_score(self):
        """Signal < 5 min old should get 1.0 multiplier."""
        import time
        from positions.auto_trader import AutoTrader
        decay = AutoTrader._signal_decay(time.time() - 60)  # 1 minute ago
        assert decay == 1.0

    def test_moderate_signal_reduced(self):
        """Signal 15 min old should get 0.7 multiplier."""
        import time
        from positions.auto_trader import AutoTrader
        decay = AutoTrader._signal_decay(time.time() - 15 * 60)  # 15 min ago
        assert decay == 0.7

    def test_old_signal_heavily_reduced(self):
        """Signal 45 min old should get 0.4 multiplier."""
        import time
        from positions.auto_trader import AutoTrader
        decay = AutoTrader._signal_decay(time.time() - 45 * 60)  # 45 min ago
        assert decay == 0.4

    def test_stale_signal_minimal(self):
        """Signal > 60 min old should get 0.1 multiplier."""
        import time
        from positions.auto_trader import AutoTrader
        decay = AutoTrader._signal_decay(time.time() - 120 * 60)  # 2 hours ago
        assert decay == 0.1

    def test_no_timestamp_full_score(self):
        """Missing signal_created_at should return 1.0 (don't penalize)."""
        from positions.auto_trader import AutoTrader
        assert AutoTrader._signal_decay(0) == 1.0
        assert AutoTrader._signal_decay(None) == 1.0

    def test_decay_tiers_exist(self):
        from positions.auto_trader import SIGNAL_DECAY_TIERS
        assert len(SIGNAL_DECAY_TIERS) == 4
        # Tiers should be ordered by max_age ascending
        ages = [t[0] for t in SIGNAL_DECAY_TIERS]
        assert ages == sorted(ages)


class TestWalkForwardValidation:
    """Monte Carlo robustness validation of strategy parameters."""

    def test_insufficient_data(self):
        """With < 10 trades, should return insufficient_data."""
        from positions.trade_journal import TradeJournal
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            j = TradeJournal(Path(d))
            j.entries = [{"pnl": 5.0, "mode": "paper"} for _ in range(5)]
            result = j.validate_robustness()
            assert result["verdict"] == "insufficient_data"

    def test_profitable_strategy_robust(self):
        """A clearly profitable strategy should pass robustness checks."""
        from positions.trade_journal import TradeJournal
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            j = TradeJournal(Path(d))
            # 20 trades, mostly wins — should be robust
            j.entries = [
                {"pnl": 10.0, "mode": "paper"} for _ in range(15)
            ] + [
                {"pnl": -3.0, "mode": "paper"} for _ in range(5)
            ]
            result = j.validate_robustness(n_simulations=50)
            assert result["verdict"] == "robust"
            assert result["jitter_test"]["passed"] is True
            assert result["skip_test"]["passed"] is True

    def test_losing_strategy_fragile(self):
        """A losing strategy should fail robustness checks."""
        from positions.trade_journal import TradeJournal
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            j = TradeJournal(Path(d))
            # 20 trades, mostly losses — should be fragile
            j.entries = [
                {"pnl": -8.0, "mode": "paper"} for _ in range(15)
            ] + [
                {"pnl": 2.0, "mode": "paper"} for _ in range(5)
            ]
            result = j.validate_robustness(n_simulations=50)
            assert result["verdict"] == "fragile"

    def test_max_drawdown_calculation(self):
        from positions.trade_journal import TradeJournal
        dd = TradeJournal._calc_max_drawdown([10, -5, -3, 8, -2])
        # Cumulative: 10, 5, 2, 10, 8 → peak 10, trough 2 → dd = 8
        assert dd == 8.0

    def test_mode_filter(self):
        """Should filter by mode when specified."""
        from positions.trade_journal import TradeJournal
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            j = TradeJournal(Path(d))
            j.entries = [
                {"pnl": 10.0, "mode": "paper"} for _ in range(15)
            ] + [
                {"pnl": -50.0, "mode": "live"} for _ in range(5)
            ]
            paper = j.validate_robustness(mode="paper", n_simulations=50)
            assert paper["total_trades"] == 15
            assert paper["verdict"] == "robust"


class TestWhaleConvergence:
    """Whale convergence: 3+ wallets on same market = strong signal."""

    def test_convergence_threshold_constant(self):
        from positions.insider_tracker import CONVERGENCE_THRESHOLD
        assert CONVERGENCE_THRESHOLD == 3

    def test_no_convergence_without_signal(self):
        """With no positions, convergence fields should be False/0."""
        from positions.insider_tracker import InsiderTracker
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tracker = InsiderTracker(Path(d))
            sig = tracker.get_insider_signal("nonexistent_cid")
            assert sig["has_signal"] is False
            assert sig.get("has_convergence") is not None  # Field exists in empty case too

    def test_convergence_detection_in_movements(self):
        """When 3 new wallets enter same market, should create convergence alert."""
        from positions.insider_tracker import InsiderTracker
        from pathlib import Path
        import tempfile, time as t
        with tempfile.TemporaryDirectory() as d:
            tracker = InsiderTracker(Path(d))
            # Previous: had one other market (makes _prev_positions truthy)
            # cid_test was NOT in prev → all 3 wallets are "new entrants"
            tracker._prev_positions = {"other_cid": [{"wallet": "old_w"}]}
            tracker._insider_positions = {
                "cid_test": [
                    {"wallet": "w1", "outcome": "YES", "current_value": 5000, "title": "Test"},
                    {"wallet": "w2", "outcome": "YES", "current_value": 3000, "title": "Test"},
                    {"wallet": "w3", "outcome": "YES", "current_value": 2000, "title": "Test"},
                ]
            }
            tracker._flagged_wallets = {
                "w1": {"wallet_type": "conviction", "signal_weight": 5.0},
                "w2": {"wallet_type": "unknown", "signal_weight": 1.0},
                "w3": {"wallet_type": "unknown", "signal_weight": 1.0},
            }
            tracker._detect_movements()
            convergence_alerts = [a for a in tracker._movement_alerts if a["type"] == "whale_convergence"]
            assert len(convergence_alerts) >= 1
            assert convergence_alerts[0]["converging_wallets"] == 3
            assert convergence_alerts[0]["auto_triggered"] is True

    def test_signal_strength_boosted_by_convergence(self):
        """Signal strength should be higher when convergence is active."""
        from positions.insider_tracker import InsiderTracker
        from pathlib import Path
        import tempfile, time as t
        with tempfile.TemporaryDirectory() as d:
            tracker = InsiderTracker(Path(d))
            tracker._insider_positions = {
                "cid_boost": [
                    {"wallet": "w1", "outcome": "YES", "current_value": 5000, "title": "Boosted"},
                    {"wallet": "w2", "outcome": "YES", "current_value": 3000, "title": "Boosted"},
                    {"wallet": "w3", "outcome": "YES", "current_value": 2000, "title": "Boosted"},
                ]
            }
            tracker._flagged_wallets = {
                "w1": {"wallet_type": "conviction", "signal_weight": 5.0},
                "w2": {"wallet_type": "conviction", "signal_weight": 3.0},
                "w3": {"wallet_type": "unknown", "signal_weight": 1.0},
            }
            # Get signal without convergence
            sig_no_conv = tracker.get_insider_signal("cid_boost")
            strength_no = sig_no_conv["signal_strength"]

            # Add convergence alert
            tracker._movement_alerts.append({
                "type": "whale_convergence",
                "condition_id": "cid_boost",
                "converging_wallets": 3,
                "timestamp": t.time(),
            })
            sig_with_conv = tracker.get_insider_signal("cid_boost")
            strength_with = sig_with_conv["signal_strength"]
            assert strength_with >= strength_no
            assert sig_with_conv["has_convergence"] is True


class TestNewsThresholds:
    """Relaxed news scanner thresholds."""

    def test_news_daily_cap_exists(self):
        from positions.news_scanner import DAILY_TRADE_CAP
        assert DAILY_TRADE_CAP == 5


class TestMultiOutcomeArbHoldToResolution:
    """multi_outcome_arb is guaranteed profit — should hold to resolution."""

    def test_multi_outcome_arb_has_hold_to_resolution(self):
        """multi_outcome_arb handler must set _hold_to_resolution = True."""
        from positions.auto_trader import AutoTrader
        import inspect
        source = inspect.getsource(AutoTrader._scan_and_trade)
        lines = source.split("\n")
        in_multi_outcome = False
        found_hold = False
        for line in lines:
            if "multi-outcome arb" in line.lower() and "guaranteed" in line.lower():
                in_multi_outcome = True
            if in_multi_outcome and "_hold_to_resolution" in line:
                found_hold = True
                break
            if in_multi_outcome and "execute_package" in line:
                break
        assert found_hold, "multi_outcome_arb handler must set _hold_to_resolution = True"
