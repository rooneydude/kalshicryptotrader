# CURSOR BUILD SPEC: Kalshi Short-Term Crypto Trading Bot

> **Instructions for the AI agent:** Build this project exactly as specified. Do not skip modules. Do not stub functions. Every function must be fully implemented. Use the demo API for development, production API for deployment. Test each module individually before integration.

---

## PROJECT OVERVIEW

A Python trading bot that runs three automated strategies on Kalshi's short-term crypto prediction markets (hourly + daily BTC/ETH/SOL contracts). The bot monitors real-time BTC/ETH/SOL spot prices, compares them against Kalshi contract prices, identifies profitable opportunities, and executes trades via the Kalshi REST API and WebSocket feeds.

### Target Markets
- **Hourly crypto:** "Bitcoin price today at Xpm EST?" — multiple strike levels per event (40-87 strikes), settles same day
- **Daily crypto:** "Bitcoin price tomorrow at 5pm EST?" — same structure, settles next day
- **15-minute:** "BTC Up or Down" — binary up/down, very low liquidity on Kalshi (deprioritize but support)
- **Assets:** BTC, ETH, SOL (primary: BTC)
- **Oracle:** CF Benchmarks Real-Time Index (BRTI) — 60-second volume-weighted average, NOT Binance

### Three Strategies
1. **Momentum Scalping** — Buy deep in-the-money (ITM) YES contracts when spot price is far above the strike but the Kalshi book hasn't repriced yet
2. **Market Making** — Post two-sided limit orders (bid + ask) on near-ATM daily strikes, capture the spread
3. **Cross-Strike Arbitrage** — Scan all strikes within an event for mathematical mispricings (monotonicity violations, parity breaks, range-sum inconsistencies)

---

## DIRECTORY STRUCTURE

```
kalshi-crypto-bot/
├── .env.example                  # Template for environment variables
├── .env                          # Actual env (gitignored)
├── .gitignore
├── requirements.txt
├── README.md
├── config.py                     # All configurable parameters
├── main.py                       # Entry point — runs all strategies
├── kalshi/
│   ├── __init__.py
│   ├── auth.py                   # RSA-PSS signing, header generation
│   ├── client.py                 # REST API client (all endpoints)
│   ├── websocket_client.py       # WebSocket client for real-time data
│   └── models.py                 # Pydantic models for API responses
├── data/
│   ├── __init__.py
│   ├── price_feed.py             # Binance + CF Benchmarks spot price feeds
│   ├── market_scanner.py         # Discover & filter crypto markets on Kalshi
│   └── orderbook.py              # Orderbook state management
├── strategies/
│   ├── __init__.py
│   ├── base.py                   # Abstract base strategy class
│   ├── momentum_scalp.py         # Strategy 1: Deep ITM momentum scalping
│   ├── market_maker.py           # Strategy 2: Two-sided quoting
│   └── cross_strike_arb.py       # Strategy 3: Cross-strike arbitrage
├── execution/
│   ├── __init__.py
│   ├── order_manager.py          # Place, cancel, amend orders
│   ├── position_tracker.py       # Track open positions and P&L
│   └── fee_calculator.py         # Kalshi fee math
├── risk/
│   ├── __init__.py
│   └── risk_manager.py           # Position limits, kill switch, daily loss
├── utils/
│   ├── __init__.py
│   ├── logger.py                 # Structured logging
│   └── fair_value.py             # Black-Scholes digital option pricer
├── tests/
│   ├── __init__.py
│   ├── test_auth.py
│   ├── test_client.py
│   ├── test_fee_calculator.py
│   ├── test_fair_value.py
│   ├── test_momentum_scalp.py
│   ├── test_market_maker.py
│   └── test_cross_strike_arb.py
└── paper_trading/
    ├── __init__.py
    ├── paper_engine.py           # Simulates order fills against real orderbook
    └── results_tracker.py        # Logs paper trade results to CSV/SQLite
```

---

## ENVIRONMENT & DEPENDENCIES

### .env.example
```env
# Kalshi API
KALSHI_API_KEY_ID=your-api-key-id
KALSHI_PRIVATE_KEY_PATH=./kalshi-key.pem
KALSHI_BASE_URL=https://demo-api.kalshi.co          # Demo
# KALSHI_BASE_URL=https://api.elections.kalshi.com   # Production
KALSHI_WS_URL=wss://demo-api.kalshi.co/trade-api/ws/v2
# KALSHI_WS_URL=wss://api.elections.kalshi.com/trade-api/ws/v2

# External price feeds
BINANCE_WS_URL=wss://stream.binance.com:9443/ws

# Trading mode
PAPER_TRADING=true    # Set to false for live trading

# Logging
LOG_LEVEL=INFO
LOG_FILE=./logs/bot.log
```

### requirements.txt
```
requests>=2.31.0
websockets>=12.0
pydantic>=2.5.0
python-dotenv>=1.0.0
cryptography>=41.0.0
scipy>=1.11.0          # For Black-Scholes (norm.cdf)
numpy>=1.25.0
aiohttp>=3.9.0         # Async HTTP for concurrent API calls
pandas>=2.1.0          # For results tracking / analysis
pytest>=7.4.0
pytest-asyncio>=0.21.0
```

