from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Mapping, Optional, Sequence

from .enums import (
    AuthenticityState,
    AuctionOpenConsistency,
    BenchmarkStatus,
    CandidateGrade,
    DataQualityState,
    ExecutionState,
    ExecutionMode,
    LeadershipState,
    IdentityRecoveryState,
    PermissionState,
    OpenPhase,
    OutcomeClass,
    SignalTradability,
    SourceAgreementState,
    SetupType,
    ValidationState,
    WeaknessResolution,
    WeaknessSource,
    WeakToStrongType,
)


@dataclass(frozen=True)
class MarketContext:
    trade_date: str
    phase: str
    regime: str
    pes: Optional[float] = None
    les: Optional[float] = None
    crs: Optional[float] = None
    market_permission: PermissionState = PermissionState.DENY
    setup_permissions: Mapping[SetupType, PermissionState] = field(default_factory=dict)
    position_permission: PermissionState = PermissionState.DENY
    theme_validation: ValidationState = ValidationState.UNVALIDATED


@dataclass(frozen=True)
class NightPlan:
    trade_date: str
    candidate_pool: Sequence[str]
    setup_by_ticker: Mapping[str, SetupType]
    generated_at: datetime
    information_available_at: datetime


@dataclass(frozen=True)
class WeaknessState:
    weakness_type: WeakToStrongType
    weakness_severity: float
    weakness_source: WeaknessSource
    repairable: bool


