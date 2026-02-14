"""
Fair value calculator for Kalshi crypto binary contracts.

Uses Black-Scholes digital option pricing (cash-or-nothing) to estimate
the theoretical probability that a crypto asset finishes above/below a
given strike at expiration.

The risk-free rate is assumed to be 0 for short-duration contracts (hours).
"""

import math

from scipy.stats import norm


def binary_call_price(
    spot: float,
    strike: float,
    vol: float,
    time_years: float,
) -> float:
    """
    Price of a binary (digital) call option — probability that spot > strike at expiry.

    Args:
        spot: Current asset price (e.g. 70500.0 for BTC).
        strike: Contract strike price (e.g. 68750.0).
        vol: Annualized volatility as a decimal (e.g. 0.65 for 65%).
        time_years: Time to expiration in years (e.g. 6/8760 for 6 hours).

    Returns:
        Fair value between 0.0 and 1.0.
    """
    if time_years <= 0:
        # At or past expiry: binary payoff
        return 1.0 if spot > strike else 0.0

    if vol <= 0:
        return 1.0 if spot > strike else 0.0

    if strike <= 0 or spot <= 0:
        return 0.0

    d2 = (math.log(spot / strike) - 0.5 * vol**2 * time_years) / (
        vol * math.sqrt(time_years)
    )
    return float(norm.cdf(d2))


def binary_put_price(
    spot: float,
    strike: float,
    vol: float,
    time_years: float,
) -> float:
    """
    Price of a binary (digital) put option — probability that spot < strike at expiry.

    Args:
        spot: Current asset price.
        strike: Contract strike price.
        vol: Annualized volatility as a decimal.
        time_years: Time to expiration in years.

    Returns:
        Fair value between 0.0 and 1.0.
    """
    return 1.0 - binary_call_price(spot, strike, vol, time_years)


def hours_to_years(hours: float) -> float:
    """Convert hours to fraction of a year (8760 hours/year)."""
    return hours / 8760.0


def calculate_fair_value(
    spot: float,
    strike: float,
    vol: float,
    hours_to_expiry: float,
    market_type: str = "above",
    upper_strike: float | None = None,
) -> float:
    """
    Calculate the fair value of a Kalshi crypto binary contract.

    Args:
        spot: Current asset price.
        strike: Contract strike price (lower bound for range).
        vol: Annualized volatility as a decimal.
        hours_to_expiry: Hours until contract settlement.
        market_type: One of "above", "below", or "range".
        upper_strike: Upper strike for "range" market type.

    Returns:
        Fair value in dollars (0.0 to 1.0).
    """
    t = hours_to_years(hours_to_expiry)

    match market_type:
        case "above":
            return binary_call_price(spot, strike, vol, t)
        case "below":
            return binary_put_price(spot, strike, vol, t)
        case "range":
            if upper_strike is None:
                raise ValueError("upper_strike required for range market type")
            # P(lower < spot < upper) = P(spot > lower) - P(spot > upper)
            return binary_call_price(spot, strike, vol, t) - binary_call_price(
                spot, upper_strike, vol, t
            )
        case _:
            raise ValueError(f"Unknown market type: {market_type}")
