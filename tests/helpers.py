from datetime import datetime, timedelta
from typing import Sequence

from domain.models import AuctionTick


def ts(value: str) -> datetime:
    return datetime.fromisoformat(f"2026-08-14T{value}+08:00")


def make_ticks(points: Sequence[tuple], ticker: str = "000001.SZ"):
    result = []
    for index, (clock, gap) in enumerate(points, start=1):
        exchange = ts(clock)
        result.append(AuctionTick(
            exchange_ts=exchange,
            receive_ts=exchange + timedelta(milliseconds=100),
            ticker=ticker,
            virtual_price=10.0 * (1 + gap / 100.0),
            gap_pct=gap,
            matched_volume=100_000.0 * index,
            matched_amount=1_000_000.0 * index,
            source="TEST",
        ))
    return result
