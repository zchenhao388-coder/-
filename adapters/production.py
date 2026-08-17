from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Iterable, Mapping, Optional, Protocol, Sequence, Tuple

from adapters.base import DataCapability, RealtimeAdapter
from domain.enums import FieldAcceptanceStatus
from domain.models import AuctionTick


class DataSourceNotCertified(RuntimeError):
    pass


@dataclass(frozen=True)
class VendorContract:
    vendor_name: str
    license_reference: str
    sla_reference: str
    valid_until: date


@dataclass(frozen=True)
class FieldSemanticAcceptance:
    canonical_field: str
    status: FieldAcceptanceStatus
    evidence_reference: str
    sample_size: int
    verified_at: datetime


@dataclass(frozen=True)
class DataSourceAcceptanceReport:
    adapter_name: str
    contract: VendorContract
    fields: Sequence[FieldSemanticAcceptance]
    auction_semantics_verified: bool
    exchange_timestamp_verified: bool
    tested_trade_dates: Sequence[str]
    evidence_version: str


class ExecutionDataSourceGuard:
    FORBIDDEN_EXECUTION_ADAPTERS = {
        "mootdx_realtime_probe",
        "tencent_realtime_probe",
    }
    REQUIRED_CORE = (
        "exchange_ts",
        "receive_ts",
        "ticker",
        "virtual_price",
        "gap_pct",
        "matched_volume",
        "matched_amount",
    )

    def authorize(
        self,
        capability: DataCapability,
        report: DataSourceAcceptanceReport,
        as_of: datetime,
    ) -> None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise DataSourceNotCertified("AS_OF_TIMESTAMP_INVALID")
        failures = []
        if (
            "eastmoney" in capability.adapter_name.lower()
            or capability.adapter_name.lower() in self.FORBIDDEN_EXECUTION_ADAPTERS
        ):
            failures.append("EXPERIMENTAL_SOURCE_FORBIDDEN")
        if capability.adapter_name != report.adapter_name:
            failures.append("ACCEPTANCE_REPORT_SOURCE_MISMATCH")
        if not capability.execution_enabled or "EXECUTION" not in capability.allowed_uses:
            failures.append("EXECUTION_NOT_ENABLED")
        if not capability.license_verified or not capability.sla_verified:
            failures.append("LICENSE_OR_SLA_UNVERIFIED")
        if report.contract.valid_until < as_of.date():
            failures.append("VENDOR_CONTRACT_EXPIRED")
        if (
            not report.contract.vendor_name
            or not report.contract.license_reference
            or not report.contract.sla_reference
        ):
            failures.append("VENDOR_CONTRACT_EVIDENCE_MISSING")
        if not capability.auction_semantics_verified or not report.auction_semantics_verified:
            failures.append("AUCTION_SEMANTICS_UNVERIFIED")
        if not report.exchange_timestamp_verified:
            failures.append("EXCHANGE_TIMESTAMP_UNVERIFIED")
        if not report.tested_trade_dates or not report.evidence_version:
            failures.append("TRADING_DAY_EVIDENCE_MISSING")
        if capability.semantic_evidence_version != report.evidence_version:
            failures.append("SEMANTIC_EVIDENCE_VERSION_MISMATCH")
        accepted = {item.canonical_field: item for item in report.fields}
        if len(accepted) != len(report.fields):
            failures.append("DUPLICATE_FIELD_EVIDENCE")
        for field in self.REQUIRED_CORE:
            if not capability.core_fields.get(field, False):
                failures.append(f"CAPABILITY_MISSING:{field}")
                continue
            evidence = accepted.get(field)
            if (
                evidence is None
                or evidence.status != FieldAcceptanceStatus.VERIFIED
                or evidence.sample_size <= 0
                or not evidence.evidence_reference
                or evidence.verified_at.tzinfo is None
                or evidence.verified_at.utcoffset() is None
                or evidence.verified_at > as_of
            ):
                failures.append(f"FIELD_UNVERIFIED:{field}")
        if failures:
            raise DataSourceNotCertified(";".join(failures))


@dataclass(frozen=True)
class CanonicalFieldMap:
    exchange_ts: str
    receive_ts: str
    ticker: str
    virtual_price: str
    gap_pct: str
    matched_volume: str
    matched_amount: str
    unmatched_side: Optional[str] = None
    unmatched_volume: Optional[str] = None
    bid: Optional[str] = None
    ask: Optional[str] = None
    orderbook: Optional[str] = None


