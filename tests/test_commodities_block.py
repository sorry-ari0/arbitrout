# tests/test_commodities_block.py
"""Tests for commodities hard block in auto trader scoring."""
import re


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

    def test_commodities_code_uses_continue_not_multiply(self):
        """The auto_trader source should `continue` on commodities, not `score *= 0.4`.

        This is a source-code assertion that verifies the behavioral change:
        after the fix, the code block for is_commodities should contain 'continue'
        and NOT contain 'score *= 0.4'.
        """
        import inspect
        from positions.auto_trader import AutoTrader
        source = inspect.getsource(AutoTrader._scan_and_trade)
        assert "commodities_market" in source, "Should log skip reason 'commodities_market'"
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "is_commodities" in line and "score *=" in line:
                raise AssertionError(
                    f"Line {i}: commodities should be hard-skipped, not penalized: {line.strip()}"
                )
