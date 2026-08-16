from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Set, Tuple

from config.thresholds import ThresholdRegistry
from domain.enums import AuthenticityState, CandidateGrade, DataQualityState, ExecutionState, PermissionState, ValidationState
from domain.models import DataQualityReport, DecisionTrace, DecisionTraceStep, MarketContext, SetupResult
from domain.reason_codes import ReasonCode


@dataclass(frozen=True)
class CompilerInput:
    trade_date: str
    decided_at: datetime
    in_night_pool: bool
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
    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds
        self._hard_cancelled: Set[Tuple[str, str]] = set()

    def compile(self, request: CompilerInput) -> DecisionTrace:
        key = (request.trade_date, request.setup_result.ticker)
        trace = DecisionTrace(request.setup_result.ticker, request.decided_at, CandidateGrade.DROP, ExecutionState.HARD_CANCELLED)
        if key in self._hard_cancelled:
            trace.steps.append(self._step("HardCancelSticky", False, ReasonCode.HARD_CANCEL_STICKY))
            return trace
        grade_cap = CandidateGrade.C_AUCTION_EMERGENT if not request.in_night_pool else None
        trace.steps.append(self._step("NightPool", request.in_night_pool, ReasonCode.OUTSIDE_NIGHT_POOL))
        if not self._permission_gate(trace, "MarketPermission", request.market_context.market_permission, ReasonCode.MARKET_PERMISSION_DENIED):
            return self._hard_cancel(trace, key)
        setup_permission = request.market_context.setup_permissions.get(request.setup_result.setup_type, PermissionState.DENY)
        if not self._permission_gate(trace, "SetupPermission", setup_permission, ReasonCode.SETUP_PERMISSION_DENIED):
            return self._hard_cancel(trace, key)
        if not self._validation_gate(trace, "Regulatory", request.regulatory_validation, ReasonCode.REGULATORY_HARD_INVALID):
            return self._hard_cancel(trace, key)
        if request.data_quality.state == DataQualityState.BROKEN:
            trace.steps.append(self._step("DQS", False, ReasonCode.DATA_QUALITY_BROKEN, request.data_quality.state.value))
            return self._hard_cancel(trace, key)
        trace.steps.append(self._step("DQS", True, observed=request.data_quality.state.value))
        if not self._validation_gate(trace, "MarketValidation", request.market_validation, ReasonCode.MARKET_VALIDATION_FAILED):
            return self._hard_cancel(trace, key)
        if not self._validation_gate(trace, "CandidateValidation", request.candidate_validation, ReasonCode.CANDIDATE_VALIDATION_FAILED):
            return self._hard_cancel(trace, key)
        authenticity_ok = request.setup_result.authenticity_state in (AuthenticityState.AUTHENTIC, AuthenticityState.HEALTHY_DISAGREEMENT)
        trace.steps.append(self._step("Authenticity", authenticity_ok, ReasonCode.AUTHENTICITY_FAILED, request.setup_result.authenticity_state.value))
        if not authenticity_ok:
            return self._hard_cancel(trace, key)
        trace.steps.append(self._step("SetupRequirements", request.setup_requirements_met, ReasonCode.SETUP_REQUIREMENT_FAILED))
        if not request.setup_requirements_met:
            return self._hard_cancel(trace, key)
        score_ok = request.setup_result.aqs >= self.thresholds.get("compiler_aqs_min") and request.setup_result.hvs >= self.thresholds.get("compiler_hvs_min") and request.auction_percentile is not None and request.auction_percentile >= self.thresholds.get("compiler_percentile_min")
        trace.steps.append(self._step("AQS/HVS/Percentile", score_ok, ReasonCode.SCORE_BELOW_GATE, {"aqs": request.setup_result.aqs, "hvs": request.setup_result.hvs, "percentile": request.auction_percentile}))
        if not score_ok:
            trace.grade = grade_cap or request.setup_result.grade_recommendation
            trace.execution_state = ExecutionState.WAIT
            return trace
        rank_ok = request.cross_setup_rank is not None and request.cross_setup_rank <= int(self.thresholds.get("compiler_rank_max"))
        trace.steps.append(self._step("CrossSetupRank", rank_ok, ReasonCode.CROSS_SETUP_RANK_FAILED, request.cross_setup_rank))
        if not rank_ok:
            trace.grade = grade_cap or CandidateGrade.B_HIGH_QUALITY
            trace.execution_state = ExecutionState.WAIT
            return trace
        position_ok = request.market_context.position_permission != PermissionState.DENY
        trace.steps.append(self._step("PositionPermission", position_ok, ReasonCode.POSITION_PERMISSION_DENIED, request.market_context.position_permission.value))
        if not position_ok:
            trace.grade = grade_cap or CandidateGrade.B_CONFIRMATION
            trace.execution_state = ExecutionState.WAIT
            return trace
        trace.grade = grade_cap or request.setup_result.grade_recommendation
        trace.execution_state = ExecutionState.WAIT if grade_cap else ExecutionState.ARMED
        return trace

    @staticmethod
    def _step(gate, passed, reason=None, observed=None):
        return DecisionTraceStep(gate, passed, None if passed or reason is None else reason.value, observed)

    def _permission_gate(self, trace, gate, state, reason):
        passed = state != PermissionState.DENY
        trace.steps.append(self._step(gate, passed, reason, state.value))
        return passed

    def _validation_gate(self, trace, gate, state, reason):
        passed = state not in (ValidationState.INVALID, ValidationState.HARD_INVALID)
        trace.steps.append(self._step(gate, passed, reason, state.value))
        return passed

    def _hard_cancel(self, trace, key):
        self._hard_cancelled.add(key)
        trace.grade = CandidateGrade.DROP
        trace.execution_state = ExecutionState.HARD_CANCELLED
        return trace
