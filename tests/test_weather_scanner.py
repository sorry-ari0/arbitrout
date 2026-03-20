"""Tests for Weather Scanner — NWS forecast edge on Kalshi weather markets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from positions.weather_scanner import WeatherScanner, CITY_COORDINATES


class TestCityExtraction:
    def test_extracts_known_city(self):
        scanner = WeatherScanner()
        assert scanner._extract_city("will new york high temp be above 75") == "new york"

    def test_extracts_alias(self):
        scanner = WeatherScanner()
        assert scanner._extract_city("will nyc temperature exceed 80") == "new york"
        assert scanner._extract_city("will chi temp be above 60") == "chicago"

    def test_returns_none_for_unknown(self):
        scanner = WeatherScanner()
        assert scanner._extract_city("will london temp be above 70") is None


class TestTempBracketParsing:
    def test_above_pattern(self):
        scanner = WeatherScanner()
        result = scanner._parse_temp_bracket("above 75")
        assert result == {"type": "above", "threshold": 75}

    def test_over_pattern(self):
        scanner = WeatherScanner()
        result = scanner._parse_temp_bracket("over 60")
        assert result == {"type": "above", "threshold": 60}

    def test_below_pattern(self):
        scanner = WeatherScanner()
        result = scanner._parse_temp_bracket("below 40")
        assert result == {"type": "below", "threshold": 40}

    def test_between_pattern(self):
        scanner = WeatherScanner()
        result = scanner._parse_temp_bracket("between 50 and 60")
        assert result == {"type": "between", "low": 50, "high": 60}

    def test_range_dash_pattern(self):
        scanner = WeatherScanner()
        result = scanner._parse_temp_bracket("50-60")
        assert result == {"type": "between", "low": 50, "high": 60}


class TestProbabilityEstimation:
    def test_above_when_forecast_much_higher(self):
        scanner = WeatherScanner()
        # Forecast 80, threshold 60 → very likely above
        prob = scanner._temp_above_probability(80, 60)
        assert prob > 0.95

    def test_above_when_forecast_much_lower(self):
        scanner = WeatherScanner()
        # Forecast 50, threshold 70 → very unlikely above
        prob = scanner._temp_above_probability(50, 70)
        assert prob < 0.05

    def test_above_when_forecast_at_threshold(self):
        scanner = WeatherScanner()
        # Forecast exactly at threshold → ~50%
        prob = scanner._temp_above_probability(65, 65)
        assert 0.45 < prob < 0.55

    def test_between_probability(self):
        scanner = WeatherScanner()
        # Forecast 75, range 70-80 → high probability
        prob = scanner._temp_between_probability(75, 70, 80)
        assert prob > 0.5

    def test_between_probability_outside(self):
        scanner = WeatherScanner()
        # Forecast 90, range 60-70 → very low probability
        prob = scanner._temp_between_probability(90, 60, 70)
        assert prob < 0.05


class TestOpportunityEvaluation:
    def test_generates_opportunity_with_edge(self):
        scanner = WeatherScanner()
        markets = [{
            "_category": "temperature",
            "title": "Will NYC high temp be above 65?",
            "_event_title": "Will NYC high temp be above 65?",
            "yes_bid": 0.30,  # Market says 30% chance
            "ticker": "KXHIGHTEMP-NYC",
            "event_ticker": "KXHIGHTEMP-NYC",
            "volume": 5000,
        }]
        # NWS says 80°F — way above 65, should be ~99% chance → big YES edge
        forecast = {"high_temp": 80, "precip_chance": 0}

        opp = scanner._evaluate_opportunity("new york", "2026-03-25", markets, forecast)
        assert opp is not None
        assert opp["opportunity_type"] == "weather_forecast"
        assert opp["side"] == "YES"  # Forecast says above, market underpricing YES
        assert opp["edge"] > 0.10

    def test_no_opportunity_when_no_edge(self):
        scanner = WeatherScanner()
        markets = [{
            "_category": "temperature",
            "title": "Will NYC high temp be above 75?",
            "_event_title": "Will NYC high temp be above 75?",
            "yes_bid": 0.50,  # Market says 50%
            "ticker": "KXHIGHTEMP-NYC",
            "event_ticker": "KXHIGHTEMP-NYC",
            "volume": 5000,
        }]
        # NWS says 75 — right at threshold, ~50% → no edge
        forecast = {"high_temp": 75, "precip_chance": 0}

        opp = scanner._evaluate_opportunity("new york", "2026-03-25", markets, forecast)
        assert opp is None  # No significant edge


class TestStrategyType:
    def test_weather_forecast_in_strategy_types(self):
        from positions.position_manager import STRATEGY_TYPES
        assert "weather_forecast" in STRATEGY_TYPES

    def test_create_weather_package(self):
        from positions.position_manager import create_package
        pkg = create_package("Weather Test", "weather_forecast")
        assert pkg["strategy_type"] == "weather_forecast"


class TestCaching:
    @pytest.mark.asyncio
    async def test_cache_returns_previous_results(self):
        scanner = WeatherScanner()
        import time
        scanner._cache = [{"test": True}]
        scanner._cache_time = time.time()
        result = await scanner.scan()
        assert result == [{"test": True}]
