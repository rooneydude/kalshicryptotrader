"""
Centralized order placement, tracking, and lifecycle management.

Handles both live and paper trading modes. In paper mode, orders are
simulated against the current orderbook state.
"""

from __future__ import annotations

import uuid
from typing import Any

from kalshi.client import KalshiClient
from kalshi.models import CreateOrderRequest, Order
from utils.logger import get_logger
from utils import discord

log = get_logger("execution.order_manager")


class OrderManager:
    """
    Manages the full order lifecycle: placement, cancellation, amendment.

    In paper mode, simulates fills against real orderbook data.
    In live mode, routes orders to the Kalshi REST API.
    """

    def __init__(
        self,
        client: KalshiClient,
        paper_mode: bool = True,
    ) -> None:
        self._client = client
        self._paper_mode = paper_mode

        # Local order tracking: order_id → Order
        self._orders: dict[str, Order] = {}
        # client_order_id → order_id mapping
        self._client_to_order: dict[str, str] = {}

        # Paper trading engine (set externally if paper_mode)
        self._paper_engine: Any = None

    def set_paper_engine(self, engine: Any) -> None:
        """Set the paper trading engine for simulated fills."""
        self._paper_engine = engine

    @property
    def paper_mode(self) -> bool:
        return self._paper_mode

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        price_cents: int,
        contracts: int,
        post_only: bool = False,
        cancel_on_pause: bool = True,
    ) -> Order | None:
        """
        Place a single order.

        Args:
            ticker: Market ticker.
            side: "yes" or "no".
            action: "buy" or "sell".
            price_cents: Limit price in cents.
            contracts: Number of contracts.
            post_only: Maker-only order (rejected if it would immediately match).
            cancel_on_pause: Cancel if market pauses.

        Returns:
            Order object if successful, None if rejected.
        """
        client_order_id = str(uuid.uuid4())

        request = CreateOrderRequest(
            ticker=ticker,
            side=side,
            action=action,
            client_order_id=client_order_id,
            count=contracts,
            type="limit",
            yes_price=price_cents if side == "yes" else None,
            no_price=price_cents if side == "no" else None,
            post_only=post_only,
            cancel_order_on_pause=cancel_on_pause,
        )

        if self._paper_mode:
            order = await self._paper_place(request)
        else:
            order = await self._live_place(request)

        if order is not None:
            await discord.notify_order(
                ticker=ticker, side=side, action=action,
                price_cents=price_cents, contracts=contracts,
                order_id=order.order_id,
            )
        return order

    async def place_order_from_signal(self, signal: Any) -> Order | None:
        """
        Place an order from a TradeSignal object.

        Args:
            signal: A TradeSignal dataclass instance.

        Returns:
            Order if successful, None if rejected.
        """
        return await self.place_order(
            ticker=signal.ticker,
            side=signal.side,
            action=signal.action,
            price_cents=signal.price_cents,
            contracts=signal.contracts,
            post_only=signal.post_only,
        )

    async def batch_place(self, signals: list[Any]) -> list[Order]:
        """
        Place multiple orders atomically (or sequentially in paper mode).

        Args:
            signals: List of TradeSignal objects.

        Returns:
            List of Order objects for successfully placed orders.
        """
        if self._paper_mode:
            orders: list[Order] = []
            for signal in signals:
                order = await self.place_order_from_signal(signal)
                if order:
                    orders.append(order)
            return orders

        # Live mode: use batch API
        requests: list[CreateOrderRequest] = []
        for signal in signals:
            client_order_id = str(uuid.uuid4())
            req = CreateOrderRequest(
                ticker=signal.ticker,
                side=signal.side,
                action=signal.action,
                client_order_id=client_order_id,
                count=signal.contracts,
                type="limit",
                yes_price=signal.price_cents if signal.side == "yes" else None,
                no_price=signal.price_cents if signal.side == "no" else None,
                post_only=signal.post_only,
                cancel_order_on_pause=True,
            )
            requests.append(req)

        try:
            resp = await self._client.batch_create_orders(requests)
            for order in resp.orders:
                self._track_order(order)
            log.info("Batch placed %d orders", len(resp.orders))
            return resp.orders
        except Exception:
            log.exception("Batch order placement failed")
            return []

    # ------------------------------------------------------------------
    # Order cancellation
    # ------------------------------------------------------------------

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a single order.

        Returns:
            True if cancelled successfully.
        """
        if self._paper_mode:
            if order_id in self._orders:
                self._orders[order_id].status = "canceled"
                log.info("Paper cancel: %s", order_id)
                return True
            return False

        try:
            await self._client.cancel_order(order_id)
            if order_id in self._orders:
                self._orders[order_id].status = "canceled"
            log.info("Cancelled order %s", order_id)
            return True
        except Exception:
            log.exception("Failed to cancel order %s", order_id)
            return False

    async def cancel_all_orders(self, ticker: str | None = None) -> int:
        """
        Cancel all resting orders, optionally filtered by ticker.

        Returns:
            Number of orders cancelled.
        """
        if self._paper_mode:
            count = 0
            for oid, order in list(self._orders.items()):
                if order.status == "resting":
                    if ticker is None or order.ticker == ticker:
                        order.status = "canceled"
                        count += 1
            log.info("Paper cancelled %d orders (ticker=%s)", count, ticker)
            return count

        # Live mode: cancel via API
        resting = self.get_open_orders(ticker=ticker)
        if not resting:
            return 0

        order_ids = [o.order_id for o in resting if o.order_id]
        if not order_ids:
            return 0

        try:
            await self._client.batch_cancel_orders(order_ids)
            for oid in order_ids:
                if oid in self._orders:
                    self._orders[oid].status = "canceled"
            log.info("Cancelled %d orders (ticker=%s)", len(order_ids), ticker)
            return len(order_ids)
        except Exception:
            log.exception("Failed to batch cancel orders")
            return 0

    # ------------------------------------------------------------------
    # Order amendment
    # ------------------------------------------------------------------

    async def amend_order(self, order_id: str, new_price_cents: int) -> Order | None:
        """
        Amend an existing order's price.

        Returns:
            Updated Order or None if failed.
        """
        if self._paper_mode:
            if order_id in self._orders:
                order = self._orders[order_id]
                if order.side == "yes":
                    order.yes_price = new_price_cents
                else:
                    order.no_price = new_price_cents
                log.info("Paper amend: %s → %d cents", order_id, new_price_cents)
                return order
            return None

        try:
            from kalshi.models import AmendOrderRequest
            request = AmendOrderRequest()
            # The amend API determines yes/no from the existing order
            request.yes_price = new_price_cents
            result = await self._client.amend_order(order_id, request)
            self._track_order(result)
            log.info("Amended order %s → %d cents", order_id, new_price_cents)
            return result
        except Exception:
            log.exception("Failed to amend order %s", order_id)
            return None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_open_orders(self, ticker: str | None = None) -> list[Order]:
        """Get all resting (open) orders, optionally filtered by ticker."""
        result = []
        for order in self._orders.values():
            if order.status in ("resting", "pending"):
                if ticker is None or order.ticker == ticker:
                    result.append(order)
        return result

    def get_order_status(self, order_id: str) -> Order | None:
        """Get the current state of an order."""
        return self._orders.get(order_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _track_order(self, order: Order) -> None:
        """Add or update an order in local tracking."""
        if order.order_id:
            self._orders[order.order_id] = order
        if order.client_order_id:
            self._client_to_order[order.client_order_id] = order.order_id

    async def _live_place(self, request: CreateOrderRequest) -> Order | None:
        """Place an order via the live Kalshi API."""
        try:
            order = await self._client.create_order(request)
            self._track_order(order)
            log.info(
                "Live order placed: %s %s %s %s @ %dc x%d (id=%s)",
                order.ticker,
                order.side,
                order.action,
                order.type,
                order.yes_price or order.no_price,
                request.count,
                order.order_id,
            )
            return order
        except Exception:
            log.exception("Failed to place order: %s", request.ticker)
            return None

    async def _paper_place(self, request: CreateOrderRequest) -> Order | None:
        """Simulate order placement in paper trading mode."""
        order_id = f"paper-{uuid.uuid4().hex[:12]}"

        order = Order(
            order_id=order_id,
            client_order_id=request.client_order_id,
            ticker=request.ticker,
            side=request.side,
            action=request.action,
            type=request.type,
            status="resting",
            yes_price=request.yes_price or 0,
            no_price=request.no_price or 0,
            remaining_count=request.count,
            count=request.count,
        )

        # If we have a paper engine, try to simulate a fill
        if self._paper_engine:
            fill = self._paper_engine.try_fill(request)
            if fill:
                order.status = "executed"
                order.remaining_count = 0

        self._track_order(order)
        log.info(
            "Paper order: %s %s %s @ %dc x%d (id=%s, status=%s)",
            request.ticker,
            request.side,
            request.action,
            request.yes_price or request.no_price or 0,
            request.count,
            order_id,
            order.status,
        )
        return order
