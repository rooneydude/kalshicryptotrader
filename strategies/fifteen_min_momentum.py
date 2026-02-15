"""
Strategy 4: 15-Minute Momentum

Trades the rolling 15-minute "BTC/ETH/SOL price up in next 15 mins?"
binary markets using short-term price momentum from the Binance feed.

These markets (KXBTC15M, KXETH15M, KXSOL15M) are simple binary contracts:
  - YES pays $1.00 if the crypto price is higher than the strike at close
  - NO pays $1.00 if the crypto price is lower at close

The edge comes from:
  1. Binance real-time price momentum — if BTC has been trending strongly
     in one direction, there's a persistence effect over the next few minutes
  2. The Kalshi orderbook reprices slowly compared to external price moves
  3. Fair value can be estimated from current price vs floor_strike + time left

Strategy flow:
  1. Find all open 15-min markets
  2. For each, get the floor_strike (starting price to beat)
  3. Calculate fair value of YES using Black-Scholes digital pricing
  4. Add a momentum bias: if price is trending up, increase YES fair value
  5. Compare fair value to current YES/NO prices on the orderbook
  6. If edge > min_edge_cents after fees, generate a trade signal
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import config
from data.market_scanner import MarketScanner
from execution.fee_calculator import calculate_fee
from strategies.base import BaseStrategy, TradeSignal
from utils.fair_value import calculate_fair_value
from utils.logger import get_logger

log = get_logger("strategies.fifteen_min")


class FifteenMinMomentumStrategy(BaseStrategy):
    """
    15-minute binary momentum strategy.

    Trades the rolling "price up in 15 mins?" markets by combining
    Black-Scholes fair value with short-term Binance momentum signals.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Track last trade time per ticker to avoid overtrading
        self._last_trade_time: dict[str, float] = {}

    @property
    def name(self) -> str:
        return "fifteen_min_momentum"

    async def scan(self) -> list[TradeSignal]:
        """
        Scan 15-minute up/down markets for momentum-driven opportunities.

        Pipeline:
        1. Find all open 15-min markets
        2. For each, determine asset, floor_strike, and time remaining
        3. Get spot price + momentum from Binance feed
        4. Calculate momentum-adjusted fair value
        5. Compare to orderbook prices for edge
        """
        if not config.FIFTEEN_MIN_ENABLED:
            return []

        signals: list[TradeSignal] = []

        try:
            markets = await self.market_scanner.scan_15m_markets()
        except Exception:
            log.exception("Failed to scan 15-min markets")
            return []

        if not markets:
            log.debug("[%s] No open 15-min markets found", self.name)
            return []

        for market in markets:
            try:
                signal = await self._evaluate_market(market)
                if signal:
                    signals.append(signal)
                else:
                    log.debug("[%s] No signal for %s", self.name, market.ticker)
            except Exception:
                log.exception("[%s] Error evaluating %s", self.name, market.ticker)

        # Sort by edge descending
        signals.sort(key=lambda s: s.edge_cents, reverse=True)
        return signals[:3]  # Top 3 opportunities

    async def _evaluate_market(self, market) -> TradeSignal | None:
        """Evaluate a single 15-min market for a trading opportunity."""

        # 1. Determine the asset
        asset = MarketScanner.get_asset_from_15m_ticker(market.ticker)
        if not asset:
            log.debug("Cannot determine asset for %s", market.ticker)
            return None

        # 2. Get current spot price
        try:
            spot = self.price_feed.get_price(asset)
        except (ValueError, AttributeError):
            return None

        if spot <= 0:
            return None

        # 3. Get the floor strike (price to beat)
        floor_strike = MarketScanner.get_floor_strike_from_market(market)
        if floor_strike is None or floor_strike <= 0:
            log.info("[%s] No floor_strike for %s", self.name, market.ticker)
            return None

        # 4. Calculate time remaining
        time_left_sec = self._get_seconds_to_close(market)
        if time_left_sec is None:
            log.info("[%s] No close_time for %s", self.name, market.ticker)
            return None

        # Don't trade if too little time left (< 2 min)
        if time_left_sec < config.FIFTEEN_MIN_MIN_TIME_LEFT_SEC:
            log.info(
                "[%s] %s: only %ds left, skipping (min=%ds)",
                self.name, market.ticker, time_left_sec,
                config.FIFTEEN_MIN_MIN_TIME_LEFT_SEC,
            )
            return None

        # Don't trade if market has been open too long (> 10 min)
        time_since_open = self._get_seconds_since_open(market)
        if time_since_open is not None and time_since_open > config.FIFTEEN_MIN_MAX_ENTRY_AGE_SEC:
            log.info(
                "[%s] %s: opened %ds ago, too stale",
                self.name, market.ticker, time_since_open,
            )
            return None

        # 5. Get momentum
        try:
            momentum = self.price_feed.get_momentum(
                asset, window_seconds=config.FIFTEEN_MIN_MOMENTUM_WINDOW_SEC
            )
        except (ValueError, AttributeError):
            momentum = 0.0

        # 6. Get volatility for fair value calculation
        vol = self.price_feed.get_volatility(asset)

        # 7. Calculate base fair value (probability that spot > floor_strike at close)
        hours_left = time_left_sec / 3600.0
        base_fair = calculate_fair_value(spot, floor_strike, vol, hours_left, "above")

        # 8. Apply momentum bias
        # If momentum is positive (price trending up), YES is more likely
        # If negative, NO is more likely
        momentum_bias = momentum * config.FIFTEEN_MIN_CONFIDENCE_BOOST / 0.001  # Scale: 0.1% move → boost
        # Clamp the bias
        momentum_bias = max(-0.15, min(0.15, momentum_bias))
        fair_value_yes = max(0.01, min(0.99, base_fair + momentum_bias))

        log.info(
            "[%s] %s: %s spot=$%.2f, strike=$%.2f, "
            "base_fair=%.3f, momentum=%.4f%%, bias=%.3f, adj_fair=%.3f, "
            "time_left=%ds",
            self.name, market.ticker, asset, spot, floor_strike,
            base_fair, momentum * 100, momentum_bias, fair_value_yes,
            time_left_sec,
        )

        # 9. Get orderbook prices
        best_yes_ask = self.orderbook.get_best_yes_ask(market.ticker)
        best_yes_bid = self.orderbook.get_best_yes_bid(market.ticker)
        best_no_ask = self._get_best_no_ask(market.ticker)

        log.info(
            "[%s] %s orderbook: yes_ask=%s, yes_bid=%s, no_ask=%s",
            self.name, market.ticker,
            f"${best_yes_ask[0]:.2f}x{best_yes_ask[1]}" if best_yes_ask else "None",
            f"${best_yes_bid[0]:.2f}x{best_yes_bid[1]}" if best_yes_bid else "None",
            f"${best_no_ask[0]:.2f}x{best_no_ask[1]}" if best_no_ask else "None",
        )

        # 10. Determine the best trade direction
        signal = None

        # Try buying YES if our fair value is higher than the ask
        if best_yes_ask:
            ask_price, ask_qty = best_yes_ask
            if ask_price <= 0.01:
                log.debug("[%s] %s: yes_ask too low (%.2f), skip", self.name, market.ticker, ask_price)
            elif fair_value_yes > ask_price:
                edge = fair_value_yes - ask_price
                signal = self._build_signal(
                    market, asset, "yes", "buy", ask_price, ask_qty,
                    edge, fair_value_yes, spot, floor_strike,
                    momentum, time_left_sec,
                )

        # Try buying NO if our fair value for NO is higher than NO ask
        fair_value_no = 1.0 - fair_value_yes
        if best_no_ask and (signal is None or fair_value_no - best_no_ask[0] > (signal.edge_cents / 100 if signal else 0)):
            no_ask_price, no_ask_qty = best_no_ask
            if no_ask_price <= 0.01:
                pass  # Skip near-zero asks
            elif fair_value_no > no_ask_price:
                edge = fair_value_no - no_ask_price
                no_signal = self._build_signal(
                    market, asset, "no", "buy", no_ask_price, no_ask_qty,
                    edge, fair_value_no, spot, floor_strike,
                    momentum, time_left_sec,
                )
                if no_signal:
                    if signal is None or no_signal.edge_cents > signal.edge_cents:
                        signal = no_signal

        return signal

    def _build_signal(
        self, market, asset: str, side: str, action: str,
        ask_price: float, ask_qty: int, edge: float,
        fair_value: float, spot: float, floor_strike: float,
        momentum: float, time_left_sec: int,
    ) -> TradeSignal | None:
        """Build a trade signal after fee/sizing checks."""

        # Calculate contracts (limited by config and available capital)
        # For 15-min markets, always allow at least 1 contract if we have the funds
        available_cap = self.risk_manager.get_available_capital()
        capital_limit = int(available_cap * config.MAX_SINGLE_TRADE_PCT / ask_price) if ask_price > 0 else 0

        # Ensure at least 1 contract if we have enough cash for it
        if capital_limit <= 0 and available_cap >= ask_price:
            capital_limit = 1

        max_contracts = min(
            config.FIFTEEN_MIN_MAX_CONTRACTS,
            ask_qty,
            capital_limit,
        )

        if max_contracts <= 0:
            log.info(
                "[%s] %s: max_contracts=0 (limit=%d, ask_qty=%d, cap_limit=%d, avail=$%.2f), REJECT",
                self.name, market.ticker,
                config.FIFTEEN_MIN_MAX_CONTRACTS, ask_qty,
                capital_limit, available_cap,
            )
            return None

        # Fee check
        fee = calculate_fee(max_contracts, ask_price, is_maker=config.FIFTEEN_MIN_USE_MAKER)
        fee_per_contract = fee / max_contracts if max_contracts > 0 else 0
        net_edge = edge - fee_per_contract
        net_edge_cents = net_edge * 100

        if net_edge_cents < config.FIFTEEN_MIN_MIN_EDGE_CENTS:
            log.info(
                "[%s] %s %s: edge=%.1fc after fees < min %dc, REJECT",
                self.name, market.ticker, side, net_edge_cents,
                config.FIFTEEN_MIN_MIN_EDGE_CENTS,
            )
            return None

        # Check minimum momentum threshold
        # Allow trades with large edge (>10c) even without momentum data,
        # since the base fair value alone can provide enough edge.
        abs_momentum = abs(momentum)
        large_edge = net_edge_cents >= 10.0
        if not large_edge and abs_momentum < config.FIFTEEN_MIN_MIN_MOMENTUM_PCT / 100:
            log.info(
                "[%s] %s: momentum=%.4f%% < min %.2f%% and edge=%.1fc < 10c, REJECT",
                self.name, market.ticker, abs_momentum * 100,
                config.FIFTEEN_MIN_MIN_MOMENTUM_PCT, net_edge_cents,
            )
            return None

        # Cooldown: don't re-trade the same ticker within 30 seconds
        now = time.time()
        last_trade = self._last_trade_time.get(market.ticker, 0)
        if now - last_trade < 30:
            return None

        # Determine entry price and maker/taker mode
        use_maker = config.FIFTEEN_MIN_USE_MAKER
        if use_maker:
            # Place 1c below the ask to try to get maker fill
            entry_cents = max(1, int(ask_price * 100) - 1)
            # Check if our bid would cross the ask — if so, use taker instead
            ask_cents = int(ask_price * 100)
            if entry_cents >= ask_cents:
                # Would cross — switch to taker at the ask price
                use_maker = False
                entry_cents = ask_cents
        else:
            entry_cents = int(ask_price * 100)

        direction = "UP" if side == "yes" else "DOWN"

        log.info(
            "[%s] SIGNAL BUILT: %s %s %s x%d @ %dc, edge=%.1fc",
            self.name, market.ticker, side, action,
            max_contracts, entry_cents, net_edge_cents,
        )

        return TradeSignal(
            strategy=self.name,
            ticker=market.ticker,
            side=side,
            action=action,
            price_cents=entry_cents,
            contracts=max_contracts,
            edge_cents=net_edge_cents,
            confidence=min(fair_value, 0.99),
            post_only=use_maker,
            reason=(
                f"15m {asset} {direction}: spot=${spot:.2f} vs strike=${floor_strike:.2f}, "
                f"fair={fair_value:.3f}, momentum={momentum*100:+.3f}%, "
                f"edge={net_edge_cents:.1f}c, {time_left_sec}s left"
            ),
            event_ticker=market.event_ticker,
        )

    def _get_best_no_ask(self, ticker: str) -> tuple[float, int] | None:
        """
        Get the best NO ask (i.e., cheapest price to buy NO).

        In Kalshi's orderbook, NO ask = 1.00 - best YES bid price.
        """
        best_yes_bid = self.orderbook.get_best_yes_bid(ticker)
        if best_yes_bid is None:
            return None
        yes_bid_price, yes_bid_qty = best_yes_bid
        no_ask_price = 1.0 - yes_bid_price
        if no_ask_price <= 0 or no_ask_price >= 1.0:
            return None
        return (no_ask_price, yes_bid_qty)

    @staticmethod
    def _get_seconds_to_close(market) -> int | None:
        """Calculate seconds until market closes."""
        time_str = market.close_time
        if not time_str:
            return None
        try:
            if time_str.endswith("Z"):
                time_str = time_str[:-1] + "+00:00"
            close_dt = datetime.fromisoformat(time_str)
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = close_dt - now
            return max(0, int(delta.total_seconds()))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _get_seconds_since_open(market) -> int | None:
        """Calculate seconds since market opened."""
        time_str = market.open_time
        if not time_str:
            return None
        try:
            if time_str.endswith("Z"):
                time_str = time_str[:-1] + "+00:00"
            open_dt = datetime.fromisoformat(time_str)
            if open_dt.tzinfo is None:
                open_dt = open_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = now - open_dt
            return max(0, int(delta.total_seconds()))
        except (ValueError, TypeError):
            return None

    async def execute(self, signals: list[TradeSignal]) -> None:
        """
        Execute 15-minute momentum signals.

        Places limit orders and monitors for fill. If not filled within
        30 seconds, cancels (these are fast-moving markets).
        """
        import asyncio

        for signal in signals:
            log.info(
                "[%s] EXECUTING: %s %s %s x%d @ %dc | %s",
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

            # Record trade time for cooldown
            self._last_trade_time[signal.ticker] = time.time()

            # Monitor fill with short timeout (30s for 15-min markets)
            if not self.order_manager.paper_mode and order.status == "resting":
                asyncio.create_task(
                    self._monitor_fill(order.order_id, timeout=30)
                )

    async def _monitor_fill(self, order_id: str, timeout: int = 30) -> None:
        """Cancel unfilled order after timeout."""
        import asyncio
        await asyncio.sleep(timeout)

        order = self.order_manager.get_order_status(order_id)
        if order and order.status == "resting":
            log.info(
                "[%s] Cancelling unfilled 15-min order %s after %ds",
                self.name, order_id, timeout,
            )
            await self.order_manager.cancel_order(order_id)