---

## MODULE SPECIFICATIONS

---

### `config.py` — Configuration

Load all parameters from .env and define trading constants.

```python
"""
All configurable parameters for the trading bot.

CRITICAL CONSTANTS (from Kalshi fee schedule, effective Feb 5, 2026):
- Standard taker fee coefficient: 0.07
- Standard maker fee coefficient: 0.0175
- S&P/NASDAQ taker fee coefficient: 0.035 (NOT used for crypto)
- S&P/NASDAQ maker fee coefficient: 0.0175
- Fee formula: roundup(coefficient × contracts × price × (1 - price))
- "roundup" = round UP to the next cent ($0.01)
- Prices are in dollars (50 cents = 0.50)
- No settlement fees, no membership fees

CRYPTO MARKET TICKERS:
- BTC series: starts with "KXBTC" (e.g., KXBTC-26FEB14-T70000 for "BTC above $70,000")
- ETH series: starts with "KXETH"
- SOL series: starts with "KXSOL"
- BTC up/down 15-min: starts with "KXBTCUD"
"""

# --- Position Sizing ---
MAX_SINGLE_TRADE_PCT = 0.10       # 10% of capital per trade
MAX_PER_STRIKE_PCT = 0.15         # 15% of capital per strike
MAX_PER_EVENT_PCT = 0.30          # 30% of capital per event
MAX_TOTAL_EXPOSURE_PCT = 0.75     # 75% of capital total
CASH_BUFFER_PCT = 0.25            # Always keep 25% in cash
DAILY_LOSS_LIMIT_PCT = 0.05       # Stop trading after 5% daily loss
WEEKLY_LOSS_LIMIT_PCT = 0.10      # Review after 10% weekly loss

# --- Strategy 1: Momentum Scalp ---
SCALP_MIN_YES_FAIR_VALUE = 0.90   # Only scalp strikes where fair value ≥ 90¢
SCALP_MAX_ENTRY_PRICE = 0.93      # Only buy if YES price ≤ 93¢ (gap of ≥ 7¢ to fair)
SCALP_MIN_EDGE_CENTS = 3          # Minimum 3¢ edge after fees
SCALP_MIN_BOOK_DEPTH = 20         # Minimum 20 contracts at best ask
SCALP_MAX_TIME_TO_SETTLE_HOURS = 8
SCALP_PREFER_MAKER = True         # Use post_only when possible

# --- Strategy 2: Market Making ---
MM_SPREAD_CENTS = 4               # Target 4¢ spread (2¢ each side of fair value)
MM_MAX_NET_POSITION = 500         # Max 500 contracts net long or short
MM_HEDGE_TRIGGER = 200            # Hedge when net > 200 contracts
MM_QUOTE_SIZE = 50                # 50 contracts per quote
MM_REQUOTE_INTERVAL_SEC = 10      # Refresh quotes every 10 seconds
MM_CANCEL_ON_MOVE_PCT = 0.02      # Cancel all if BTC moves > 2% in 30 min
MM_MIN_VOLUME_24H = 10000         # Only make markets with 24h vol > 10K

# --- Strategy 3: Cross-Strike Arb ---
ARB_MIN_PROFIT_CENTS = 2          # Minimum 2¢ profit per contract after fees
ARB_MAX_CONTRACTS = 100           # Max contracts per arb leg
ARB_SCAN_INTERVAL_SEC = 15        # Scan for arbs every 15 seconds

# --- Polling & Rate Limits ---
ORDERBOOK_POLL_INTERVAL_SEC = 2   # Poll orderbooks every 2 seconds
MARKET_SCAN_INTERVAL_SEC = 60     # Scan for new markets every 60 seconds
PRICE_FEED_RECONNECT_SEC = 5      # Reconnect WebSocket after 5 seconds
```

---

### `kalshi/auth.py` — Authentication

```
PURPOSE: Generate RSA-PSS signed headers for every Kalshi API request.

IMPLEMENTATION:
- Load RSA private key from PEM file (path from .env)
- Sign: message = "{timestamp_ms}{HTTP_METHOD}{path_without_query}"
- Use RSA-PSS with SHA256, salt_length=PSS.DIGEST_LENGTH
- Return base64-encoded signature

FUNCTION: load_private_key(file_path: str) -> RSAPrivateKey
  - Read PEM file, deserialize with cryptography library
  - Raise clear error if file not found or invalid format

FUNCTION: sign_request(private_key: RSAPrivateKey, timestamp_ms: str, method: str, path: str) -> str
  - Strip query params from path (split on '?', take first part)
  - Construct message: f"{timestamp_ms}{method}{path_without_query}"
  - Sign with RSA-PSS, return base64 encoded string

FUNCTION: get_auth_headers(private_key: RSAPrivateKey, api_key_id: str, method: str, path: str) -> dict
  - Generate current timestamp in milliseconds: str(int(time.time() * 1000))
  - Call sign_request
  - Return dict:
    {
      "KALSHI-ACCESS-KEY": api_key_id,
      "KALSHI-ACCESS-SIGNATURE": signature,
      "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
      "Content-Type": "application/json"
    }
```

