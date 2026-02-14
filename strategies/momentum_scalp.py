"""
Strategy 1: Momentum Scalping

Buy deep in-the-money (ITM) YES contracts when the spot price is far
above the strike but the Kalshi orderbook hasn't fully repriced yet.

The edge comes from the delay between spot price moves on Binance and
the Kalshi book updating. Deep ITM contracts (fair value >= 90c) should
be priced near $1.00 but often trade at 90-93c.
"""

from __future__ import annotations

import asyncio

import config
from data.market_scanner import MarketScanner
from strategies.base import BaseStrategy, TradeSignal
from utils.fair_value import calculate_fair_value
from execution.fee_calculator import calculate_fee
from utils.logger import get_logger

log = get_logger("strategies.momentum_scalp")


class MomentumScalpStrategy(BaseStrategy):
    """
    Deep ITM momentum scalping strategy.

    Scans hourly and daily crypto events for strikes where:
    - Fair value >= 90c (deep ITM)
    - Best YES ask <= 93c (underpriced)
    - Edge after fees >= 3c
    - Sufficient book depth (>= 20 contracts)
    - Settlement within 8 hours
    """

    @property
    def name(self) -> str:
        return "momentum_scalp"

    async def scan(self) -> list[TradeSignal]:
        """
        Scan for deep ITM YES contracts that are underpriced.

        Pipeline:
        1. Get current spot prices
        2. Get all active hourly + daily crypto events
        3. For each strike, calculate fair value and compare to ask
        4. Generate signals for profitable opportunities
        """
        signals: list[TradeSignal] = []

        # Get current spot prices
        for asset in config.SUPPORTED_ASSETS:
            try:
                spot = self.price_feed.get_price(asset)
            except (ValueError, AttributeError):
                continue

            if spot <= 0:
                continue

            vol = self.price_feed.get_volatility(asset)

            # Get active events for this asset
            try:
                events = await self.market_scanner.find_active_events(asset)
            except Exception:
                log.exception("Failed to find events for %s", asset)
                continue

            for event in events:
                event_signals = await self._scan_event(
                    event.event_ticker, spot, vol, asset
                )
                signals.extend(event_signals)

        # Sort by edge descending, return top 5
        signals.sort(key=lambda s: s.edge_cents, reverse=True)
        return signals[:5]

    async def _scan_event(
        self,
        event_ticker: str,
        spot: float,
        vol: float,
        asset: str,
    ) -> list[TradeSignal]:
        """Scan a single event for scalping opportunities."""
        signals: list[TradeSignal] = []

        try:
            strikes = await self.market_scanner.get_event_strikes(event_ticker)
        except Exception:
            log.exception("Failed to get strikes for %s", event_ticker)
            return []

        for market in strikes:
            # Only consider "above" markets
            market_type = MarketScanner.classify_market_type(market)
            if market_type != "above":
                continue

            # Parse strike price
            strike = MarketScanner.parse_strike_price(market)
            if strike is None:
                continue

            # Calculate time to expiry
            hours = MarketScanner.get_hours_to_expiry(market)
            if hours is None or hours <= 0:
                continue

            if hours > config.SCALP_MAX_TIME_TO_SETTLE_HOURS:
                continue

            # Calculate fair value
            fair = calculate_fair_value(spot, strike, vol, hours, "above")

            if fair < config.SCALP_MIN_YES_FAIR_VALUE:
                continue

            # Get current best YES ask from orderbook
            best_ask = self.orderbook.get_best_yes_ask(market.ticker)
            if best_ask is None:
                continue

            ask_price, ask_qty = best_ask

            if ask_price > config.SCALP_MAX_ENTRY_PRICE:
                continue

            if ask_qty < config.SCALP_MIN_BOOK_DEPTH:
                continue

            # Calculate edge after fees
            # If we buy at ask and hold to settlement ($1.00), profit = 1.00 - ask - fees
            contracts = min(
                ask_qty,
                config.SCALP_MIN_BOOK_DEPTH,
                int(self.risk_manager.get_available_capital() * config.MAX_SINGLE_TRADE_PCT / ask_price),
            )

            if contracts <= 0:
                continue

            buy_fee = calculate_fee(contracts, ask_price, is_maker=config.SCALP_PREFER_MAKER)
            # At settlement, settled contracts pay no fee (settlement at $1.00)
            # But we use the conservative estimate with the buy fee
            expected_profit_per_contract = fair - ask_price
            total_fees_per_contract = buy_fee / contracts

            edge_dollars = expected_profit_per_contract - total_fees_per_contract
            edge_cents = edge_dollars * 100

            if edge_cents < config.SCALP_MIN_EDGE_CENTS:
                continue

            # Determine price: place 1c above best YES bid for queue priority
            best_bid = self.orderbook.get_best_yes_bid(market.ticker)
            if best_bid:
                bid_price = best_bid[0]
                entry_price_cents = int((bid_price + 0.01) * 100)
            else:
                entry_price_cents = int(ask_price * 100)

            # Ensure we're still within max entry price
            if entry_price_cents / 100.0 > config.SCALP_MAX_ENTRY_PRICE:
                entry_price_cents = int(config.SCALP_MAX_ENTRY_PRICE * 100)

            signal = TradeSignal(
                strategy=self.name,
                ticker=market.ticker,
                side="yes",
                action="buy",
                price_cents=entry_price_cents,
                contracts=contracts,
                edge_cents=edge_cents,
                confidence=min(fair, 0.99),
                post_only=config.SCALP_PREFER_MAKER,
                reason=(
                    f"{asset} spot=${spot:.0f} > strike=${strike:.0f}, "
                    f"fair={fair:.3f}, ask={ask_price:.2f}, "
                    f"edge={edge_cents:.1f}c, {hours:.1f}h to settle"
                ),
                event_ticker=event_ticker,
            )
            signals.append(signal)

        return signals

    async def execute(self, signals: list[TradeSignal]) -> None:
        """
        Execute scalp signals.

        For each signal:
        1. Place a limit order (post_only for maker fees)
        2. Log the attempt
        3. If not filled within 60 seconds, cancel
        """
        for signal in signals:
            log.info(
                "[%s] Executing: %s %s %s x%d @ %dc | %s",
                self.name,
                signal.ticker,
                signal.side,
                signal.action,
                signal.contracts,
                signal.price_cents,
                signal.reason,
            )

            order = await self.order_manager.place_order(
                ticker=signal.ticker,
                side=signal.side,
                action=signal.action,
                price_cents=signal.price_cents,
                contracts=signal.contracts,
                post_only=signal.post_only,
            )

            if order is None:
                log.warning("[%s] Order rejected for %s", self.name, signal.ticker)
                continue

            # In live mode, monitor fill for 60 seconds
            if not self.order_manager.paper_mode and order.status == "resting":
                asyncio.create_task(
                    self._monitor_fill(order.order_id, timeout=60)
                )

    async def _monitor_fill(self, order_id: str, timeout: int = 60) -> None:
        """Monitor an order and cancel if not filled within timeout."""
        await asyncio.sleep(timeout)

        order = self.order_manager.get_order_status(order_id)
        if order and order.status == "resting":
            log.info("[%s] Cancelling unfilled order %s after %ds", self.name, order_id, timeout)
            await self.order_manager.cancel_order(order_id)
