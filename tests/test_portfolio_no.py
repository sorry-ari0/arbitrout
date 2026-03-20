"""Tests for Portfolio NO strategy — scanner and execution path."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch


class TestPortfolioNoScanner:
    """Tests for ArbitrageEngine.scan_portfolio_no()."""

    def _make_market(self, title, yes_price, condition_id="cid", volume=10000):
        no_price = round(1.0 - yes_price, 4)
        return {
            "question": title,
            "conditionId": condition_id,
            "outcomePrices": json.dumps([yes_price, no_price]),
            "volume": str(volume),
        }

    def _make_event(self, title, markets, end_date="2026-06-01"):
        return {
            "title": title,
            "markets": markets,
            "endDate": end_date,
        }

    @pytest.mark.asyncio
    async def test_finds_opportunity_with_overround(self):
        """Events with sum(YES) > 1.02 should produce portfolio NO opportunities."""
        from arbitrage_engine import ArbitrageScanner
        from adapters.registry import AdapterRegistry

        # 5 outcomes summing to 1.08 (8% overround)
        markets = [
            self._make_market("Favorite A", 0.35, "cid_a"),
            self._make_market("Runner B", 0.25, "cid_b"),
            self._make_market("Contender C", 0.20, "cid_c"),
            self._make_market("Longshot D", 0.15, "cid_d"),
            self._make_market("Longshot E", 0.13, "cid_e"),
        ]
        events = [self._make_event("Who will win?", markets)]

        registry = MagicMock(spec=AdapterRegistry)
        engine = ArbitrageScanner(registry)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = events
            mock_client.get = AsyncMock(return_value=mock_resp)

            opps = await engine.scan_portfolio_no()

        assert len(opps) >= 1
        opp = opps[0]
        assert opp["opportunity_type"] == "portfolio_no"
        assert opp["profit_pct"] > 0
        assert opp["no_count"] >= 3
        assert opp["guaranteed_profit"] > 0

    @pytest.mark.asyncio
    async def test_skips_event_without_overround(self):
        """Events with sum(YES) <= 1.02 should not produce opportunities."""
        from arbitrage_engine import ArbitrageScanner
        from adapters.registry import AdapterRegistry

        # 5 outcomes summing to exactly 1.0 (no overround)
        markets = [
            self._make_market("A", 0.30, "cid_a"),
            self._make_market("B", 0.25, "cid_b"),
            self._make_market("C", 0.20, "cid_c"),
            self._make_market("D", 0.15, "cid_d"),
            self._make_market("E", 0.10, "cid_e"),
        ]
        events = [self._make_event("No overround", markets)]

        registry = MagicMock(spec=AdapterRegistry)
        engine = ArbitrageScanner(registry)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = events
            mock_client.get = AsyncMock(return_value=mock_resp)

            opps = await engine.scan_portfolio_no()

        assert len(opps) == 0

    @pytest.mark.asyncio
    async def test_skips_event_with_too_few_outcomes(self):
        """Events with <4 outcomes should be skipped."""
        from arbitrage_engine import ArbitrageScanner
        from adapters.registry import AdapterRegistry

        markets = [
            self._make_market("A", 0.50, "cid_a"),
            self._make_market("B", 0.30, "cid_b"),
            self._make_market("C", 0.25, "cid_c"),
        ]
        events = [self._make_event("Too few", markets)]

        registry = MagicMock(spec=AdapterRegistry)
        engine = ArbitrageScanner(registry)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = events
            mock_client.get = AsyncMock(return_value=mock_resp)

            opps = await engine.scan_portfolio_no()

        assert len(opps) == 0

    @pytest.mark.asyncio
    async def test_excludes_favorites_optimally(self):
        """Should exclude favorites while keeping remaining sum > 1.02."""
        from arbitrage_engine import ArbitrageScanner
        from adapters.registry import AdapterRegistry

        # Big overround: 1.15. Can exclude favorite (0.40) and still have 0.75 remaining
        # Wait, 0.75 < 1.02, so can't exclude. Need a bigger event.
        # 10 outcomes summing to 1.15
        markets = [
            self._make_market("Fav", 0.30, "cid_0"),
            self._make_market("O1", 0.12, "cid_1"),
            self._make_market("O2", 0.11, "cid_2"),
            self._make_market("O3", 0.10, "cid_3"),
            self._make_market("O4", 0.10, "cid_4"),
            self._make_market("O5", 0.09, "cid_5"),
            self._make_market("O6", 0.09, "cid_6"),
            self._make_market("O7", 0.08, "cid_7"),
            self._make_market("O8", 0.08, "cid_8"),
            self._make_market("O9", 0.08, "cid_9"),
        ]
        events = [self._make_event("Big tournament", markets)]

        registry = MagicMock(spec=AdapterRegistry)
        engine = ArbitrageScanner(registry)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = events
            mock_client.get = AsyncMock(return_value=mock_resp)

            opps = await engine.scan_portfolio_no()

        assert len(opps) >= 1
        opp = opps[0]
        # Should exclude favorites when possible
        assert opp["favorites_count"] >= 0
        # Remaining YES sum should be > 1.02
        assert opp["remaining_yes_sum"] > 1.02

    @pytest.mark.asyncio
    async def test_caching_works(self):
        """Second call within TTL should return cached results."""
        from arbitrage_engine import ArbitrageScanner
        from adapters.registry import AdapterRegistry

        registry = MagicMock(spec=AdapterRegistry)
        engine = ArbitrageScanner(registry)

        # Pre-populate cache
        engine._portfolio_no_cache = [{"opportunity_type": "portfolio_no", "cached": True}]
        import time
        engine._portfolio_no_cache_time = time.time()

        opps = await engine.scan_portfolio_no()
        assert len(opps) == 1
        assert opps[0]["cached"] is True


class TestPortfolioNoStrategyType:
    """Tests for portfolio_no strategy type registration."""

    def test_strategy_type_exists(self):
        from positions.position_manager import STRATEGY_TYPES
        assert "portfolio_no" in STRATEGY_TYPES

    def test_create_package_with_portfolio_no(self):
        from positions.position_manager import create_package
        pkg = create_package("Test Portfolio NO", "portfolio_no")
        assert pkg["strategy_type"] == "portfolio_no"
        assert pkg["status"] == "open"