---

### `kalshi/client.py` — REST API Client

```
PURPOSE: Typed wrapper around all Kalshi REST API endpoints used by the bot.

BASE_URL: from config (demo or production)
API_PREFIX: "/trade-api/v2"

All methods should:
- Call get_auth_headers() for authenticated endpoints
- Handle HTTP errors (4xx, 5xx) with retries + exponential backoff
- Parse responses into Pydantic models (from models.py)
- Log every request and response at DEBUG level
- Respect rate limits (implement token bucket or simple sleep)

ENDPOINTS TO IMPLEMENT:

--- Public (no auth) ---

GET /trade-api/v2/exchange/status
  → get_exchange_status() -> ExchangeStatus

GET /trade-api/v2/markets
  → get_markets(limit=100, cursor=None, event_ticker=None, series_ticker=None, 
                 status=None, max_close_ts=None, min_close_ts=None, tickers=None) -> GetMarketsResponse
  NOTES:
  - Use for discovering crypto markets. Filter by series_ticker (e.g., "KXBTC")
  - status="open" filters to tradeable markets (API returns "active" in response)
  - Paginate with cursor from previous response
  - limit max 1000

GET /trade-api/v2/markets/{ticker}
  → get_market(ticker: str) -> Market

GET /trade-api/v2/markets/{ticker}/orderbook
  → get_orderbook(ticker: str, depth: int = 20) -> OrderBook
  NOTES:
  - Returns YES bids and NO bids only (no asks)
  - A YES bid at 7¢ = NO ask at 93¢
  - Use orderbook_fp for fixed-point counts (preferred over legacy integer)

GET /trade-api/v2/events/{event_ticker}
  → get_event(event_ticker: str) -> Event

--- Authenticated ---

GET /trade-api/v2/portfolio/balance
  → get_balance() -> Balance

GET /trade-api/v2/portfolio/positions
  → get_positions(event_ticker=None) -> Positions

GET /trade-api/v2/portfolio/orders
  → get_orders(ticker=None, status=None) -> Orders

POST /trade-api/v2/portfolio/orders
  → create_order(order: CreateOrderRequest) -> Order
  BODY FIELDS:
    ticker: str           # Market ticker (e.g., "KXBTC-26FEB14-T70000")
    side: "yes" | "no"
    action: "buy" | "sell"
    client_order_id: str  # UUID for deduplication
    count: int            # Number of contracts (integer)
    type: "limit"         # Always use limit orders
    yes_price: int        # Price in cents (50 = $0.50). Use this OR no_price.
    no_price: int         # Price in cents for NO side
    post_only: bool       # If true, order is rejected if it would immediately match (maker only)
    time_in_force: str    # Optional: "fill_or_kill", "immediate_or_cancel", or omit for GTC
    buy_max_cost: int     # Optional: max total cost in cents
    cancel_order_on_pause: bool  # Cancel if market pauses (default true for safety)

DELETE /trade-api/v2/portfolio/orders/{order_id}
  → cancel_order(order_id: str) -> CancelResponse

POST /trade-api/v2/portfolio/orders/{order_id}/amend
  → amend_order(order_id: str, data: AmendOrderRequest) -> Order

POST /trade-api/v2/portfolio/orders/batched
  → batch_create_orders(orders: list[CreateOrderRequest]) -> BatchResponse

DELETE /trade-api/v2/portfolio/orders/batched
  → batch_cancel_orders(order_ids: list[str]) -> BatchCancelResponse
```

---

### `kalshi/websocket_client.py` — WebSocket Client

```
PURPOSE: Real-time data from Kalshi via WebSocket for orderbook updates, fills, and ticker changes.

CONNECTION: wss://api.elections.kalshi.com/trade-api/ws/v2 (or demo equivalent)

AUTH: Same RSA-PSS headers as REST, passed as WebSocket headers on connect.
  - Method: "GET"
  - Path: "/trade-api/ws/v2"

CHANNELS (subscribe after connecting):

Public (no auth required):
  - "ticker" or "ticker_v2": Real-time yes_bid, yes_ask, last_price for markets
  - "trade": Real-time executed trades
  - "market_lifecycle_v2": Market open/close/settle events

Private (auth required):
  - "orderbook_delta": Orderbook changes for subscribed markets
  - "fill": Your order fills
  - "order_group_updates": Order group status changes

SUBSCRIPTION MESSAGE FORMAT:
{
  "id": <incrementing_int>,
  "cmd": "subscribe",
  "params": {
    "channels": ["ticker"],
    "market_tickers": ["KXBTC-26FEB14-T70000"]  // optional: filter to specific markets
  }
}

IMPLEMENTATION:
- Use asyncio + websockets library
- Auto-reconnect on disconnect with exponential backoff
- Parse incoming messages by "type" field:
  - "ticker" → update local price state
  - "orderbook_snapshot" → replace local orderbook
  - "orderbook_delta" → apply incremental update
  - "fill" → notify execution engine
  - "error" → log and handle
- Emit events/callbacks that strategies can subscribe to
- Heartbeat: send ping every 30 seconds
```

