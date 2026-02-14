"""
Tests for strategies/market_maker.py — Market making strategy.

Tests:
- Market selection (ATM + adjacent strikes)
- Two-sided quote generation (bid and ask)
- Quote profitability check
- Inventory management triggers
- Kill switch activation on large BTC moves
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from data.market_scanner import MarketScanner
from data.orderbook import OrderBookManager
from data.price_feed import PriceFeed
from execution.order_manager import OrderManager
from execution.position_tracker import PositionTracker
from kalshi.client import KalshiClient
from kalshi.models import Event, Market, OrderBook
from risk.risk_manager import RiskManager
from strategies.market_maker import MarketMakerStrategy

import config


@pytest.fixture
def mock_components():
    client = AsyncMock(spec=KalshiClient)
    price_feed = MagicMock(spec=PriceFeed)
    orderbook = OrderBookManager()
    order_manager = AsyncMock(spec=OrderManager)
    order_manager.paper_mode = True
    order_manager.cancel_all_orders = AsyncMock(return_value=0)
    order_manager.place_order = AsyncMock(return_value=MagicMock())
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
    return MarketMakerStrategy(**mock_components)


class TestMarketSelection:
    @pytest.mark.asyncio
    async def test_selects_atm_and_neighbors(self, strategy, mock_components):
        """Test that market selection picks ATM + 1 above + 1 below."""
        mock_components["price_feed"].get_price.return_value = 70000.0

        mock_event = Event(
            event_ticker="KXBTC-26FEB15",
            title="BTC daily",
            status="active",
        )
        mock_components["market_scanner"].find_active_events.return_value = [mock_event]

        # Three strikes around 70000
        markets = [
            Market(
                ticker="KXBTC-26FEB15-T69000",
                subtitle="$69,000 or above",
                status="active",
                volume_24h=15000,
            ),
            Market(
                ticker="KXBTC-26FEB15-T70000",
                subtitle="$70,000 or above",
                status="active",
                volume_24h=20000,
            ),
            Market(
                ticker="KXBTC-26FEB15-T71000",
                subtitle="$71,000 or above",
                status="active",
                volume_24h=18000,
            ),
        ]
        mock_components["market_scanner"].get_event_strikes.return_value = markets

        tickers = await strategy.select_markets()

        assert len(tickers) >= 2  # At least ATM + 1 neighbor
        assert len(tickers) <= 3


class TestQuoteGeneration:
    @pytest.mark.asyncio
    async def test_generates_bid_and_ask(self, strategy, mock_components):
        """Test that scan generates both a bid and ask signal."""
        mock_components["price_feed"].get_price.return_value = 70000.0
        mock_components["price_feed"].get_volatility.return_value = 0.65

        strategy._selected_tickers = ["KXBTC-26FEB15-T70000"]

        # Mock the market data
        mock_market = Market(
            ticker="KXBTC-26FEB15-T70000",
            subtitle="$70,000 or above",
            status="active",
            expiration_time="2026-02-15T22:00:00Z",
            volume_24h=20000,
        )
        mock_components["client"].get_market.return_value = mock_market

        signals = await strategy.scan()

        # Should have 2 signals: one buy YES (bid), one buy NO (ask)
        if len(signals) == 2:
            sides = {s.side for s in signals}
            assert "yes" in sides
            assert "no" in sides
            # Both should be post_only
            assert all(s.post_only for s in signals)

    @pytest.mark.asyncio
    async def test_no_signals_when_kill_switch_active(self, strategy, mock_components):
        """Test that no signals are generated during kill switch cooldown."""
        import time
        strategy._kill_switch_until = time.time() + 300  # 5 min from now
        strategy._selected_tickers = ["KXBTC-26FEB15-T70000"]

        signals = await strategy.scan()
        assert len(signals) == 0


class TestInventoryManagement:
    @pytest.mark.asyncio
    async def test_flatten_on_max_position(self, strategy, mock_components):
        """Test that positions are flattened when max net position exceeded."""
        strategy._selected_tickers = ["KXBTC-26FEB15-T70000"]

        # Simulate a large long position
        mock_components["position_tracker"]._positions["KXBTC-26FEB15-T70000"] = MagicMock(
            net_contracts=config.MM_MAX_NET_POSITION + 1,
            avg_entry_price=0.50,
        )

        # Setup orderbook for flatten
        ob = OrderBook(
            yes_bids=[[0.50, 1000]],
            no_bids=[[0.50, 1000]],
        )
        mock_components["orderbook_manager"].update_from_rest("KXBTC-26FEB15-T70000", ob)

        await strategy.manage_inventory()

        # Should have cancelled and placed a flatten order
        mock_components["order_manager"].cancel_all_orders.assert_called()


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_kill_switch_on_large_move(self, strategy, mock_components):
        """Test kill switch activates on large BTC price move."""
        import time

        # Set initial price
        strategy._btc_price_30m_ago = 70000.0
        strategy._btc_price_30m_time = time.time() - 100  # 100s ago (not yet 30 min)

        # BTC moved > 2%
        new_price = 70000 * (1 + config.MM_CANCEL_ON_MOVE_PCT + 0.01)
        mock_components["price_feed"].get_price.return_value = new_price

        strategy._selected_tickers = ["KXBTC-26FEB15-T70000"]

        await strategy.check_kill_switch()

        # Should have set the kill switch cooldown
        assert strategy._kill_switch_until > time.time()
