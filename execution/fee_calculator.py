"""
Exact Kalshi fee calculations.

Every trade decision must account for fees. The fee formula is:
    fee = roundup(coefficient * contracts * price * (1 - price))

Where roundup = round UP to the next cent ($0.01).

Fee coefficients (effective Feb 5, 2026):
- Standard taker: 0.07
- Standard maker: 0.0175
- S&P/NASDAQ taker: 0.035 (not used for crypto)
- S&P/NASDAQ maker: 0.0175 (not used for crypto)
"""

import math

# Fee coefficients for standard (crypto) markets
TAKER_COEFF: float = 0.07
MAKER_COEFF: float = 0.0175

# Fee coefficients for index (S&P/NASDAQ) markets — not used for crypto
INDEX_TAKER_COEFF: float = 0.035
INDEX_MAKER_COEFF: float = 0.0175


def calculate_fee(
    contracts: int,
    price_dollars: float,
    is_maker: bool = False,
) -> float:
    """
    Calculate the Kalshi fee for a trade.

    Args:
        contracts: Number of contracts.
        price_dollars: Price per contract in dollars (e.g. 0.50 for 50c).
        is_maker: True if this is a maker (post_only) order.

    Returns:
        Fee in dollars, rounded UP to the next cent.
    """
    coeff = MAKER_COEFF if is_maker else TAKER_COEFF
    fee = coeff * contracts * price_dollars * (1.0 - price_dollars)
    # Round UP to next cent.
    # Subtract a tiny epsilon before ceiling to compensate for IEEE 754
    # floating-point drift (e.g., 1.75 becoming 1.7500000000000002).
    fee_cents_raw = fee * 100
    fee_cents = math.ceil(fee_cents_raw - 1e-9)
    return max(fee_cents, 1) / 100  # Minimum 1 cent


def calculate_net_profit(
    buy_price: float,
    sell_price: float,
    contracts: int,
    is_maker_buy: bool = False,
    is_maker_sell: bool = False,
) -> float:
    """
    Calculate net profit after fees for a round-trip trade.

    Args:
        buy_price: Price paid per contract in dollars.
        sell_price: Price received per contract in dollars (or $1.00 at settlement).
        contracts: Number of contracts.
        is_maker_buy: True if buy order is maker.
        is_maker_sell: True if sell order is maker.

    Returns:
        Net profit in dollars (can be negative).
    """
    gross = (sell_price - buy_price) * contracts
    buy_fee = calculate_fee(contracts, buy_price, is_maker_buy)
    sell_fee = calculate_fee(contracts, sell_price, is_maker_sell)
    return gross - buy_fee - sell_fee


def min_profitable_spread(
    price_dollars: float,
    contracts: int,
    is_maker: bool = True,
) -> float:
    """
    Calculate the minimum spread needed to be profitable after fees.

    Assumes both buy and sell sides have the same maker/taker status.
    The spread is symmetric around the given price.

    Args:
        price_dollars: The mid-price in dollars.
        contracts: Number of contracts per side.
        is_maker: True if both sides are maker orders.

    Returns:
        Minimum spread in dollars (e.g. 0.02 = 2 cents).
    """
    coeff = MAKER_COEFF if is_maker else TAKER_COEFF

    # Total fee for buying at (price - spread/2) and selling at (price + spread/2)
    # We need gross profit > total fees
    # Gross = spread * contracts
    # Fee(buy) ~= ceil(coeff * contracts * p * (1-p) * 100) / 100
    # Fee(sell) ~= ceil(coeff * contracts * p * (1-p) * 100) / 100
    # Approximate: total_fees ~= 2 * coeff * contracts * price * (1 - price)
    # But we need to be exact with the ceil, so iterate

    # Start with the approximate minimum spread
    approx_total_fee = 2 * coeff * contracts * price_dollars * (1.0 - price_dollars)

    # Check increasing spreads in 1-cent increments
    spread_cents = max(1, math.ceil(approx_total_fee * 100 / contracts))

    for _ in range(100):  # Safety limit
        spread = spread_cents / 100.0
        half_spread = spread / 2.0

        buy_price = max(0.01, price_dollars - half_spread)
        sell_price = min(0.99, price_dollars + half_spread)

        net = calculate_net_profit(buy_price, sell_price, contracts, is_maker, is_maker)
        if net > 0:
            return spread

        spread_cents += 1

    # Fallback — should not reach here for reasonable inputs
    return spread_cents / 100.0
