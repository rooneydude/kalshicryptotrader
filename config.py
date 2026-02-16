"""
All configurable parameters for the trading bot.

CRITICAL CONSTANTS (from Kalshi fee schedule, effective Feb 5, 2026):
- Standard taker fee coefficient: 0.07
- Standard maker fee coefficient: 0.0175
- S&P/NASDAQ taker fee coefficient: 0.035 (NOT used for crypto)
- S&P/NASDAQ maker fee coefficient: 0.0175
- Fee formula: roundup(coefficient * contracts * price * (1 - price))
- "roundup" = round UP to the next cent ($0.01)
- Prices are in dollars (50 cents = 0.50)
- No settlement fees, no membership fees

CRYPTO MARKET TICKERS:
- BTC daily: starts with "KXBTCD" (e.g., KXBTCD-26FEB1507-T78749.99)
- ETH daily: starts with "KXETHD"
- SOL daily: starts with "KXSOLD"
- BTC 15-min up/down: "KXBTC15M" (e.g., KXBTC15M-26FEB150645-45)
- ETH 15-min up/down: "KXETH15M"
- SOL 15-min up/down: "KXSOL15M"
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Kalshi API ---
KALSHI_API_KEY_ID: str = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi-key.pem")
KALSHI_BASE_URL: str = os.getenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co")
KALSHI_WS_URL: str = os.getenv("KALSHI_WS_URL", "wss://demo-api.kalshi.co/trade-api/ws/v2")
API_PREFIX: str = "/trade-api/v2"

# --- External Price Feeds ---
BINANCE_WS_URL: str = os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws")

# --- Trading Mode ---
# TRADING_MODE controls the overall posture of the bot:
#   "demo"        → Use demo API + paper trading (default, zero risk)
#   "paper_live"  → Use production API for real market data, but simulate trades
#   "small_live"  → Use production API + real orders, but tiny position sizes
#   "full_live"   → Use production API + full position sizes from spec
TRADING_MODE: str = os.getenv("TRADING_MODE", "demo")
PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"

# Auto-configure based on TRADING_MODE
if TRADING_MODE == "demo":
    PAPER_TRADING = True
    KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co")
    KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "wss://demo-api.kalshi.co/trade-api/ws/v2")
elif TRADING_MODE == "paper_live":
    PAPER_TRADING = True
    KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com")
    KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")
elif TRADING_MODE == "small_live":
    PAPER_TRADING = False
    KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com")
    KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")
elif TRADING_MODE == "full_live":
    PAPER_TRADING = False
    KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com")
    KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")

# --- Dashboard ---
DASHBOARD_PORT: int = int(os.getenv("PORT", "8080"))

# --- Paper Trading ---
PAPER_STARTING_BALANCE: float = float(os.getenv("PAPER_STARTING_BALANCE", "1000.0"))

# --- Logging ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.getenv("LOG_FILE", "./logs/bot.log")

# --- Crypto Series Tickers ---
CRYPTO_SERIES_TICKERS: list[str] = ["KXBTCD", "KXETHD", "KXSOLD"]
CRYPTO_15M_SERIES_TICKERS: list[str] = ["KXBTC15M", "KXETH15M", "KXSOL15M"]
ALL_CRYPTO_SERIES: list[str] = CRYPTO_SERIES_TICKERS + CRYPTO_15M_SERIES_TICKERS
SUPPORTED_ASSETS: list[str] = ["BTC", "ETH", "SOL"]

# Map 15-min series ticker → asset symbol
SERIES_TO_ASSET: dict[str, str] = {
    "KXBTC15M": "BTC",
    "KXETH15M": "ETH",
    "KXSOL15M": "SOL",
}

# --- Fee Coefficients ---
TAKER_FEE_COEFF: float = 0.07
MAKER_FEE_COEFF: float = 0.0175
INDEX_TAKER_FEE_COEFF: float = 0.035   # S&P/NASDAQ only, not crypto
INDEX_MAKER_FEE_COEFF: float = 0.0175  # S&P/NASDAQ only

# --- Position Sizing ---
MAX_SINGLE_TRADE_PCT: float = 0.10       # 10% of capital per trade
MAX_PER_STRIKE_PCT: float = 0.15         # 15% of capital per strike
MAX_PER_EVENT_PCT: float = 0.30          # 30% of capital per event
MAX_TOTAL_EXPOSURE_PCT: float = 0.75     # 75% of capital total
CASH_BUFFER_PCT: float = 0.25            # Always keep 25% in cash
DAILY_LOSS_LIMIT_PCT: float = 0.05       # Stop trading after 5% daily loss
WEEKLY_LOSS_LIMIT_PCT: float = 0.10      # Review after 10% weekly loss

# --- Strategy 1: Momentum Scalp ---
SCALP_MIN_YES_FAIR_VALUE: float = 0.90   # Only scalp strikes where fair value >= 90c
SCALP_MAX_ENTRY_PRICE: float = 0.93      # Only buy if YES price <= 93c
SCALP_MIN_EDGE_CENTS: int = 3            # Minimum 3c edge after fees
SCALP_MIN_BOOK_DEPTH: int = 20           # Minimum 20 contracts at best ask
SCALP_MAX_TIME_TO_SETTLE_HOURS: int = 8
SCALP_PREFER_MAKER: bool = True          # Use post_only when possible

# --- Strategy 2: Market Making ---
MM_ENABLED: bool = True                  # Master switch for market maker strategy
MM_SPREAD_CENTS: int = 4                 # Target 4c spread (2c each side of fair value)
MM_MAX_NET_POSITION: int = 500           # Max 500 contracts net long or short
MM_HEDGE_TRIGGER: int = 200              # Hedge when net > 200 contracts
MM_QUOTE_SIZE: int = 50                  # 50 contracts per quote
MM_REQUOTE_INTERVAL_SEC: int = 3         # Refresh quotes every 3 seconds
MM_CANCEL_ON_MOVE_PCT: float = 0.02      # Cancel all if BTC moves > 2% in 30 min
MM_MIN_VOLUME_24H: int = 10000           # Only make markets with 24h vol > 10K

# --- Strategy 3: Cross-Strike Arb ---
ARB_MIN_PROFIT_CENTS: int = 2            # Minimum 2c profit per contract after fees
ARB_MAX_CONTRACTS: int = 100             # Max contracts per arb leg
ARB_SCAN_INTERVAL_SEC: int = 5           # Scan for arbs every 5 seconds

# --- Strategy 4: 15-Minute Momentum ---
# Trades the rolling 15-min "price up?" binary markets using short-term momentum
FIFTEEN_MIN_ENABLED: bool = True
FIFTEEN_MIN_INTERVAL_SEC: int = 5          # Scan every 5 seconds
FIFTEEN_MIN_MOMENTUM_WINDOW_SEC: int = 120 # Lookback window for momentum (2 min)
FIFTEEN_MIN_MIN_MOMENTUM_PCT: float = 0.08 # Min 0.08% move to trigger a signal
FIFTEEN_MIN_MIN_EDGE_CENTS: int = 2        # Min 2c edge after fees
FIFTEEN_MIN_MAX_CONTRACTS: int = 20        # Max contracts per trade
FIFTEEN_MIN_MIN_TIME_LEFT_SEC: int = 120   # Don't trade if < 2 min remaining
FIFTEEN_MIN_MAX_ENTRY_AGE_SEC: int = 600   # Don't trade if market opened > 10 min ago
FIFTEEN_MIN_CONFIDENCE_BOOST: float = 0.10 # Boost fair value toward momentum direction
FIFTEEN_MIN_USE_MAKER: bool = True         # Prefer maker orders

# --- Polling & Rate Limits ---
ORDERBOOK_POLL_INTERVAL_SEC: int = 1     # Poll orderbooks every 1 second (parallel)
MARKET_SCAN_INTERVAL_SEC: int = 60       # Scan for new markets every 60 seconds
PRICE_FEED_RECONNECT_SEC: int = 5        # Reconnect WebSocket after 5 seconds
API_MAX_RETRIES: int = 3                 # Max retries for API calls
API_RETRY_BASE_DELAY: float = 1.0        # Base delay for exponential backoff
API_RATE_LIMIT_PER_SEC: float = 25.0     # Max requests per second (increased for speed)

# --- Paths ---
PROJECT_ROOT: Path = Path(__file__).parent
LOG_DIR: Path = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# SMALL LIVE MODE OVERRIDES
# When TRADING_MODE="small_live", drastically reduce sizes to limit risk
# while validating the bot works correctly with real money.
# ---------------------------------------------------------------------------
if TRADING_MODE == "small_live":
    MAX_SINGLE_TRADE_PCT = 0.04        # 4% of capital per trade (was 10%) — ~$0.08 on $2
    MAX_PER_STRIKE_PCT = 0.10          # 10% per strike (was 15%)
    MAX_PER_EVENT_PCT = 0.20           # 20% per event (was 30%)
    MAX_TOTAL_EXPOSURE_PCT = 0.50      # 50% total (was 75%)
    CASH_BUFFER_PCT = 0.50             # Keep 50% cash (was 25%)
    DAILY_LOSS_LIMIT_PCT = 0.10        # Stop after 10% daily loss (was 5%)
    WEEKLY_LOSS_LIMIT_PCT = 0.20       # Stop after 20% weekly loss (was 10%)

    SCALP_MIN_BOOK_DEPTH = 10          # Relax depth (was 20)

    # Market maker DISABLED for small accounts — it was the main loss driver
    MM_ENABLED = False
    MM_QUOTE_SIZE = 1                  # 1 contract per quote (was 50)
    MM_MAX_NET_POSITION = 3            # Max 3 net (was 500) — hard cap
    MM_HEDGE_TRIGGER = 2               # Hedge at 2 (was 200)

    ARB_MAX_CONTRACTS = 2              # Max 2 per arb leg (was 100)
    FIFTEEN_MIN_MAX_CONTRACTS = 5      # Max 5 per 15-min trade (was 20)