class CanonicalAuctionMapper:
    def __init__(self, fields: CanonicalFieldMap, source: str):
        self.fields = fields
        self.source = source

    def map(self, event: Mapping[str, object]) -> AuctionTick:
        exchange_ts = event.get(self.fields.exchange_ts)
        receive_ts = event.get(self.fields.receive_ts)
        if exchange_ts is not None and not isinstance(exchange_ts, datetime):
            raise TypeError("mapped exchange_ts must be datetime")
        if receive_ts is not None and not isinstance(receive_ts, datetime):
            raise TypeError("mapped receive_ts must be datetime")
        return AuctionTick(
            exchange_ts=exchange_ts,
            receive_ts=receive_ts,
            ticker=str(event[self.fields.ticker]),
            virtual_price=self._number(event.get(self.fields.virtual_price)),
            gap_pct=self._number(event.get(self.fields.gap_pct)),
            matched_volume=self._number(event.get(self.fields.matched_volume)),
            matched_amount=self._number(event.get(self.fields.matched_amount)),
            unmatched_side=self._optional_value(event, self.fields.unmatched_side),
            unmatched_volume=self._optional_number(event, self.fields.unmatched_volume),
            bid=self._optional_number(event, self.fields.bid),
            ask=self._optional_number(event, self.fields.ask),
            orderbook=self._optional_value(event, self.fields.orderbook),
            source=self.source,
            raw=dict(event),
        )

    @staticmethod
    def _number(value) -> Optional[float]:
        return None if value is None else float(value)

    @staticmethod
    def _optional_value(event: Mapping[str, object], field: Optional[str]):
        return None if field is None else event.get(field)

    @classmethod
    def _optional_number(cls, event: Mapping[str, object], field: Optional[str]) -> Optional[float]:
        return None if field is None else cls._number(event.get(field))


class RealtimeTransport(Protocol):
    def connect(self) -> None:
        ...

    def subscribe(self, tickers: Sequence[str]) -> None:
        ...

    def unsubscribe(self, tickers: Sequence[str]) -> None:
        ...

    def stream(self) -> Iterable[Mapping[str, object]]:
        ...

    def health(self) -> Mapping[str, object]:
        ...

    def close(self) -> None:
        ...


class CertifiedRealtimeAdapter(RealtimeAdapter):
    """Vendor-neutral production boundary; it cannot connect without acceptance evidence."""

    def __init__(
        self,
        capability: DataCapability,
        acceptance_report: DataSourceAcceptanceReport,
        transport: RealtimeTransport,
        mapper: CanonicalAuctionMapper,
        clock: Callable[[], datetime],
        guard: Optional[ExecutionDataSourceGuard] = None,
    ):
        self._capability = capability
        self.acceptance_report = acceptance_report
        self.transport = transport
        self.mapper = mapper
        self.clock = clock
        self.guard = guard or ExecutionDataSourceGuard()
        self._latest = {}
        self._connected = False

    @property
    def capability(self) -> DataCapability:
        return self._capability

    def connect(self) -> None:
        self.guard.authorize(self.capability, self.acceptance_report, self.clock())
        self.transport.connect()
        self._connected = True

    def subscribe(self, tickers: Sequence[str]) -> None:
        self._require_connected()
        self.transport.subscribe(tickers)

    def unsubscribe(self, tickers: Sequence[str]) -> None:
        self._require_connected()
        self.transport.unsubscribe(tickers)

    def get_latest(self, ticker: str) -> Optional[AuctionTick]:
        return self._latest.get(ticker)

    def stream(self) -> Iterable[AuctionTick]:
        self._require_connected()
        for event in self.transport.stream():
            tick = self.mapper.map(event)
            self._latest[tick.ticker] = tick
            yield tick

    def load(
        self,
        tickers: Sequence[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[AuctionTick]:
        selected = set(tickers)
        for tick in self.stream():
            if tick.ticker not in selected or tick.exchange_ts is None:
                continue
            if start is not None and tick.exchange_ts < start:
                continue
            if end is not None and tick.exchange_ts > end:
                continue
            yield tick

    def health(self) -> Mapping[str, object]:
        return self.transport.health()

    def close(self) -> None:
        self.transport.close()
        self._connected = False

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("adapter is not connected")