@dataclass(frozen=True)
class AuctionTick:
    exchange_ts: Optional[datetime]
    receive_ts: Optional[datetime]
    ticker: str
    virtual_price: Optional[float]
    gap_pct: Optional[float]
    matched_volume: Optional[float]
    matched_amount: Optional[float]
    unmatched_side: Optional[str] = None
    unmatched_volume: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    orderbook: Optional[Mapping[str, Any]] = None
    provider_ts: Optional[datetime] = None
    source: str = "UNKNOWN"
    raw: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if not self.ticker:
            raise ValueError("ticker is required")
        for name in ("matched_volume", "matched_amount"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class SourceFieldComparison:
    field: str
    primary_value: Optional[float]
    secondary_value: Optional[float]
    absolute_difference: Optional[float]
    relative_difference_pct: Optional[float]
    state: SourceAgreementState


@dataclass(frozen=True)
class SourceValidationReport:
    ticker: str
    as_of: datetime
    primary_source: str
    secondary_source: str
    state: SourceAgreementState
    dqs_state: DataQualityState
    comparisons: Sequence[SourceFieldComparison]
    reason_codes: Sequence[str]
    execution_eligible: bool = False
    receive_time_skew_seconds: Optional[float] = None
    provider_time_skew_seconds: Optional[float] = None


@dataclass(frozen=True)
class OpenTick:
    exchange_ts: Optional[datetime]
    receive_ts: Optional[datetime]
    ticker: str
    last_price: Optional[float]
    trade_volume: Optional[float]
    trade_amount: Optional[float]
    open_price: Optional[float]
    high: Optional[float]
    low: Optional[float]
    bid1: Optional[float] = None
    ask1: Optional[float] = None
    aggressive_buy: Optional[float] = None
    aggressive_sell: Optional[float] = None
    orderbook_depth: Optional[Mapping[str, Any]] = None
    source: str = "UNKNOWN"
    raw: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if not self.ticker:
            raise ValueError("ticker is required")
        for name in ("trade_volume", "trade_amount"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass
class CandidateAuctionState:
    ticker: str
    setup_type: SetupType
    ticks: Sequence[AuctionTick]
    leadership_state: LeadershipState = LeadershipState.UNKNOWN
    features: Mapping[str, Optional[float]] = field(default_factory=dict)
    fake_strong_flags: Sequence[str] = field(default_factory=list)
    validation_state: ValidationState = ValidationState.UNVALIDATED
    authenticity_state: AuthenticityState = AuthenticityState.UNKNOWN


@dataclass(frozen=True)
class SetupResult:
    ticker: str
    setup_type: SetupType
    expectation_score: Optional[float]
    authenticity_score: Optional[float]
    relative_strength_score: Optional[float]
    liquidity_score: Optional[float]
    context_score: Optional[float]
    hvs: Optional[float]
    aqs: float
    validation_state: ValidationState
    authenticity_state: AuthenticityState
    flags: Sequence[str]
    grade_recommendation: CandidateGrade
    effective_weights: Mapping[str, float] = field(default_factory=dict)
    computed_as_of: Optional[datetime] = None
    benchmark_status: BenchmarkStatus = BenchmarkStatus.INSUFFICIENT
    surprise_status: BenchmarkStatus = BenchmarkStatus.INSUFFICIENT
    leadership_state: LeadershipState = LeadershipState.UNKNOWN
    reason_codes: Sequence[str] = field(default_factory=tuple)
    ecosystem_state: Optional[str] = None
    weakness_resolved: Optional[WeaknessResolution] = None
    identity_recovery: Optional[IdentityRecoveryState] = None
    setup_quality_percentile: Optional[float] = None


@dataclass(frozen=True)
class OpenExecutionPlan:
    ticker: str
    state: ExecutionState
    valid_from: datetime
    expires_at: datetime
    triggers: Sequence[str]
    hard_cancels: Sequence[str]
    setup_type: Optional[SetupType] = None
    auction_grade: Optional[CandidateGrade] = None
    execution_mode: Optional[ExecutionMode] = None
    auction_price: Optional[float] = None
    auction_theme_rank: Optional[float] = None
    must_hold: Sequence[str] = field(default_factory=tuple)
    positive_triggers: Sequence[str] = field(default_factory=tuple)
    soft_invalid: Sequence[str] = field(default_factory=tuple)
    upgrade_requirements: Sequence[str] = field(default_factory=tuple)
    chase_policy: str = "NO_CHASE_IF_EXTENDED"
    context_id: Optional[str] = None


@dataclass(frozen=True)
class OpenFeatureSnapshot:
    ticker: str
    trade_date: str
    computed_as_of: datetime
    values: Mapping[str, Optional[float]]


@dataclass(frozen=True)
class OpenExecutionDecision:
    ticker: str
    decided_at: datetime
    phase: OpenPhase
    state: ExecutionState
    consistency: AuctionOpenConsistency
    tradability: SignalTradability
    features: OpenFeatureSnapshot
    reason_codes: Sequence[str] = field(default_factory=tuple)
    trigger_price: Optional[float] = None


@dataclass(frozen=True)
class DecisionTraceStep:
    gate: str
    passed: bool
    reason: Optional[str] = None
    observed: Optional[Any] = None


@dataclass
class DecisionTrace:
    ticker: str
    decided_at: datetime
    grade: CandidateGrade
    execution_state: ExecutionState
    steps: List[DecisionTraceStep] = field(default_factory=list)


@dataclass(frozen=True)
class Outcome:
    ticker: str
    trade_date: str
    decision_grade: CandidateGrade
    executed: bool
    entry_price: Optional[float]
    return_1m: Optional[float]
    return_5m: Optional[float]
    return_close: Optional[float]
    max_favorable_excursion: Optional[float]
    max_adverse_excursion: Optional[float]
    touched_limit_up: Optional[bool] = None
    closed_limit_up: Optional[bool] = None
    second_board_quality: Optional[float] = None
    executable_at_open: Optional[bool] = None
    return_30m: Optional[float] = None
    mfe_5m: Optional[float] = None
    mfe_day: Optional[float] = None
    mae_5m: Optional[float] = None
    mae_day: Optional[float] = None
    strategy_return: Optional[float] = None
    d2_open_gap: Optional[float] = None
    fast_failure: Optional[bool] = None
    outcome_class: Optional[OutcomeClass] = None
    leadership_retained_close: Optional[bool] = None
    high_board_blowup: Optional[bool] = None
    next_day_nuclear_open: Optional[bool] = None
    weakness_resolved_at_925: Optional[bool] = None
    weakness_resolved_at_931: Optional[bool] = None
    identity_recovered: Optional[bool] = None
    leadership_transfer_success: Optional[bool] = None
    repair_retention_30m: Optional[float] = None


@dataclass(frozen=True)
class DataQualityReport:
    state: DataQualityState
    missing_fields: Sequence[str]
    reasons: Sequence[str]
    completeness: float
