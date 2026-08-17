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
    allowed_uses: Sequence[str] = field(default_factory=tuple)
    execution_enabled: bool = False
    license_verified: bool = False
    sla_verified: bool = False
    semantic_evidence_version: Optional[str] = None

    @property
    def supports_core(self) -> bool:
        return all(self.core_fields.values())


class BaseAdapter(ABC):
    @property
    @abstractmethod
    def capability(self) -> DataCapability:
        raise NotImplementedError


class RealtimeAdapter(BaseAdapter):
    @abstractmethod
    def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def subscribe(self, tickers: Sequence[str]) -> None:
        raise NotImplementedError

    @abstractmethod
    def unsubscribe(self, tickers: Sequence[str]) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_latest(self, ticker: str) -> Optional[AuctionTick]:
        raise NotImplementedError

    @abstractmethod
    def stream(self) -> Iterable[AuctionTick]:
        raise NotImplementedError

    @abstractmethod
    def health(self) -> Mapping[str, object]:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def load(
        self,
        tickers: Sequence[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[AuctionTick]:
        raise NotImplementedError
