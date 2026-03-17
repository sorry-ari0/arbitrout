import pytest
from arbitrage import calculate_profit, calculate_trade_ratio

def same_platform_pairs(yes_event, no_event):
    return yes_event["platform"] == no_event["platform"]

def test_profit_calculation():
    yes_price = 0.40
    no_price = 0.55
    profit = calculate_profit(yes_price, no_price)
    assert profit == 0.05  # Changed to correct profit calculation

def test_same_platform_exclusion():
    yes_event = {"platform": "A"}
    no_event = {"platform": "A"}
    assert not same_platform_pairs(yes_event, no_event)

def test_trade_ratio_calculation():
    yes_price = 0.40
    no_price = 0.55
    trade_ratio = calculate_trade_ratio(yes_price, no_price)
    assert trade_ratio == (0.40 / (0.40 + 0.55)) * 100
