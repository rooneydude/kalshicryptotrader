"""
Tests for utils/fair_value.py — Black-Scholes digital option pricing.

Test cases from the spec:
- BTC at 70000, strike 68000, vol 0.65, 6 hours → ~0.85-0.95 (deep ITM)
- BTC at 70000, strike 70000, vol 0.65, 6 hours → ~0.50 (ATM)
- BTC at 70000, strike 72000, vol 0.65, 6 hours → ~0.15-0.25 (OTM)
- As time → 0, deep ITM → 1.0, deep OTM → 0.0
"""

import pytest

from utils.fair_value import (
    binary_call_price,
    binary_put_price,
    calculate_fair_value,
    hours_to_years,
)


class TestBinaryCallPrice:
    """Test the binary call pricing model."""

    def test_deep_itm(self):
        """BTC at 70000, strike 68000 (deep ITM) → ~0.85-0.95"""
        price = binary_call_price(70000, 68000, 0.65, hours_to_years(6))
        assert 0.80 <= price <= 0.99, f"Deep ITM price {price} out of expected range"

    def test_atm(self):
        """BTC at 70000, strike 70000 (ATM) → ~0.50"""
        price = binary_call_price(70000, 70000, 0.65, hours_to_years(6))
        assert 0.40 <= price <= 0.60, f"ATM price {price} out of expected range"

    def test_otm(self):
        """BTC at 70000, strike 72000 (OTM) → small value, well below 0.50"""
        price = binary_call_price(70000, 72000, 0.65, hours_to_years(6))
        assert 0.01 <= price <= 0.40, f"OTM price {price} out of expected range"

    def test_expired_itm(self):
        """At expiry, deep ITM → 1.0"""
        price = binary_call_price(70000, 68000, 0.65, 0)
        assert price == 1.0

    def test_expired_otm(self):
        """At expiry, deep OTM → 0.0"""
        price = binary_call_price(70000, 72000, 0.65, 0)
        assert price == 0.0

    def test_expired_exact_atm(self):
        """At expiry, exactly ATM → 0.0 (spot not > strike)."""
        price = binary_call_price(70000, 70000, 0.65, 0)
        assert price == 0.0

    def test_higher_vol_widens_distribution(self):
        """Higher volatility should move prices toward 0.50 (more uncertainty)."""
        low_vol = binary_call_price(70000, 68000, 0.30, hours_to_years(6))
        high_vol = binary_call_price(70000, 68000, 1.00, hours_to_years(6))
        # ITM contract: higher vol → lower probability (more uncertain)
        assert high_vol < low_vol

    def test_more_time_approaches_50(self):
        """More time to expiry → prices move toward 0.50 (more uncertainty)."""
        short_time = binary_call_price(70000, 68000, 0.65, hours_to_years(1))
        long_time = binary_call_price(70000, 68000, 0.65, hours_to_years(24))
        # ITM: shorter time = higher confidence = closer to 1.0
        assert short_time > long_time

    def test_output_bounded_0_1(self):
        """All outputs should be between 0 and 1."""
        for spot in [50000, 70000, 100000]:
            for strike in [60000, 70000, 80000]:
                for vol in [0.30, 0.65, 1.00]:
                    for hours in [1, 6, 24]:
                        p = binary_call_price(spot, strike, vol, hours_to_years(hours))
                        assert 0.0 <= p <= 1.0, f"Out of bounds: {p}"


class TestBinaryPutPrice:
    def test_put_call_parity(self):
        """Put + Call should equal 1.0 (complete probability space)."""
        for spot, strike in [(70000, 68000), (70000, 70000), (70000, 72000)]:
            call = binary_call_price(spot, strike, 0.65, hours_to_years(6))
            put = binary_put_price(spot, strike, 0.65, hours_to_years(6))
            assert abs(call + put - 1.0) < 1e-10

    def test_deep_otm_put(self):
        """Deep OTM put (spot >> strike) → small value."""
        price = binary_put_price(70000, 60000, 0.65, hours_to_years(6))
        assert price < 0.10


class TestHoursToYears:
    def test_one_hour(self):
        assert abs(hours_to_years(1) - 1 / 8760) < 1e-10

    def test_one_day(self):
        assert abs(hours_to_years(24) - 24 / 8760) < 1e-10

    def test_one_year(self):
        assert abs(hours_to_years(8760) - 1.0) < 1e-10


class TestCalculateFairValue:
    def test_above_market(self):
        """Test 'above' market type routing."""
        fv = calculate_fair_value(70000, 68000, 0.65, 6, "above")
        assert 0.80 <= fv <= 0.99

    def test_below_market(self):
        """Test 'below' market type routing."""
        fv = calculate_fair_value(70000, 72000, 0.65, 6, "below")
        assert 0.55 <= fv <= 0.99

    def test_range_market(self):
        """Test 'range' market: P(68000 < BTC < 72000)."""
        fv = calculate_fair_value(70000, 68000, 0.65, 6, "range", upper_strike=72000)
        assert 0.0 < fv < 1.0
        # Should be the probability of being between 68000 and 72000
        call_low = binary_call_price(70000, 68000, 0.65, hours_to_years(6))
        call_high = binary_call_price(70000, 72000, 0.65, hours_to_years(6))
        expected = call_low - call_high
        assert abs(fv - expected) < 1e-10

    def test_range_requires_upper_strike(self):
        """Range market type should raise if upper_strike missing."""
        with pytest.raises(ValueError, match="upper_strike"):
            calculate_fair_value(70000, 68000, 0.65, 6, "range")

    def test_unknown_market_type(self):
        """Unknown market type should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown market type"):
            calculate_fair_value(70000, 68000, 0.65, 6, "exotic")
