"""Tests for AI advisor."""
import pytest
from positions.ai_advisor import AIAdvisor

@pytest.fixture
def advisor(): return AIAdvisor(max_calls_per_min=10)

class TestPromptBuilding:
    def test_build_context(self, advisor):
        pkg = {"id":"p1","name":"T","strategy_type":"cross_platform_arb","legs":[
            {"leg_id":"l1","platform":"poly","type":"prediction_yes","asset_label":"BTC>100k",
             "entry_price":0.60,"current_price":0.70,"quantity":16.67,"cost":10,"current_value":11.67,
             "expiry":"2026-12-31","status":"open","leg_status":"ITM"}],
            "exit_rules":[{"rule_id":"r1","type":"trailing_stop","params":{"bound_min":5,"bound_max":25,"current":12},"active":True}],
            "unrealized_pnl":1.67,"unrealized_pnl_pct":16.7}
        ctx = advisor._build_context(pkg)
        assert "BTC>100k" in ctx

class TestParseResponse:
    def test_approve(self, advisor):
        v = advisor._parse_response("trailing_stop: APPROVE\n")
        assert v["trailing_stop"]["action"] == "APPROVE"
    def test_modify(self, advisor):
        v = advisor._parse_response("trailing_stop: MODIFY 8\n")
        assert v["trailing_stop"]["action"] == "MODIFY" and v["trailing_stop"]["value"] == 8.0
    def test_reject(self, advisor):
        v = advisor._parse_response("trailing_stop: REJECT too risky\n")
        assert v["trailing_stop"]["action"] == "REJECT"
