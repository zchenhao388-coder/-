import sqlite3
from pathlib import Path
from typing import Protocol, Union


class HardCancelStore(Protocol):
    def contains(self, trade_date: str, ticker: str, strategy_context_id: str) -> bool:
        ...

    def record(self, trade_date: str, ticker: str, strategy_context_id: str, reason: str) -> None:
        ...


class SQLiteHardCancelStore:
    """Process-safe persistent same-day cancellation state."""

    def __init__(self, path: Union[str, Path]):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS hard_cancels (
                    trade_date TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    strategy_context_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (trade_date, ticker, strategy_context_id)
                )
                """
            )

    def contains(self, trade_date: str, ticker: str, strategy_context_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM hard_cancels WHERE trade_date=? AND ticker=? AND strategy_context_id=?",
                (trade_date, ticker, strategy_context_id),
            ).fetchone()
        return row is not None

    def record(self, trade_date: str, ticker: str, strategy_context_id: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO hard_cancels (trade_date, ticker, strategy_context_id, reason) VALUES (?, ?, ?, ?)",
                (trade_date, ticker, strategy_context_id, reason),
            )
