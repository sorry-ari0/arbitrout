"""Tests for per-category Polymarket fee model."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from execution.paper_executor import get_taker_fee_rate


class TestGetTakerFeeRate:
    """Test the Polymarket fee curve: rate = feeRate * (price * (1-price))^exponent"""

    def test_politics_zero_at_any_price(self):
        """Politics/entertainment markets have 0% taker fee on Polymarket."""
        assert get_taker_fee_rate("politics", 0.50) == 0.0
        assert get_taker_fee_rate("politics", 0.10) == 0.0
        assert get_taker_fee_rate("other", 0.50) == 0.0

    def test_crypto_peak_at_half(self):
        """Crypto fee peaks at p=0.50: 0.25 * (0.25)^2 = 0.015625."""
        rate = get_taker_fee_rate("crypto", 0.50)
        assert abs(rate - 0.015625) < 1e-6

    def test_crypto_low_at_extreme(self):
        """Crypto fee near-zero at p=0.10: 0.25 * (0.09)^2 = 0.002025."""
        rate = get_taker_fee_rate("crypto", 0.10)
        assert abs(rate - 0.002025) < 1e-6

    def test_sports_peak_at_half(self):
        """Sports fee at p=0.50: 0.0175 * (0.25)^1 = 0.004375."""
        rate = get_taker_fee_rate("sports", 0.50)
        assert abs(rate - 0.004375) < 1e-6

    def test_sports_low_at_extreme(self):
        """Sports fee at p=0.10: 0.0175 * 0.09 = 0.001575."""
        rate = get_taker_fee_rate("sports", 0.10)
        assert abs(rate - 0.001575) < 1e-6

    def test_boundary_prices_zero(self):
        """Fee is 0 at price=0 and price=1."""
        assert get_taker_fee_rate("crypto", 0.0) == 0.0
        assert get_taker_fee_rate("crypto", 1.0) == 0.0

    def test_finance_uses_default(self):
        """Unknown Polymarket categories default to 0%."""
        assert get_taker_fee_rate("finance", 0.50) == 0.0
        assert get_taker_fee_rate("weather", 0.50) == 0.0


import asyncio
from unittest.mock import MagicMock, AsyncMock
from execution.paper_executor import PaperExecutor
from execution.base_executor import ExecutionResult


class TestSellWithCategory:
    """Test that sell() uses category-aware fees when category is provided."""

    def _make_executor(self):
        """Create a paper executor wrapping a mock Polymarket executor."""
        real = MagicMock()
        real.__class__.__name__ = "PolymarketExecutor"
        real.get_current_price = AsyncMock(return_value=0.50)
        ex = PaperExecutor(real, starting_balance=1000.0)
        # Seed a position
        ex.positions["tok1:YES"] = {"quantity": 10.0, "avg_entry_price": 0.40}
        return ex

    def test_sell_without_category_uses_flat_rate(self):
        """sell() without category uses self.sell_fee_rate (flat maker 0%)."""
        ex = self._make_executor()
        result = asyncio.run(
            ex.sell("tok1:YES", 10.0))
        assert result.success
        # Polymarket maker sell_fee_rate = 0.0
        assert result.fees == 0.0

    def test_sell_with_crypto_category_uses_curve(self):
        """sell() with category='crypto' uses fee curve instead of flat rate."""
        ex = self._make_executor()
        result = asyncio.run(
            ex.sell("tok1:YES", 10.0, category="crypto"))
        assert result.success
        # At price=0.50, crypto rate = 0.015625
        # proceeds = 10 * 0.50 = 5.0, fee = 5.0 * 0.015625 = 0.0781
        assert abs(result.fees - 0.0781) < 0.001

    def test_sell_with_politics_category_zero_fee(self):
        """sell() with category='politics' has 0% taker fee."""
        ex = self._make_executor()
        result = asyncio.run(
            ex.sell("tok1:YES", 10.0, category="politics"))
        assert result.success
        assert result.fees == 0.0
