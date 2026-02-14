"""
Async WebSocket client for real-time Kalshi market data.

Connects to the Kalshi WebSocket API for:
- Real-time ticker updates (yes_bid, yes_ask, last_price)
- Orderbook snapshots and deltas
- Trade executions
- Fill notifications (private)
- Market lifecycle events

Features auto-reconnect with exponential backoff and heartbeat pings.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

import config
from kalshi.auth import get_auth_headers, load_private_key
from utils.logger import get_logger

log = get_logger("kalshi.websocket")

# Type alias for message callbacks
MessageCallback = Callable[[dict[str, Any]], None]


class KalshiWebSocketClient:
    """
    Async WebSocket client for the Kalshi streaming API.

    Usage:
        ws_client = KalshiWebSocketClient()
        ws_client.on_ticker(my_ticker_handler)
        ws_client.on_fill(my_fill_handler)
        await ws_client.connect()
        await ws_client.subscribe_ticker(["KXBTC-26FEB14-T70000"])
    """

    def __init__(
        self,
        ws_url: str | None = None,
        api_key_id: str | None = None,
        private_key_path: str | None = None,
    ):
        self._ws_url = ws_url or config.KALSHI_WS_URL
        self._api_key_id = api_key_id or config.KALSHI_API_KEY_ID
        self._private_key_path = private_key_path or config.KALSHI_PRIVATE_KEY_PATH

        self._connection: ClientConnection | None = None
        self._private_key = None
        self._running = False
        self._sub_id = 0
        self._reconnect_delay = config.PRICE_FEED_RECONNECT_SEC

        # Callbacks by message type
        self._callbacks: dict[str, list[MessageCallback]] = {
            "ticker": [],
            "orderbook_snapshot": [],
            "orderbook_delta": [],
            "trade": [],
            "fill": [],
            "market_lifecycle": [],
            "error": [],
        }

        # Background tasks
        self._listen_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def on_ticker(self, callback: MessageCallback) -> None:
        self._callbacks["ticker"].append(callback)

    def on_orderbook_snapshot(self, callback: MessageCallback) -> None:
        self._callbacks["orderbook_snapshot"].append(callback)

    def on_orderbook_delta(self, callback: MessageCallback) -> None:
        self._callbacks["orderbook_delta"].append(callback)

    def on_trade(self, callback: MessageCallback) -> None:
        self._callbacks["trade"].append(callback)

    def on_fill(self, callback: MessageCallback) -> None:
        self._callbacks["fill"].append(callback)

    def on_market_lifecycle(self, callback: MessageCallback) -> None:
        self._callbacks["market_lifecycle"].append(callback)

    def on_error(self, callback: MessageCallback) -> None:
        self._callbacks["error"].append(callback)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish WebSocket connection with authentication."""
        self._private_key = load_private_key(self._private_key_path)
        self._running = True
        await self._connect_ws()

        # Start background listener and heartbeat
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        log.info("Kalshi WebSocket connected to %s", self._ws_url)

    async def _connect_ws(self) -> None:
        """Create the underlying WebSocket connection."""
        # Generate auth headers for the WS handshake
        headers = get_auth_headers(
            self._private_key,
            self._api_key_id,
            "GET",
            "/trade-api/ws/v2",
        )

        self._connection = await websockets.connect(
            self._ws_url,
            additional_headers=headers,
            ping_interval=None,  # We manage our own heartbeat
        )

    async def disconnect(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

        if self._connection:
            await self._connection.close()
            self._connection = None

        log.info("Kalshi WebSocket disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and self._running

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def _next_sub_id(self) -> int:
        self._sub_id += 1
        return self._sub_id

    async def _send(self, message: dict[str, Any]) -> None:
        """Send a JSON message over the WebSocket."""
        if self._connection is None:
            log.warning("Cannot send — WebSocket not connected")
            return
        raw = json.dumps(message)
        await self._connection.send(raw)
        log.debug("WS sent: %s", raw[:200])

    async def subscribe_ticker(self, market_tickers: list[str] | None = None) -> None:
        """Subscribe to the ticker channel for real-time price updates."""
        msg: dict[str, Any] = {
            "id": self._next_sub_id(),
            "cmd": "subscribe",
            "params": {"channels": ["ticker"]},
        }
        if market_tickers:
            msg["params"]["market_tickers"] = market_tickers
        await self._send(msg)

    async def subscribe_orderbook_delta(self, market_tickers: list[str]) -> None:
        """Subscribe to orderbook delta updates (private, requires auth)."""
        msg: dict[str, Any] = {
            "id": self._next_sub_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": market_tickers,
            },
        }
        await self._send(msg)

    async def subscribe_trade(self, market_tickers: list[str] | None = None) -> None:
        """Subscribe to real-time trade executions."""
        msg: dict[str, Any] = {
            "id": self._next_sub_id(),
            "cmd": "subscribe",
            "params": {"channels": ["trade"]},
        }
        if market_tickers:
            msg["params"]["market_tickers"] = market_tickers
        await self._send(msg)

    async def subscribe_fill(self) -> None:
        """Subscribe to your order fill notifications (private)."""
        msg: dict[str, Any] = {
            "id": self._next_sub_id(),
            "cmd": "subscribe",
            "params": {"channels": ["fill"]},
        }
        await self._send(msg)

    async def subscribe_market_lifecycle(self) -> None:
        """Subscribe to market open/close/settle events."""
        msg: dict[str, Any] = {
            "id": self._next_sub_id(),
            "cmd": "subscribe",
            "params": {"channels": ["market_lifecycle_v2"]},
        }
        await self._send(msg)

    async def unsubscribe(self, channels: list[str], market_tickers: list[str] | None = None) -> None:
        """Unsubscribe from channels."""
        msg: dict[str, Any] = {
            "id": self._next_sub_id(),
            "cmd": "unsubscribe",
            "params": {"channels": channels},
        }
        if market_tickers:
            msg["params"]["market_tickers"] = market_tickers
        await self._send(msg)

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def _dispatch_message(self, data: dict[str, Any]) -> None:
        """Route an incoming message to the appropriate callbacks."""
        msg_type = data.get("type", "")

        # Map Kalshi message types to our callback keys
        callback_key: str | None = None

        if msg_type in ("ticker", "ticker_v2"):
            callback_key = "ticker"
        elif msg_type == "orderbook_snapshot":
            callback_key = "orderbook_snapshot"
        elif msg_type == "orderbook_delta":
            callback_key = "orderbook_delta"
        elif msg_type == "trade":
            callback_key = "trade"
        elif msg_type == "fill":
            callback_key = "fill"
        elif msg_type in ("market_lifecycle", "market_lifecycle_v2"):
            callback_key = "market_lifecycle"
        elif msg_type == "error":
            callback_key = "error"
            log.error("WS error message: %s", data)
        elif msg_type in ("subscribed", "unsubscribed"):
            log.debug("WS subscription update: %s", data)
            return
        else:
            log.debug("WS unhandled message type: %s", msg_type)
            return

        if callback_key:
            for cb in self._callbacks.get(callback_key, []):
                try:
                    cb(data)
                except Exception:
                    log.exception("Error in WS callback for %s", callback_key)

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _listen_loop(self) -> None:
        """Listen for messages, auto-reconnect on failure."""
        while self._running:
            try:
                if self._connection is None:
                    await self._connect_ws()

                async for raw_message in self._connection:
                    if not self._running:
                        break

                    try:
                        data = json.loads(raw_message)
                        self._dispatch_message(data)
                    except json.JSONDecodeError:
                        log.warning("WS received non-JSON: %s", str(raw_message)[:100])

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("WS connection error — reconnecting in %ds", self._reconnect_delay)
                self._connection = None
                await asyncio.sleep(self._reconnect_delay)
                # Exponential backoff capped at 60s
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

        log.debug("WS listen loop ended")

    async def _heartbeat_loop(self) -> None:
        """Send periodic pings to keep the connection alive."""
        while self._running:
            try:
                await asyncio.sleep(30)
                if self._connection:
                    await self._connection.ping()
                    log.debug("WS heartbeat ping sent")
            except asyncio.CancelledError:
                break
            except Exception:
                log.debug("WS heartbeat ping failed (connection may be down)")
