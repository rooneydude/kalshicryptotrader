"""
Kalshi Short-Term Crypto Trading Bot — Entry Point

Initializes all components and runs the main event loop:
- Every 2s:  Poll orderbooks, update market prices
- Every 10s: Market maker scan → execute → manage inventory → kill switch
- Every 15s: Cross-strike arb scan → execute
- Every 30s: Momentum scalp scan → execute
- Every 60s: Market scanner, position sync, portfolio log, risk check
- Every 5m:  Export trade log, P&L summary

Graceful shutdown on SIGINT/SIGTERM: cancel all orders, close connections,
export final trade log, print session summary.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
import uuid
from datetime import datetime, timezone

import config
from data.market_scanner import MarketScanner
from data.orderbook import OrderBookManager
from data.price_feed import PriceFeed
from execution.order_manager import OrderManager
from execution.position_tracker import PositionTracker
from kalshi.client import KalshiClient
from kalshi.websocket_client import KalshiWebSocketClient
from paper_trading.paper_engine import PaperEngine
from paper_trading.results_tracker import ResultsTracker
from risk.risk_manager import RiskManager
from strategies.cross_strike_arb import CrossStrikeArbStrategy
from strategies.market_maker import MarketMakerStrategy
from strategies.momentum_scalp import MomentumScalpStrategy
from utils.logger import get_logger

log = get_logger("main")


class TradingBot:
    """
    Main trading bot orchestrator.

    Wires all components together and runs the event loop.
    """

    def __init__(self) -> None:
        self._running = False
        self._session_id = str(uuid.uuid4())[:8]
        self._session_start = time.time()

        # Components (initialized in setup())
        self.client: KalshiClient | None = None
        self.ws_client: KalshiWebSocketClient | None = None
        self.price_feed: PriceFeed | None = None
        self.orderbook_manager: OrderBookManager | None = None
        self.market_scanner: MarketScanner | None = None
        self.order_manager: OrderManager | None = None
        self.position_tracker: PositionTracker | None = None
        self.risk_manager: RiskManager | None = None
        self.paper_engine: PaperEngine | None = None
        self.results_tracker: ResultsTracker | None = None

        # Strategies
        self.momentum_scalp: MomentumScalpStrategy | None = None
        self.market_maker: MarketMakerStrategy | None = None
        self.cross_strike_arb: CrossStrikeArbStrategy | None = None

        # Watched market tickers (for orderbook polling)
        self._watched_tickers: list[str] = []

    async def setup(self) -> None:
        """Initialize all components."""
        log.info("=" * 60)
        log.info("Kalshi Crypto Trading Bot — Starting")
        log.info("Session: %s", self._session_id)
        log.info("Mode: %s", "PAPER" if config.PAPER_TRADING else "LIVE")
        log.info("API: %s", config.KALSHI_BASE_URL)
        log.info("=" * 60)

        # 1. Kalshi REST client
        self.client = KalshiClient()
        await self.client.connect()

        # 2. Check exchange status
        try:
            status = await self.client.get_exchange_status()
            log.info("Exchange status: %s", status.exchange_status)
        except Exception:
            log.warning("Could not check exchange status — continuing anyway")

        # 3. Get balance
        initial_balance = 0.0
        try:
            balance = await self.client.get_balance()
            initial_balance = balance.available_balance_dollars
            log.info("Account balance: $%.2f", initial_balance)
        except Exception:
            log.warning("Could not fetch balance — using default $1000 for paper")
            initial_balance = 1000.0

        # 4. Price feed
        self.price_feed = PriceFeed()
        await self.price_feed.start()

        # 5. WebSocket client
        self.ws_client = KalshiWebSocketClient()
        try:
            await self.ws_client.connect()
            await self.ws_client.subscribe_fill()
            await self.ws_client.subscribe_market_lifecycle()
        except Exception:
            log.warning("WebSocket connection failed — will use REST polling only")
            self.ws_client = None

        # 6. Data layer
        self.orderbook_manager = OrderBookManager()
        self.market_scanner = MarketScanner(self.client)

        # 7. Execution
        self.order_manager = OrderManager(
            client=self.client,
            paper_mode=config.PAPER_TRADING,
        )

        # 8. Paper trading engine
        if config.PAPER_TRADING:
            self.paper_engine = PaperEngine(self.orderbook_manager)
            self.order_manager.set_paper_engine(self.paper_engine)
            self.results_tracker = ResultsTracker()
            self.results_tracker.start_session(self._session_id, initial_balance)

        # 9. Position tracking & risk
        self.position_tracker = PositionTracker(self.client)
        self.position_tracker.initial_balance = initial_balance

        self.risk_manager = RiskManager(
            position_tracker=self.position_tracker,
            initial_balance=initial_balance,
        )

        # 10. Wire WebSocket callbacks
        if self.ws_client:
            self.ws_client.on_fill(self._handle_fill)
            self.ws_client.on_orderbook_delta(self._handle_orderbook_delta)
            self.ws_client.on_ticker(self._handle_ticker)

        # 11. Initialize strategies
        strategy_args = dict(
            client=self.client,
            price_feed=self.price_feed,
            risk_manager=self.risk_manager,
            order_manager=self.order_manager,
            position_tracker=self.position_tracker,
            orderbook_manager=self.orderbook_manager,
            market_scanner=self.market_scanner,
        )

        self.momentum_scalp = MomentumScalpStrategy(**strategy_args)
        self.market_maker = MarketMakerStrategy(**strategy_args)
        self.cross_strike_arb = CrossStrikeArbStrategy(**strategy_args)

        log.info("All components initialized — ready to trade")

    # ------------------------------------------------------------------
    # Main event loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the main trading loop."""
        self._running = True

        # Timers (seconds since last execution)
        last_orderbook_poll = 0.0
        last_mm_run = 0.0
        last_arb_run = 0.0
        last_scalp_run = 0.0
        last_market_scan = 0.0
        last_trade_export = 0.0

        # Initial market scan
        await self._scan_markets()

        log.info("Main loop started")

        while self._running:
            try:
                now = time.time()

                # Every 1 second: orderbook poll (parallel) + price update
                if now - last_orderbook_poll >= config.ORDERBOOK_POLL_INTERVAL_SEC:
                    await self._poll_orderbooks()
                    self._update_position_prices()
                    last_orderbook_poll = now

                # Every 3 seconds: market maker
                if now - last_mm_run >= config.MM_REQUOTE_INTERVAL_SEC:
                    await self._run_safe(self.market_maker.run_once, "market_maker")
                    await self._run_safe(self.market_maker.manage_inventory, "mm_inventory")
                    await self._run_safe(self.market_maker.check_kill_switch, "mm_killswitch")
                    last_mm_run = now

                # Every 5 seconds: cross-strike arb
                if now - last_arb_run >= config.ARB_SCAN_INTERVAL_SEC:
                    await self._run_safe(self.cross_strike_arb.run_once, "cross_strike_arb")
                    last_arb_run = now

                # Every 10 seconds: momentum scalp
                if now - last_scalp_run >= 10:
                    await self._run_safe(self.momentum_scalp.run_once, "momentum_scalp")
                    last_scalp_run = now

                # Every 60 seconds: market scan, sync, log
                if now - last_market_scan >= config.MARKET_SCAN_INTERVAL_SEC:
                    await self._scan_markets()
                    await self._sync_positions()
                    self._log_portfolio()
                    self._check_risk()
                    last_market_scan = now

                # Every 5 minutes: export trade log
                if now - last_trade_export >= 300:
                    self._export_trades()
                    last_trade_export = now

                # Sleep to avoid busy-waiting (0.5 second tick)
                await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Error in main loop — continuing")
                await asyncio.sleep(5.0)

    # ------------------------------------------------------------------
    # Periodic tasks
    # ------------------------------------------------------------------

    async def _poll_orderbooks(self) -> None:
        """Poll orderbooks for all watched markets via REST."""
        tickers = self._watched_tickers[:20]  # Limit to avoid rate limits
        if not tickers:
            return

        async def _fetch_one(ticker: str) -> tuple[str, bool, str]:
            try:
                ob = await self.client.get_orderbook(ticker)
                self.orderbook_manager.update_from_rest(ticker, ob)
                return (ticker, True, "")
            except Exception as e:
                return (ticker, False, f"{type(e).__name__}: {str(e)[:120]}")

        results = await asyncio.gather(*[_fetch_one(t) for t in tickers])

        updated = sum(1 for _, ok, _ in results if ok)
        failed = sum(1 for _, ok, _ in results if not ok)
        first_err = next((err for _, ok, err in results if not ok and err), "")

        if updated > 0:
            log.info("Orderbooks polled: %d updated, %d failed (parallel)", updated, failed)
        elif failed > 0:
            log.warning("Orderbooks: 0 updated, %d failed (%s)", failed, first_err)

    async def _scan_markets(self) -> None:
        """Discover new crypto markets and update watch list."""
        try:
            markets = await self.market_scanner.scan_crypto_markets()
            self._watched_tickers = [m.ticker for m in markets[:50]]
            log.info("Watching %d markets", len(self._watched_tickers))

            # Subscribe to WebSocket ticker updates
            if self.ws_client and self.ws_client.is_connected and self._watched_tickers:
                await self.ws_client.subscribe_ticker(self._watched_tickers[:20])

        except Exception:
            log.exception("Market scan failed")

    async def _sync_positions(self) -> None:
        """Sync local positions with exchange."""
        try:
            await self.position_tracker.sync_with_exchange()
        except Exception:
            log.debug("Position sync failed")

    def _update_position_prices(self) -> None:
        """Update position mark-to-market from orderbook data."""
        prices: dict[str, float] = {}
        for ticker in self.position_tracker.positions:
            bid = self.orderbook_manager.get_best_yes_bid(ticker)
            ask = self.orderbook_manager.get_best_yes_ask(ticker)
            if bid and ask:
                mid = (bid[0] + ask[0]) / 2.0
                prices[ticker] = mid
            elif bid:
                prices[ticker] = bid[0]
            elif ask:
                prices[ticker] = ask[0]
        if prices:
            self.position_tracker.update_market_prices(prices)

    def _log_portfolio(self) -> None:
        """Log current portfolio summary."""
        summary = self.position_tracker.get_portfolio_summary()
        log.info(
            "PORTFOLIO: positions=%d, exposure=$%.2f, "
            "realized=$%.2f, unrealized=$%.2f, total=$%.2f, "
            "fees=$%.2f, trades_today=%d, daily_pnl=$%.2f",
            summary["active_positions"],
            summary["net_exposure"],
            summary["realized_pnl"],
            summary["unrealized_pnl"],
            summary["total_pnl"],
            summary["total_fees"],
            summary["trades_today"],
            summary["daily_pnl"],
        )

    def _check_risk(self) -> None:
        """Run risk manager checks."""
        if self.risk_manager.check_kill_switch():
            log.critical("KILL SWITCH ACTIVE — trading paused")

        if self.risk_manager.should_flatten_all():
            log.critical("FLATTEN ALL triggered — cancelling all orders")
            asyncio.create_task(self._emergency_flatten())

    def _export_trades(self) -> None:
        """Export trade log to CSV."""
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filepath = f"./logs/trades_{self._session_id}_{timestamp}.csv"
            self.position_tracker.export_trades_csv(filepath)
        except Exception:
            log.debug("Trade export failed")

    async def _emergency_flatten(self) -> None:
        """Cancel all orders in an emergency."""
        try:
            count = await self.order_manager.cancel_all_orders()
            log.critical("Emergency flatten: cancelled %d orders", count)
        except Exception:
            log.exception("Emergency flatten failed")

    async def _run_safe(self, coro_func, label: str) -> None:
        """Run a coroutine safely, catching and logging errors."""
        try:
            await coro_func()
        except Exception:
            log.exception("Error in %s", label)

    # ------------------------------------------------------------------
    # WebSocket callbacks
    # ------------------------------------------------------------------

    def _handle_fill(self, data: dict) -> None:
        """Handle a fill notification from WebSocket."""
        try:
            from kalshi.models import Fill
            fill = Fill.model_validate(data.get("msg", data))
            self.position_tracker.update_from_fill(fill)

            if self.results_tracker:
                from execution.fee_calculator import calculate_fee
                fee = calculate_fee(fill.count, fill.price_dollars, not fill.is_taker)
                self.results_tracker.record_fill(
                    fill, fee_dollars=fee, session_id=self._session_id
                )
        except Exception:
            log.exception("Failed to process fill: %s", data)

    def _handle_orderbook_delta(self, data: dict) -> None:
        """Handle an orderbook delta from WebSocket."""
        try:
            msg = data.get("msg", data)
            ticker = msg.get("market_ticker", "")
            if ticker:
                self.orderbook_manager.update_from_delta(ticker, msg)
        except Exception:
            log.debug("Failed to process orderbook delta")

    def _handle_ticker(self, data: dict) -> None:
        """Handle a ticker update from WebSocket."""
        # Ticker updates are informational — we rely on orderbook for pricing
        pass

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Graceful shutdown: cancel orders, close connections, export logs."""
        log.info("=" * 60)
        log.info("Shutting down...")
        self._running = False

        # 1. Cancel all resting orders
        if self.order_manager is not None:
            try:
                count = await self.order_manager.cancel_all_orders()
                log.info("Cancelled %d resting orders", count)
            except Exception:
                log.exception("Failed to cancel orders during shutdown")

        # 2. Close WebSocket connections
        if self.ws_client:
            try:
                await self.ws_client.disconnect()
            except Exception:
                pass

        # 3. Stop price feed
        if self.price_feed:
            try:
                await self.price_feed.stop()
            except Exception:
                pass

        # 4. Close REST client
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass

        # 5. Export final trade log
        if self.position_tracker is not None:
            try:
                self.position_tracker.export_trades_csv(
                    f"./logs/trades_{self._session_id}_final.csv"
                )
            except Exception:
                pass

        # 6. Close results tracker
        if self.results_tracker and self.position_tracker is not None:
            try:
                summary = self.position_tracker.get_portfolio_summary()
                self.results_tracker.end_session(
                    self._session_id,
                    final_balance=self.position_tracker.initial_balance + summary["total_pnl"],
                    total_trades=summary["trades_today"],
                    total_pnl=summary["total_pnl"],
                    total_fees=summary["total_fees"],
                )
                self.results_tracker.export_trades_csv(
                    f"./paper_results/trades_{self._session_id}.csv",
                    session_id=self._session_id,
                )
                self.results_tracker.close()
            except Exception:
                pass

        # 7. Print session summary
        elapsed = time.time() - self._session_start
        if self.position_tracker is not None:
            summary = self.position_tracker.get_portfolio_summary()
            log.info("=" * 60)
            log.info("SESSION SUMMARY")
            log.info("  Duration: %.1f minutes", elapsed / 60)
            log.info("  Trades today: %d", summary["trades_today"])
            log.info("  Realized P&L: $%.2f", summary["realized_pnl"])
            log.info("  Unrealized P&L: $%.2f", summary["unrealized_pnl"])
            log.info("  Total P&L: $%.2f", summary["total_pnl"])
            log.info("  Total fees: $%.2f", summary["total_fees"])
            log.info("  Active positions: %d", summary["active_positions"])
            log.info("=" * 60)
        else:
            log.info("Bot shut down before initialization completed (%.1f seconds)", elapsed)


async def main() -> None:
    """Entry point: set up, run, and handle shutdown."""
    bot = TradingBot()

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def handle_signal(sig: signal.Signals) -> None:
        log.info("Received signal %s — initiating shutdown", sig.name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal, sig)

    try:
        await bot.setup()

        # Run bot until shutdown signal
        bot_task = asyncio.create_task(bot.run())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        done, pending = await asyncio.wait(
            [bot_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception:
        log.exception("Fatal error during setup")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete.")
