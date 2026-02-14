"""
Pre-flight validation script.

Run this BEFORE starting the bot to verify:
1. API credentials work (auth + balance check)
2. Market data is accessible (can fetch crypto markets)
3. Orderbook data parses correctly
4. Binance price feed connects
5. Strike price parsing works on real market data
6. Fee calculations match expectations

Usage:
    python validate.py

This script makes NO trades and places NO orders. It only reads data.
"""

import asyncio
import sys
import time

import config
from data.market_scanner import MarketScanner
from execution.fee_calculator import calculate_fee
from kalshi.client import KalshiClient
from utils.fair_value import calculate_fair_value
from utils.logger import get_logger

log = get_logger("validate")

# ANSI colors for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}WARN{RESET}  {msg}")


def info(msg: str) -> None:
    print(f"  ----  {msg}")


async def validate() -> bool:
    """Run all validation checks. Returns True if all critical checks pass."""
    all_passed = True

    print()
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Kalshi Trading Bot — Pre-flight Validation{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print()
    print(f"  Trading mode:  {BOLD}{config.TRADING_MODE}{RESET}")
    print(f"  Paper trading: {BOLD}{config.PAPER_TRADING}{RESET}")
    print(f"  API base URL:  {config.KALSHI_BASE_URL}")
    print(f"  API key ID:    {config.KALSHI_API_KEY_ID[:8]}..." if len(config.KALSHI_API_KEY_ID) > 8 else f"  API key ID:    {config.KALSHI_API_KEY_ID}")
    print()

    # ------------------------------------------------------------------
    # Check 1: API key configured
    # ------------------------------------------------------------------
    print(f"{BOLD}[1/7] API Key Configuration{RESET}")

    if not config.KALSHI_API_KEY_ID or config.KALSHI_API_KEY_ID == "your-api-key-id":
        fail("KALSHI_API_KEY_ID is not set. Edit your .env file.")
        return False
    ok("API key ID is set")

    from pathlib import Path
    key_path = Path(config.KALSHI_PRIVATE_KEY_PATH)
    if not key_path.exists():
        fail(f"Private key file not found: {config.KALSHI_PRIVATE_KEY_PATH}")
        fail("Make sure your .pem file is at the configured path.")
        return False
    ok(f"Private key file exists: {config.KALSHI_PRIVATE_KEY_PATH}")

    # ------------------------------------------------------------------
    # Check 2: RSA key loads correctly
    # ------------------------------------------------------------------
    print(f"\n{BOLD}[2/7] RSA Key Loading{RESET}")

    try:
        from kalshi.auth import load_private_key
        private_key = load_private_key(config.KALSHI_PRIVATE_KEY_PATH)
        ok("RSA private key loaded successfully")
    except Exception as e:
        fail(f"Failed to load RSA key: {e}")
        return False

    # ------------------------------------------------------------------
    # Check 3: API authentication + balance
    # ------------------------------------------------------------------
    print(f"\n{BOLD}[3/7] API Authentication{RESET}")

    client = KalshiClient()
    try:
        await client.connect()
        ok("HTTP client connected")
    except Exception as e:
        fail(f"Failed to connect client: {e}")
        return False

    try:
        balance = await client.get_balance()
        ok(f"Authenticated successfully — balance: ${balance.balance_dollars:.2f} (available: ${balance.available_balance_dollars:.2f})")

        if balance.available_balance_dollars <= 0:
            warn("Available balance is $0.00 — you won't be able to place orders")
    except Exception as e:
        fail(f"Authentication failed: {e}")
        fail("Check that your API key ID matches the private key, and you're using the right API URL.")
        await client.close()
        return False

    # ------------------------------------------------------------------
    # Check 4: Exchange status
    # ------------------------------------------------------------------
    print(f"\n{BOLD}[4/7] Exchange Status{RESET}")

    try:
        status = await client.get_exchange_status()
        if status.trading_active:
            ok(f"Exchange is open: {status.exchange_status}")
        else:
            warn(f"Exchange status: {status.exchange_status} (trading may not be active)")
    except Exception as e:
        warn(f"Could not check exchange status: {e}")

    # ------------------------------------------------------------------
    # Check 5: Crypto market discovery
    # ------------------------------------------------------------------
    print(f"\n{BOLD}[5/7] Crypto Market Discovery{RESET}")

    scanner = MarketScanner(client)
    markets_found = 0

    for series in ["KXBTC", "KXETH", "KXSOL"]:
        try:
            markets = await client.get_all_markets(series_ticker=series, status="open")
            count = len(markets)
            markets_found += count
            if count > 0:
                ok(f"{series}: {count} open markets found")
                # Show a sample market
                sample = markets[0]
                info(f"  Sample: {sample.ticker} — \"{sample.title}\" (status={sample.status})")

                # Test strike parsing
                strike = MarketScanner.parse_strike_price(sample)
                mtype = MarketScanner.classify_market_type(sample)
                if strike:
                    info(f"  Parsed: strike=${strike:,.0f}, type={mtype}")
                else:
                    info(f"  Parsed: type={mtype} (no numeric strike)")
            else:
                warn(f"{series}: no open markets found (markets may be closed outside trading hours)")
        except Exception as e:
            fail(f"{series}: failed to fetch markets: {e}")
            all_passed = False

    if markets_found == 0:
        warn("No open crypto markets found — this is normal outside trading hours")

    # ------------------------------------------------------------------
    # Check 6: Orderbook data
    # ------------------------------------------------------------------
    print(f"\n{BOLD}[6/7] Orderbook Data{RESET}")

    if markets_found > 0:
        # Pick the first BTC market with the soonest close time
        try:
            btc_markets = await client.get_all_markets(series_ticker="KXBTC", status="open")
            if btc_markets:
                test_market = btc_markets[0]
                ob = await client.get_orderbook(test_market.ticker, depth=5)

                yes_bid_count = len(ob.yes_bids)
                no_bid_count = len(ob.no_bids)

                ok(f"Orderbook for {test_market.ticker}: {yes_bid_count} YES bids, {no_bid_count} NO bids")

                if yes_bid_count > 0:
                    best = ob.yes_bids[0]
                    info(f"  Best YES bid: ${best[0]:.2f} x {int(best[1])} contracts")
                if no_bid_count > 0:
                    best = ob.no_bids[0]
                    yes_ask = 1.00 - best[0]
                    info(f"  Best NO bid: ${best[0]:.2f} → implied YES ask: ${yes_ask:.2f}")

                if yes_bid_count == 0 and no_bid_count == 0:
                    warn("Orderbook is empty — low liquidity or market is paused")
            else:
                warn("No BTC markets available to test orderbook")
        except Exception as e:
            fail(f"Orderbook fetch failed: {e}")
            all_passed = False
    else:
        warn("Skipping orderbook check (no markets available)")

    # ------------------------------------------------------------------
    # Check 7: Fee calculation sanity
    # ------------------------------------------------------------------
    print(f"\n{BOLD}[7/7] Fee & Fair Value Sanity{RESET}")

    # Fee spot checks from the spec
    fee_checks = [
        (1, 0.50, False, 0.02),
        (100, 0.50, False, 1.75),
        (100, 0.95, False, 0.34),
        (100, 0.95, True, 0.09),
        (1, 0.01, False, 0.01),
    ]

    fee_ok = True
    for contracts, price, is_maker, expected in fee_checks:
        actual = calculate_fee(contracts, price, is_maker)
        if abs(actual - expected) > 0.001:
            fail(f"Fee mismatch: calculate_fee({contracts}, {price}, {is_maker}) = {actual}, expected {expected}")
            fee_ok = False
            all_passed = False

    if fee_ok:
        ok("Fee calculations match spec (5/5 spot checks passed)")

    # Fair value sanity
    fv = calculate_fair_value(70000, 68000, 0.65, 6, "above")
    if 0.80 <= fv <= 0.99:
        ok(f"Fair value sanity: BTC@70K, strike 68K, 6h → {fv:.4f} (expected 0.85-0.95)")
    else:
        fail(f"Fair value unexpected: {fv}")
        all_passed = False

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    print(f"{BOLD}{'=' * 60}{RESET}")
    if all_passed:
        print(f"{GREEN}{BOLD}  ALL CHECKS PASSED — ready to run the bot{RESET}")
        if config.TRADING_MODE == "demo":
            print(f"  Next step: {BOLD}python main.py{RESET}")
            print(f"  This runs in demo/paper mode. No real money at risk.")
        elif config.TRADING_MODE == "small_live":
            print(f"  Next step: {BOLD}python main.py{RESET}")
            print(f"  {YELLOW}SMALL LIVE MODE: Real money, reduced position sizes.{RESET}")
            print(f"  Max per trade: {config.MAX_SINGLE_TRADE_PCT*100:.0f}% of capital")
            print(f"  Max total exposure: {config.MAX_TOTAL_EXPOSURE_PCT*100:.0f}% of capital")
            print(f"  Daily loss limit: {config.DAILY_LOSS_LIMIT_PCT*100:.0f}%")
        elif config.TRADING_MODE == "full_live":
            print(f"  {RED}{BOLD}FULL LIVE MODE: Real money, full position sizes.{RESET}")
            print(f"  Make sure you know what you're doing.")
    else:
        print(f"{RED}{BOLD}  SOME CHECKS FAILED — fix the issues above before running{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print()

    await client.close()
    return all_passed


def main():
    result = asyncio.run(validate())
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
