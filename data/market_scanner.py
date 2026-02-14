"""
Market discovery and filtering for Kalshi crypto markets.

Scans for active crypto events and markets, parses strike prices from
tickers and titles, and classifies market types (above, below, range, up_down).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from kalshi.client import KalshiClient
from kalshi.models import Event, Market
from utils.logger import get_logger

import config

log = get_logger("data.market_scanner")

# Regex for extracting strike price from ticker (e.g., KXBTC-26FEB14-T70000 → 70000)
TICKER_STRIKE_RE = re.compile(r"-T(\d+)$")

# Regex for extracting price from title/subtitle (e.g., "$70,500 or above" → 70500)
TITLE_PRICE_RE = re.compile(r"\$?([\d,]+(?:\.\d+)?)")


class MarketScanner:
    """
    Find and filter relevant crypto markets on Kalshi.

    Discovers active events and their strike-level markets for
    BTC, ETH, SOL across hourly, daily, and 15-minute timeframes.
    """

    def __init__(self, client: KalshiClient) -> None:
        self._client = client
        self._cached_events: dict[str, Event] = {}

    async def scan_crypto_markets(
        self, timeframe: str = "all"
    ) -> list[Market]:
        """
        Scan for all open crypto markets on Kalshi.

        Args:
            timeframe: Filter by timeframe — "15min", "hourly", "daily", or "all".

        Returns:
            List of Market objects sorted by close_time (soonest first).
        """
        all_markets: list[Market] = []

        for series in config.CRYPTO_SERIES_TICKERS:
            # Apply timeframe filtering
            if timeframe != "all":
                if timeframe == "15min" and series != "KXBTCUD":
                    continue
                if timeframe in ("hourly", "daily") and series == "KXBTCUD":
                    continue

            try:
                markets = await self._client.get_all_markets(
                    series_ticker=series,
                    status="open",
                )
                all_markets.extend(markets)
            except Exception:
                log.exception("Failed to scan markets for series %s", series)

        # Sort by close_time ascending (soonest first)
        all_markets.sort(key=lambda m: m.close_time or "")

        # Filter by timeframe heuristic if needed
        if timeframe == "hourly":
            all_markets = [m for m in all_markets if self._is_hourly(m)]
        elif timeframe == "daily":
            all_markets = [m for m in all_markets if self._is_daily(m)]

        log.info(
            "Scanned %d crypto markets (timeframe=%s)",
            len(all_markets),
            timeframe,
        )
        return all_markets

    async def get_event_strikes(self, event_ticker: str) -> list[Market]:
        """
        Get all markets (strike levels) for a given event.

        Returns markets sorted by strike price ascending.
        Markets where the strike price cannot be parsed are appended at the end.

        Args:
            event_ticker: The event ticker (e.g., "KXBTC-26FEB14").

        Returns:
            Sorted list of Market objects.
        """
        markets = await self._client.get_all_markets(
            event_ticker=event_ticker,
            status="open",
        )

        # Attach parsed strike prices for sorting
        priced: list[tuple[float, Market]] = []
        unpriced: list[Market] = []

        for m in markets:
            strike = self.parse_strike_price(m)
            if strike is not None:
                priced.append((strike, m))
            else:
                unpriced.append(m)

        priced.sort(key=lambda x: x[0])
        result = [m for _, m in priced] + unpriced

        log.debug(
            "Event %s has %d strikes (%d priced, %d unpriced)",
            event_ticker,
            len(result),
            len(priced),
            len(unpriced),
        )
        return result

    @staticmethod
    def parse_strike_price(market: Market) -> float | None:
        """
        Parse the strike price from a market ticker or title.

        Examples:
            - Ticker "KXBTC-26FEB14-T70000" → 70000.0
            - Ticker "KXBTC-26FEB14-T69750" → 69750.0
            - Title subtitle "$70,500 or above" → 70500.0
            - Up/down markets → None

        Args:
            market: A Kalshi Market object.

        Returns:
            Strike price as a float, or None if unparseable.
        """
        # Try ticker first (most reliable)
        match = TICKER_STRIKE_RE.search(market.ticker)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass

        # Try subtitle
        if market.subtitle:
            price_match = TITLE_PRICE_RE.search(market.subtitle)
            if price_match:
                try:
                    price_str = price_match.group(1).replace(",", "")
                    return float(price_str)
                except ValueError:
                    pass

        # Try title
        if market.title:
            price_match = TITLE_PRICE_RE.search(market.title)
            if price_match:
                try:
                    price_str = price_match.group(1).replace(",", "")
                    return float(price_str)
                except ValueError:
                    pass

        return None

    @staticmethod
    def classify_market_type(market: Market) -> str:
        """
        Classify a market as "above", "below", "range", or "up_down".

        Args:
            market: A Kalshi Market object.

        Returns:
            One of "above", "below", "range", "up_down".
        """
        text = (market.title + " " + market.subtitle).lower()

        if "up or down" in text or "updown" in text:
            return "up_down"
        if "between" in text or "range" in text:
            return "range"
        if "below" in text or "under" in text:
            return "below"
        # Default: "above" (most Kalshi crypto markets are "X or above" style)
        return "above"

    async def find_active_events(self, asset: str = "BTC") -> list[Event]:
        """
        Get all active events for an asset.

        Args:
            asset: Asset symbol ("BTC", "ETH", "SOL").

        Returns:
            Events sorted by expiration (soonest first).
        """
        asset = asset.upper()
        series_map: dict[str, list[str]] = {
            "BTC": ["KXBTC", "KXBTCUD"],
            "ETH": ["KXETH"],
            "SOL": ["KXSOL"],
        }

        series_tickers = series_map.get(asset, [])
        if not series_tickers:
            log.warning("No series tickers configured for asset %s", asset)
            return []

        # Get all markets and group by event_ticker
        all_markets: list[Market] = []
        for series in series_tickers:
            try:
                markets = await self._client.get_all_markets(
                    series_ticker=series,
                    status="open",
                )
                all_markets.extend(markets)
            except Exception:
                log.exception("Failed to fetch markets for %s", series)

        # Group by event_ticker
        event_map: dict[str, list[Market]] = {}
        for m in all_markets:
            if m.event_ticker:
                event_map.setdefault(m.event_ticker, []).append(m)

        # Build Event objects
        events: list[Event] = []
        for event_ticker, markets in event_map.items():
            # Try to fetch full event data, fallback to constructing from markets
            try:
                event = await self._client.get_event(event_ticker)
                event.markets = markets
            except Exception:
                event = Event(
                    event_ticker=event_ticker,
                    title=markets[0].title if markets else "",
                    status="active",
                    markets=markets,
                )
            events.append(event)

        # Sort by earliest close_time among markets
        events.sort(
            key=lambda e: min(
                (m.close_time for m in e.markets if m.close_time),
                default="",
            )
        )

        log.info("Found %d active events for %s", len(events), asset)
        return events

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_hourly(market: Market) -> bool:
        """Check if a market is hourly (settles same day, not 15-min)."""
        if "KXBTCUD" in market.ticker:
            return False
        title_lower = market.title.lower()
        return "today" in title_lower or "hourly" in title_lower

    @staticmethod
    def _is_daily(market: Market) -> bool:
        """Check if a market is daily (settles next day)."""
        title_lower = market.title.lower()
        return "tomorrow" in title_lower or "daily" in title_lower

    @staticmethod
    def get_hours_to_expiry(market: Market) -> float | None:
        """
        Calculate hours until a market expires.

        Args:
            market: A Market object with close_time or expiration_time set.

        Returns:
            Hours until expiry, or None if times are missing.
        """
        time_str = market.expiration_time or market.close_time
        if not time_str:
            return None

        try:
            # Try ISO format parsing
            if time_str.endswith("Z"):
                time_str = time_str[:-1] + "+00:00"
            expiry = datetime.fromisoformat(time_str)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = expiry - now
            hours = delta.total_seconds() / 3600.0
            return max(0.0, hours)
        except (ValueError, TypeError):
            log.warning("Could not parse time string: %s", time_str)
            return None