---

### `kalshi/models.py` — Pydantic Models

```
PURPOSE: Type-safe data models for all API responses.

Define Pydantic BaseModel classes for:
- Market: ticker, event_ticker, title, status, yes_bid, yes_ask, no_bid, no_ask,
          last_price, volume, volume_24h, open_time, close_time, expiration_time,
          open_interest, result, can_close_early, settlement_timer_seconds
          (Include both cent and dollar fields; prefer dollar fields)
- Event: event_ticker, title, category, markets (list), status
- OrderBook: yes bids list[[price_dollars, quantity]], no bids list[[price_dollars, quantity]]
- Order: order_id, client_order_id, ticker, side, action, type, status, yes_price, no_price,
         created_time, expiration_time, remaining_count, queue_position
- Balance: balance (cents), available_balance
- Position: market_ticker, market_exposure, total_traded, realized_pnl, 
            resting_orders_count, fees_paid, position (yes count), position_cost
- Fill: trade_id, order_id, ticker, side, action, count, yes_price, no_price, 
        created_time, is_taker
- CreateOrderRequest: ticker, side, action, client_order_id, count, type, 
                      yes_price (cents), post_only, cancel_order_on_pause
```

---

### `data/price_feed.py` — External Price Feeds

```
PURPOSE: Real-time BTC/ETH/SOL spot prices from Binance WebSocket + CF Benchmarks.

The Kalshi oracle is CF Benchmarks BRTI (not Binance). However, Binance updates faster
and is a constituent of BRTI. Monitoring both lets us detect when they diverge.

CLASS: PriceFeed
  - Connects to Binance WebSocket: wss://stream.binance.com:9443/ws
  - Subscribes to streams: btcusdt@trade, ethusdt@trade, solusdt@trade
  - Maintains latest price, 1-min VWAP, and 30-min rolling volatility per asset
  
  PROPERTIES:
    btc_price: float          # Latest Binance BTCUSDT price
    eth_price: float
    sol_price: float
    btc_vwap_1m: float        # 1-minute VWAP (approximates BRTI)
    btc_volatility_30m: float # 30-min annualized realized volatility
    last_update: datetime
  
  METHODS:
    async start(): Connect and begin streaming
    async stop(): Disconnect gracefully
    get_price(asset: str) -> float
    get_volatility(asset: str) -> float
    get_vwap(asset: str, window_seconds: int = 60) -> float

IMPORTANT: BRTI uses a 60-second volume-weighted average across multiple exchanges.
The 1-minute VWAP from Binance is a reasonable proxy but NOT identical.
When precision matters (near settlement), note this in logs.
```

---

### `data/market_scanner.py` — Market Discovery

```
PURPOSE: Find and filter relevant crypto markets on Kalshi.

CLASS: MarketScanner
  METHODS:
  
  scan_crypto_markets(timeframe: str = "all") -> list[Market]:
    - Call GET /markets with series_ticker filtering
    - Series tickers to scan: KXBTC, KXETH, KXSOL, KXBTCUD (15-min up/down)
    - Filter: status="open", sort by close_time ascending (soonest first)
    - Optionally filter by timeframe: "15min", "hourly", "daily"
    - Return list of Market objects
  
  get_event_strikes(event_ticker: str) -> list[Market]:
    - Get all markets for a given event (all strike levels)
    - Sort by strike price ascending
    - Extract strike price from ticker or title (parse "$70,000 or above" → 70000)
    - Return sorted list
  
  parse_strike_price(market: Market) -> float | None:
    - Parse the strike price from market title or ticker
    - Examples: "Bitcoin price today at 5pm EST?" with subtitle "$70,500 or above" → 70500.0
    - Handle "above", "below", "between" variants
    - Return None if unparseable (e.g., up/down markets)
  
  classify_market_type(market: Market) -> str:
    - Return one of: "above", "below", "range", "up_down"
    - Based on market title/subtitle parsing

  find_active_events(asset: str = "BTC") -> list[Event]:
    - Get all active events for an asset
    - Group markets by event_ticker
    - Sort events by expiration (soonest first)
```

---

### `data/orderbook.py` — Orderbook State

```
PURPOSE: Maintain a local representation of orderbooks for markets we're watching.

CLASS: OrderBookManager
  - Stores orderbook snapshots keyed by market ticker
  - Updates from REST polling or WebSocket deltas
  
  METHODS:
  update_from_rest(ticker: str, orderbook: OrderBook): 
    - Replace local state with fresh REST data
  
  update_from_delta(ticker: str, delta: dict):
    - Apply incremental WebSocket orderbook_delta
  
  get_best_yes_bid(ticker: str) -> tuple[float, int] | None:
    - Returns (price_dollars, quantity) of highest YES bid
  
  get_best_yes_ask(ticker: str) -> tuple[float, int] | None:
    - In Kalshi, YES ask = (1.00 - best NO bid price)
    - Returns (price_dollars, quantity)
  
  get_spread(ticker: str) -> float | None:
    - yes_ask - yes_bid in dollars
  
  get_depth(ticker: str, side: str, levels: int = 5) -> list[tuple[float, int]]:
    - Return top N price levels with quantities
  
  get_total_volume_at_price(ticker: str, side: str, price: float) -> int:
    - Total contracts available at a specific price
```

