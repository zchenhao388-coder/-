from datetime import datetime
from typing import Iterable, Optional, Sequence

from domain.models import AuctionTick

from .base import BaseAdapter, DataCapability


class MockAdapter(BaseAdapter):
    def __init__(self, ticks: Sequence[AuctionTick]):
        self._ticks = tuple(sorted(ticks, key=lambda tick: float("inf") if tick.exchange_ts is None else tick.exchange_ts.timestamp()))

    @property
    def capability(self) -> DataCapability:
        return DataCapability(
            adapter_name="mock",
            core_fields={
                "exchange_ts": True,
                "receive_ts": True,
                "ticker": True,
                "virtual_price": True,
                "gap_pct": True,
                "matched_volume": True,
                "matched_amount": True,
            },
            auction_semantics_verified=True,
            timestamp_semantics="synthetic exchange time",
        )

    def load(
        self,
        tickers: Sequence[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[AuctionTick]:
        selected = set(tickers)
        for tick in self._ticks:
            if tick.ticker not in selected or tick.exchange_ts is None:
                continue
            if start is not None and tick.exchange_ts < start:
                continue
            if end is not None and tick.exchange_ts > end:
                continue
            yield tick
