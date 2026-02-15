"""
Pydantic models for all Kalshi API request and response types.

All prices are stored in dollars (float) internally.
Convert to cents (int) only at the API boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Exchange
# ---------------------------------------------------------------------------

class ExchangeStatus(BaseModel):
    """GET /exchange/status response."""
    exchange_status: str  # "open", "closed", etc.
    trading_active: bool = True


# ---------------------------------------------------------------------------
# Market
# ---------------------------------------------------------------------------

class Market(BaseModel):
    """A single Kalshi market (contract)."""
    ticker: str
    event_ticker: str = ""
    title: str = ""
    subtitle: str = ""
    status: str = ""  # "active", "closed", "settled"
    yes_bid: float = 0.0  # Best YES bid in dollars
    yes_ask: float = 0.0  # Best YES ask in dollars
    no_bid: float = 0.0
    no_ask: float = 0.0
    last_price: float = 0.0
    volume: int = 0
    volume_24h: int = 0
    open_time: str = ""
    close_time: str = ""
    expiration_time: str = ""
    open_interest: int = 0
    result: str = ""  # "yes", "no", "" (unsettled)
    can_close_early: bool = False
    settlement_timer_seconds: int = 0

    # Fields that may or may not be present depending on endpoint
    category: str = ""
    series_ticker: str = ""
    rules_primary: str = ""
    rules_secondary: str = ""

    # 15-min up/down market fields
    yes_sub_title: str = ""
    no_sub_title: str = ""
    floor_strike: float | None = None
    cap_strike: float | None = None
    market_type: str = ""  # "binary" for 15-min up/down
    strike_type: str = ""  # "greater_or_equal" for 15-min up/down

    model_config = ConfigDict(extra="allow")


class GetMarketsResponse(BaseModel):
    """GET /markets response."""
    markets: list[Market] = Field(default_factory=list)
    cursor: str = ""


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

class Event(BaseModel):
    """A Kalshi event (group of related markets/strikes)."""
    event_ticker: str
    title: str = ""
    category: str = ""
    status: str = ""
    markets: list[Market] = Field(default_factory=list)
    series_ticker: str = ""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Orderbook
# ---------------------------------------------------------------------------

class OrderBookLevel(BaseModel):
    """A single price level in the orderbook."""
    price: float  # Price in dollars
    quantity: int  # Number of contracts


class OrderBook(BaseModel):
    """
    GET /markets/{ticker}/orderbook response.

    Kalshi orderbooks only contain bids (YES bids and NO bids).
    YES ask = 1.00 - best NO bid price.
    """
    ticker: str = ""
    yes_bids: list[list[float]] = Field(default_factory=list)  # [[price, qty], ...]
    no_bids: list[list[float]] = Field(default_factory=list)   # [[price, qty], ...]

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------

class Order(BaseModel):
    """A single order on Kalshi."""
    order_id: str = ""
    client_order_id: str = ""
    ticker: str = ""
    event_ticker: str = ""
    side: str = ""  # "yes" or "no"
    action: str = ""  # "buy" or "sell"
    type: str = ""  # "limit"
    status: str = ""  # "resting", "canceled", "executed", "pending"
    yes_price: int = 0  # Price in cents
    no_price: int = 0   # Price in cents
    created_time: str = ""
    expiration_time: str | None = None  # Can be null from API
    remaining_count: int = 0
    queue_position: int = 0
    count: int = 0  # Original order size

    model_config = ConfigDict(extra="allow")


class GetOrdersResponse(BaseModel):
    """GET /portfolio/orders response."""
    orders: list[Order] = Field(default_factory=list)
    cursor: str = ""


class CancelResponse(BaseModel):
    """DELETE /portfolio/orders/{order_id} response."""
    order: Order | None = None
    reduced_by: int = 0

    model_config = ConfigDict(extra="allow")


class BatchResponse(BaseModel):
    """POST /portfolio/orders/batched response."""
    orders: list[Order] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class BatchCancelResponse(BaseModel):
    """DELETE /portfolio/orders/batched response."""
    orders: list[Order] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class AmendOrderRequest(BaseModel):
    """Request body for amending an order."""
    count: int | None = None
    yes_price: int | None = None
    no_price: int | None = None


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------

class Balance(BaseModel):
    """GET /portfolio/balance response."""
    balance: int = 0  # Balance in cents
    available_balance: int | None = None  # Available to trade, in cents
    portfolio_value: int = 0  # Portfolio value in cents

    @property
    def balance_dollars(self) -> float:
        return self.balance / 100.0

    @property
    def available_balance_dollars(self) -> float:
        # If available_balance not returned, assume full balance is available
        if self.available_balance is not None:
            return self.available_balance / 100.0
        return self.balance_dollars

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

class Position(BaseModel):
    """A single market position."""
    market_ticker: str = ""
    event_ticker: str = ""
    market_exposure: int = 0  # Cents
    total_traded: int = 0
    realized_pnl: int = 0  # Cents
    resting_orders_count: int = 0
    fees_paid: int = 0  # Cents
    position: int = 0  # YES contract count (positive = long YES)
    position_cost: int = 0  # Cents

    @property
    def realized_pnl_dollars(self) -> float:
        return self.realized_pnl / 100.0

    @property
    def fees_paid_dollars(self) -> float:
        return self.fees_paid / 100.0

    model_config = ConfigDict(extra="allow")


class GetPositionsResponse(BaseModel):
    """GET /portfolio/positions response."""
    market_positions: list[Position] = Field(default_factory=list)
    cursor: str = ""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Fill
# ---------------------------------------------------------------------------

class Fill(BaseModel):
    """A trade execution (fill)."""
    trade_id: str = ""
    order_id: str = ""
    ticker: str = ""
    side: str = ""  # "yes" or "no"
    action: str = ""  # "buy" or "sell"
    count: int = 0
    yes_price: int = 0  # Cents
    no_price: int = 0   # Cents
    created_time: str = ""
    is_taker: bool = True

    @property
    def price_dollars(self) -> float:
        """Price in dollars for the relevant side."""
        if self.side == "yes":
            return self.yes_price / 100.0
        return self.no_price / 100.0

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Create Order Request
# ---------------------------------------------------------------------------

class CreateOrderRequest(BaseModel):
    """Request body for creating a new order."""
    ticker: str
    side: str  # "yes" or "no"
    action: str  # "buy" or "sell"
    client_order_id: str
    count: int
    type: str = "limit"
    yes_price: int | None = None  # Price in cents
    no_price: int | None = None   # Price in cents
    post_only: bool = False
    time_in_force: str | None = None  # "fill_or_kill", "immediate_or_cancel", or omit
    buy_max_cost: int | None = None
    cancel_order_on_pause: bool = True

    def to_api_dict(self) -> dict[str, Any]:
        """Convert to dict suitable for the Kalshi API (exclude None values)."""
        d: dict[str, Any] = {
            "ticker": self.ticker,
            "side": self.side,
            "action": self.action,
            "client_order_id": self.client_order_id,
            "count": self.count,
            "type": self.type,
            "post_only": self.post_only,
            "cancel_order_on_pause": self.cancel_order_on_pause,
        }
        if self.yes_price is not None:
            d["yes_price"] = self.yes_price
        if self.no_price is not None:
            d["no_price"] = self.no_price
        if self.time_in_force is not None:
            d["time_in_force"] = self.time_in_force
        if self.buy_max_cost is not None:
            d["buy_max_cost"] = self.buy_max_cost
        return d
