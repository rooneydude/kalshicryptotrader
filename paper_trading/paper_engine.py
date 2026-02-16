"""
Paper trading engine: simulates order fills against the real orderbook.

In paper mode, no real orders are sent to Kalshi. Instead, the engine
checks whether an order *would* have been filled based on the current
orderbook state and returns a simulated fill.

For maker (post_only) orders:
- If the order would immediately cross the spread → rejected (realistic)
- If the order is within the spread → fill at the order price as maker
  (optimistic assumption that maker orders eventually get filled)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from data.orderbook import OrderBookManager
from kalshi.models import CreateOrderRequest, Fill
from utils.logger import get_logger

log = get_logger("paper_trading.engine")


class PaperEngine:
    """
    Simulates order fills against the current orderbook state.

    For taker orders (post_only=False):
    - Fills immediately at the best available price if the order crosses.

    For maker orders (post_only=True):
    - Rejected if they would immediately cross (realistic Kalshi behavior).
    - Otherwise, fills at the order price as maker (simulates queue fill).
    """

    def __init__(self, orderbook_manager: OrderBookManager) -> None:
        self._ob = orderbook_manager
        self._fills: list[Fill] = []
        self._fill_count = 0

    @property
    def fills(self) -> list[Fill]:
        """All simulated fills."""
        return self._fills

    def try_fill(self, order: CreateOrderRequest) -> Fill | None:
        """
        Attempt to simulate a fill for the given order.

        Args:
            order: The order to simulate.

        Returns:
            A Fill object if the order would execute, None otherwise.
        """
        ticker = order.ticker
        side = order.side
        action = order.action
        order_price_cents = order.yes_price if side == "yes" else order.no_price

        if order_price_cents is None or order_price_cents <= 0:
            log.debug("Paper: no price set for %s — skipping", ticker)
            return None

        order_price = order_price_cents / 100.0

        would_fill = False
        fill_price = order_price
        is_taker = True

        if side == "yes" and action == "buy":
            best_ask = self._ob.get_best_yes_ask(ticker)
            if best_ask is not None:
                ask_price, ask_qty = best_ask
                if order_price >= ask_price:
                    if order.post_only:
                        log.debug(
                            "Paper: post_only buy YES rejected (would cross at %.2f)",
                            ask_price,
                        )
                        return None
                    would_fill = True
                    fill_price = ask_price
                elif order.post_only:
                    # Maker order within the spread — assume it fills
                    would_fill = True
                    fill_price = order_price
                    is_taker = False

        elif side == "yes" and action == "sell":
            best_bid = self._ob.get_best_yes_bid(ticker)
            if best_bid is not None:
                bid_price, bid_qty = best_bid
                if order_price <= bid_price:
                    if order.post_only:
                        log.debug(
                            "Paper: post_only sell YES rejected (would cross at %.2f)",
                            bid_price,
                        )
                        return None
                    would_fill = True
                    fill_price = bid_price
                elif order.post_only:
                    would_fill = True
                    fill_price = order_price
                    is_taker = False

        elif side == "no" and action == "buy":
            best_no_ask = self._ob.get_best_no_ask(ticker)
            if best_no_ask is not None:
                no_ask_price, no_ask_qty = best_no_ask
                if order_price >= no_ask_price:
                    if order.post_only:
                        log.debug(
                            "Paper: post_only buy NO rejected (would cross at %.2f)",
                            no_ask_price,
                        )
                        return None
                    would_fill = True
                    fill_price = no_ask_price
                elif order.post_only:
                    would_fill = True
                    fill_price = order_price
                    is_taker = False

        elif side == "no" and action == "sell":
            book = self._ob._books.get(ticker)
            if book and book.no_bids:
                best_no_bid = book.no_bids[0]
                if order_price <= best_no_bid[0]:
                    if order.post_only:
                        return None
                    would_fill = True
                    fill_price = best_no_bid[0]
                elif order.post_only:
                    would_fill = True
                    fill_price = order_price
                    is_taker = False

        if not would_fill:
            log.debug(
                "Paper: order did not fill — %s %s %s @ %dc",
                ticker,
                side,
                action,
                order_price_cents,
            )
            return None

        # Create simulated fill
        self._fill_count += 1
        fill_price_cents = int(fill_price * 100)

        fill = Fill(
            trade_id=f"paper-fill-{self._fill_count}",
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            ticker=ticker,
            side=side,
            action=action,
            count=order.count,
            yes_price=fill_price_cents if side == "yes" else (100 - fill_price_cents),
            no_price=fill_price_cents if side == "no" else (100 - fill_price_cents),
            created_time=datetime.now(timezone.utc).isoformat(),
            is_taker=is_taker,
        )

        self._fills.append(fill)

        log.info(
            "Paper FILL: %s %s %s x%d @ %.2f (%s)",
            ticker,
            side,
            action,
            order.count,
            fill_price,
            "taker" if is_taker else "maker",
        )

        return fill

    def get_fill_summary(self) -> dict[str, Any]:
        """Get summary statistics for all paper fills."""
        if not self._fills:
            return {
                "total_fills": 0,
                "total_contracts": 0,
                "unique_tickers": 0,
            }

        total_contracts = sum(f.count for f in self._fills)
        tickers = set(f.ticker for f in self._fills)
        buys = sum(1 for f in self._fills if f.action == "buy")
        sells = sum(1 for f in self._fills if f.action == "sell")

        return {
            "total_fills": len(self._fills),
            "total_contracts": total_contracts,
            "unique_tickers": len(tickers),
            "buys": buys,
            "sells": sells,
        }

    def reset(self) -> None:
        """Clear all fill history."""
        self._fills.clear()
        self._fill_count = 0
        log.info("Paper engine reset")
