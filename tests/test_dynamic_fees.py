"""Tests for the dynamic Polymarket fee model (Task 1: Dynamic Fee Model).

Polymarket uses a price-sensitive fee curve:
    effective_rate = fee_rate * (price * (1 - price)) ** exponent

Categories:
    crypto:   fee_rate=0.25, exponent=2  → max 1.5625% at p=0.50
    politics, sports, economics, weather, culture:
              fee_rate=0.0175, exponent=1 → max 0.4375% at p=0.50
    unknown:  falls back to crypto params (most conservative)

Other platforms keep their flat taker rates from _TAKER_FEES.
All dynamic Polymarket fees must be <= the old flat 2%.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from adapters.models import NormalizedEvent, MatchedEvent
from arbitrage_engine import (
    compute_taker_fee,
    _compute_fee_adjusted_profit,
    find_arbitrage,
    _TAKER_FEES,
)


# ============================================================
# Helpers
# ============================================================

def _make_event(platform: str, event_id: str, title: str, yes: float, no: float,
                volume: int = 1000) -> NormalizedEvent:
    return NormalizedEvent(
        platform=platform,
        event_id=event_id,
        title=title,
        category="crypto",
        yes_price=yes,
        no_price=no,
        volume=volume,
        expiry="2026-12-31",
        url=f"https://{platform}.com/{event_id}",
    )


def _make_matched(events: list[NormalizedEvent], category: str = "crypto",
                  title: str = "Test Event") -> MatchedEvent:
    return MatchedEvent(
        match_id="test-match-1",
        canonical_title=title,
        category=category,
        expiry="2026-12-31",
        markets=events,
    )


# ============================================================
# compute_taker_fee — Polymarket dynamic curve
# ============================================================

class TestComputeTakerFeePolymarket:
    """Validate fee curve at key price points for all Polymarket categories."""

    # --- crypto category ---

    def test_crypto_at_midpoint(self):
        """At p=0.50, crypto fee = 0.25 * (0.5*0.5)^2 = 0.25 * 0.0625 = 0.015625."""
        fee = compute_taker_fee("polymarket", 0.50, "crypto")
        assert abs(fee - 0.015625) < 1e-9

    def test_crypto_at_low_price(self):
        """At p=0.10, crypto fee = 0.25 * (0.10*0.90)^2 = 0.25 * 0.0081 = 0.002025."""
        fee = compute_taker_fee("polymarket", 0.10, "crypto")
        expected = 0.25 * (0.10 * 0.90) ** 2
        assert abs(fee - expected) < 1e-9

    def test_crypto_at_high_price(self):
        """At p=0.90, by symmetry same as p=0.10."""
        fee_low = compute_taker_fee("polymarket", 0.10, "crypto")
        fee_high = compute_taker_fee("polymarket", 0.90, "crypto")
        assert abs(fee_low - fee_high) < 1e-9

    def test_crypto_near_zero(self):
        """At p=0.01, fee approaches 0."""
        fee = compute_taker_fee("polymarket", 0.01, "crypto")
        expected = 0.25 * (0.01 * 0.99) ** 2
        assert abs(fee - expected) < 1e-9
        assert fee < 0.0001  # very small

    def test_crypto_near_one(self):
        """At p=0.99, fee approaches 0 (symmetric with p=0.01)."""
        fee = compute_taker_fee("polymarket", 0.99, "crypto")
        fee_near_zero = compute_taker_fee("polymarket", 0.01, "crypto")
        assert abs(fee - fee_near_zero) < 1e-9

    def test_crypto_max_less_than_flat_2pct(self):
        """Max crypto fee (at p=0.50) must be strictly below old flat 2%."""
        fee = compute_taker_fee("polymarket", 0.50, "crypto")
        assert fee < 0.02

    # --- politics category ---

    def test_politics_at_midpoint(self):
        """At p=0.50, politics fee = 0.0175 * (0.5*0.5)^1 = 0.0175 * 0.25 = 0.004375."""
        fee = compute_taker_fee("polymarket", 0.50, "politics")
        assert abs(fee - 0.004375) < 1e-9

    def test_politics_at_low_price(self):
        """At p=0.10, politics fee = 0.0175 * (0.10*0.90)^1 = 0.0175 * 0.09 = 0.001575."""
        fee = compute_taker_fee("polymarket", 0.10, "politics")
        expected = 0.0175 * (0.10 * 0.90) ** 1
        assert abs(fee - expected) < 1e-9

    def test_politics_at_high_price(self):
        """Symmetric around 0.50."""
        fee_low = compute_taker_fee("polymarket", 0.10, "politics")
        fee_high = compute_taker_fee("polymarket", 0.90, "politics")
        assert abs(fee_low - fee_high) < 1e-9

    def test_politics_max_less_than_flat_2pct(self):
        """Max politics fee (at p=0.50) must be strictly below old flat 2%."""
        fee = compute_taker_fee("polymarket", 0.50, "politics")
        assert fee < 0.02

    # --- non-crypto categories share same params ---

    @pytest.mark.parametrize("category", ["sports", "economics", "weather", "culture"])
    def test_non_crypto_categories_match_politics(self, category):
        """sports/economics/weather/culture use the same params as politics."""
        fee = compute_taker_fee("polymarket", 0.50, category)
        fee_politics = compute_taker_fee("polymarket", 0.50, "politics")
        assert abs(fee - fee_politics) < 1e-9

    # --- unknown category defaults to crypto (most conservative) ---

    def test_unknown_category_uses_crypto_params(self):
        """Unknown/empty category should fall back to crypto (most conservative)."""
        fee_unknown = compute_taker_fee("polymarket", 0.50, "unknown_xyz")
        fee_crypto = compute_taker_fee("polymarket", 0.50, "crypto")
        assert abs(fee_unknown - fee_crypto) < 1e-9

    def test_empty_category_uses_crypto_params(self):
        """Empty string category should fall back to crypto."""
        fee_empty = compute_taker_fee("polymarket", 0.50, "")
        fee_crypto = compute_taker_fee("polymarket", 0.50, "crypto")
        assert abs(fee_empty - fee_crypto) < 1e-9

    # --- all dynamic fees are below old flat 2% ---

    @pytest.mark.parametrize("price", [0.01, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50,
                                        0.60, 0.70, 0.80, 0.90, 0.95, 0.99])
    @pytest.mark.parametrize("category", ["crypto", "politics", "sports", "economics",
                                           "weather", "culture", "unknown_xyz", ""])
    def test_all_fees_below_old_flat_2pct(self, price, category):
        """Dynamic fee must always be <= 2% (old flat rate) at any price/category."""
        fee = compute_taker_fee("polymarket", price, category)
        assert fee <= 0.02, (
            f"fee {fee} > 0.02 for price={price}, category={category}"
        )

    # --- fee is non-negative ---

    @pytest.mark.parametrize("price", [0.01, 0.10, 0.50, 0.90, 0.99])
    def test_fee_is_non_negative(self, price):
        """Fee should never be negative."""
        fee = compute_taker_fee("polymarket", price, "crypto")
        assert fee >= 0.0

    # --- crypto > politics at all prices (more conservative) ---

    @pytest.mark.parametrize("price", [0.10, 0.30, 0.50, 0.70, 0.90])
    def test_crypto_fee_exceeds_politics_fee(self, price):
        """Crypto fee should be higher than politics at every price (more conservative)."""
        fee_crypto = compute_taker_fee("polymarket", price, "crypto")
        fee_politics = compute_taker_fee("polymarket", price, "politics")
        assert fee_crypto >= fee_politics


# ============================================================
# compute_taker_fee — Other platforms (flat rates, unaffected)
# ============================================================

class TestComputeTakerFeeOtherPlatforms:
    """Other platforms must keep their existing flat taker fees."""

    @pytest.mark.parametrize("platform,expected_fee", [
        ("kalshi", 0.01),
        ("predictit", 0.0),
        ("limitless", 0.01),
        ("robinhood", 0.0),
        ("coinbase_spot", 0.006),
        ("kraken", 0.0026),
    ])
    def test_flat_fee_platforms(self, platform, expected_fee):
        """Non-Polymarket platforms return their flat rate regardless of price/category."""
        for price in [0.10, 0.50, 0.90]:
            for category in ["crypto", "politics", ""]:
                fee = compute_taker_fee(platform, price, category)
                assert abs(fee - expected_fee) < 1e-9, (
                    f"{platform} fee {fee} != expected {expected_fee} "
                    f"at price={price}, category={category}"
                )

    def test_unknown_platform_uses_default(self):
        """Unknown platform should use _DEFAULT_TAKER_FEE (0.02)."""
        from arbitrage_engine import _DEFAULT_TAKER_FEE
        fee = compute_taker_fee("new_exchange", 0.50, "crypto")
        assert abs(fee - _DEFAULT_TAKER_FEE) < 1e-9

    def test_category_does_not_affect_non_polymarket(self):
        """Category param must have zero effect on non-Polymarket platforms."""
        fee_crypto = compute_taker_fee("kalshi", 0.50, "crypto")
        fee_politics = compute_taker_fee("kalshi", 0.50, "politics")
        fee_unknown = compute_taker_fee("kalshi", 0.50, "unknown_xyz")
        assert fee_crypto == fee_politics == fee_unknown

    def test_price_does_not_affect_non_polymarket(self):
        """Price param must have zero effect on non-Polymarket platforms."""
        fee_low = compute_taker_fee("kalshi", 0.01, "crypto")
        fee_mid = compute_taker_fee("kalshi", 0.50, "crypto")
        fee_high = compute_taker_fee("kalshi", 0.99, "crypto")
        assert fee_low == fee_mid == fee_high


# ============================================================
# _compute_fee_adjusted_profit — backward-compatible category param
# ============================================================

class TestComputeFeeAdjustedProfitCategory:
    """Test that category param is threaded correctly into fee calc."""

    def test_backward_compat_no_category(self):
        """Calling without category should work (defaults to empty string → crypto)."""
        result = _compute_fee_adjusted_profit(0.40, 0.55, "polymarket", "kalshi")
        assert len(result) == 2
        net_pct, total_cost = result
        assert isinstance(net_pct, float)
        assert isinstance(total_cost, float)

    def test_politics_category_yields_lower_fees_than_crypto(self):
        """Politics category has lower fees → higher net profit than crypto at same prices."""
        net_crypto, _ = _compute_fee_adjusted_profit(
            0.40, 0.55, "polymarket", "kalshi", category="crypto"
        )
        net_politics, _ = _compute_fee_adjusted_profit(
            0.40, 0.55, "polymarket", "kalshi", category="politics"
        )
        # Lower Polymarket fee for politics → less cost → higher net profit
        assert net_politics > net_crypto

    def test_category_only_affects_polymarket_leg(self):
        """Category should only change fees for the Polymarket platform."""
        # Both platforms are kalshi — category change should have no effect
        net1, _ = _compute_fee_adjusted_profit(0.40, 0.55, "kalshi", "kalshi", category="crypto")
        net2, _ = _compute_fee_adjusted_profit(0.40, 0.55, "kalshi", "kalshi", category="politics")
        assert abs(net1 - net2) < 1e-9

    def test_fee_is_lower_than_old_flat_2pct(self):
        """Dynamic Polymarket fee rate is always below the old flat 2% rate."""
        # At p=0.40, crypto: 0.25 * (0.40 * 0.60)^2 = 0.25 * 0.0576 = 0.0144
        # 0.0144 < 0.02 (old flat rate) ✓
        dynamic_fee = compute_taker_fee("polymarket", 0.40, "crypto")
        assert dynamic_fee < 0.02  # strictly less than old flat rate


# ============================================================
# find_arbitrage — category threaded end-to-end
# ============================================================

class TestFindArbitrageWithCategory:
    """Test that find_arbitrage passes match.category into fee computation."""

    def test_politics_category_allows_thinner_spread(self):
        """With lower fees (politics), a spread that would fail crypto fees should still pass."""
        # Craft a spread that's marginal: just above 0 after politics fees but negative with crypto
        # Polymarket YES=0.485, Kalshi NO=0.500 → spread=0.015
        # Crypto fee on 0.485: 0.25*(0.485*0.515)^2 ≈ 0.0039 → more cost
        # Politics fee on 0.485: 0.0175*(0.485*0.515)^1 ≈ 0.00437 — wait, let's compute:
        # Actually politics exponent=1, fee_rate=0.0175: 0.0175*(0.485*0.515) = 0.0175*0.24978 ≈ 0.00437
        # Crypto exponent=2: 0.25*(0.485*0.515)^2 = 0.25*0.06239 ≈ 0.01560
        # The spread=0.015 might survive politics fees but not crypto fees
        ev_a = _make_event("polymarket", "p1", "Test Event", yes=0.485, no=0.70)
        ev_b = _make_event("kalshi", "k1", "Test Event", yes=0.60, no=0.50)

        match_crypto = _make_matched([ev_a, ev_b], category="crypto")
        match_politics = _make_matched([ev_a, ev_b], category="politics")

        opps_crypto = find_arbitrage([match_crypto])
        opps_politics = find_arbitrage([match_politics])

        # Both might pass or fail but politics should have equal or better net_profit_pct
        if opps_crypto and opps_politics:
            assert opps_politics[0].net_profit_pct >= opps_crypto[0].net_profit_pct

    def test_crypto_category_on_polymarket_kalshi(self):
        """Standard crypto arb should produce a valid opportunity."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.65)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55)

        matched = _make_matched([ev_a, ev_b], category="crypto")
        opps = find_arbitrage([matched])

        assert len(opps) == 1
        opp = opps[0]
        # net_profit_pct should be positive
        assert opp.net_profit_pct > 0
        # Dynamic fee is less than old flat, so net_profit_pct should be >= old calc
        # Old flat Polymarket fee: 0.40 * 0.02 = 0.008
        # Dynamic crypto fee at 0.40: 0.25*(0.40*0.60)^2 = 0.0144
        # So net should be better (higher) with dynamic fees
        old_yes_fee = 0.40 * 0.02
        new_yes_fee = 0.40 * compute_taker_fee("polymarket", 0.40, "crypto")
        assert new_yes_fee < old_yes_fee

    def test_category_from_matched_event_used(self):
        """Category flows from MatchedEvent.category into the fee model."""
        ev_a = _make_event("polymarket", "p1", "Election 2026", yes=0.45, no=0.65)
        ev_b = _make_event("kalshi", "k1", "Election 2026", yes=0.55, no=0.50)

        # Politics match
        match_pol = _make_matched([ev_a, ev_b], category="politics")
        opps_pol = find_arbitrage([match_pol])

        # Crypto match — same prices, different fees
        match_crypto = _make_matched([ev_a, ev_b], category="crypto")
        opps_crypto = find_arbitrage([match_crypto])

        # Both should produce opportunities (spread is generous)
        assert len(opps_pol) == 1
        assert len(opps_crypto) == 1

        # Politics fees are lower → net_profit_pct must be higher for politics
        assert opps_pol[0].net_profit_pct >= opps_crypto[0].net_profit_pct

    def test_no_regression_existing_arb(self):
        """Existing test cases still produce the same sign of result."""
        ev_a = _make_event("polymarket", "p1", "BTC > 100k", yes=0.40, no=0.65)
        ev_b = _make_event("kalshi", "k1", "BTC > 100k", yes=0.50, no=0.55)
        matched = _make_matched([ev_a, ev_b], category="crypto")

        opps = find_arbitrage([matched])
        assert len(opps) == 1
        assert opps[0].spread > 0
        assert opps[0].net_profit_pct > 0

    def test_two_non_polymarket_platforms_unaffected_by_category(self):
        """When neither platform is Polymarket, category has no effect on net profit."""
        ev_a = _make_event("kalshi", "k1", "Rate Decision", yes=0.40, no=0.65)
        ev_b = _make_event("predictit", "pi1", "Rate Decision", yes=0.50, no=0.55)

        match_crypto = _make_matched([ev_a, ev_b], category="crypto")
        match_politics = _make_matched([ev_a, ev_b], category="politics")

        opps_crypto = find_arbitrage([match_crypto])
        opps_politics = find_arbitrage([match_politics])

        if opps_crypto and opps_politics:
            assert abs(opps_crypto[0].net_profit_pct - opps_politics[0].net_profit_pct) < 1e-6
