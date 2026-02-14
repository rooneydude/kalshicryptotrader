"""
Tests for kalshi/client.py — REST API client with mocked responses.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from kalshi.client import KalshiClient, RateLimiter
from kalshi.models import (
    CreateOrderRequest,
    GetMarketsResponse,
    Market,
    Order,
    OrderBook,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client():
    """Create a KalshiClient with mocked internals."""
    client = KalshiClient(
        api_key_id="test-key",
        private_key_path="./test.pem",
        base_url="https://demo-api.kalshi.co",
    )
    # Mock the private key and session
    client._private_key = MagicMock()
    client._session = AsyncMock()
    return client


@pytest.fixture
def sample_market_data():
    return {
        "markets": [
            {
                "ticker": "KXBTC-26FEB14-T70000",
                "event_ticker": "KXBTC-26FEB14",
                "title": "Bitcoin price today at 5pm EST?",
                "subtitle": "$70,000 or above",
                "status": "active",
                "yes_bid": 0.85,
                "yes_ask": 0.87,
                "volume_24h": 15000,
            },
            {
                "ticker": "KXBTC-26FEB14-T71000",
                "event_ticker": "KXBTC-26FEB14",
                "title": "Bitcoin price today at 5pm EST?",
                "subtitle": "$71,000 or above",
                "status": "active",
                "yes_bid": 0.65,
                "yes_ask": 0.67,
                "volume_24h": 12000,
            },
        ],
        "cursor": "",
    }


@pytest.fixture
def sample_orderbook_data():
    return {
        "orderbook": {
            "yes": [[0.85, 100], [0.83, 200], [0.80, 500]],
            "no": [[0.15, 150], [0.18, 250]],
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_allows_first_request(self):
        limiter = RateLimiter(rate=10.0)
        # Should not block on first call
        await limiter.acquire()

    @pytest.mark.asyncio
    async def test_rate_limits_requests(self):
        import time
        limiter = RateLimiter(rate=5.0)  # 5 req/sec = 200ms between
        await limiter.acquire()
        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.15  # Should wait ~200ms (with some tolerance)


class TestKalshiClientParsing:
    """Test that API responses are correctly parsed into models."""

    @pytest.mark.asyncio
    async def test_get_markets_parsing(self, mock_client, sample_market_data):
        """Test that get_markets correctly parses the response."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=sample_market_data)

        mock_client._session.request = MagicMock(
            return_value=AsyncContextManager(mock_response)
        )

        result = await mock_client.get_markets(series_ticker="KXBTC")

        assert isinstance(result, GetMarketsResponse)
        assert len(result.markets) == 2
        assert result.markets[0].ticker == "KXBTC-26FEB14-T70000"
        assert result.markets[1].yes_bid == 0.65

    @pytest.mark.asyncio
    async def test_get_orderbook_parsing(self, mock_client, sample_orderbook_data):
        """Test orderbook parsing with YES/NO bids."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=sample_orderbook_data)

        mock_client._session.request = MagicMock(
            return_value=AsyncContextManager(mock_response)
        )

        result = await mock_client.get_orderbook("KXBTC-26FEB14-T70000")

        assert isinstance(result, OrderBook)
        assert len(result.yes_bids) == 3
        assert len(result.no_bids) == 2
        assert result.yes_bids[0][0] == 0.85

    @pytest.mark.asyncio
    async def test_get_market_single(self, mock_client):
        """Test fetching a single market."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "market": {
                "ticker": "KXBTC-26FEB14-T70000",
                "title": "Bitcoin",
                "status": "active",
            }
        })

        mock_client._session.request = MagicMock(
            return_value=AsyncContextManager(mock_response)
        )

        result = await mock_client.get_market("KXBTC-26FEB14-T70000")
        assert isinstance(result, Market)
        assert result.ticker == "KXBTC-26FEB14-T70000"

    @pytest.mark.asyncio
    async def test_create_order(self, mock_client):
        """Test order creation."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "order": {
                "order_id": "abc123",
                "ticker": "KXBTC-26FEB14-T70000",
                "side": "yes",
                "action": "buy",
                "status": "resting",
                "yes_price": 85,
                "count": 10,
            }
        })

        mock_client._session.request = MagicMock(
            return_value=AsyncContextManager(mock_response)
        )

        # Mock _auth_headers to avoid real RSA signing
        mock_client._auth_headers = MagicMock(return_value={
            "KALSHI-ACCESS-KEY": "test-key",
            "KALSHI-ACCESS-SIGNATURE": "dummysig",
            "KALSHI-ACCESS-TIMESTAMP": "1234567890",
            "Content-Type": "application/json",
        })

        request = CreateOrderRequest(
            ticker="KXBTC-26FEB14-T70000",
            side="yes",
            action="buy",
            client_order_id="test-uuid",
            count=10,
            yes_price=85,
            post_only=True,
        )

        result = await mock_client.create_order(request)
        assert isinstance(result, Order)
        assert result.order_id == "abc123"
        assert result.yes_price == 85


class TestClientURL:
    def test_url_construction(self, mock_client):
        """Test that URLs are constructed correctly."""
        url = mock_client._url("/markets")
        assert url == "https://demo-api.kalshi.co/trade-api/v2/markets"

        url = mock_client._url("/portfolio/balance")
        assert url == "https://demo-api.kalshi.co/trade-api/v2/portfolio/balance"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class AsyncContextManager:
    """Helper to make an AsyncMock usable as an async context manager."""

    def __init__(self, mock_response):
        self.mock_response = mock_response

    async def __aenter__(self):
        return self.mock_response

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
