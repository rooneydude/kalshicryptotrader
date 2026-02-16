"""
Risk management: position limits, kill switch, and safety checks.

This is the ONLY module that can block trades. All strategies must
pass their trade signals through the risk manager before execution.
"""

from __future__ import annotations

from typing import Any

import config
from execution.fee_calculator import calculate_fee
from execution.position_tracker import PositionTracker
from utils.logger import get_logger

log = get_logger("risk.risk_manager")


class RiskManager:
    """
    Enforces all position limits and safety checks.

    Every trade signal must pass through filter_signals() before execution.
    """

    def __init__(
        self,
        position_tracker: PositionTracker,
        initial_balance: float,
    ) -> None:
        self._tracker = position_tracker
        self._initial_balance = initial_balance
        self._kill_switch_active = False
        self._manual_override = False
        self._exchange_open = True

        # Order manager reference — set after construction via set_order_manager()
        self._order_manager: Any = None

    @property
    def initial_balance(self) -> float:
        return self._initial_balance

    @initial_balance.setter
    def initial_balance(self, value: float) -> None:
        self._initial_balance = value

    def set_order_manager(self, order_manager: Any) -> None:
        """Set the order manager for resting-order queries."""
        self._order_manager = order_manager

    # ------------------------------------------------------------------
    # Signal filtering
    # ------------------------------------------------------------------

    def filter_signals(self, signals: list[Any]) -> list[Any]:
        """
        Filter trade signals through all risk checks.

        Each signal is checked against:
        1. Single trade size limit
        2. Per-strike exposure limit
        3. Per-event exposure limit
        4. Total exposure limit
        5. Cash buffer requirement
        6. Daily loss limit
        7. Weekly loss limit
        8. Kill switch

        Args:
            signals: List of TradeSignal objects.

        Returns:
            List of approved TradeSignal objects (rejected ones are logged).
        """
        if self.check_kill_switch():
            log.warning("Kill switch active — all signals rejected")
            return []

        approved: list[Any] = []

        for signal in signals:
            rejection = self._check_signal(signal)
            if rejection:
                log.warning(
                    "Signal REJECTED: %s %s %s x%d @ %dc — %s",
                    signal.ticker,
                    signal.side,
                    signal.action,
                    signal.contracts,
                    signal.price_cents,
                    rejection,
                )
            else:
                approved.append(signal)
                log.debug(
                    "Signal APPROVED: %s %s %s x%d @ %dc (edge=%.1fc)",
                    signal.ticker,
                    signal.side,
                    signal.action,
                    signal.contracts,
                    signal.price_cents,
                    signal.edge_cents,
                )

        if len(signals) > 0:
            log.info(
                "Risk filter: %d/%d signals approved",
                len(approved),
                len(signals),
            )

        return approved

    def _check_signal(self, signal: Any) -> str | None:
        """
        Run all risk checks on a single signal.

        Returns:
            Rejection reason string, or None if approved.
        """
        trade_cost = (signal.price_cents / 100.0) * signal.contracts
        capital = self._initial_balance

        if capital <= 0:
            return "No capital available"

        # 1. Single trade size
        if trade_cost > config.MAX_SINGLE_TRADE_PCT * capital:
            return (
                f"Single trade {trade_cost:.2f} exceeds "
                f"{config.MAX_SINGLE_TRADE_PCT * 100:.0f}% limit "
                f"({config.MAX_SINGLE_TRADE_PCT * capital:.2f})"
            )

        # 2. Per-strike exposure (includes filled positions + resting orders)
        filled_contracts = abs(self._tracker.get_net_position(signal.ticker))
        resting_contracts = 0
        if self._order_manager is not None:
            resting_contracts = sum(
                o.remaining_count
                for o in self._order_manager.get_open_orders(ticker=signal.ticker)
            )
        total_contracts = filled_contracts + resting_contracts
        current_strike_exposure = total_contracts * (signal.price_cents / 100.0)
        new_strike_exposure = current_strike_exposure + trade_cost
        if new_strike_exposure > config.MAX_PER_STRIKE_PCT * capital:
            return (
                f"Per-strike exposure {new_strike_exposure:.2f} (filled={filled_contracts}, "
                f"resting={resting_contracts}) exceeds "
                f"{config.MAX_PER_STRIKE_PCT * 100:.0f}% limit"
            )

        # 3. Per-event exposure
        event_ticker = self._extract_event_ticker(signal.ticker)
        if event_ticker:
            current_event_exposure = self._tracker.get_event_exposure(event_ticker)
            new_event_exposure = current_event_exposure + trade_cost
            if new_event_exposure > config.MAX_PER_EVENT_PCT * capital:
                return (
                    f"Per-event exposure {new_event_exposure:.2f} exceeds "
                    f"{config.MAX_PER_EVENT_PCT * 100:.0f}% limit"
                )

        # 4. Total exposure
        current_total = self._tracker.get_net_exposure()
        if current_total + trade_cost > config.MAX_TOTAL_EXPOSURE_PCT * capital:
            return (
                f"Total exposure {current_total + trade_cost:.2f} exceeds "
                f"{config.MAX_TOTAL_EXPOSURE_PCT * 100:.0f}% limit"
            )

        # 5. Cash buffer
        remaining_cash = capital - current_total - trade_cost
        required_buffer = config.CASH_BUFFER_PCT * capital
        if remaining_cash < required_buffer:
            return (
                f"Cash after trade ({remaining_cash:.2f}) below "
                f"buffer requirement ({required_buffer:.2f})"
            )

        # 6. Daily loss limit
        if self._tracker.daily_pnl < -(config.DAILY_LOSS_LIMIT_PCT * capital):
            return (
                f"Daily loss ${abs(self._tracker.daily_pnl):.2f} exceeds "
                f"{config.DAILY_LOSS_LIMIT_PCT * 100:.0f}% limit"
            )

        # 7. Weekly loss limit
        if self._tracker.weekly_pnl < -(config.WEEKLY_LOSS_LIMIT_PCT * capital):
            return (
                f"Weekly loss ${abs(self._tracker.weekly_pnl):.2f} exceeds "
                f"{config.WEEKLY_LOSS_LIMIT_PCT * 100:.0f}% limit"
            )

        return None

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def check_kill_switch(self) -> bool:
        """
        Check if trading should be stopped.

        Conditions:
        - Daily loss exceeds limit
        - Exchange not open
        - Manual override activated

        When the kill switch first activates, all exchange orders are cancelled.
        """
        if self._kill_switch_active:
            return True

        if self._manual_override:
            return True

        if not self._exchange_open:
            log.warning("Kill switch: exchange not open")
            self._activate_kill_switch("exchange not open")
            return True

        capital = self._initial_balance
        if capital > 0 and self._tracker.daily_pnl < -(config.DAILY_LOSS_LIMIT_PCT * capital):
            log.warning(
                "Kill switch: daily loss $%.2f exceeds limit",
                abs(self._tracker.daily_pnl),
            )
            self._activate_kill_switch(
                f"daily loss ${abs(self._tracker.daily_pnl):.2f}"
            )
            return True

        return False

    def _activate_kill_switch(self, reason: str) -> None:
        """Activate the kill switch and cancel all exchange orders."""
        self._kill_switch_active = True
        log.critical("KILL SWITCH ACTIVATED: %s", reason)

        # Cancel all exchange orders immediately
        if self._order_manager is not None:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(
                        self._order_manager.cancel_all_exchange_orders()
                    )
                else:
                    loop.run_until_complete(
                        self._order_manager.cancel_all_exchange_orders()
                    )
            except Exception:
                log.exception("Failed to cancel exchange orders on kill switch")

    def should_flatten_all(self) -> bool:
        """
        Check if all positions should be closed immediately.

        Triggered by: daily loss > 2x DAILY_LOSS_LIMIT, or manual override.
        """
        if self._manual_override:
            return True

        capital = self._initial_balance
        if capital > 0:
            threshold = 2.0 * config.DAILY_LOSS_LIMIT_PCT * capital
            if self._tracker.daily_pnl < -threshold:
                log.critical(
                    "FLATTEN ALL: daily loss $%.2f exceeds 2x limit ($%.2f)",
                    abs(self._tracker.daily_pnl),
                    threshold,
                )
                return True

        return False

    def reset_kill_switch(self) -> None:
        """Reset the kill switch (e.g., new trading day)."""
        self._kill_switch_active = False
        log.info("Kill switch reset")

    def set_exchange_status(self, is_open: bool) -> None:
        """Update the exchange open/closed status."""
        self._exchange_open = is_open
        if not is_open:
            log.warning("Exchange marked as closed")

    def activate_manual_override(self) -> None:
        """Manually stop all trading."""
        self._manual_override = True
        log.critical("Manual override activated — all trading stopped")

    def deactivate_manual_override(self) -> None:
        """Remove manual trading stop."""
        self._manual_override = False
        log.info("Manual override deactivated")

    # ------------------------------------------------------------------
    # Capital
    # ------------------------------------------------------------------

    def get_available_capital(self) -> float:
        """
        Calculate available capital for new trades.

        Returns:
            Available dollars = balance - exposure - cash buffer.
        """
        exposure = self._tracker.get_net_exposure()
        buffer = config.CASH_BUFFER_PCT * self._initial_balance
        available = self._initial_balance - exposure - buffer
        return max(0.0, available)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_event_ticker(market_ticker: str) -> str:
        """
        Extract the event ticker from a market ticker.

        Example: "KXBTC-26FEB14-T70000" → "KXBTC-26FEB14"
        """
        parts = market_ticker.split("-")
        if len(parts) >= 2:
            return "-".join(parts[:2])
        return market_ticker