---

### `execution/fee_calculator.py` — Fee Math

```
PURPOSE: Exact Kalshi fee calculations. Every trade decision must account for fees.

CONSTANTS:
  TAKER_COEFF = 0.07        # Standard markets
  MAKER_COEFF = 0.0175      # Standard markets
  INDEX_TAKER_COEFF = 0.035  # S&P/NASDAQ only (not crypto)
  INDEX_MAKER_COEFF = 0.0175 # S&P/NASDAQ only

FUNCTION: calculate_fee(contracts: int, price_dollars: float, is_maker: bool = False) -> float
  - coeff = MAKER_COEFF if is_maker else TAKER_COEFF
  - fee = coeff * contracts * price_dollars * (1 - price_dollars)
  - Return math.ceil(fee * 100) / 100  # Round UP to next cent
  - CRITICAL: This "round up to next cent" behavior must be exact.
    math.ceil(fee * 100) / 100 handles this.

FUNCTION: calculate_net_profit(
    buy_price: float,     # Price you pay per contract (dollars)
    sell_price: float,    # Price you receive (or $1.00 at settlement)
    contracts: int,
    is_maker_buy: bool,
    is_maker_sell: bool
) -> float:
  - gross = (sell_price - buy_price) * contracts
  - buy_fee = calculate_fee(contracts, buy_price, is_maker_buy)
  - sell_fee = calculate_fee(contracts, sell_price, is_maker_sell)
  - Return gross - buy_fee - sell_fee

FUNCTION: min_profitable_spread(price_dollars: float, contracts: int, is_maker: bool = True) -> float:
  - Calculate the minimum spread (in dollars) needed to be profitable after fees
  - Both sides assumed same maker/taker status
  - Return the spread in dollars (e.g., 0.02 = 2¢)

TESTS (test_fee_calculator.py):
  - calculate_fee(1, 0.50, False) == 0.02     # 0.07 * 1 * 0.50 * 0.50 = 0.0175 → roundup = 0.02
  - calculate_fee(100, 0.50, False) == 1.75   # 0.07 * 100 * 0.50 * 0.50 = 1.75
  - calculate_fee(100, 0.95, False) == 0.34   # 0.07 * 100 * 0.95 * 0.05 = 0.3325 → 0.34
  - calculate_fee(100, 0.95, True) == 0.09    # 0.0175 * 100 * 0.95 * 0.05 = 0.083125 → 0.09
  - calculate_fee(100, 0.10, False) == 0.63   # 0.07 * 100 * 0.10 * 0.90 = 0.63
  - calculate_fee(1, 0.01, False) == 0.01     # 0.07 * 1 * 0.01 * 0.99 = 0.000693 → 0.01
```

---

### `utils/fair_value.py` — Fair Value Calculator

```
PURPOSE: Calculate theoretical fair value of Kalshi crypto binary contracts.

Uses Black-Scholes digital option pricing (cash-or-nothing call for "above" contracts).

FUNCTION: binary_call_price(
    spot: float,          # Current BTC price (e.g., 70500.0)
    strike: float,        # Contract strike (e.g., 68750.0)
    vol: float,           # Annualized volatility (e.g., 0.65 for 65%)
    time_years: float     # Time to expiration in years (6 hours = 6/8760)
) -> float:
  - d2 = (ln(spot/strike) + (0.5 * vol² * time_years) - (vol² * time_years)) / (vol * sqrt(time_years))
  - Simplified: d2 = (ln(spot/strike) - 0.5 * vol² * time_years) / (vol * sqrt(time_years))
  - Return norm.cdf(d2)  # from scipy.stats
  - This gives probability that price finishes above strike
  - Risk-free rate assumed 0 for short durations (hours)

FUNCTION: binary_put_price(spot, strike, vol, time_years) -> float:
  - Return 1.0 - binary_call_price(spot, strike, vol, time_years)

FUNCTION: hours_to_years(hours: float) -> float:
  - Return hours / 8760.0

FUNCTION: calculate_fair_value(
    spot: float,
    strike: float,
    vol: float,
    hours_to_expiry: float,
    market_type: str = "above"   # "above", "below", or "range"
) -> float:
  - Dispatch to binary_call_price or binary_put_price
  - For "range" (between X and Y): binary_call_price(X) - binary_call_price(Y)
  - Return fair value in dollars (0.0 to 1.0)

TESTS:
  - BTC at 70000, strike 68000, vol 0.65, 6 hours → should be ~0.85-0.95 (deep ITM)
  - BTC at 70000, strike 70000, vol 0.65, 6 hours → should be ~0.50 (ATM)
  - BTC at 70000, strike 72000, vol 0.65, 6 hours → should be ~0.15-0.25 (OTM)
  - As time → 0, deep ITM → 1.0, deep OTM → 0.0
```

