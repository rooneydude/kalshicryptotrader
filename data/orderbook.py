"""
Local orderbook state manager.

Maintains a snapshot of the orderbook for each watched market,
updated from REST polling or WebSocket deltas.

Kalshi orderbooks only contain bids:
- YES bids: buyers willing to buy YES at a given price
- NO bids: buyers willing to buy NO at a given price

To derive the YES ask: best NO bid price → YES ask = 1.00 - NO bid price
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshi.models import OrderBook
from utils.logger import get_logger

log = get_logger("data.orderbook")


@dataclass
class LocalOrderBook:
    """Local representation of a single market's orderbook."""
    ticker: str = ""
    # YES bids: sorted by price descending → [[price_dollars, quantity], ...]
    yes_bids: list[list[float]] = field(default_factory=list)
    # NO bids: sorted by price descending → [[price_dollars, quantity], ...]
    no_bids: list[list[float]] = field(default_factory=list)


class OrderBookManager:
    """
    Maintains local orderbook state for all watched markets.

    Updated from REST responses or WebSocket deltas.
    """

    def __init__(self) -> None:
        self._books: dict[str, LocalOrderBook] = {}

    def update_from_rest(self, ticker: str, orderbook: OrderBook) -> None:
        """
        Replace the local orderbook with fresh data from the REST API.

        Args:
            ticker: The market ticker.
            orderbook: The OrderBook response from the API.
        """
        book = LocalOrderBook(
            ticker=ticker,
            yes_bids=sorted(orderbook.yes_bids, key=lambda x: x[0], reverse=True),
            no_bids=sorted(orderbook.no_bids, key=lambda x: x[0], reverse=True),
        )
        self._books[ticker] = book
        log.debug(
            "Orderbook updated (REST) for %s: %d YES bids, %d NO bids",
            ticker,
            len(book.yes_bids),
            len(book.no_bids),
        )

    def update_from_delta(self, ticker: str, delta: dict) -> None:
        """
        Apply an incremental WebSocket orderbook delta.

        The delta format from Kalshi typically contains:
        - "yes": [[price, new_qty], ...] — levels that changed on YES side
        - "no": [[price, new_qty], ...] — levels that changed on NO side
        A quantity of 0 means remove that level.

        Args:
            ticker: The market ticker.
            delta: The raw delta dict from the WebSocket.
        """
        if ticker not in self._books:
            self._books[ticker] = LocalOrderBook(ticker=ticker)

        book = self._books[ticker]

        # Apply YES side changes
        for level in delta.get("yes", []):
            if len(level) >= 2:
                self._apply_level(book.yes_bids, float(level[0]), float(level[1]))

        # Apply NO side changes
        for level in delta.get("no", []):
            if len(level) >= 2:
                self._apply_level(book.no_bids, float(level[0]), float(level[1]))

        log.debug("Orderbook delta applied for %s", ticker)

    @staticmethod
    def _apply_level(bids: list[list[float]], price: float, quantity: float) -> None:
        """
        Apply a single level update to a bid list.

        If quantity is 0, remove the level. Otherwise, insert/update.
        """
        for i, level in enumerate(bids):
            if abs(level[0] - price) < 0.001:
                if quantity <= 0:
                    bids.pop(i)
                else:
                    level[1] = quantity
                return

        # Level not found — insert if quantity > 0
        if quantity > 0:
            bids.append([price, quantity])
            bids.sort(key=lambda x: x[0], reverse=True)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_best_yes_bid(self, ticker: str) -> tuple[float, int] | None:
        """
        Get the best (highest) YES bid.

        Returns:
            (price_dollars, quantity) or None if no bids exist.
        """
        book = self._books.get(ticker)
        if not book or not book.yes_bids:
            return None
        level = book.yes_bids[0]
        return (level[0], int(level[1]))

    def get_best_yes_ask(self, ticker: str) -> tuple[float, int] | None:
        """
        Get the best (lowest) YES ask.

        In Kalshi, YES ask = 1.00 - best NO bid price.
        The quantity comes from the NO bid side.

        Returns:
            (price_dollars, quantity) or None if no NO bids exist.
        """
        book = self._books.get(ticker)
        if not book or not book.no_bids:
            return None
        best_no_bid = book.no_bids[0]
        yes_ask_price = round(1.00 - best_no_bid[0], 2)
        return (yes_ask_price, int(best_no_bid[1]))

    def get_best_no_ask(self, ticker: str) -> tuple[float, int] | None:
        """
        Get the best (lowest) NO ask.

        NO ask = 1.00 - best YES bid price.

        Returns:
            (price_dollars, quantity) or None.
        """
        book = self._books.get(ticker)
        if not book or not book.yes_bids:
            return None
        best_yes_bid = book.yes_bids[0]
        no_ask_price = round(1.00 - best_yes_bid[0], 2)
        return (no_ask_price, int(best_yes_bid[1]))

    def get_spread(self, ticker: str) -> float | None:
        """
        Get the YES bid-ask spread in dollars.

        Returns:
            Spread (yes_ask - yes_bid) in dollars, or None.
        """
        bid = self.get_best_yes_bid(ticker)
        ask = self.get_best_yes_ask(ticker)
        if bid is None or ask is None:
            return None
        return round(ask[0] - bid[0], 4)

    def get_depth(
        self, ticker: str, side: str, levels: int = 5
    ) -> list[tuple[float, int]]:
        """
        Get the top N price levels for a side.

        Args:
            ticker: Market ticker.
            side: "yes_bid", "yes_ask", "no_bid", or "no_ask".
            levels: Number of levels to return.

        Returns:
            List of (price_dollars, quantity) tuples.
        """
        book = self._books.get(ticker)
        if not book:
            return []

        match side:
            case "yes_bid":
                raw = book.yes_bids[:levels]
                return [(r[0], int(r[1])) for r in raw]
            case "no_bid":
                raw = book.no_bids[:levels]
                return [(r[0], int(r[1])) for r in raw]
            case "yes_ask":
                # Derive from NO bids (ascending ask price = descending NO bid price)
                raw = book.no_bids[:levels]
                return [(round(1.00 - r[0], 2), int(r[1])) for r in raw]
            case "no_ask":
                # Derive from YES bids
                raw = book.yes_bids[:levels]
                return [(round(1.00 - r[0], 2), int(r[1])) for r in raw]
            case _:
                raise ValueError(f"Unknown side: {side}")

    def get_total_volume_at_price(
        self, ticker: str, side: str, price: float
    ) -> int:
        """
        Get total contracts available at a specific price.

        Args:
            ticker: Market ticker.
            side: "yes_bid", "no_bid", "yes_ask", or "no_ask".
            price: Price in dollars to check.

        Returns:
            Number of contracts at that price level, or 0.
        """
        book = self._books.get(ticker)
        if not book:
            return 0

        match side:
            case "yes_bid":
                bids = book.yes_bids
            case "no_bid":
                bids = book.no_bids
            case "yes_ask":
                # Convert price to NO bid price
                no_price = round(1.00 - price, 2)
                bids = book.no_bids
                price = no_price
            case "no_ask":
                yes_price = round(1.00 - price, 2)
                bids = book.yes_bids
                price = yes_price
            case _:
                return 0

        total = 0
        for level in bids:
            if abs(level[0] - price) < 0.001:
                total += int(level[1])
        return total

    def has_book(self, ticker: str) -> bool:
        """Check if we have orderbook data for a ticker."""
        return ticker in self._books

    def get_watched_tickers(self) -> list[str]:
        """Return all tickers we have orderbook data for."""
        return list(self._books.keys())
