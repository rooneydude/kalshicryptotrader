"""
Real-time crypto price feeds from Binance WebSocket.

Provides latest price, 1-minute VWAP, and 30-minute rolling volatility
for BTC, ETH, and SOL.

The Kalshi oracle is CF Benchmarks BRTI (60-second volume-weighted average
across multiple exchanges). The 1-minute VWAP from Binance is a reasonable
proxy but NOT identical. Near settlement, this divergence matters.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import websockets

import config
from utils.logger import get_logger

log = get_logger("data.price_feed")


@dataclass
class TradeRecord:
    """A single trade used for VWAP and volatility calculations."""
    price: float
    quantity: float
    timestamp: float  # Unix timestamp in seconds


@dataclass
class AssetState:
    """State for a single asset (BTC, ETH, SOL)."""
    latest_price: float = 0.0
    last_update: float = 0.0

    # Rolling trade history for VWAP and volatility
    trades: deque[TradeRecord] = field(default_factory=lambda: deque(maxlen=50000))

    # 30-minute price snapshots for volatility (sampled every 10s)
    price_snapshots: deque[tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=180)  # 30 min / 10s = 180
    )
    last_snapshot_time: float = 0.0


class PriceFeed:
    """
    Real-time price feed from Binance WebSocket.

    Maintains latest price, 1-min VWAP, and 30-min rolling volatility
    for BTC, ETH, and SOL.
    """

    # Binance stream names
    STREAMS: dict[str, str] = {
        "BTC": "btcusdt@trade",
        "ETH": "ethusdt@trade",
        "SOL": "solusdt@trade",
    }

    def __init__(self, ws_url: str | None = None):
        self._ws_url = ws_url or config.BINANCE_WS_URL
        self._assets: dict[str, AssetState] = {
            asset: AssetState() for asset in self.STREAMS
        }
        self._running = False
        self._connection = None
        self._listen_task: asyncio.Task | None = None
        self._snapshot_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def btc_price(self) -> float:
        return self._assets["BTC"].latest_price

    @property
    def eth_price(self) -> float:
        return self._assets["ETH"].latest_price

    @property
    def sol_price(self) -> float:
        return self._assets["SOL"].latest_price

    @property
    def last_update(self) -> datetime:
        latest = max(a.last_update for a in self._assets.values())
        if latest == 0:
            return datetime.now(timezone.utc)
        return datetime.fromtimestamp(latest, tz=timezone.utc)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_price(self, asset: str) -> float:
        """Get the latest price for an asset (BTC, ETH, SOL)."""
        asset = asset.upper()
        if asset not in self._assets:
            raise ValueError(f"Unsupported asset: {asset}")
        return self._assets[asset].latest_price

    def get_vwap(self, asset: str, window_seconds: int = 60) -> float:
        """
        Calculate volume-weighted average price over a time window.

        Args:
            asset: Asset symbol (BTC, ETH, SOL).
            window_seconds: Lookback window in seconds (default 60 for BRTI proxy).

        Returns:
            VWAP in dollars. Returns latest price if insufficient data.
        """
        asset = asset.upper()
        state = self._assets.get(asset)
        if state is None:
            raise ValueError(f"Unsupported asset: {asset}")

        if not state.trades:
            return state.latest_price

        now = time.time()
        cutoff = now - window_seconds

        total_pv = 0.0  # price * volume
        total_v = 0.0   # volume

        for trade in reversed(state.trades):
            if trade.timestamp < cutoff:
                break
            total_pv += trade.price * trade.quantity
            total_v += trade.quantity

        if total_v == 0:
            return state.latest_price

        return total_pv / total_v

    def get_volatility(self, asset: str, window_minutes: int = 30) -> float:
        """
        Calculate annualized realized volatility from price snapshots.

        Uses 10-second interval log returns, annualized to yearly.

        Args:
            asset: Asset symbol.
            window_minutes: Lookback window in minutes.

        Returns:
            Annualized volatility as a decimal (e.g. 0.65 for 65%).
        """
        asset = asset.upper()
        state = self._assets.get(asset)
        if state is None:
            raise ValueError(f"Unsupported asset: {asset}")

        snapshots = list(state.price_snapshots)
        if len(snapshots) < 2:
            return 0.65  # Default BTC volatility assumption

        now = time.time()
        cutoff = now - (window_minutes * 60)
        relevant = [(t, p) for t, p in snapshots if t >= cutoff]

        if len(relevant) < 2:
            return 0.65

        # Calculate log returns
        log_returns: list[float] = []
        for i in range(1, len(relevant)):
            if relevant[i - 1][1] > 0 and relevant[i][1] > 0:
                lr = math.log(relevant[i][1] / relevant[i - 1][1])
                log_returns.append(lr)

        if len(log_returns) < 2:
            return 0.65

        # Standard deviation of log returns
        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        std = math.sqrt(variance)

        # Annualize: snapshots are every 10s → ~3,153,600 periods per year
        # periods_per_year = 365.25 * 24 * 3600 / 10
        periods_per_year = 3_153_600
        annualized = std * math.sqrt(periods_per_year)

        # Clamp to reasonable range
        return max(0.05, min(annualized, 5.0))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to Binance WebSocket and begin streaming prices."""
        self._running = True

        # Build combined stream URL
        streams = "/".join(self.STREAMS.values())
        url = f"{self._ws_url}/{streams}"

        self._listen_task = asyncio.create_task(self._listen_loop(url))
        self._snapshot_task = asyncio.create_task(self._snapshot_loop())

        log.info("PriceFeed started — streaming from %s", url)

    async def stop(self) -> None:
        """Disconnect gracefully."""
        self._running = False

        if self._snapshot_task:
            self._snapshot_task.cancel()
            try:
                await self._snapshot_task
            except asyncio.CancelledError:
                pass

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

        if self._connection:
            await self._connection.close()
            self._connection = None

        log.info("PriceFeed stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _listen_loop(self, url: str) -> None:
        """Listen for trade messages with auto-reconnect."""
        reconnect_delay = config.PRICE_FEED_RECONNECT_SEC

        while self._running:
            try:
                async with websockets.connect(url) as ws:
                    self._connection = ws
                    reconnect_delay = config.PRICE_FEED_RECONNECT_SEC
                    log.info("Binance WebSocket connected")

                    async for raw_message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw_message)
                            self._handle_trade(data)
                        except json.JSONDecodeError:
                            log.warning("Non-JSON from Binance: %s", str(raw_message)[:100])

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception(
                    "Binance WS error — reconnecting in %ds", reconnect_delay
                )
                self._connection = None
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    def _handle_trade(self, data: dict) -> None:
        """Process a Binance trade message."""
        # Binance combined stream wraps in {"stream": ..., "data": {...}}
        if "stream" in data:
            stream = data["stream"]
            trade_data = data.get("data", {})
        else:
            # Direct stream (single subscription)
            stream = data.get("s", "").lower() + "@trade"
            trade_data = data

        # Determine asset from stream name
        asset: str | None = None
        for a, s in self.STREAMS.items():
            if s == stream or stream.startswith(s.split("@")[0]):
                asset = a
                break

        if asset is None:
            return

        try:
            price = float(trade_data.get("p", 0))
            quantity = float(trade_data.get("q", 0))
            trade_time = trade_data.get("T", time.time() * 1000) / 1000.0
        except (ValueError, TypeError):
            return

        if price <= 0:
            return

        state = self._assets[asset]
        state.latest_price = price
        state.last_update = trade_time

        # Record for VWAP
        state.trades.append(TradeRecord(price=price, quantity=quantity, timestamp=trade_time))

    async def _snapshot_loop(self) -> None:
        """Take periodic price snapshots for volatility calculation."""
        while self._running:
            try:
                await asyncio.sleep(10)  # Every 10 seconds
                now = time.time()

                for asset, state in self._assets.items():
                    if state.latest_price > 0:
                        state.price_snapshots.append((now, state.latest_price))

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Snapshot loop error")
