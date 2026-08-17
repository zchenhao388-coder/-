from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from auction.phase import auction_phase_at
from config.thresholds import ThresholdRegistry
from domain.context import night_plan_context_id
from domain.enums import AuctionPhase, AuthenticityState, BenchmarkStatus, CandidateGrade, DataQualityState, ExecutionState, PermissionState, ValidationState
from domain.models import DataQualityReport, DecisionTrace, DecisionTraceStep, MarketContext, NightPlan, SetupResult
from domain.reason_codes import ReasonCode
from storage.hard_cancel import HardCancelStore


@dataclass(frozen=True)
class CompilerInput:
    trade_date: str
    decided_at: datetime
    night_plan: NightPlan
    market_context: MarketContext
    setup_result: SetupResult
    regulatory_validation: ValidationState
    data_quality: DataQualityReport
    market_validation: ValidationState
    candidate_validation: ValidationState
    setup_requirements_met: bool
    auction_percentile: Optional[float]
    cross_setup_rank: Optional[int]


class DecisionCompiler:
    def __init__(self, thresholds: ThresholdRegistry, hard_cancel_store: HardCancelStore):
        self.thresholds = thresholds
        self.hard_cancel_store = hard_cancel_store

    def compile(self, request: CompilerInput) -> DecisionTrace:
        self._validate_identity(request)
        ticker = request.setup_result.ticker
        key = (request.trade_date, ticker, night_plan_context_id(request.night_plan))
        trace = DecisionTrace(ticker, request.decided_at, CandidateGrade.DROP, ExecutionState.WAIT)
        if self.hard_cancel_store.contains(*key):
            trace.execution_state = ExecutionState.HARD_CANCELLED
            trace.steps.append(self._step("HardCancelSticky", False, ReasonCode.HARD_CANCEL_STICKY))
            return trace

        phase = auction_phase_at(request.decided_at)
        if phase not in (AuctionPhase.FINAL, AuctionPhase.OPEN_EXECUTION):
            trace.steps.append(self._step("TimeGate", False, ReasonCode.FINAL_DECISION_NOT_REACHED, phase.value))
            return trace

        outside_pool = ticker not in request.night_plan.candidate_pool
        grade_cap = CandidateGrade.C_AUCTION_EMERGENT if outside_pool else None
        trace.steps.append(self._step("NightPool", not outside_pool, ReasonCode.OUTSIDE_NIGHT_POOL))
        if not self._permission_gate(trace, "MarketPermission", request.market_context.market_permission, ReasonCode.MARKET_PERMISSION_DENIED):
            return self._hard_cancel(trace, key, ReasonCode.MARKET_PERMISSION_DENIED)
        setup_permission = request.market_context.setup_permissions.get(request.setup_result.setup_type, PermissionState.DENY)
        if not self._permission_gate(trace, "SetupPermission", setup_permission, ReasonCode.SETUP_PERMISSION_DENIED):
            return self._hard_cancel(trace, key, ReasonCode.SETUP_PERMISSION_DENIED)
        if not self._validation_gate(trace, "Regulatory", request.regulatory_validation, ReasonCode.REGULATORY_HARD_INVALID):
            return self._hard_cancel(trace, key, ReasonCode.REGULATORY_HARD_INVALID)
        if request.data_quality.state == DataQualityState.BROKEN:
            trace.steps.append(self._step("DQS", False, ReasonCode.DATA_QUALITY_BROKEN, request.data_quality.state.value))
            return self._hard_cancel(trace, key, ReasonCode.DATA_QUALITY_BROKEN)
        trace.steps.append(self._step("DQS", True, observed=request.data_quality.state.value))
        if request.data_quality.state == DataQualityState.DEGRADED and grade_cap is None:
            grade_cap = CandidateGrade.B_HIGH_QUALITY
        if not self._validation_gate(trace, "MarketValidation", request.market_validation, ReasonCode.MARKET_VALIDATION_FAILED):
            return self._hard_cancel(trace, key, ReasonCode.MARKET_VALIDATION_FAILED)
        if not self._validation_gate(trace, "CandidateValidation", request.candidate_validation, ReasonCode.CANDIDATE_VALIDATION_FAILED):
            return self._hard_cancel(trace, key, ReasonCode.CANDIDATE_VALIDATION_FAILED)
        authenticity_ok = request.setup_result.authenticity_state in (
            AuthenticityState.AUTHENTIC,
            AuthenticityState.HEALTHY_DISAGREEMENT,
        )
        trace.steps.append(self._step("Authenticity", authenticity_ok, ReasonCode.AUTHENTICITY_FAILED, request.setup_result.authenticity_state.value))
        if not authenticity_ok:
            return self._hard_cancel(trace, key, ReasonCode.AUTHENTICITY_FAILED)
        trace.steps.append(self._step("SetupRequirements", request.setup_requirements_met, ReasonCode.SETUP_REQUIREMENT_FAILED))
        if not request.setup_requirements_met:
            return self._hard_cancel(trace, key, ReasonCode.SETUP_REQUIREMENT_FAILED)
        benchmark_ok = (
            request.setup_result.benchmark_status == BenchmarkStatus.SUFFICIENT
            and request.setup_result.surprise_status == BenchmarkStatus.SUFFICIENT
        )
        trace.steps.append(self._step(
            "BenchmarkMinimumSample",
            benchmark_ok,
            ReasonCode.BENCHMARK_MINIMUM_SAMPLE_FAILED,
            {
                "benchmark": request.setup_result.benchmark_status.value,
                "surprise": request.setup_result.surprise_status.value,
            },
        ))
        if not benchmark_ok:
            trace.grade = CandidateGrade.B_HIGH_QUALITY
            return self._enforce_a_invariant(trace)
        score_ok = (
            request.setup_result.aqs >= self.thresholds.get("compiler_aqs_min")
            and request.setup_result.hvs is not None
            and request.setup_result.hvs >= self.thresholds.get("compiler_hvs_min")
            and request.auction_percentile is not None
            and request.auction_percentile >= self.thresholds.get("compiler_percentile_min")
        )
        trace.steps.append(self._step(
            "AQS/HVS/Percentile",
            score_ok,
            ReasonCode.SCORE_BELOW_GATE,
            {"aqs": request.setup_result.aqs, "hvs": request.setup_result.hvs, "percentile": request.auction_percentile},
        ))
        if not score_ok:
            trace.grade = grade_cap or self._non_a_grade(request.setup_result.grade_recommendation)
            return self._enforce_a_invariant(trace)
        rank_ok = request.cross_setup_rank is not None and request.cross_setup_rank <= int(self.thresholds.get("compiler_rank_max"))
        trace.steps.append(self._step("CrossSetupRank", rank_ok, ReasonCode.CROSS_SETUP_RANK_FAILED, request.cross_setup_rank))
        if not rank_ok:
            trace.grade = grade_cap or CandidateGrade.B_HIGH_QUALITY
            return self._enforce_a_invariant(trace)
        position_ok = request.market_context.position_permission != PermissionState.DENY
        trace.steps.append(self._step("PositionPermission", position_ok, ReasonCode.POSITION_PERMISSION_DENIED, request.market_context.position_permission.value))
        if not position_ok:
            trace.grade = grade_cap or CandidateGrade.B_CONFIRMATION
            return self._enforce_a_invariant(trace)

        trace.grade = grade_cap or request.setup_result.grade_recommendation
        trace.execution_state = ExecutionState.ARMED if trace.grade in (CandidateGrade.A1, CandidateGrade.A2) else ExecutionState.WAIT
        return self._enforce_a_invariant(trace)

    @staticmethod
    def _validate_identity(request: CompilerInput) -> None:
        if request.decided_at.tzinfo is None or request.decided_at.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")
        decided_date = request.decided_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        date.fromisoformat(request.trade_date)
        if request.trade_date != request.market_context.trade_date or request.trade_date != request.night_plan.trade_date or request.trade_date != decided_date:
            raise ValueError("trade_date, NightPlan, MarketContext, and decided_at date must match")
        if request.night_plan.generated_at > request.night_plan.information_available_at:
            raise ValueError("NightPlan generated_at exceeds its information cutoff")
        if request.night_plan.information_available_at > request.decided_at:
            raise ValueError("NightPlan contains future information")
        if request.setup_result.computed_as_of is not None and request.setup_result.computed_as_of > request.decided_at:
            raise ValueError("setup result was computed with future data")

    @staticmethod
    def _step(gate, passed, reason=None, observed=None):
        return DecisionTraceStep(gate, passed, None if passed or reason is None else reason.value, observed)

    def _permission_gate(self, trace, gate, state, reason):
        passed = state != PermissionState.DENY
        trace.steps.append(self._step(gate, passed, reason, state.value))
        return passed

    def _validation_gate(self, trace, gate, state, reason):
        passed = state not in (ValidationState.FALSIFIED, ValidationState.INVALID, ValidationState.HARD_INVALID)
        trace.steps.append(self._step(gate, passed, reason, state.value))
        return passed

    def _hard_cancel(self, trace, key, reason):
        self.hard_cancel_store.record(*key, reason.value)
        trace.grade = CandidateGrade.DROP
        trace.execution_state = ExecutionState.HARD_CANCELLED
        return trace

    @staticmethod
    def _non_a_grade(recommendation: CandidateGrade) -> CandidateGrade:
        if recommendation in (CandidateGrade.A1, CandidateGrade.A2):
            return CandidateGrade.B_HIGH_QUALITY
        return recommendation

    @staticmethod
    def _enforce_a_invariant(trace: DecisionTrace) -> DecisionTrace:
        is_a = trace.grade in (CandidateGrade.A1, CandidateGrade.A2)
        is_armed = trace.execution_state == ExecutionState.ARMED
        if is_a != is_armed:
            raise AssertionError("CandidateGrade A1/A2 iff ExecutionState ARMED invariant violated")
        return trace
