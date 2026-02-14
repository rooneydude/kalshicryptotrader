"""
Tests for strategies/cross_strike_arb.py — Arbitrage detection.

Tests:
- Monotonicity violation detection
- YES + NO parity break detection
- Range sum underpricing detection
- No false positives when fees exceed edge
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
from strategies.cross_strike_arb import CrossStrikeArbStrategy

import config


@pytest.fixture
def mock_components():
    client = AsyncMock(spec=KalshiClient)
    price_feed = MagicMock(spec=PriceFeed)
    orderbook = OrderBookManager()
    order_manager = AsyncMock(spec=OrderManager)
    order_manager.paper_mode = True
    order_manager.batch_place = AsyncMock(return_value=[])
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
    return CrossStrikeArbStrategy(**mock_components)


class TestMonotonicity:
    @pytest.mark.asyncio
    async def test_detects_monotonicity_violation(self, strategy, mock_components):
        """
        P(above $68K) should >= P(above $69K).
        If the $69K strike YES bid > $68K strike YES ask, that's an arb.
        """
        # Setup markets
        low_market = Market(
            ticker="KXBTC-26FEB14-T68000",
            subtitle="$68,000 or above",
            status="active",
        )
        high_market = Market(
            ticker="KXBTC-26FEB14-T69000",
            subtitle="$69,000 or above",
            status="active",
        )

        mock_event = Event(
            event_ticker="KXBTC-26FEB14",
            status="active",
        )
        mock_components["market_scanner"].find_active_events.return_value = [mock_event]
        mock_components["market_scanner"].get_event_strikes.return_value = [
            low_market, high_market
        ]

        # Setup orderbook: higher strike YES bid > lower strike YES ask
        # Low strike: YES ask = 1.00 - 0.20 = 0.80
        ob_low = OrderBook(
            yes_bids=[[0.78, 100]],
            no_bids=[[0.20, 100]],  # YES ask = 0.80
        )
        mock_components["orderbook_manager"].update_from_rest("KXBTC-26FEB14-T68000", ob_low)

        # High strike: YES bid = 0.85 (higher than low strike's ask of 0.80!)
        ob_high = OrderBook(
            yes_bids=[[0.85, 100]],
            no_bids=[[0.10, 100]],
        )
        mock_components["orderbook_manager"].update_from_rest("KXBTC-26FEB14-T69000", ob_high)

        signals = await strategy.scan()

        # Should find monotonicity arb signals (2 legs per asset scanning)
        # scan() iterates over BTC, ETH, SOL — each returns the same mock event
        mono_signals = [s for s in signals if "Monotonicity" in s.reason]
        assert len(mono_signals) >= 2  # At least one pair of legs
        assert len(mono_signals) % 2 == 0  # Always in pairs

        # Each pair should have a buy and a sell
        actions = {s.action for s in mono_signals}
        assert "buy" in actions
        assert "sell" in actions

    @pytest.mark.asyncio
    async def test_no_arb_when_properly_ordered(self, strategy, mock_components):
        """No arb when lower strike is priced higher than higher strike."""
        low_market = Market(
            ticker="KXBTC-26FEB14-T68000",
            subtitle="$68,000 or above",
            status="active",
        )
        high_market = Market(
            ticker="KXBTC-26FEB14-T69000",
            subtitle="$69,000 or above",
            status="active",
        )

        mock_event = Event(event_ticker="KXBTC-26FEB14", status="active")
        mock_components["market_scanner"].find_active_events.return_value = [mock_event]
        mock_components["market_scanner"].get_event_strikes.return_value = [
            low_market, high_market
        ]

        # Properly ordered: low strike ask < high strike bid? No.
        # Low strike YES ask = 0.80, high strike YES bid = 0.70 — no arb
        ob_low = OrderBook(
            yes_bids=[[0.78, 100]],
            no_bids=[[0.20, 100]],
        )
        mock_components["orderbook_manager"].update_from_rest("KXBTC-26FEB14-T68000", ob_low)

        ob_high = OrderBook(
            yes_bids=[[0.70, 100]],
            no_bids=[[0.35, 100]],
        )
        mock_components["orderbook_manager"].update_from_rest("KXBTC-26FEB14-T69000", ob_high)

        signals = await strategy.scan()
        mono_signals = [s for s in signals if "Monotonicity" in s.reason]
        assert len(mono_signals) == 0


class TestParity:
    @pytest.mark.asyncio
    async def test_detects_parity_break(self, strategy, mock_components):
        """
        YES ask + NO ask should sum to ~$1.00.
        If sum < $1.00, buying both guarantees profit.
        """
        # Need at least 2 markets (scan() skips events with < 2 strikes)
        market1 = Market(
            ticker="KXBTC-26FEB14-T70000",
            subtitle="$70,000 or above",
            status="active",
        )
        market2 = Market(
            ticker="KXBTC-26FEB14-T71000",
            subtitle="$71,000 or above",
            status="active",
        )

        mock_event = Event(event_ticker="KXBTC-26FEB14", status="active")
        mock_components["market_scanner"].find_active_events.return_value = [mock_event]
        mock_components["market_scanner"].get_event_strikes.return_value = [market1, market2]

        # Market 1: parity break
        # YES ask = 1.00 - 0.55 = 0.45, NO ask = 1.00 - 0.60 = 0.40
        # Total = 0.45 + 0.40 = 0.85 < $1.00 → parity break!
        ob1 = OrderBook(
            yes_bids=[[0.60, 200]],  # NO ask = 0.40
            no_bids=[[0.55, 200]],   # YES ask = 0.45
        )
        mock_components["orderbook_manager"].update_from_rest("KXBTC-26FEB14-T70000", ob1)

        # Market 2: no parity break (YES ask + NO ask = 1.02)
        ob2 = OrderBook(
            yes_bids=[[0.50, 200]],  # NO ask = 0.50
            no_bids=[[0.48, 200]],   # YES ask = 0.52
        )
        mock_components["orderbook_manager"].update_from_rest("KXBTC-26FEB14-T71000", ob2)

        signals = await strategy.scan()
        parity_signals = [s for s in signals if "Parity" in s.reason]

        # Should find parity arb signals (2 legs per occurrence per asset)
        assert len(parity_signals) >= 2
        sides = {s.side for s in parity_signals}
        assert "yes" in sides
        assert "no" in sides

    @pytest.mark.asyncio
    async def test_no_parity_arb_when_sum_at_1(self, strategy, mock_components):
        """No arb when YES ask + NO ask >= $1.00."""
        market1 = Market(
            ticker="KXBTC-26FEB14-T70000",
            subtitle="$70,000 or above",
            status="active",
        )
        market2 = Market(
            ticker="KXBTC-26FEB14-T71000",
            subtitle="$71,000 or above",
            status="active",
        )

        mock_event = Event(event_ticker="KXBTC-26FEB14", status="active")
        mock_components["market_scanner"].find_active_events.return_value = [mock_event]
        mock_components["market_scanner"].get_event_strikes.return_value = [market1, market2]

        # Both markets: no parity break
        for ticker in ["KXBTC-26FEB14-T70000", "KXBTC-26FEB14-T71000"]:
            ob = OrderBook(
                yes_bids=[[0.50, 200]],  # NO ask = 0.50
                no_bids=[[0.48, 200]],   # YES ask = 0.52
            )
            mock_components["orderbook_manager"].update_from_rest(ticker, ob)

        signals = await strategy.scan()
        parity_signals = [s for s in signals if "Parity" in s.reason]
        assert len(parity_signals) == 0


class TestRangeSum:
    @pytest.mark.asyncio
    async def test_detects_range_sum_underpricing(self, strategy, mock_components):
        """
        If range markets sum to < $0.95, buying all gives guaranteed $1.00 payout.
        """
        range_markets = [
            Market(
                ticker=f"KXBTC-26FEB14-R{i}",
                title=f"BTC between ${68000 + i*1000} and ${69000 + i*1000}",
                subtitle=f"Between ${68000 + i*1000} and ${69000 + i*1000}",
                status="active",
            )
            for i in range(5)
        ]

        mock_event = Event(event_ticker="KXBTC-26FEB14", status="active")
        mock_components["market_scanner"].find_active_events.return_value = [mock_event]
        mock_components["market_scanner"].get_event_strikes.return_value = range_markets

        # Each range priced at ~15c, total = 75c < $0.95
        for i, market in enumerate(range_markets):
            ob = OrderBook(
                yes_bids=[[0.13, 200]],
                no_bids=[[0.85, 200]],  # YES ask = 0.15
            )
            mock_components["orderbook_manager"].update_from_rest(market.ticker, ob)

        signals = await strategy.scan()
        range_signals = [s for s in signals if "Range sum" in s.reason]

        # Should find range sum arb (5 legs per asset scan, 3 assets = 15)
        assert len(range_signals) >= 5
        assert len(range_signals) % 5 == 0  # Always in groups of 5


class TestExecution:
    @pytest.mark.asyncio
    async def test_execute_uses_batch_place(self, strategy, mock_components):
        """Test that arb execution uses batch_place for atomicity."""
        from strategies.base import TradeSignal

        signals = [
            TradeSignal(
                strategy="cross_strike_arb",
                ticker="KXBTC-26FEB14-T68000",
                side="yes",
                action="buy",
                price_cents=80,
                contracts=50,
                edge_cents=3.0,
                confidence=0.9,
                reason="Test leg 1",
            ),
            TradeSignal(
                strategy="cross_strike_arb",
                ticker="KXBTC-26FEB14-T69000",
                side="yes",
                action="sell",
                price_cents=85,
                contracts=50,
                edge_cents=3.0,
                confidence=0.9,
                reason="Test leg 2",
            ),
        ]

        await strategy.execute(signals)
        mock_components["order_manager"].batch_place.assert_called_once_with(signals)
