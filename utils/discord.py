"""
Discord webhook notifications for trade alerts.

Sends formatted messages to a Discord channel via webhook when:
- A trade signal is found
- An order is placed / filled
- Portfolio summary updates
- Risk events (kill switch, daily loss limit)
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import aiohttp

from utils.logger import get_logger

log = get_logger("utils.discord")

WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

# Rate limit: max 1 message per 2 seconds to avoid Discord throttling
_last_send: float = 0.0
_MIN_INTERVAL: float = 2.0


async def send(content: str, username: str = "Kalshi Bot") -> None:
    """Send a plain text message to the Discord webhook."""
    if not WEBHOOK_URL:
        return

    global _last_send
    now = asyncio.get_event_loop().time()
    if now - _last_send < _MIN_INTERVAL:
        await asyncio.sleep(_MIN_INTERVAL - (now - _last_send))
    _last_send = asyncio.get_event_loop().time()

    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                WEBHOOK_URL,
                json={"content": content, "username": username},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        log.debug("Discord webhook send failed")


async def send_embed(
    title: str,
    description: str,
    color: int = 0x00FF00,
    fields: list[dict] | None = None,
    username: str = "Kalshi Bot",
) -> None:
    """Send a rich embed message to the Discord webhook."""
    if not WEBHOOK_URL:
        return

    global _last_send
    now = asyncio.get_event_loop().time()
    if now - _last_send < _MIN_INTERVAL:
        await asyncio.sleep(_MIN_INTERVAL - (now - _last_send))
    _last_send = asyncio.get_event_loop().time()

    embed: dict = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if fields:
        embed["fields"] = fields

    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                WEBHOOK_URL,
                json={"embeds": [embed], "username": username},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        log.debug("Discord webhook embed send failed")


# -------------------------------------------------------------------
# Convenience helpers
# -------------------------------------------------------------------

GREEN = 0x00FF00
RED = 0xFF0000
YELLOW = 0xFFCC00
BLUE = 0x0099FF


async def notify_signal(signal) -> None:
    """Notify when a trade signal is found."""
    color = GREEN if signal.action == "buy" else RED
    await send_embed(
        title=f"Signal: {signal.action.upper()} {signal.side.upper()}",
        description=signal.reason or "",
        color=color,
        fields=[
            {"name": "Ticker", "value": signal.ticker, "inline": True},
            {"name": "Price", "value": f"${signal.price_cents / 100:.2f}", "inline": True},
            {"name": "Qty", "value": str(signal.contracts), "inline": True},
            {"name": "Edge", "value": f"{signal.edge_cents:.1f}c", "inline": True},
            {"name": "Strategy", "value": signal.strategy, "inline": True},
        ],
    )


async def notify_order(ticker: str, side: str, action: str, price_cents: int, contracts: int, order_id: str = "") -> None:
    """Notify when an order is placed."""
    await send_embed(
        title=f"Order Placed: {action.upper()} {side.upper()}",
        description=f"**{ticker}**",
        color=BLUE,
        fields=[
            {"name": "Price", "value": f"${price_cents / 100:.2f}", "inline": True},
            {"name": "Contracts", "value": str(contracts), "inline": True},
            {"name": "Order ID", "value": order_id[:8] + "..." if order_id else "paper", "inline": True},
        ],
    )


async def notify_fill(ticker: str, side: str, price: float, contracts: int, fee: float = 0) -> None:
    """Notify when an order is filled."""
    await send_embed(
        title=f"Filled: {side.upper()}",
        description=f"**{ticker}**",
        color=GREEN,
        fields=[
            {"name": "Price", "value": f"${price:.2f}", "inline": True},
            {"name": "Contracts", "value": str(contracts), "inline": True},
            {"name": "Fee", "value": f"${fee:.2f}", "inline": True},
        ],
    )


async def notify_portfolio(summary: dict) -> None:
    """Send periodic portfolio summary."""
    pnl = summary.get("total_pnl", 0)
    color = GREEN if pnl >= 0 else RED
    await send_embed(
        title="Portfolio Update",
        description="",
        color=color,
        fields=[
            {"name": "Positions", "value": str(summary.get("active_positions", 0)), "inline": True},
            {"name": "Total P&L", "value": f"${pnl:.2f}", "inline": True},
            {"name": "Realized", "value": f"${summary.get('realized_pnl', 0):.2f}", "inline": True},
            {"name": "Fees", "value": f"${summary.get('total_fees', 0):.2f}", "inline": True},
            {"name": "Trades Today", "value": str(summary.get("trades_today", 0)), "inline": True},
            {"name": "Daily P&L", "value": f"${summary.get('daily_pnl', 0):.2f}", "inline": True},
        ],
    )


async def notify_risk(message: str) -> None:
    """Notify on risk events (kill switch, loss limits)."""
    await send_embed(
        title="RISK ALERT",
        description=message,
        color=RED,
    )


async def notify_startup(mode: str, balance: float, session_id: str) -> None:
    """Notify when the bot starts."""
    await send_embed(
        title="Bot Started",
        description=f"Session `{session_id}`",
        color=BLUE,
        fields=[
            {"name": "Mode", "value": mode, "inline": True},
            {"name": "Balance", "value": f"${balance:.2f}", "inline": True},
        ],
    )
