"""
Paper trading results tracker: stores results in SQLite with CSV export.

Provides persistent storage of all paper trades, daily P&L summaries,
and strategy performance metrics.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshi.models import Fill
from utils.logger import get_logger

log = get_logger("paper_trading.results")

DEFAULT_DB_PATH = "./paper_results/results.db"


class ResultsTracker:
    """
    Persistent storage for paper trading results.

    Uses SQLite for queryable storage with CSV export capability.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        log.info("ResultsTracker initialized: %s", db_path)

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                trade_id TEXT,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                price_dollars REAL NOT NULL,
                fee_dollars REAL NOT NULL DEFAULT 0,
                is_taker INTEGER NOT NULL DEFAULT 1,
                strategy TEXT DEFAULT '',
                session_id TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                total_trades INTEGER DEFAULT 0,
                total_contracts INTEGER DEFAULT 0,
                realized_pnl REAL DEFAULT 0,
                total_fees REAL DEFAULT 0,
                strategies_used TEXT DEFAULT '',
                session_id TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                initial_balance REAL DEFAULT 0,
                final_balance REAL DEFAULT 0,
                total_trades INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                total_fees REAL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
            CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_summary(date);
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_fill(
        self,
        fill: Fill,
        fee_dollars: float = 0.0,
        strategy: str = "",
        session_id: str = "",
    ) -> None:
        """
        Record a paper trade fill.

        Args:
            fill: The Fill object from the paper engine.
            fee_dollars: Calculated fee for this fill.
            strategy: The strategy that generated this trade.
            session_id: Current session identifier.
        """
        self._conn.execute(
            """
            INSERT INTO trades (timestamp, trade_id, ticker, side, action,
                                contracts, price_dollars, fee_dollars, is_taker,
                                strategy, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.created_time or datetime.now(timezone.utc).isoformat(),
                fill.trade_id,
                fill.ticker,
                fill.side,
                fill.action,
                fill.count,
                fill.price_dollars,
                fee_dollars,
                1 if fill.is_taker else 0,
                strategy,
                session_id,
            ),
        )
        self._conn.commit()

    def record_daily_summary(
        self,
        date: str,
        total_trades: int,
        total_contracts: int,
        realized_pnl: float,
        total_fees: float,
        strategies_used: list[str],
        session_id: str = "",
    ) -> None:
        """Record end-of-day summary."""
        self._conn.execute(
            """
            INSERT INTO daily_summary (date, total_trades, total_contracts,
                                       realized_pnl, total_fees, strategies_used,
                                       session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date,
                total_trades,
                total_contracts,
                realized_pnl,
                total_fees,
                ",".join(strategies_used),
                session_id,
            ),
        )
        self._conn.commit()

    def start_session(
        self,
        session_id: str,
        initial_balance: float,
    ) -> None:
        """Record the start of a trading session."""
        self._conn.execute(
            """
            INSERT INTO sessions (session_id, start_time, initial_balance)
            VALUES (?, ?, ?)
            """,
            (session_id, datetime.now(timezone.utc).isoformat(), initial_balance),
        )
        self._conn.commit()

    def end_session(
        self,
        session_id: str,
        final_balance: float,
        total_trades: int,
        total_pnl: float,
        total_fees: float,
    ) -> None:
        """Record the end of a trading session."""
        self._conn.execute(
            """
            UPDATE sessions
            SET end_time = ?, final_balance = ?, total_trades = ?,
                total_pnl = ?, total_fees = ?
            WHERE session_id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                final_balance,
                total_trades,
                total_pnl,
                total_fees,
                session_id,
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_trade_count(self, session_id: str | None = None) -> int:
        """Get total number of recorded trades."""
        if session_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM trades WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        return row[0] if row else 0

    def get_strategy_stats(self, strategy: str) -> dict[str, Any]:
        """Get performance statistics for a specific strategy."""
        rows = self._conn.execute(
            """
            SELECT COUNT(*) as trades, SUM(contracts) as total_contracts,
                   SUM(fee_dollars) as total_fees
            FROM trades WHERE strategy = ?
            """,
            (strategy,),
        ).fetchone()

        if not rows or rows[0] == 0:
            return {"trades": 0, "total_contracts": 0, "total_fees": 0}

        return {
            "trades": rows[0],
            "total_contracts": rows[1] or 0,
            "total_fees": rows[2] or 0,
        }

    def get_recent_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get the most recent trades."""
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_daily_pnl(self, days: int = 30) -> list[dict[str, Any]]:
        """Get daily P&L summaries for the last N days."""
        rows = self._conn.execute(
            "SELECT * FROM daily_summary ORDER BY date DESC LIMIT ?",
            (days,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_all_sessions(self) -> list[dict[str, Any]]:
        """Get all recorded trading sessions."""
        rows = self._conn.execute(
            "SELECT * FROM sessions ORDER BY start_time DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_trades_csv(self, filepath: str, session_id: str | None = None) -> None:
        """
        Export trades to a CSV file.

        Args:
            filepath: Path to the output CSV file.
            session_id: Optional session filter.
        """
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE session_id = ? ORDER BY timestamp",
                (session_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY timestamp"
            ).fetchall()

        if not rows:
            log.info("No trades to export")
            return

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            # Header
            writer.writerow([
                "timestamp", "trade_id", "ticker", "side", "action",
                "contracts", "price_dollars", "fee_dollars", "is_taker",
                "strategy", "session_id",
            ])
            for row in rows:
                writer.writerow([
                    row["timestamp"],
                    row["trade_id"],
                    row["ticker"],
                    row["side"],
                    row["action"],
                    row["contracts"],
                    row["price_dollars"],
                    row["fee_dollars"],
                    row["is_taker"],
                    row["strategy"],
                    row["session_id"],
                ])

        log.info("Exported %d trades to %s", len(rows), filepath)

    def export_daily_csv(self, filepath: str) -> None:
        """Export daily summaries to CSV."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        rows = self._conn.execute(
            "SELECT * FROM daily_summary ORDER BY date"
        ).fetchall()

        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "date", "total_trades", "total_contracts",
                "realized_pnl", "total_fees", "strategies_used",
            ])
            for row in rows:
                writer.writerow([
                    row["date"],
                    row["total_trades"],
                    row["total_contracts"],
                    row["realized_pnl"],
                    row["total_fees"],
                    row["strategies_used"],
                ])

        log.info("Exported %d daily summaries to %s", len(rows), filepath)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
        log.info("ResultsTracker closed")
