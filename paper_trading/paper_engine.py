"""
Paper trading engine: simulates order fills against the real orderbook.

In paper mode, no real orders are sent to Kalshi. Instead, the engine
simulates fills based on the current orderbook state.

Fill assumptions:
- Taker orders (post_only=False): fill at the best available price
- Maker orders (post_only=True): fill at the limit price
  (optimistic — assumes all maker orders eventually get filled,
   which is the standard assumption for paper trading simulators)
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

    For taker orders: fills immediately at the best available price.
    For maker orders: fills at the order's limit price.
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

        # Maker orders always fill at limit price (standard paper trading)
        if order.post_only:
            return self._create_fill(
                ticker, side, action, order.count,
                order_price, is_taker=False,
            )

        # Taker orders fill at the best available price
        fill_price = self._get_taker_fill_price(ticker, side, action, order_price)
        if fill_price is None:
            log.debug(
                "Paper: no fill available — %s %s %s @ %dc",
                ticker, side, action, order_price_cents,
            )
            return None

        return self._create_fill(
            ticker, side, action, order.count,
            fill_price, is_taker=True,
        )

    def _get_taker_fill_price(
        self, ticker: str, side: str, action: str, order_price: float,
    ) -> float | None:
        """Determine the fill price for a taker order, or None if unfillable."""

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

        # Couldn't match at a valid price
        return None

    def _create_fill(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        fill_price: float,
        is_taker: bool,
    ) -> Fill:
        """Create a simulated fill."""
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

        log.info(
            "Paper FILL: %s %s %s x%d @ %.2f (%s)",
            ticker, side, action, count, fill_price,
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
