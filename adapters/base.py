from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Mapping, Optional, Sequence

from domain.models import AuctionTick


@dataclass(frozen=True)
class DataCapability:
    adapter_name: str
    core_fields: Mapping[str, bool]
    enhanced_fields: Mapping[str, bool] = field(default_factory=dict)
    field_provenance: Mapping[str, str] = field(default_factory=dict)
    timestamp_semantics: str = "UNKNOWN"
    auction_semantics_verified: bool = False
    notes: Sequence[str] = field(default_factory=tuple)

    @property
    def supports_core(self) -> bool:
        return all(self.core_fields.values())


class BaseAdapter(ABC):
    @property
    @abstractmethod
    def capability(self) -> DataCapability:
        raise NotImplementedError

    @abstractmethod
    def load(
        self,
        tickers: Sequence[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[AuctionTick]:
        raise NotImplementedError
