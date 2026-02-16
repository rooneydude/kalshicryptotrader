"""
Queue-based paper trading engine with realistic maker fill simulation.

Taker orders (post_only=False) fill instantly at the best available price.

Maker orders (post_only=True) are placed into a resting queue and only
fill when the Kalshi real-time trade stream shows that enough volume has
traded through their price level to exhaust the queue ahead of them.
This models real exchange queue priority and produces realistic fill rates.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from data.orderbook import OrderBookManager
from kalshi.models import CreateOrderRequest, Fill
from utils.logger import get_logger

log = get_logger("paper_trading.engine")

# Default TTL for resting paper orders (seconds)
RESTING_ORDER_TTL_SEC = 60.0


@dataclass
class RestingPaperOrder:
    """A maker order sitting in the simulated queue."""
    order_id: str
    ticker: str
    side: str          # "yes" or "no"
    action: str        # "buy" or "sell"
    price: float       # limit price in dollars
    count: int         # total contracts requested
    filled: int = 0    # contracts filled so far
    queue_ahead: int = 0  # contracts in queue ahead of us at placement time
    created_at: float = field(default_factory=time.time)

    @property
    def remaining(self) -> int:
        return self.count - self.filled

    @property
    def is_fully_filled(self) -> bool:
        return self.filled >= self.count

    @property
    def age_sec(self) -> float:
        return time.time() - self.created_at


class PaperEngine:
    """
    Queue-based paper trading engine.

    Taker orders fill immediately against the real orderbook.
    Maker orders rest in a queue and fill only when the Kalshi trade
    stream shows real volume trading through their price level.
    """

    def __init__(self, orderbook_manager: OrderBookManager) -> None:
        self._ob = orderbook_manager
        self._fills: list[Fill] = []
        self._fill_count = 0

        # Resting maker orders: order_id -> RestingPaperOrder
        self._resting: dict[str, RestingPaperOrder] = {}

        # Stats
        self._total_maker_orders_placed = 0
        self._total_maker_orders_filled = 0
        self._total_maker_orders_expired = 0
        self._total_maker_orders_cancelled = 0

        # Callback for deferred maker fills (set by main.py)
        self._on_fill_callback: Callable[[Fill], None] | None = None

    def set_on_fill(self, callback: Callable[[Fill], None]) -> None:
        """Register callback invoked when a resting order fills."""
        self._on_fill_callback = callback

    @property
    def fills(self) -> list[Fill]:
        return self._fills

    @property
    def resting_orders(self) -> dict[str, RestingPaperOrder]:
        return self._resting

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def try_fill(self, order: CreateOrderRequest) -> Fill | None:
        """
        Process an incoming order.

        Taker orders fill immediately. Maker orders are queued and return
        None (they will fill later via process_trade).
        """
        ticker = order.ticker
        side = order.side
        action = order.action
        price_cents = order.yes_price if side == "yes" else order.no_price

        if price_cents is None or price_cents <= 0:
            return None

        price = price_cents / 100.0

        # Taker: fill immediately at best available price
        if not order.post_only:
            fill_price = self._get_taker_fill_price(ticker, side, action, price)
            if fill_price is None:
                log.debug("Paper taker: no fill — %s %s %s @ %dc", ticker, side, action, price_cents)
                return None
            return self._create_fill(ticker, side, action, order.count, fill_price, is_taker=True)

        # Maker: add to resting queue
        queue_ahead = self._estimate_queue_position(ticker, side, action, price)
        order_id = f"paper-{uuid.uuid4().hex[:12]}"

        resting = RestingPaperOrder(
            order_id=order_id,
            ticker=ticker,
            side=side,
            action=action,
            price=price,
            count=order.count,
            queue_ahead=queue_ahead,
        )
        self._resting[order_id] = resting
        self._total_maker_orders_placed += 1

        log.info(
            "Paper QUEUE: %s %s %s x%d @ %.2f (queue_ahead=%d, id=%s)",
            ticker, side, action, order.count, price, queue_ahead, order_id,
        )
        return None  # Not filled yet

    # ------------------------------------------------------------------
    # Trade stream processing (fills resting orders)
    # ------------------------------------------------------------------

    def process_trade(self, ticker: str, side: str, price_cents: int, count: int) -> list[Fill]:
        """
        Process a real trade from the Kalshi WebSocket trade stream.

        When a real trade happens at a price that matches our resting
        orders, decrement the queue ahead. Once queue is exhausted,
        fill our paper order.

        Args:
            ticker: Market ticker where the trade occurred.
            side: "yes" or "no" — the side that was bought.
            price_cents: Trade price in cents.
            count: Number of contracts traded.

        Returns:
            List of Fill objects for any paper orders that filled.
        """
        trade_price = price_cents / 100.0
        fills: list[Fill] = []
        remaining_volume = count

        for oid, order in list(self._resting.items()):
            if remaining_volume <= 0:
                break
            if order.ticker != ticker:
                continue
            if order.is_fully_filled:
                continue

            # Match: a trade at our price level on the matching side
            if not self._trade_matches_order(order, side, trade_price):
                continue

            # Decrement queue ahead first
            if order.queue_ahead > 0:
                consumed = min(remaining_volume, order.queue_ahead)
                order.queue_ahead -= consumed
                remaining_volume -= consumed
                log.debug(
                    "Paper queue drain: %s queue_ahead=%d (-%d)",
                    oid, order.queue_ahead, consumed,
                )

            # Fill our order with whatever volume is left
            if order.queue_ahead <= 0 and remaining_volume > 0:
                can_fill = min(remaining_volume, order.remaining)
                order.filled += can_fill
                remaining_volume -= can_fill

                fill = self._create_fill(
                    order.ticker, order.side, order.action,
                    can_fill, order.price, is_taker=False,
                )
                fills.append(fill)

                log.info(
                    "Paper MAKER FILL: %s %s %s x%d @ %.2f (partial=%s, id=%s)",
                    order.ticker, order.side, order.action,
                    can_fill, order.price,
                    "no" if order.is_fully_filled else f"{order.remaining} left",
                    oid,
                )

                if order.is_fully_filled:
                    self._total_maker_orders_filled += 1
                    del self._resting[oid]

                # Notify position tracker
                if self._on_fill_callback:
                    try:
                        self._on_fill_callback(fill)
                    except Exception:
                        log.exception("Paper fill callback failed")

        return fills

    def _trade_matches_order(
        self, order: RestingPaperOrder, trade_side: str, trade_price: float,
    ) -> bool:
        """
        Check if a real trade would fill a resting paper order.

        A buy YES order at price P fills when someone sells YES at P
        (i.e., a trade on the YES side at price <= P).
        A buy NO order at price P fills when someone sells NO at P.
        """
        if order.action == "buy":
            # Our buy rests as a bid. It fills when someone sells into it.
            # A trade on our side at our price or better means someone hit our bid.
            if order.side == trade_side and abs(trade_price - order.price) < 0.005:
                return True
            # Also match if trade price is worse for the seller (they sold cheaper)
            if order.side == trade_side and trade_price <= order.price:
                return True
        elif order.action == "sell":
            if order.side == trade_side and abs(trade_price - order.price) < 0.005:
                return True
            if order.side == trade_side and trade_price >= order.price:
                return True
        return False

    # ------------------------------------------------------------------
    # Cancellation and expiry
    # ------------------------------------------------------------------

    def cancel_resting_orders(self, ticker: str | None = None) -> int:
        """
        Cancel resting paper orders, optionally filtered by ticker.

        Returns number of orders cancelled.
        """
        to_remove = []
        for oid, order in self._resting.items():
            if ticker is None or order.ticker == ticker:
                to_remove.append(oid)

        for oid in to_remove:
            del self._resting[oid]
            self._total_maker_orders_cancelled += 1

        if to_remove:
            log.info("Paper cancelled %d resting orders (ticker=%s)", len(to_remove), ticker)
        return len(to_remove)

    def expire_stale_orders(self, ttl_sec: float = RESTING_ORDER_TTL_SEC) -> int:
        """
        Remove resting orders older than ttl_sec.

        Returns number of orders expired.
        """
        now = time.time()
        to_remove = [
            oid for oid, order in self._resting.items()
            if (now - order.created_at) > ttl_sec
        ]
        for oid in to_remove:
            del self._resting[oid]
            self._total_maker_orders_expired += 1

        if to_remove:
            log.debug("Paper expired %d stale resting orders (ttl=%ds)", len(to_remove), int(ttl_sec))
        return len(to_remove)

    # ------------------------------------------------------------------
    # Queue position estimation
    # ------------------------------------------------------------------

    def _estimate_queue_position(
        self, ticker: str, side: str, action: str, price: float,
    ) -> int:
        """
        Estimate how many contracts are ahead of us in the queue.

        Uses the current orderbook depth at our price level.
        We assume we are at the BACK of the queue.
        """
        if action == "buy":
            if side == "yes":
                return self._ob.get_total_volume_at_price(ticker, "yes_bid", price)
            else:
                return self._ob.get_total_volume_at_price(ticker, "no_bid", price)
        else:
            if side == "yes":
                return self._ob.get_total_volume_at_price(ticker, "yes_ask", price)
            else:
                return self._ob.get_total_volume_at_price(ticker, "no_ask", price)

    # ------------------------------------------------------------------
    # Taker fill logic (unchanged from before)
    # ------------------------------------------------------------------

    def _get_taker_fill_price(
        self, ticker: str, side: str, action: str, order_price: float,
    ) -> float | None:
        if side == "yes" and action == "buy":
            best_ask = self._ob.get_best_yes_ask(ticker)
            if best_ask and best_ask[0] > 0 and order_price >= best_ask[0]:
                return best_ask[0]
        elif side == "yes" and action == "sell":
            best_bid = self._ob.get_best_yes_bid(ticker)
            if best_bid and order_price <= best_bid[0]:
                return best_bid[0]
        elif side == "no" and action == "buy":
            best_no_ask = self._ob.get_best_no_ask(ticker)
            if best_no_ask and best_no_ask[0] > 0 and order_price >= best_no_ask[0]:
                return best_no_ask[0]
        elif side == "no" and action == "sell":
            book = self._ob._books.get(ticker)
            if book and book.no_bids:
                best_no_bid = book.no_bids[0]
                if order_price <= best_no_bid[0]:
                    return best_no_bid[0]
        return None

    # ------------------------------------------------------------------
    # Fill creation
    # ------------------------------------------------------------------

    def _create_fill(
        self, ticker: str, side: str, action: str,
        count: int, fill_price: float, is_taker: bool,
    ) -> Fill:
        self._fill_count += 1
        fill_price_cents = int(fill_price * 100)

        fill = Fill(
            trade_id=f"paper-fill-{self._fill_count}",
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            ticker=ticker,
            side=side,
            action=action,
            count=count,
            yes_price=fill_price_cents if side == "yes" else (100 - fill_price_cents),
            no_price=fill_price_cents if side == "no" else (100 - fill_price_cents),
            created_time=datetime.now(timezone.utc).isoformat(),
            is_taker=is_taker,
        )
        self._fills.append(fill)
        return fill

    # ------------------------------------------------------------------
    # Stats and queries
    # ------------------------------------------------------------------

    def get_fill_summary(self) -> dict[str, Any]:
        if not self._fills:
            return {"total_fills": 0, "total_contracts": 0, "unique_tickers": 0}

        return {
            "total_fills": len(self._fills),
            "total_contracts": sum(f.count for f in self._fills),
            "unique_tickers": len(set(f.ticker for f in self._fills)),
            "buys": sum(1 for f in self._fills if f.action == "buy"),
            "sells": sum(1 for f in self._fills if f.action == "sell"),
        }

    def get_resting_summary(self) -> dict[str, Any]:
        """Stats for the dashboard about resting order queue."""
        resting_list = []
        for oid, o in self._resting.items():
            resting_list.append({
                "order_id": oid,
                "ticker": o.ticker,
                "side": o.side,
                "action": o.action,
                "price": round(o.price, 4),
                "total": o.count,
                "filled": o.filled,
                "remaining": o.remaining,
                "queue_ahead": o.queue_ahead,
                "age_sec": round(o.age_sec, 1),
            })

        total_placed = self._total_maker_orders_placed
        total_filled = self._total_maker_orders_filled
        fill_rate = (total_filled / total_placed * 100) if total_placed > 0 else 0.0

        return {
            "resting_count": len(self._resting),
            "resting_orders": resting_list,
            "maker_orders_placed": total_placed,
            "maker_orders_filled": total_filled,
            "maker_orders_expired": self._total_maker_orders_expired,
            "maker_orders_cancelled": self._total_maker_orders_cancelled,
            "fill_rate_pct": round(fill_rate, 1),
        }

    def reset(self) -> None:
        self._fills.clear()
        self._fill_count = 0
        self._resting.clear()
        self._total_maker_orders_placed = 0
        self._total_maker_orders_filled = 0
        self._total_maker_orders_expired = 0
        self._total_maker_orders_cancelled = 0
        log.info("Paper engine reset")
