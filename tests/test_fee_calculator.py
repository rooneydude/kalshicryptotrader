"""
Tests for execution/fee_calculator.py — Exact Kalshi fee calculations.

Test cases from the spec:
- calculate_fee(1, 0.50, False)   == 0.02     # 0.07 * 1 * 0.50 * 0.50 = 0.0175 → roundup = 0.02
- calculate_fee(100, 0.50, False) == 1.75     # 0.07 * 100 * 0.50 * 0.50 = 1.75
- calculate_fee(100, 0.95, False) == 0.34     # 0.07 * 100 * 0.95 * 0.05 = 0.3325 → 0.34
- calculate_fee(100, 0.95, True)  == 0.09     # 0.0175 * 100 * 0.95 * 0.05 = 0.083125 → 0.09
- calculate_fee(100, 0.10, False) == 0.63     # 0.07 * 100 * 0.10 * 0.90 = 0.63
- calculate_fee(1, 0.01, False)   == 0.01     # 0.07 * 1 * 0.01 * 0.99 = 0.000693 → 0.01
"""

import pytest

from execution.fee_calculator import (
    calculate_fee,
    calculate_net_profit,
    min_profitable_spread,
)


class TestCalculateFee:
    """Test the fee calculation with exact spec-provided values."""

    def test_1_contract_50c_taker(self):
        """1 contract at 50c taker: 0.07 * 1 * 0.50 * 0.50 = 0.0175 → 0.02"""
        assert calculate_fee(1, 0.50, is_maker=False) == 0.02

    def test_100_contracts_50c_taker(self):
        """100 contracts at 50c taker: 0.07 * 100 * 0.50 * 0.50 = 1.75"""
        assert calculate_fee(100, 0.50, is_maker=False) == 1.75

    def test_100_contracts_95c_taker(self):
        """100 contracts at 95c taker: 0.07 * 100 * 0.95 * 0.05 = 0.3325 → 0.34"""
        assert calculate_fee(100, 0.95, is_maker=False) == 0.34

    def test_100_contracts_95c_maker(self):
        """100 contracts at 95c maker: 0.0175 * 100 * 0.95 * 0.05 = 0.083125 → 0.09"""
        assert calculate_fee(100, 0.95, is_maker=True) == 0.09

    def test_100_contracts_10c_taker(self):
        """100 contracts at 10c taker: 0.07 * 100 * 0.10 * 0.90 = 0.63"""
        assert calculate_fee(100, 0.10, is_maker=False) == 0.63

    def test_1_contract_1c_taker(self):
        """1 contract at 1c taker: 0.07 * 1 * 0.01 * 0.99 = 0.000693 → 0.01"""
        assert calculate_fee(1, 0.01, is_maker=False) == 0.01

    def test_fee_is_always_at_least_1_cent(self):
        """Even tiny trades should have at least 1 cent fee (minimum enforced)."""
        fee = calculate_fee(1, 0.01, is_maker=True)
        assert fee == 0.01

    def test_fee_at_extremes(self):
        """Fees at price extremes (near 0 or 1) should be small."""
        fee_low = calculate_fee(100, 0.01, is_maker=False)
        fee_high = calculate_fee(100, 0.99, is_maker=False)
        # Both use p * (1-p), which is small at extremes
        assert fee_low < 1.0
        assert fee_high < 1.0

    def test_fee_maximized_at_50c(self):
        """Fee is maximized when p * (1-p) is maximized at p = 0.50."""
        fee_50 = calculate_fee(100, 0.50, is_maker=False)
        fee_30 = calculate_fee(100, 0.30, is_maker=False)
        fee_70 = calculate_fee(100, 0.70, is_maker=False)
        assert fee_50 >= fee_30
        assert fee_50 >= fee_70

    def test_maker_cheaper_than_taker(self):
        """Maker fees should always be less than taker fees."""
        for price in [0.10, 0.30, 0.50, 0.70, 0.90]:
            maker = calculate_fee(100, price, is_maker=True)
            taker = calculate_fee(100, price, is_maker=False)
            assert maker <= taker, f"Maker >= Taker at price {price}"

    def test_fee_rounds_up(self):
        """Verify that fees always round UP to the next cent."""
        # 0.07 * 10 * 0.50 * 0.50 = 0.175 → should round UP to 0.18
        assert calculate_fee(10, 0.50, is_maker=False) == 0.18


class TestCalculateNetProfit:
    def test_profitable_trade(self):
        """Buying at 85c, settling at $1.00 with 100 contracts."""
        profit = calculate_net_profit(0.85, 1.00, 100, is_maker_buy=True, is_maker_sell=False)
        # Gross = (1.00 - 0.85) * 100 = 15.00
        # Buy fee (maker): ceil(0.0175 * 100 * 0.85 * 0.15 * 100) / 100 = ceil(22.3125) / 100 = 0.23
        # Sell fee: at settlement price 1.00, fee = coeff * contracts * 1.0 * 0.0 = 0
        # Net = 15.00 - 0.23 - 0.00 = 14.77
        assert profit > 14.0  # Should be profitable
        assert profit < 15.0  # Less than gross

    def test_unprofitable_trade(self):
        """Buying at 50c, selling at 49c — should lose money."""
        profit = calculate_net_profit(0.50, 0.49, 100, is_maker_buy=False, is_maker_sell=False)
        assert profit < 0

    def test_zero_spread(self):
        """Buying and selling at the same price — should lose fees."""
        profit = calculate_net_profit(0.50, 0.50, 100, is_maker_buy=True, is_maker_sell=True)
        assert profit < 0  # Fees make it negative


class TestMinProfitableSpread:
    def test_spread_is_positive(self):
        """Minimum profitable spread should always be positive."""
        spread = min_profitable_spread(0.50, 100, is_maker=True)
        assert spread > 0

    def test_maker_spread_less_than_taker(self):
        """Maker minimum spread should be less than taker."""
        maker_spread = min_profitable_spread(0.50, 100, is_maker=True)
        taker_spread = min_profitable_spread(0.50, 100, is_maker=False)
        assert maker_spread <= taker_spread

    def test_spread_profitable(self):
        """Verify that the minimum spread actually results in profit."""
        price = 0.50
        contracts = 100
        spread = min_profitable_spread(price, contracts, is_maker=True)

        buy_price = price - spread / 2
        sell_price = price + spread / 2
        profit = calculate_net_profit(buy_price, sell_price, contracts, True, True)
        assert profit > 0
