from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable, Mapping, Optional, Sequence, Tuple

from adapters.base import DataCapability
from adapters.production import DataSourceAcceptanceReport, DataSourceNotCertified, ExecutionDataSourceGuard
from domain.enums import CircuitBreakerState, DailyStage
from monitoring.feed import DataCircuitBreaker, FeedHealthReport


@dataclass(frozen=True)
class RuntimeMode:
    shadow_mode: bool = True
    auto_order: bool = False

    def __post_init__(self) -> None:
        if self.auto_order:
            raise ValueError("V0.1 M11 does not permit automatic order placement")


@dataclass(frozen=True)
class OrchestrationEvent:
    trade_date: str
    as_of: datetime
    stage: DailyStage
    checkpoint: str
    executed: bool
    detail: str


class DailySchedule:
    @staticmethod
    def locate(timestamp: datetime) -> Tuple[DailyStage, str]:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("orchestrator timestamp must be timezone-aware")
        current = timestamp.time().replace(tzinfo=None)
        if current < time(9, 15):
            return DailyStage.PRE_FLIGHT, "PRE_FLIGHT"
        if current < time(9, 20):
            return DailyStage.AUCTION, "AUCTION_START_0915"
        if current < time(9, 24, 30):
            return DailyStage.AUCTION, "AUTHENTICITY_0920"
        if current < time(9, 25):
            return DailyStage.AUCTION, "FINAL_WINDOW_092430"
        if current < time(9, 30):
            return DailyStage.FINAL_DECISION, "FINAL_DECISION_0925"
        if current < time(9, 35):
            return DailyStage.OPEN_EXECUTION, "OPEN_EXECUTION_0930_0935"
        if current < time(15, 0):
            return DailyStage.INTRADAY_IDLE, "INTRADAY_IDLE"
        return DailyStage.AFTER_CLOSE, "AFTER_CLOSE_OUTCOME_NIGHT_PLAN"


class ApplicationOrchestrator:
    """Fail-closed checkpoint orchestration; handlers produce signals, never orders."""

    def __init__(
        self,
        capability: DataCapability,
        acceptance_report: DataSourceAcceptanceReport,
        circuit_breaker: DataCircuitBreaker,
        handlers: Mapping[str, Callable[[datetime], object]],
        mode: RuntimeMode = RuntimeMode(),
        guard: Optional[ExecutionDataSourceGuard] = None,
    ):
        self.capability = capability
        self.acceptance_report = acceptance_report
        self.circuit_breaker = circuit_breaker
        self.handlers = dict(handlers)
        self.mode = mode
        self.guard = guard or ExecutionDataSourceGuard()
        self._completed = set()

    def run_checkpoint(self, as_of: datetime, health: FeedHealthReport) -> OrchestrationEvent:
        stage, checkpoint = DailySchedule.locate(as_of)
        key = (as_of.date().isoformat(), checkpoint)
        if health.as_of != as_of:
            return OrchestrationEvent(
                key[0], as_of, DailyStage.HALTED, checkpoint, False, "HEALTH_REPORT_AS_OF_MISMATCH"
            )
        try:
            self.guard.authorize(self.capability, self.acceptance_report, as_of)
        except DataSourceNotCertified as error:
            return OrchestrationEvent(key[0], as_of, DailyStage.HALTED, checkpoint, False, str(error))
        breaker_state = self.circuit_breaker.observe(health)
        if breaker_state == CircuitBreakerState.OPEN:
            return OrchestrationEvent(key[0], as_of, DailyStage.HALTED, checkpoint, False, "DATA_CIRCUIT_OPEN")
        if key in self._completed:
            return OrchestrationEvent(key[0], as_of, stage, checkpoint, False, "IDEMPOTENT_ALREADY_COMPLETED")
        handler = self.handlers.get(checkpoint)
        if handler is None:
            return OrchestrationEvent(key[0], as_of, stage, checkpoint, False, "NO_HANDLER")
        handler(as_of)
        self._completed.add(key)
        mode = "SHADOW" if self.mode.shadow_mode else "SIGNAL_ONLY"
        return OrchestrationEvent(key[0], as_of, stage, checkpoint, True, mode)