---

### `strategies/base.py` — Base Strategy

```
PURPOSE: Abstract base class all strategies inherit from.

CLASS: BaseStrategy(ABC)
  __init__(self, client: KalshiClient, price_feed: PriceFeed, risk_manager: RiskManager,
           order_manager: OrderManager, position_tracker: PositionTracker, config: dict)
  
  @abstractmethod
  async scan(self) -> list[TradeSignal]:
    """Scan for opportunities. Return list of trade signals."""
  
  @abstractmethod
  async execute(self, signals: list[TradeSignal]):
    """Execute trade signals after risk checks."""
  
  async run_once(self):
    """Single iteration: scan → filter → risk check → execute."""
    signals = await self.scan()
    approved = self.risk_manager.filter_signals(signals)
    if approved:
      await self.execute(approved)

DATACLASS: TradeSignal
  strategy: str           # "momentum_scalp", "market_maker", "cross_strike_arb"
  ticker: str             # Market ticker
  side: str               # "yes" or "no"
  action: str             # "buy" or "sell"
  price_cents: int        # Limit price in cents
  contracts: int          # Number of contracts
  edge_cents: float       # Expected edge in cents after fees
  confidence: float       # 0.0 to 1.0
  post_only: bool         # Use maker order?
  reason: str             # Human-readable explanation
```

---

### `strategies/momentum_scalp.py` — Strategy 1

```
PURPOSE: Buy deep ITM YES contracts when spot price is far above the strike but the
Kalshi orderbook hasn't fully repriced.

CLASS: MomentumScalpStrategy(BaseStrategy)

  async scan(self) -> list[TradeSignal]:
    1. Get current BTC/ETH/SOL spot price from price_feed
    2. Get all active hourly + daily crypto events from market_scanner
    3. For each event, get all strike-level markets
    4. For each "above" market:
       a. Parse the strike price
       b. Calculate fair value using fair_value.calculate_fair_value()
       c. Get the current best YES ask from orderbook
       d. Calculate edge = fair_value - yes_ask_price - estimated_fees
       e. IF:
          - fair_value >= SCALP_MIN_YES_FAIR_VALUE (0.90)
          - yes_ask <= SCALP_MAX_ENTRY_PRICE (0.93)  
          - edge >= SCALP_MIN_EDGE_CENTS / 100 (0.03)
          - book depth at ask >= SCALP_MIN_BOOK_DEPTH (20)
          - time to settlement <= SCALP_MAX_TIME_TO_SETTLE_HOURS
       THEN: Generate TradeSignal(side="yes", action="buy", post_only=True)
    5. Sort signals by edge descending, return top 5

  async execute(self, signals: list[TradeSignal]):
    For each signal:
    1. Risk check: does this violate position limits?
    2. Place limit order (post_only=True for maker fees)
       - Set price 1¢ above best bid (to get queue priority while staying maker)
    3. Log the trade attempt
    4. Monitor fill via WebSocket "fill" channel
    5. If not filled within 60 seconds, cancel and re-evaluate
```

---

### `strategies/market_maker.py` — Strategy 2

```
PURPOSE: Post two-sided quotes on near-ATM daily BTC strikes. Capture bid-ask spread.

CLASS: MarketMakerStrategy(BaseStrategy)

  select_markets(self) -> list[str]:
    1. Get active daily BTC events
    2. Find the ATM strike (closest to current BTC spot)
    3. Also select 1 strike above and 1 below (3 markets total)
    4. Filter: volume_24h >= MM_MIN_VOLUME_24H
    5. Return tickers of selected markets

  async scan(self) -> list[TradeSignal]:
    1. For each selected market:
       a. Get current fair value from fair_value calculator
       b. Get current orderbook
       c. Calculate bid_price = fair_value - (MM_SPREAD_CENTS / 2 / 100)
       d. Calculate ask_price = fair_value + (MM_SPREAD_CENTS / 2 / 100)
       e. Clamp to [0.01, 0.99]
       f. Check: is net profit positive after maker fees on both sides?
       g. Check: current net position < MM_MAX_NET_POSITION
       h. Generate two signals: buy YES at bid, sell YES at ask (via buy NO at 1-ask)
    2. Return all signals

  async execute(self, signals: list[TradeSignal]):
    1. Cancel all existing resting orders for these markets (fresh quotes)
    2. Place new limit orders (post_only=True)
    3. Set requote timer (MM_REQUOTE_INTERVAL_SEC)

  async manage_inventory(self):
    """Called periodically to check and hedge inventory."""
    1. For each market we're making:
       a. Get net position from position_tracker
       b. If abs(net_position) > MM_HEDGE_TRIGGER:
          - Find adjacent strike
          - Place offsetting order to reduce exposure
       c. If abs(net_position) > MM_MAX_NET_POSITION:
          - Cancel all quotes on this market
          - Aggressively flatten (use taker if necessary)
    
  async check_kill_switch(self):
    """Cancel everything if BTC moves too fast."""
    1. Compare BTC price now vs 30 minutes ago
    2. If abs(pct_change) > MM_CANCEL_ON_MOVE_PCT:
       - Cancel ALL resting orders across all markets
       - Log warning
       - Wait 5 minutes before re-quoting
```

