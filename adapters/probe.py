from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional


@dataclass(frozen=True)
class RawSourcePayload:
    """Source-native payload envelope for append-only probe capture."""

    source: str
    event_type: str
    ticker: str
    receive_ts: datetime
    payload: Mapping[str, object]
    provenance: Mapping[str, str]
    provider_ts: Optional[datetime] = None
    exchange_ts: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.source or not self.event_type or not self.ticker:
            raise ValueError("source, event_type and ticker are required")
        if self.receive_ts.tzinfo is None or self.receive_ts.utcoffset() is None:
            raise ValueError("receive_ts must be timezone-aware")
        for timestamp in (self.provider_ts, self.exchange_ts):
            if timestamp is not None and (timestamp.tzinfo is None or timestamp.utcoffset() is None):
                raise ValueError("provider_ts and exchange_ts must be timezone-aware when present")
