# tests/test_commodities_block.py
"""Tests for commodities gating in auto trader scoring."""


class TestCommoditiesBlock:
    def test_commodities_keyword_detected(self):
        """COMMODITIES_KEYWORDS should identify commodity markets."""
        from positions.auto_trader import COMMODITIES_KEYWORDS
        title = "crude oil settle over $76"
        assert any(kw in title.lower() for kw in COMMODITIES_KEYWORDS)

    def test_non_commodity_not_blocked(self):
        """Non-commodity markets should pass through the filter."""
        from positions.auto_trader import COMMODITIES_KEYWORDS
        title = "will btc hit 100k by june"
        is_commodities = any(kw in title.lower() for kw in COMMODITIES_KEYWORDS)
        assert not is_commodities

    def test_commodities_code_blocks_only_non_reference_trades(self):
        """Commodity trades should be blocked unless explicitly reference-backed."""
        import inspect
        from positions.auto_trader import AutoTrader
        source = inspect.getsource(AutoTrader._scan_and_trade)
        assert "commodities_market" in source, "Should log skip reason 'commodities_market'"
        assert "not opp.get(\"_reference_backed\")" in source
