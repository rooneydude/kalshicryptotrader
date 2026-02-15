"""
Async REST API client for the Kalshi trading API.

Uses aiohttp for non-blocking HTTP requests with:
- RSA-PSS signed authentication headers
- Automatic retry with exponential backoff
- Token-bucket rate limiting
- Pydantic model parsing for all responses
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

import config
from kalshi.auth import get_auth_headers, load_private_key
from kalshi.models import (
    AmendOrderRequest,
    Balance,
    BatchCancelResponse,
    BatchResponse,
    CancelResponse,
    CreateOrderRequest,
    Event,
    ExchangeStatus,
    GetMarketsResponse,
    GetOrdersResponse,
    GetPositionsResponse,
    Market,
    Order,
    OrderBook,
)
from utils.logger import get_logger

log = get_logger("kalshi.client")


class RateLimiter:
    """Simple token-bucket rate limiter."""

    def __init__(self, rate: float = 10.0):
        """
        Args:
            rate: Maximum requests per second.
        """
        self._rate = rate
        self._min_interval = 1.0 / rate
        self._last_request_time: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request slot is available."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_request_time = time.monotonic()


class KalshiClient:
    """
    Async client for the Kalshi REST API.

    Usage:
        client = KalshiClient()
        await client.connect()
        try:
            balance = await client.get_balance()
        finally:
            await client.close()
    """

    def __init__(
        self,
        api_key_id: str | None = None,
        private_key_path: str | None = None,
        base_url: str | None = None,
    ):
        self._api_key_id = api_key_id or config.KALSHI_API_KEY_ID
        self._private_key_path = private_key_path or config.KALSHI_PRIVATE_KEY_PATH
        self._base_url = (base_url or config.KALSHI_BASE_URL).rstrip("/")
        self._api_prefix = config.API_PREFIX

        self._private_key = None
        self._session: aiohttp.ClientSession | None = None
        self._rate_limiter = RateLimiter(config.API_RATE_LIMIT_PER_SEC)

    async def connect(self) -> None:
        """Load private key and create HTTP session."""
        self._private_key = load_private_key(self._private_key_path)
        self._session = aiohttp.ClientSession()
        log.info("KalshiClient connected to %s", self._base_url)

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None
        log.info("KalshiClient disconnected")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Build full URL from a path like /markets."""
        return f"{self._base_url}{self._api_prefix}{path}"

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Generate signed auth headers."""
        if self._private_key is None:
            raise RuntimeError("Client not connected — call connect() first")
        full_path = f"{self._api_prefix}{path}"
        return get_auth_headers(self._private_key, self._api_key_id, method, full_path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        """
        Make an authenticated (or public) API request with retry + rate limiting.

        Args:
            method: HTTP method (GET, POST, DELETE).
            path: API path after the prefix (e.g. "/markets").
            params: Query parameters.
            json_body: JSON request body.
            auth: Whether to include auth headers.

        Returns:
            Parsed JSON response as a dict.
        """
        if self._session is None:
            raise RuntimeError("Client not connected — call connect() first")

        url = self._url(path)
        headers: dict[str, str] = {}
        if auth:
            headers = self._auth_headers(method.upper(), path)
        else:
            headers = {"Content-Type": "application/json"}

        last_exc: Exception | None = None

        for attempt in range(config.API_MAX_RETRIES + 1):
            await self._rate_limiter.acquire()

            try:
                log.debug(
                    "API %s %s params=%s body=%s (attempt %d)",
                    method.upper(),
                    path,
                    params,
                    json_body,
                    attempt + 1,
                )

                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                ) as resp:
                    # Rate limited — back off
                    if resp.status == 429:
                        delay = config.API_RETRY_BASE_DELAY * (2**attempt)
                        log.warning("Rate limited (429), retrying in %.1fs", delay)
                        await asyncio.sleep(delay)
                        continue

                    # Server error — retry
                    if resp.status >= 500:
                        delay = config.API_RETRY_BASE_DELAY * (2**attempt)
                        body = await resp.text()
                        log.warning(
                            "Server error %d: %s — retrying in %.1fs",
                            resp.status,
                            body[:200],
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    # Client error — don't retry
                    if resp.status >= 400:
                        body = await resp.text()
                        log.error("API error %d: %s %s → %s", resp.status, method, path, body[:500])
                        raise aiohttp.ClientResponseError(
                            resp.request_info,
                            resp.history,
                            status=resp.status,
                            message=body,
                        )

                    data = await resp.json()
                    log.debug("API response %s %s → %d bytes", method, path, len(str(data)))
                    return data

            except aiohttp.ClientResponseError:
                raise
            except Exception as exc:
                last_exc = exc
                delay = config.API_RETRY_BASE_DELAY * (2**attempt)
                log.warning("Request error: %s — retrying in %.1fs", exc, delay)
                await asyncio.sleep(delay)

        raise RuntimeError(
            f"API request failed after {config.API_MAX_RETRIES + 1} attempts: {last_exc}"
        )

    # ------------------------------------------------------------------
    # Public endpoints (no auth)
    # ------------------------------------------------------------------

    async def get_exchange_status(self) -> ExchangeStatus:
        """GET /exchange/status"""
        data = await self._request("GET", "/exchange/status", auth=False)
        return ExchangeStatus.model_validate(data)

    async def get_markets(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        status: str | None = None,
        max_close_ts: int | None = None,
        min_close_ts: int | None = None,
        tickers: str | None = None,
    ) -> GetMarketsResponse:
        """GET /markets — Discover and filter markets."""
        params: dict[str, Any] = {"limit": min(limit, 1000)}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if max_close_ts is not None:
            params["max_close_ts"] = max_close_ts
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if tickers:
            params["tickers"] = tickers

        data = await self._request("GET", "/markets", params=params, auth=False)
        return GetMarketsResponse.model_validate(data)

    async def get_market(self, ticker: str) -> Market:
        """GET /markets/{ticker}"""
        data = await self._request("GET", f"/markets/{ticker}", auth=False)
        market_data = data.get("market", data)
        return Market.model_validate(market_data)

    async def get_orderbook(self, ticker: str, depth: int = 20) -> OrderBook:
        """GET /markets/{ticker}/orderbook"""
        data = await self._request(
            "GET",
            f"/markets/{ticker}/orderbook",
            params={"depth": depth},
            auth=False,
        )
        ob_data = data.get("orderbook", data)
        # Normalize field names
        parsed: dict[str, Any] = {"ticker": ticker}

        def _normalize_levels(raw_levels: list) -> list[list[float]]:
            """Convert API levels to [[price_dollars, qty], ...] regardless of format.

            The Kalshi API returns prices in cents (1-99). We convert to
            dollars (0.01 - 0.99) so that all internal code can use a
            consistent 0-1 dollar scale.
            """
            result = []
            for level in raw_levels:
                if isinstance(level, (list, tuple)):
                    p = float(level[0])
                    q = float(level[1])
                elif isinstance(level, dict):
                    p = float(level.get("price", level.get("p", 0)))
                    q = float(level.get("quantity", level.get("q", level.get("size", 0))))
                else:
                    log.warning("Unknown orderbook level format: %s", type(level))
                    continue

                # Convert cents → dollars if price > 1 (heuristic: dollar
                # prices are in 0-1 range, cent prices are in 1-99 range)
                if p > 1.0:
                    p = p / 100.0

                result.append([p, q])
            return result

        # Handle both "yes" and "yes_bids" key names
        raw_yes = ob_data.get("yes") or ob_data.get("yes_bids") or []
        raw_no = ob_data.get("no") or ob_data.get("no_bids") or []

        parsed["yes_bids"] = _normalize_levels(raw_yes)
        parsed["no_bids"] = _normalize_levels(raw_no)

        return OrderBook.model_validate(parsed)

    async def get_event(self, event_ticker: str) -> Event:
        """GET /events/{event_ticker}"""
        data = await self._request("GET", f"/events/{event_ticker}", auth=False)
        event_data = data.get("event", data)
        return Event.model_validate(event_data)

    # ------------------------------------------------------------------
    # Authenticated endpoints
    # ------------------------------------------------------------------

    async def get_balance(self) -> Balance:
        """GET /portfolio/balance"""
        data = await self._request("GET", "/portfolio/balance")
        return Balance.model_validate(data)

    async def get_positions(
        self,
        *,
        event_ticker: str | None = None,
    ) -> GetPositionsResponse:
        """GET /portfolio/positions"""
        params: dict[str, Any] = {}
        if event_ticker:
            params["event_ticker"] = event_ticker
        data = await self._request("GET", "/portfolio/positions", params=params or None)
        return GetPositionsResponse.model_validate(data)

    async def get_orders(
        self,
        *,
        ticker: str | None = None,
        status: str | None = None,
    ) -> GetOrdersResponse:
        """GET /portfolio/orders"""
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        data = await self._request("GET", "/portfolio/orders", params=params or None)
        return GetOrdersResponse.model_validate(data)

    async def create_order(self, order: CreateOrderRequest) -> Order:
        """POST /portfolio/orders"""
        data = await self._request(
            "POST",
            "/portfolio/orders",
            json_body=order.to_api_dict(),
        )
        order_data = data.get("order", data)
        return Order.model_validate(order_data)

    async def cancel_order(self, order_id: str) -> CancelResponse:
        """DELETE /portfolio/orders/{order_id}"""
        data = await self._request("DELETE", f"/portfolio/orders/{order_id}")
        return CancelResponse.model_validate(data)

    async def amend_order(self, order_id: str, request: AmendOrderRequest) -> Order:
        """POST /portfolio/orders/{order_id}/amend"""
        body = request.model_dump(exclude_none=True)
        data = await self._request(
            "POST",
            f"/portfolio/orders/{order_id}/amend",
            json_body=body,
        )
        order_data = data.get("order", data)
        return Order.model_validate(order_data)

    async def batch_create_orders(
        self, orders: list[CreateOrderRequest]
    ) -> BatchResponse:
        """POST /portfolio/orders/batched"""
        body = {"orders": [o.to_api_dict() for o in orders]}
        data = await self._request(
            "POST",
            "/portfolio/orders/batched",
            json_body=body,
        )
        return BatchResponse.model_validate(data)

    async def batch_cancel_orders(self, order_ids: list[str]) -> BatchCancelResponse:
        """DELETE /portfolio/orders/batched"""
        body = {"order_ids": order_ids}
        data = await self._request(
            "DELETE",
            "/portfolio/orders/batched",
            json_body=body,
        )
        return BatchCancelResponse.model_validate(data)

    # ------------------------------------------------------------------
    # Convenience: paginated market fetch
    # ------------------------------------------------------------------

    async def get_all_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
    ) -> list[Market]:
        """Fetch all markets matching filters, handling pagination automatically."""
        all_markets: list[Market] = []
        cursor: str | None = None

        while True:
            resp = await self.get_markets(
                limit=1000,
                cursor=cursor,
                series_ticker=series_ticker,
                event_ticker=event_ticker,
                status=status,
            )
            all_markets.extend(resp.markets)

            if not resp.cursor or len(resp.markets) == 0:
                break
            cursor = resp.cursor

        log.debug(
            "Fetched %d markets (series=%s, event=%s, status=%s)",
            len(all_markets),
            series_ticker,
            event_ticker,
            status,
        )
        return all_markets