---

### `strategies/cross_strike_arb.py` — Strategy 3

```
PURPOSE: Scan all strikes within a crypto event for mathematical mispricings.

CLASS: CrossStrikeArbStrategy(BaseStrategy)

  async scan(self) -> list[TradeSignal]:
    For each active crypto event:
      strikes = market_scanner.get_event_strikes(event_ticker)
      Sort strikes by strike price ascending
      
      # --- Check 1: Monotonicity ---
      # P(above $68K) should always >= P(above $69K)
      for i in range(len(strikes) - 1):
        if strikes[i] and strikes[i+1] are both "above" type:
          low_strike_yes_ask = orderbook.get_best_yes_ask(strikes[i].ticker)
          high_strike_yes_bid = orderbook.get_best_yes_bid(strikes[i+1].ticker)
          if high_strike_yes_bid > low_strike_yes_ask:
            # Higher strike priced higher than lower strike → arb!
            edge = high_strike_yes_bid - low_strike_yes_ask
            fees = fee_calculator.calculate_fee(...) * 2
            if edge > fees + ARB_MIN_PROFIT_CENTS/100:
              Generate signal: BUY low_strike YES, SELL high_strike YES
      
      # --- Check 2: YES + NO parity ---
      # For each strike: buying YES + NO should cost ~$1.00
      for strike in strikes:
        yes_ask = orderbook.get_best_yes_ask(strike.ticker)
        no_ask = orderbook.get_best_no_ask(strike.ticker)  # = 1 - best_yes_bid
        if yes_ask and no_ask:
          total_cost = yes_ask + no_ask
          if total_cost < 1.00:
            gap = 1.00 - total_cost
            fees = calculate fees for buying both
            if gap > fees + ARB_MIN_PROFIT_CENTS/100:
              Generate signal: BUY YES + BUY NO on same strike

      # --- Check 3: Range sum ---
      # If event has range markets, sum of all range YES prices should ≈ $1.00
      range_markets = [m for m in strikes if classify_market_type(m) == "range"]
      if range_markets:
        total = sum(get_best_yes_ask(r.ticker) for r in range_markets if ask exists)
        if total < 0.95:  # Significantly under $1.00
          # Buy YES on all ranges — guaranteed $1.00 payout
          # Only profitable if (1.00 - total) > total fees
          ...

  async execute(self, signals: list[TradeSignal]):
    For arb signals, BOTH legs must execute or neither:
    1. Place both orders simultaneously using batch_create_orders
    2. If one leg fills but other doesn't within 30 seconds, cancel unfilled leg
    3. Log partial fill as risk event
```

---

### `execution/order_manager.py` — Order Management

```
PURPOSE: Centralized order placement, tracking, and lifecycle management.

CLASS: OrderManager
  __init__(self, client: KalshiClient, paper_mode: bool = True)
  
  async place_order(self, signal: TradeSignal) -> Order | None:
    - Convert TradeSignal to CreateOrderRequest
    - Generate client_order_id (UUID)
    - If paper_mode: simulate fill against current orderbook, return mock Order
    - If live: call client.create_order()
    - Track order in local state
    - Return Order object or None if rejected

  async cancel_order(self, order_id: str) -> bool
  async cancel_all_orders(self, ticker: str = None) -> int  # Returns count cancelled
  async amend_order(self, order_id: str, new_price: int) -> Order | None
  
  async batch_place(self, signals: list[TradeSignal]) -> list[Order]:
    - Use batch_create_orders for atomicity
  
  get_open_orders(self, ticker: str = None) -> list[Order]
  get_order_status(self, order_id: str) -> Order
```

---

### `execution/position_tracker.py` — Position Tracking

```
PURPOSE: Track all open positions, realized + unrealized P&L, and trade history.

CLASS: PositionTracker
  
  PROPERTIES:
    positions: dict[str, PositionState]  # ticker → position state
    total_realized_pnl: float
    total_unrealized_pnl: float
    total_fees_paid: float
    trade_count_today: int
    daily_pnl: float
  
  DATACLASS: PositionState
    ticker: str
    net_contracts: int       # Positive = long YES, negative = short YES (long NO)
    avg_entry_price: float
    current_market_price: float
    unrealized_pnl: float
    realized_pnl: float
    fees_paid: float
  
  METHODS:
    update_from_fill(fill: Fill): Update position based on a trade execution
    update_market_prices(prices: dict[str, float]): Mark to market
    sync_with_exchange(): Call GET /portfolio/positions to reconcile local vs exchange state
    get_net_exposure() -> float: Total dollars at risk across all positions
    get_net_position(ticker: str) -> int: Net contracts for a specific market
    export_trades_csv(filepath: str): Export all trades for analysis
```

