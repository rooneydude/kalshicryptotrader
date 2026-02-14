# Kalshi Short-Term Crypto Trading Bot

Automated trading bot for Kalshi's short-term crypto prediction markets (hourly + daily BTC/ETH/SOL contracts).

## Strategies

1. **Momentum Scalping** -- Buy deep ITM YES contracts when the Kalshi book lags behind spot price moves
2. **Market Making** -- Two-sided quoting on near-ATM daily strikes to capture the bid-ask spread
3. **Cross-Strike Arbitrage** -- Detect monotonicity violations, parity breaks, and range-sum inconsistencies across strikes

## Setup

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your Kalshi API key and private key path
```

## Configuration

All trading parameters are in `config.py`. Key settings:

- Position sizing limits (per-trade, per-strike, per-event, total exposure)
- Strategy-specific thresholds (min edge, spread width, scan intervals)
- Risk controls (daily loss limit, kill switch triggers)

## Usage

```bash
# Run in paper trading mode (default)
python main.py

# The bot will:
# 1. Connect to Binance for real-time crypto prices
# 2. Connect to Kalshi for market data and order execution
# 3. Run all three strategies on their configured intervals
# 4. Log all activity to ./logs/bot.log
```

## Project Structure

```
├── config.py                 # All configurable parameters
├── main.py                   # Entry point
├── kalshi/                   # Kalshi API integration
│   ├── auth.py               # RSA-PSS authentication
│   ├── client.py             # REST API client
│   ├── websocket_client.py   # WebSocket client
│   └── models.py             # Pydantic data models
├── data/                     # Market data
│   ├── price_feed.py         # Binance price feeds
│   ├── market_scanner.py     # Market discovery
│   └── orderbook.py          # Orderbook state
├── strategies/               # Trading strategies
│   ├── base.py               # Abstract base strategy
│   ├── momentum_scalp.py     # Strategy 1
│   ├── market_maker.py       # Strategy 2
│   └── cross_strike_arb.py   # Strategy 3
├── execution/                # Order execution
│   ├── order_manager.py      # Order lifecycle
│   ├── position_tracker.py   # Position & P&L tracking
│   └── fee_calculator.py     # Kalshi fee math
├── risk/                     # Risk management
│   └── risk_manager.py       # Position limits & kill switch
├── paper_trading/            # Paper trading
│   ├── paper_engine.py       # Fill simulation
│   └── results_tracker.py    # Results storage (SQLite + CSV)
└── tests/                    # Test suite
```

## Important Notes

- **Start in paper trading mode** (`PAPER_TRADING=true`) until you have 200+ simulated trades with positive expected value.
- The oracle for Kalshi crypto markets is **CF Benchmarks BRTI**, not Binance. Binance is used as a fast proxy.
- All maker orders use `post_only=True` to guarantee maker fee rates (0.0175 vs 0.07 taker).
- Demo API: `https://demo-api.kalshi.co` / Production: `https://api.elections.kalshi.com`
