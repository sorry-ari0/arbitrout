# tests/test_kyle_ab.py
"""Tests for Kyle's lambda A/B test toggle."""
from positions.kyle_lambda import KyleLambdaEstimator


class TestKyleABToggle:
    def test_ab_disabled_returns_neutral_multiplier(self):
        """When A/B test disables kyle, get_lambda_signal should return multiplier=1.0."""
        estimator = KyleLambdaEstimator()
        estimator.ab_test_enabled = False
        result = estimator.get_lambda_signal("test_asset", "YES")
        assert result["multiplier"] == 1.0

    def test_ab_enabled_returns_dict(self):
        """When A/B test enables kyle, should return dict with multiplier key."""
        estimator = KyleLambdaEstimator()
        estimator.ab_test_enabled = True
        result = estimator.get_lambda_signal("test_asset", "YES")
        assert isinstance(result, dict)
        assert "multiplier" in result
        assert isinstance(result["multiplier"], float)

    def test_ab_default_is_enabled(self):
        """Default A/B state should be enabled."""
        estimator = KyleLambdaEstimator()
        assert estimator.ab_test_enabled is True

    def test_ab_disabled_signal_includes_ab_flag(self):
        """When disabled, the returned signal should indicate A/B status."""
        estimator = KyleLambdaEstimator()
        estimator.ab_test_enabled = False
        result = estimator.get_lambda_signal("test_asset", "YES")
        assert result.get("ab_disabled") is True