---

### `risk/risk_manager.py` — Risk Management

```
PURPOSE: Enforce all position limits and safety checks. 
This is the ONLY module that can block trades. All strategies must pass through it.

CLASS: RiskManager
  __init__(self, position_tracker: PositionTracker, balance: float, config: dict)
  
  METHODS:
  
  filter_signals(self, signals: list[TradeSignal]) -> list[TradeSignal]:
    For each signal, check ALL of the following. Reject if any fails:
    1. Single trade size ≤ MAX_SINGLE_TRADE_PCT of capital
    2. Existing exposure on this strike + new trade ≤ MAX_PER_STRIKE_PCT
    3. Existing exposure on this event + new trade ≤ MAX_PER_EVENT_PCT
    4. Total exposure + new trade ≤ MAX_TOTAL_EXPOSURE_PCT
    5. Cash after trade ≥ CASH_BUFFER_PCT of original capital
    6. Daily P&L loss hasn't exceeded DAILY_LOSS_LIMIT_PCT
    7. Weekly P&L loss hasn't exceeded WEEKLY_LOSS_LIMIT_PCT
    Return only approved signals. Log rejection reasons.
  
  check_kill_switch(self) -> bool:
    - Return True if trading should stop
    - Conditions: daily loss exceeded, exchange status not "open", etc.
  
  should_flatten_all(self) -> bool:
    - Return True if all positions should be closed immediately
    - Triggered by: daily loss > 2x DAILY_LOSS_LIMIT, or manual override
  
  get_available_capital(self) -> float:
    - Current balance - (capital reserved for open positions) - cash buffer
```

---

### `main.py` — Entry Point

```
PURPOSE: Initialize all components, run the main event loop.

FLOW:
1. Load config from .env
2. Initialize KalshiClient (auth, base URL)
3. Initialize PriceFeed (connect to Binance WebSocket)
4. Initialize KalshiWebSocketClient (connect to Kalshi WS)
5. Initialize MarketScanner, OrderBookManager, FeeCalculator
6. Initialize OrderManager (paper or live based on PAPER_TRADING env var)
7. Initialize PositionTracker, RiskManager
8. Initialize all three strategies
9. Start main loop:

MAIN LOOP (runs indefinitely):
  Every 2 seconds:
    - Poll orderbooks for watched markets (REST fallback if WS not connected)
    - Update position tracker market prices
  
  Every 10 seconds:
    - Run MarketMaker.scan() → execute()
    - Run MarketMaker.manage_inventory()
    - Run MarketMaker.check_kill_switch()
  
  Every 15 seconds:
    - Run CrossStrikeArb.scan() → execute()
  
  Every 30 seconds:
    - Run MomentumScalp.scan() → execute()
  
  Every 60 seconds:
    - Run MarketScanner to discover new markets
    - Sync positions with exchange
    - Log portfolio summary
    - Check risk manager kill switch
  
  Every 300 seconds (5 min):
    - Export trade log to CSV
    - Log P&L summary

GRACEFUL SHUTDOWN (on SIGINT/SIGTERM):
  1. Cancel all resting orders
  2. Close WebSocket connections
  3. Export final trade log
  4. Print session summary (total P&L, trade count, win rate)
```

---

## CRITICAL IMPLEMENTATION NOTES

1. **Always use `post_only=True`** for maker orders. Maker fees are 4x cheaper (0.0175 vs 0.07). If a post_only order would immediately match, it's rejected — that's the desired behavior.

2. **Prices in the Kalshi API are in CENTS** (integer). yes_price=50 means $0.50. But orderbook_fp and dollar fields use decimal strings. Be consistent: convert everything to dollars internally, convert to cents only when calling the API.

3. **Orderbook only shows bids.** A YES bid at 70¢ = NO ask at 30¢. There are no explicit ask arrays. To get the YES ask: find the best NO bid, subtract from 100¢.

4. **client_order_id must be unique** (UUID). Kalshi uses it for deduplication. Never reuse.

5. **Demo API** is at `https://demo-api.kalshi.co`. Production is `https://api.elections.kalshi.com`. The "elections" subdomain is for ALL markets, not just elections.

6. **Rate limits** exist but exact tiers aren't public for standard users. Implement exponential backoff on 429 responses. Don't poll faster than every 1 second per endpoint.

7. **cancel_order_on_pause=true** should be set on all market-making orders. If Kalshi pauses a market (e.g., for a data issue), your resting orders auto-cancel instead of filling at stale prices.

8. **Fee rounding is UP to the next cent.** Use `math.ceil(fee * 100) / 100`, not `round()`.

9. **CF Benchmarks BRTI** is the oracle, not Binance. The 1-minute VWAP from Binance is a proxy. This matters near settlement.

10. **Start in paper trading mode.** Set `PAPER_TRADING=true` until you have 200+ simulated trades with positive expected value.
