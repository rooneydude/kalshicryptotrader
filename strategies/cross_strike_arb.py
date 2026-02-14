"""
Strategy 3: Cross-Strike Arbitrage

Scans all strikes within a crypto event for mathematical mispricings:

1. Monotonicity violations: P(above $68K) should >= P(above $69K)
2. YES + NO parity: buying YES + NO on the same strike should cost ~$1.00
3. Range sum: sum of all range market YES prices should approximate $1.00

When a mispricing is found, the bot places both legs simultaneously
using batch orders.
"""

from __future__ import annotations

import config
from data.market_scanner import MarketScanner
from execution.fee_calculator import calculate_fee
from strategies.base import BaseStrategy, TradeSignal
from utils.logger import get_logger

log = get_logger("strategies.cross_strike_arb")


class CrossStrikeArbStrategy(BaseStrategy):
    """
    Cross-strike arbitrage across Kalshi crypto events.

    Scans for three types of mispricings within each event:
    - Monotonicity: adjacent strike prices out of order
    - Parity: YES + NO doesn't sum to $1.00
    - Range sum: all range markets don't sum to $1.00
    """

    @property
    def name(self) -> str:
        return "cross_strike_arb"

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    async def scan(self) -> list[TradeSignal]:
        """
        Scan all active crypto events for arbitrage opportunities.

        Returns combined signals from all three checks.
        """
        all_signals: list[TradeSignal] = []

        for asset in config.SUPPORTED_ASSETS:
            try:
                events = await self.market_scanner.find_active_events(asset)
            except Exception:
                log.exception("Failed to find events for %s", asset)
                continue

            for event in events:
                try:
                    strikes = await self.market_scanner.get_event_strikes(
                        event.event_ticker
                    )
                except Exception:
                    continue

                if len(strikes) < 2:
                    continue

                # Run all three checks
                mono_signals = self._check_monotonicity(strikes, event.event_ticker)
                parity_signals = self._check_parity(strikes, event.event_ticker)
                range_signals = self._check_range_sum(strikes, event.event_ticker)

                all_signals.extend(mono_signals)
                all_signals.extend(parity_signals)
                all_signals.extend(range_signals)

        # Sort by edge descending
        all_signals.sort(key=lambda s: s.edge_cents, reverse=True)
        return all_signals

    # ------------------------------------------------------------------
    # Check 1: Monotonicity
    # ------------------------------------------------------------------

    def _check_monotonicity(
        self,
        strikes: list,
        event_ticker: str,
    ) -> list[TradeSignal]:
        """
        Check that P(above $X) >= P(above $Y) when X < Y.

        If a higher strike is priced higher than a lower strike,
        we can buy the low-strike YES and sell the high-strike YES
        for a guaranteed profit.
        """
        signals: list[TradeSignal] = []

        # Filter to "above" type with parseable strikes
        above_markets: list[tuple[float, object]] = []
        for m in strikes:
            if MarketScanner.classify_market_type(m) != "above":
                continue
            s = MarketScanner.parse_strike_price(m)
            if s is not None:
                above_markets.append((s, m))

        above_markets.sort(key=lambda x: x[0])  # Ascending by strike

        for i in range(len(above_markets) - 1):
            low_strike_val, low_market = above_markets[i]
            high_strike_val, high_market = above_markets[i + 1]

            # Get prices
            low_ask = self.orderbook.get_best_yes_ask(low_market.ticker)
            high_bid = self.orderbook.get_best_yes_bid(high_market.ticker)

            if low_ask is None or high_bid is None:
                continue

            low_ask_price, low_ask_qty = low_ask
            high_bid_price, high_bid_qty = high_bid

            # Monotonicity violation: higher strike bid > lower strike ask
            if high_bid_price > low_ask_price:
                edge = high_bid_price - low_ask_price
                contracts = min(
                    low_ask_qty,
                    high_bid_qty,
                    config.ARB_MAX_CONTRACTS,
                )

                if contracts <= 0:
                    continue

                # Calculate total fees for both legs
                buy_fee = calculate_fee(contracts, low_ask_price, is_maker=False)
                sell_fee = calculate_fee(contracts, high_bid_price, is_maker=False)
                total_fees = buy_fee + sell_fee
                net_profit = edge * contracts - total_fees
                edge_cents = (net_profit / contracts) * 100 if contracts else 0

                if edge_cents < config.ARB_MIN_PROFIT_CENTS:
                    continue

                # Leg 1: Buy low-strike YES
                signals.append(TradeSignal(
                    strategy=self.name,
                    ticker=low_market.ticker,
                    side="yes",
                    action="buy",
                    price_cents=int(low_ask_price * 100),
                    contracts=contracts,
                    edge_cents=edge_cents,
                    confidence=0.9,
                    post_only=False,  # Taker for speed (arb is time-sensitive)
                    reason=(
                        f"Monotonicity arb: buy {low_market.ticker} YES @ "
                        f"{low_ask_price:.2f}, sell {high_market.ticker} YES @ "
                        f"{high_bid_price:.2f}, edge={edge_cents:.1f}c"
                    ),
                    event_ticker=event_ticker,
                ))

                # Leg 2: Sell high-strike YES
                signals.append(TradeSignal(
                    strategy=self.name,
                    ticker=high_market.ticker,
                    side="yes",
                    action="sell",
                    price_cents=int(high_bid_price * 100),
                    contracts=contracts,
                    edge_cents=edge_cents,
                    confidence=0.9,
                    post_only=False,
                    reason=(
                        f"Monotonicity arb (leg 2): sell {high_market.ticker} YES @ "
                        f"{high_bid_price:.2f}"
                    ),
                    event_ticker=event_ticker,
                ))

                log.info(
                    "Monotonicity arb found: %s (ask=%.2f) vs %s (bid=%.2f), edge=%.1fc",
                    low_market.ticker,
                    low_ask_price,
                    high_market.ticker,
                    high_bid_price,
                    edge_cents,
                )

        return signals

    # ------------------------------------------------------------------
    # Check 2: YES + NO parity
    # ------------------------------------------------------------------

    def _check_parity(
        self,
        strikes: list,
        event_ticker: str,
    ) -> list[TradeSignal]:
        """
        Check that YES_ask + NO_ask >= $1.00 for each strike.

        If YES_ask + NO_ask < $1.00, we can buy both and are
        guaranteed a $1.00 payout regardless of outcome.
        """
        signals: list[TradeSignal] = []

        for market in strikes:
            yes_ask = self.orderbook.get_best_yes_ask(market.ticker)
            no_ask = self.orderbook.get_best_no_ask(market.ticker)

            if yes_ask is None or no_ask is None:
                continue

            yes_ask_price, yes_ask_qty = yes_ask
            no_ask_price, no_ask_qty = no_ask

            total_cost = yes_ask_price + no_ask_price

            if total_cost >= 1.00:
                continue

            gap = 1.00 - total_cost
            contracts = min(
                yes_ask_qty,
                no_ask_qty,
                config.ARB_MAX_CONTRACTS,
            )

            if contracts <= 0:
                continue

            # Calculate fees
            yes_fee = calculate_fee(contracts, yes_ask_price, is_maker=False)
            no_fee = calculate_fee(contracts, no_ask_price, is_maker=False)
            total_fees = yes_fee + no_fee

            net_profit = gap * contracts - total_fees
            edge_cents = (net_profit / contracts) * 100 if contracts else 0

            if edge_cents < config.ARB_MIN_PROFIT_CENTS:
                continue

            # Leg 1: Buy YES
            signals.append(TradeSignal(
                strategy=self.name,
                ticker=market.ticker,
                side="yes",
                action="buy",
                price_cents=int(yes_ask_price * 100),
                contracts=contracts,
                edge_cents=edge_cents,
                confidence=0.95,
                post_only=False,
                reason=(
                    f"Parity arb: buy YES @ {yes_ask_price:.2f} + NO @ "
                    f"{no_ask_price:.2f} = {total_cost:.2f} < $1.00, "
                    f"gap={gap:.2f}, edge={edge_cents:.1f}c"
                ),
                event_ticker=event_ticker,
            ))

            # Leg 2: Buy NO
            signals.append(TradeSignal(
                strategy=self.name,
                ticker=market.ticker,
                side="no",
                action="buy",
                price_cents=int(no_ask_price * 100),
                contracts=contracts,
                edge_cents=edge_cents,
                confidence=0.95,
                post_only=False,
                reason=(
                    f"Parity arb (leg 2): buy NO @ {no_ask_price:.2f}"
                ),
                event_ticker=event_ticker,
            ))

            log.info(
                "Parity arb found on %s: YES=%.2f + NO=%.2f = %.2f, edge=%.1fc",
                market.ticker,
                yes_ask_price,
                no_ask_price,
                total_cost,
                edge_cents,
            )

        return signals

    # ------------------------------------------------------------------
    # Check 3: Range sum
    # ------------------------------------------------------------------

    def _check_range_sum(
        self,
        strikes: list,
        event_ticker: str,
    ) -> list[TradeSignal]:
        """
        Check that the sum of all range market YES asks approximates $1.00.

        If the sum is significantly below $1.00, buying YES on all ranges
        guarantees a $1.00 payout.
        """
        signals: list[TradeSignal] = []

        range_markets = [
            m for m in strikes
            if MarketScanner.classify_market_type(m) == "range"
        ]

        if len(range_markets) < 2:
            return []

        # Get all asks
        asks: list[tuple[object, float, int]] = []
        total_ask = 0.0

        for market in range_markets:
            ask = self.orderbook.get_best_yes_ask(market.ticker)
            if ask is None:
                return []  # Need all range markets to have asks
            price, qty = ask
            asks.append((market, price, qty))
            total_ask += price

        if total_ask >= 0.95:
            return []  # Not significantly under $1.00

        gap = 1.00 - total_ask

        # Minimum contracts available across all legs
        max_contracts = min(
            min(qty for _, _, qty in asks),
            config.ARB_MAX_CONTRACTS,
        )

        if max_contracts <= 0:
            return []

        # Calculate total fees for buying all ranges
        total_fees = sum(
            calculate_fee(max_contracts, price, is_maker=False)
            for _, price, _ in asks
        )

        net_profit = gap * max_contracts - total_fees
        edge_cents = (net_profit / max_contracts) * 100 if max_contracts else 0

        if edge_cents < config.ARB_MIN_PROFIT_CENTS:
            return []

        # Generate signals for each range leg
        for market, price, qty in asks:
            signals.append(TradeSignal(
                strategy=self.name,
                ticker=market.ticker,
                side="yes",
                action="buy",
                price_cents=int(price * 100),
                contracts=max_contracts,
                edge_cents=edge_cents,
                confidence=0.85,
                post_only=False,
                reason=(
                    f"Range sum arb: buy all ranges, sum={total_ask:.2f}, "
                    f"gap={gap:.2f}, edge={edge_cents:.1f}c"
                ),
                event_ticker=event_ticker,
            ))

        if signals:
            log.info(
                "Range sum arb found in %s: %d ranges, sum=%.2f, gap=%.2f, edge=%.1fc",
                event_ticker,
                len(range_markets),
                total_ask,
                gap,
                edge_cents,
            )

        return signals

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, signals: list[TradeSignal]) -> None:
        """
        Execute arb signals. Both legs must execute or neither.

        Uses batch orders for atomicity. If one leg fills but the
        other doesn't within 30 seconds, cancel the unfilled leg.
        """
        # Group signals by event (arb legs belong together)
        # For monotonicity and parity, signals come in pairs
        # For range sum, all signals for an event form one group

        if len(signals) < 2:
            return

        # Try batch placement
        log.info(
            "[%s] Executing %d arb leg(s)",
            self.name,
            len(signals),
        )

        orders = await self.order_manager.batch_place(signals)

        if len(orders) < len(signals):
            log.warning(
                "[%s] Only %d/%d legs placed — checking for partial fills",
                self.name,
                len(orders),
                len(signals),
            )

        # In live mode, monitor for partial fills
        if not self.order_manager.paper_mode:
            import asyncio
            asyncio.create_task(
                self._monitor_arb_legs(orders, timeout=30)
            )

    async def _monitor_arb_legs(
        self,
        orders: list,
        timeout: int = 30,
    ) -> None:
        """
        Monitor arb legs and cancel unfilled ones after timeout.

        If only some legs fill, the unfilled ones are cancelled
        and the situation is logged as a risk event.
        """
        import asyncio
        await asyncio.sleep(timeout)

        filled = []
        unfilled = []

        for order in orders:
            status = self.order_manager.get_order_status(order.order_id)
            if status and status.status in ("executed", "filled"):
                filled.append(order)
            elif status and status.status == "resting":
                unfilled.append(order)

        if unfilled:
            log.warning(
                "[%s] PARTIAL FILL: %d/%d legs filled, cancelling %d unfilled",
                self.name,
                len(filled),
                len(orders),
                len(unfilled),
            )
            for order in unfilled:
                await self.order_manager.cancel_order(order.order_id)
