from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Mapping, Optional, Sequence

from .enums import (
    AuthenticityState,
    CandidateGrade,
    DataQualityState,
    ExecutionState,
    LeadershipState,
    PermissionState,
    SetupType,
    ValidationState,
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
    source: str = "UNKNOWN"
    raw: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if not self.ticker:
            raise ValueError("ticker is required")
        for name in ("matched_volume", "matched_amount"):
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
    expectation_score: float
    authenticity_score: float
    relative_strength_score: float
    liquidity_score: float
    context_score: float
    hvs: float
    aqs: float
    validation_state: ValidationState
    authenticity_state: AuthenticityState
    flags: Sequence[str]
    grade_recommendation: CandidateGrade


@dataclass(frozen=True)
class OpenExecutionPlan:
    ticker: str
    state: ExecutionState
    valid_from: datetime
    expires_at: datetime
    triggers: Sequence[str]
    hard_cancels: Sequence[str]


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


@dataclass(frozen=True)
class DataQualityReport:
    state: DataQualityState
    missing_fields: Sequence[str]
    reasons: Sequence[str]
    completeness: float
