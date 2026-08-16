from datetime import datetime
from typing import Iterable, Optional, Sequence

from domain.models import AuctionTick

from .base import BaseAdapter, DataCapability


class ReplayAdapter(BaseAdapter):
    """Adapter over immutable canonical ticks stored by an earlier feed."""

    def __init__(self, ticks: Sequence[AuctionTick]):
        if any(tick.exchange_ts is None for tick in ticks):
            raise ValueError("ReplayAdapter requires exchange_ts on every tick")
        self._ticks = tuple(sorted(ticks, key=lambda tick: tick.exchange_ts.timestamp()))  # type: ignore[union-attr]

    @property
    def capability(self) -> DataCapability:
        return DataCapability(
            adapter_name="replay",
            core_fields={name: True for name in (
                "exchange_ts", "receive_ts", "ticker", "virtual_price", "gap_pct",
                "matched_volume", "matched_amount",
            )},
            auction_semantics_verified=True,
            timestamp_semantics="preserved source exchange timestamp",
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
