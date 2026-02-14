"""
Position tracking: open positions, realized + unrealized P&L, trade history.

Maintains a local view of all positions and reconciles periodically
with the Kalshi API.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kalshi.client import KalshiClient
from kalshi.models import Fill
from execution.fee_calculator import calculate_fee
from utils.logger import get_logger

log = get_logger("execution.position_tracker")


@dataclass
class PositionState:
    """State for a single market position."""
    ticker: str = ""
    net_contracts: int = 0          # Positive = long YES, negative = short YES (long NO)
    avg_entry_price: float = 0.0    # Dollars
    current_market_price: float = 0.0  # Dollars
    unrealized_pnl: float = 0.0     # Dollars
    realized_pnl: float = 0.0       # Dollars
    fees_paid: float = 0.0          # Dollars
    total_bought: int = 0           # Total YES contracts bought
    total_sold: int = 0             # Total YES contracts sold


@dataclass
class TradeRecord:
    """Record of a single trade for history export."""
    timestamp: str
    ticker: str
    side: str
    action: str
    contracts: int
    price_dollars: float
    fee_dollars: float
    is_maker: bool
    strategy: str = ""


class PositionTracker:
    """
    Tracks all open positions, P&L, and trade history.

    Positions are updated from fill events (WebSocket or polling)
    and periodically reconciled with the exchange.
    """

    def __init__(self, client: KalshiClient | None = None) -> None:
        self._client = client
        self._positions: dict[str, PositionState] = {}
        self._trade_history: list[TradeRecord] = []
        self._daily_reset_date: str = ""
        self._daily_pnl: float = 0.0
        self._weekly_pnl: float = 0.0
        self._total_fees_paid: float = 0.0
        self._initial_balance: float = 0.0
        self._session_start = time.time()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def positions(self) -> dict[str, PositionState]:
        return self._positions

    @property
    def total_realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self._positions.values())

    @property
    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self._positions.values())

    @property
    def total_fees_paid(self) -> float:
        return self._total_fees_paid

    @property
    def trade_count_today(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return sum(1 for t in self._trade_history if t.timestamp.startswith(today))

    @property
    def daily_pnl(self) -> float:
        self._check_daily_reset()
        return self._daily_pnl

    @property
    def weekly_pnl(self) -> float:
        return self._weekly_pnl

    @property
    def initial_balance(self) -> float:
        return self._initial_balance

    @initial_balance.setter
    def initial_balance(self, value: float) -> None:
        self._initial_balance = value

    # ------------------------------------------------------------------
    # Fill processing
    # ------------------------------------------------------------------

    def update_from_fill(self, fill: Fill, strategy: str = "") -> None:
        """
        Update position based on a trade execution.

        Args:
            fill: The Fill object from the API or paper engine.
            strategy: The strategy that generated this trade.
        """
        ticker = fill.ticker
        if ticker not in self._positions:
            self._positions[ticker] = PositionState(ticker=ticker)

        pos = self._positions[ticker]
        price = fill.price_dollars
        contracts = fill.count
        is_buy = fill.action == "buy"
        is_yes = fill.side == "yes"

        # Calculate fee
        fee = calculate_fee(contracts, price, not fill.is_taker)
        pos.fees_paid += fee
        self._total_fees_paid += fee

        # Update position
        if is_yes:
            if is_buy:
                # Buying YES: increases net_contracts
                if pos.net_contracts >= 0:
                    # Adding to long position
                    total_cost = pos.avg_entry_price * pos.net_contracts + price * contracts
                    pos.net_contracts += contracts
                    pos.avg_entry_price = total_cost / pos.net_contracts if pos.net_contracts else 0
                else:
                    # Closing short position
                    closing = min(contracts, abs(pos.net_contracts))
                    pnl = (pos.avg_entry_price - price) * closing
                    pos.realized_pnl += pnl - fee
                    self._daily_pnl += pnl - fee
                    self._weekly_pnl += pnl - fee
                    pos.net_contracts += contracts
                    if pos.net_contracts > 0:
                        pos.avg_entry_price = price
                pos.total_bought += contracts
            else:
                # Selling YES: decreases net_contracts
                if pos.net_contracts > 0:
                    closing = min(contracts, pos.net_contracts)
                    pnl = (price - pos.avg_entry_price) * closing
                    pos.realized_pnl += pnl - fee
                    self._daily_pnl += pnl - fee
                    self._weekly_pnl += pnl - fee
                    pos.net_contracts -= contracts
                else:
                    # Opening/adding short
                    if pos.net_contracts <= 0 and abs(pos.net_contracts) > 0:
                        total_cost = pos.avg_entry_price * abs(pos.net_contracts) + price * contracts
                        pos.net_contracts -= contracts
                        pos.avg_entry_price = total_cost / abs(pos.net_contracts)
                    else:
                        pos.net_contracts -= contracts
                        pos.avg_entry_price = price
                pos.total_sold += contracts
        else:
            # NO side: buying NO = selling YES equivalent
            if is_buy:
                no_price = price
                yes_equiv = 1.0 - no_price
                if pos.net_contracts > 0:
                    closing = min(contracts, pos.net_contracts)
                    pnl = (yes_equiv - pos.avg_entry_price) * closing
                    pos.realized_pnl += pnl - fee
                    self._daily_pnl += pnl - fee
                    self._weekly_pnl += pnl - fee
                pos.net_contracts -= contracts
                if pos.net_contracts < 0:
                    pos.avg_entry_price = yes_equiv
                pos.total_sold += contracts
            else:
                no_price = price
                yes_equiv = 1.0 - no_price
                pos.net_contracts += contracts
                if pos.net_contracts > 0:
                    pos.avg_entry_price = yes_equiv
                pos.total_bought += contracts

        # Record trade
        self._trade_history.append(
            TradeRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                ticker=ticker,
                side=fill.side,
                action=fill.action,
                contracts=contracts,
                price_dollars=price,
                fee_dollars=fee,
                is_maker=not fill.is_taker,
                strategy=strategy,
            )
        )

        log.info(
            "Fill processed: %s %s %s x%d @ $%.2f (fee=$%.2f) → net=%d pnl=$%.2f",
            ticker,
            fill.side,
            fill.action,
            contracts,
            price,
            fee,
            pos.net_contracts,
            pos.realized_pnl,
        )

    # ------------------------------------------------------------------
    # Market price updates
    # ------------------------------------------------------------------

    def update_market_prices(self, prices: dict[str, float]) -> None:
        """
        Mark all positions to market.

        Args:
            prices: Dict of ticker → current YES price in dollars.
        """
        for ticker, price in prices.items():
            if ticker in self._positions:
                pos = self._positions[ticker]
                pos.current_market_price = price
                if pos.net_contracts > 0:
                    pos.unrealized_pnl = (price - pos.avg_entry_price) * pos.net_contracts
                elif pos.net_contracts < 0:
                    pos.unrealized_pnl = (pos.avg_entry_price - price) * abs(pos.net_contracts)
                else:
                    pos.unrealized_pnl = 0.0

    # ------------------------------------------------------------------
    # Exchange reconciliation
    # ------------------------------------------------------------------

    async def sync_with_exchange(self) -> None:
        """Reconcile local positions with the Kalshi API."""
        if self._client is None:
            return

        try:
            resp = await self._client.get_positions()
            for api_pos in resp.market_positions:
                ticker = api_pos.market_ticker
                if ticker not in self._positions:
                    self._positions[ticker] = PositionState(ticker=ticker)

                local = self._positions[ticker]
                api_count = api_pos.position  # YES contract count from exchange

                if local.net_contracts != api_count:
                    log.warning(
                        "Position mismatch for %s: local=%d, exchange=%d — using exchange value",
                        ticker,
                        local.net_contracts,
                        api_count,
                    )
                    local.net_contracts = api_count

            log.debug("Position sync complete: %d positions", len(resp.market_positions))
        except Exception:
            log.exception("Failed to sync positions with exchange")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_net_exposure(self) -> float:
        """Total dollars at risk across all positions."""
        total = 0.0
        for pos in self._positions.values():
            if pos.net_contracts != 0:
                total += abs(pos.net_contracts) * pos.avg_entry_price
        return total

    def get_net_position(self, ticker: str) -> int:
        """Net contracts for a specific market."""
        pos = self._positions.get(ticker)
        return pos.net_contracts if pos else 0

    def get_event_exposure(self, event_ticker: str) -> float:
        """Total dollar exposure for all markets in an event."""
        total = 0.0
        for ticker, pos in self._positions.items():
            if event_ticker in ticker and pos.net_contracts != 0:
                total += abs(pos.net_contracts) * pos.avg_entry_price
        return total

    def get_portfolio_summary(self) -> dict:
        """Get a summary of the current portfolio state."""
        active = {k: v for k, v in self._positions.items() if v.net_contracts != 0}
        return {
            "active_positions": len(active),
            "total_positions": len(self._positions),
            "net_exposure": self.get_net_exposure(),
            "realized_pnl": self.total_realized_pnl,
            "unrealized_pnl": self.total_unrealized_pnl,
            "total_pnl": self.total_realized_pnl + self.total_unrealized_pnl,
            "total_fees": self._total_fees_paid,
            "trades_today": self.trade_count_today,
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
        }

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_trades_csv(self, filepath: str) -> None:
        """
        Export all trade history to a CSV file.

        Args:
            filepath: Path to the output CSV file.
        """
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "ticker", "side", "action", "contracts",
                "price_dollars", "fee_dollars", "is_maker", "strategy",
            ])
            for trade in self._trade_history:
                writer.writerow([
                    trade.timestamp,
                    trade.ticker,
                    trade.side,
                    trade.action,
                    trade.contracts,
                    f"{trade.price_dollars:.4f}",
                    f"{trade.fee_dollars:.4f}",
                    trade.is_maker,
                    trade.strategy,
                ])

        log.info("Exported %d trades to %s", len(self._trade_history), filepath)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_daily_reset(self) -> None:
        """Reset daily P&L counter at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            if self._daily_reset_date:
                log.info(
                    "Daily P&L reset: previous day = $%.2f", self._daily_pnl
                )
            self._daily_pnl = 0.0
            self._daily_reset_date = today
