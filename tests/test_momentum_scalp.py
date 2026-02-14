"""
Tests for strategies/momentum_scalp.py — Momentum scalping signal generation.

Uses mocked market data and orderbook state to test:
- Signal generation for deep ITM opportunities
- Edge calculation after fees
- Filter criteria (fair value, entry price, book depth, time to settle)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from data.market_scanner import MarketScanner
from data.orderbook import OrderBookManager
from data.price_feed import PriceFeed
from execution.order_manager import OrderManager
from execution.position_tracker import PositionTracker
from kalshi.client import KalshiClient
from kalshi.models import Event, Market
from risk.risk_manager import RiskManager
from strategies.momentum_scalp import MomentumScalpStrategy


def future_time(hours: int = 4) -> str:
    """Return an ISO timestamp hours from now (for test market expiration)."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_components():
    """Create mock versions of all strategy dependencies."""
    client = AsyncMock(spec=KalshiClient)
    price_feed = MagicMock(spec=PriceFeed)
    orderbook = OrderBookManager()
    order_manager = AsyncMock(spec=OrderManager)
    order_manager.paper_mode = True
    position_tracker = PositionTracker()
    position_tracker.initial_balance = 10000.0
    risk_manager = RiskManager(position_tracker, initial_balance=10000.0)
    market_scanner = AsyncMock(spec=MarketScanner)

    return {
        "client": client,
        "price_feed": price_feed,
        "risk_manager": risk_manager,
        "order_manager": order_manager,
        "position_tracker": position_tracker,
        "orderbook_manager": orderbook,
        "market_scanner": market_scanner,
    }


@pytest.fixture
def strategy(mock_components):
    """Create a MomentumScalpStrategy with mocked dependencies."""
    return MomentumScalpStrategy(**mock_components)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMomentumScalpScan:
    @pytest.mark.asyncio
    async def test_generates_signal_for_deep_itm(self, strategy, mock_components):
        """Test that a signal is generated when all criteria are met."""
        # Setup: BTC at $70,000, strike at $68,000 (deep ITM)
        mock_components["price_feed"].get_price.return_value = 70000.0
        mock_components["price_feed"].get_volatility.return_value = 0.65

        # Mock active events
        mock_event = Event(event_ticker="KXBTC-26FEB14", title="BTC hourly", status="active")
        mock_components["market_scanner"].find_active_events.return_value = [mock_event]

        # Mock strike markets
        expiry = future_time(4)  # 4 hours from now (within 8h limit)
        mock_market = Market(
            ticker="KXBTC-26FEB14-T68000",
            event_ticker="KXBTC-26FEB14",
            title="Bitcoin price today at 5pm EST?",
            subtitle="$68,000 or above",
            status="active",
            close_time=expiry,
            expiration_time=expiry,
        )
        mock_components["market_scanner"].get_event_strikes.return_value = [mock_market]

        # Setup orderbook with ask at 91c and good depth
        from kalshi.models import OrderBook
        ob = OrderBook(
            ticker="KXBTC-26FEB14-T68000",
            yes_bids=[[0.89, 50]],
            no_bids=[[0.09, 30]],  # YES ask = 1.00 - 0.09 = 0.91
        )
        mock_components["orderbook_manager"].update_from_rest("KXBTC-26FEB14-T68000", ob)

        signals = await strategy.scan()

        # Should find at least one signal
        assert len(signals) >= 1
        signal = signals[0]
        assert signal.strategy == "momentum_scalp"
        assert signal.ticker == "KXBTC-26FEB14-T68000"
        assert signal.side == "yes"
        assert signal.action == "buy"
        assert signal.edge_cents > 0

    @pytest.mark.asyncio
    async def test_no_signal_when_ask_too_high(self, strategy, mock_components):
        """Test that no signal is generated when ask > 93c."""
        mock_components["price_feed"].get_price.return_value = 70000.0
        mock_components["price_feed"].get_volatility.return_value = 0.65

        mock_event = Event(event_ticker="KXBTC-26FEB14", status="active")
        mock_components["market_scanner"].find_active_events.return_value = [mock_event]

        mock_market = Market(
            ticker="KXBTC-26FEB14-T68000",
            subtitle="$68,000 or above",
            status="active",
            expiration_time=future_time(4),
        )
        mock_components["market_scanner"].get_event_strikes.return_value = [mock_market]

        # Ask at 95c (too high)
        from kalshi.models import OrderBook
        ob = OrderBook(
            yes_bids=[[0.93, 50]],
            no_bids=[[0.05, 30]],  # YES ask = 0.95
        )
        mock_components["orderbook_manager"].update_from_rest("KXBTC-26FEB14-T68000", ob)

        signals = await strategy.scan()
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_signal_when_insufficient_depth(self, strategy, mock_components):
        """Test that no signal is generated when book depth < 20."""
        mock_components["price_feed"].get_price.return_value = 70000.0
        mock_components["price_feed"].get_volatility.return_value = 0.65

        mock_event = Event(event_ticker="KXBTC-26FEB14", status="active")
        mock_components["market_scanner"].find_active_events.return_value = [mock_event]

        mock_market = Market(
            ticker="KXBTC-26FEB14-T68000",
            subtitle="$68,000 or above",
            status="active",
            expiration_time=future_time(4),
        )
        mock_components["market_scanner"].get_event_strikes.return_value = [mock_market]

        # Only 5 contracts at ask (below min depth of 20)
        from kalshi.models import OrderBook
        ob = OrderBook(
            yes_bids=[[0.89, 50]],
            no_bids=[[0.09, 5]],  # YES ask qty = 5
        )
        mock_components["orderbook_manager"].update_from_rest("KXBTC-26FEB14-T68000", ob)

        signals = await strategy.scan()
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_no_signal_when_no_price(self, strategy, mock_components):
        """Test that no signals when price feed returns 0."""
        mock_components["price_feed"].get_price.return_value = 0.0
        mock_components["market_scanner"].find_active_events.return_value = []

        signals = await strategy.scan()
        assert len(signals) == 0


class TestMomentumScalpExecute:
    @pytest.mark.asyncio
    async def test_execute_places_orders(self, strategy, mock_components):
        """Test that execute places orders for each signal."""
        from strategies.base import TradeSignal

        signals = [
            TradeSignal(
                strategy="momentum_scalp",
                ticker="KXBTC-26FEB14-T68000",
                side="yes",
                action="buy",
                price_cents=90,
                contracts=20,
                edge_cents=5.0,
                confidence=0.95,
                post_only=True,
                reason="Test signal",
            )
        ]

        mock_components["order_manager"].place_order = AsyncMock(return_value=MagicMock())

        await strategy.execute(signals)

        mock_components["order_manager"].place_order.assert_called_once()
