from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence, Tuple

from adapters.base import DataCapability
from adapters.production import DataSourceAcceptanceReport, ExecutionDataSourceGuard
from config.thresholds import ThresholdRegistry
from domain.enums import CircuitBreakerState, DataQualityState
from domain.models import SourceValidationReport
from market.asof import AsOfMarketView


@dataclass(frozen=True)
class FeedHealthReport:
    ticker: str
    as_of: datetime
    state: DataQualityState
    latest_exchange_age_seconds: Optional[float]
    latest_receive_lag_seconds: Optional[float]
    missing_fraction: float
    reasons: Sequence[str]


class FeedHealthMonitor:
    CORE_FIELDS = ("virtual_price", "gap_pct", "matched_volume", "matched_amount")

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds

    def evaluate(
        self,
        market_view: AsOfMarketView,
        ticker: str,
        source_validation: Optional[SourceValidationReport] = None,
    ) -> FeedHealthReport:
        ticks = market_view.get_ticks(ticker)
        if not ticks:
            return FeedHealthReport(ticker, market_view.as_of, DataQualityState.BROKEN, None, None, 1.0, ("NO_VISIBLE_TICKS",))
        latest = ticks[-1]
        missing = sum(1 for name in self.CORE_FIELDS if getattr(latest, name) is None)
        missing_fraction = missing / len(self.CORE_FIELDS)
        exchange_age = None if latest.exchange_ts is None else (market_view.as_of - latest.exchange_ts).total_seconds()
        receive_lag = None
        if latest.exchange_ts is not None and latest.receive_ts is not None:
            receive_lag = (latest.receive_ts - latest.exchange_ts).total_seconds()
        reasons = []
        if exchange_age is None:
            reasons.append("MISSING_EXCHANGE_TIMESTAMP")
        if exchange_age is not None and exchange_age > self.thresholds.get("feed_broken_latency_seconds"):
            reasons.append("FEED_LATENCY_BROKEN")
        elif exchange_age is not None and exchange_age > self.thresholds.get("feed_degraded_latency_seconds"):
            reasons.append("FEED_LATENCY_DEGRADED")
        if missing_fraction >= self.thresholds.get("feed_broken_missing_fraction"):
            reasons.append("CORE_FIELDS_BROKEN")
        elif missing_fraction >= self.thresholds.get("feed_degraded_missing_fraction"):
            reasons.append("CORE_FIELDS_DEGRADED")
        if "FEED_LATENCY_BROKEN" in reasons or "CORE_FIELDS_BROKEN" in reasons or "MISSING_EXCHANGE_TIMESTAMP" in reasons:
            state = DataQualityState.BROKEN
        elif reasons:
            state = DataQualityState.DEGRADED
        else:
            state = DataQualityState.GOOD
        if source_validation is not None:
            reasons.extend(code for code in source_validation.reason_codes if code not in reasons)
            if source_validation.dqs_state == DataQualityState.BROKEN:
                state = DataQualityState.BROKEN
            elif source_validation.dqs_state == DataQualityState.DEGRADED and state == DataQualityState.GOOD:
                state = DataQualityState.DEGRADED
        return FeedHealthReport(ticker, market_view.as_of, state, exchange_age, receive_lag, missing_fraction, tuple(reasons))


class DataCircuitBreaker:
    """BROKEN opens fail-closed and requires explicit certified reset."""

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds
        self.state = CircuitBreakerState.CLOSED
        self._good_while_open = 0

    def observe(self, report: FeedHealthReport) -> CircuitBreakerState:
        if report.state == DataQualityState.BROKEN:
            self.state = CircuitBreakerState.OPEN
            self._good_while_open = 0
        elif self.state == CircuitBreakerState.OPEN:
            if report.state == DataQualityState.GOOD:
                self._good_while_open += 1
            else:
                self._good_while_open = 0
        elif report.state == DataQualityState.DEGRADED:
            self.state = CircuitBreakerState.DEGRADED
        else:
            self.state = CircuitBreakerState.CLOSED
        return self.state

    def reset(
        self,
        capability: DataCapability,
        acceptance_report: DataSourceAcceptanceReport,
        as_of: datetime,
        guard: Optional[ExecutionDataSourceGuard] = None,
    ) -> None:
        (guard or ExecutionDataSourceGuard()).authorize(capability, acceptance_report, as_of)
        required = int(self.thresholds.get("breaker_recovery_good_samples"))
        if self._good_while_open < required:
            raise RuntimeError("circuit breaker recovery sample requirement is not met")
        self.state = CircuitBreakerState.CLOSED
        self._good_while_open = 0
