from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from auction.features import FeatureSnapshot
from auction.phase import auction_phase_at
from config.thresholds import ThresholdRegistry
from domain.enums import (
    AuctionPhase,
    AuthenticityState,
    BenchmarkStatus,
    CandidateGrade,
    FakeStrongFlag,
    IdentityRecoveryState,
    SetupType,
    ValidationState,
    WeaknessResolution,
)
from domain.models import SetupResult, WeaknessState
from domain.reason_codes import ReasonCode
from market.asof import AsOfMarketView


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class WeakToStrongInput:
    ticker: str
    market_view: AsOfMarketView
    feature_snapshot: FeatureSnapshot
    candidate_validation: ValidationState
    weakness_state: WeaknessState
    previous_theme_rank: Optional[float]
    theme_repair_confirmed: bool
    leadership_transfer_confirmed: bool
    context_strength: Optional[float]
    direct_competitor_group: str = "direct_competitors"


class WeakToStrongEngine:
    """Milestone 7 weak-to-strong hypothesis validation over AsOfMarketView."""

    WEIGHT_KEYS = {
        "repair_surprise": "weight_repair_surprise",
        "authenticity": "weight_authenticity",
        "identity_relative": "weight_identity_relative",
        "liquidity": "weight_liquidity",
        "context": "weight_context",
    }

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds
        self.thresholds.require(self.WEIGHT_KEYS.values())
        total = sum(self.thresholds.get(key) for key in self.WEIGHT_KEYS.values())
        if abs(total - 100.0) > 1e-9:
            raise ValueError(f"weak-to-strong AQS weights must sum to 100, got {total}")

    def evaluate(self, request: WeakToStrongInput) -> SetupResult:
        snapshot = request.feature_snapshot
        if snapshot.ticker != request.ticker or snapshot.trade_date != request.market_view.trade_date:
            raise ValueError("feature snapshot does not match ticker/trade_date")
        request.market_view.assert_timestamp_visible(snapshot.computed_as_of)
        if request.weakness_state.weakness_severity < 0:
            raise ValueError("weakness_severity cannot be negative")

        features = snapshot.values
        repair_score = self._repair_surprise_score(features)
        authenticity_score = self._authenticity_score(snapshot.authenticity_state)
        liquidity_score = self._mean_percentiles(
            features,
            "HistoricalAmountPercentile",
            "HistoricalVolumePercentile",
            "PeerAmountPercentile",
            "PeerVolumePercentile",
        )
        identity_state, identity_score = self._identity_recovery(request)
        weakness_resolved = self._weakness_resolution(request, repair_score)
        context_score = None if request.context_strength is None else _clamp(request.context_strength * 100.0)
        components = {
            "repair_surprise": repair_score,
            "authenticity": authenticity_score,
            "identity_relative": identity_score,
            "liquidity": liquidity_score,
            "context": context_score,
        }
        effective_weights = self._effective_weights(components)
        aqs = sum(components[name] * weight / 100.0 for name, weight in effective_weights.items())  # type: ignore[operator]
        hvs = self._hvs(
            weakness_resolved,
            identity_state,
            snapshot.authenticity_state,
            request.candidate_validation,
            context_score,
        )
        flags = tuple(snapshot.fake_strong_flags) + self._repair_flags(
            request,
            repair_score,
            identity_state,
            liquidity_score,
        )
        grade = self._recommend_grade(request, weakness_resolved, identity_state, flags, aqs, hvs)
        reasons = self._reason_codes(weakness_resolved, identity_state, flags)
        return SetupResult(
            ticker=request.ticker,
            setup_type=SetupType.WEAK_TO_STRONG,
            expectation_score=repair_score,
            authenticity_score=authenticity_score,
            relative_strength_score=identity_score,
            liquidity_score=liquidity_score,
            context_score=context_score,
            hvs=hvs,
            aqs=aqs,
            validation_state=request.candidate_validation,
            authenticity_state=snapshot.authenticity_state,
            flags=tuple(flag.value for flag in flags),
            grade_recommendation=grade,
            effective_weights=effective_weights,
            computed_as_of=snapshot.computed_as_of,
            benchmark_status=snapshot.benchmark_status,
            surprise_status=snapshot.surprise_status,
            reason_codes=tuple(reason.value for reason in reasons),
            weakness_resolved=weakness_resolved,
            identity_recovery=identity_state,
        )

    def _weakness_resolution(
        self,
        request: WeakToStrongInput,
        repair_score: Optional[float],
    ) -> WeaknessResolution:
        if not request.weakness_state.repairable:
            return WeaknessResolution.FALSE
        features = request.feature_snapshot.values
        surprise_confirmed = repair_score is not None and repair_score >= self.thresholds.get("repair_surprise_percentile_min") * 100.0
        post20 = features.get("Post20Delta")
        trajectory_confirmed = post20 is not None and post20 >= self.thresholds.get("repair_post20_delta_min")
        authenticity_confirmed = request.feature_snapshot.authenticity_state in (
            AuthenticityState.AUTHENTIC,
            AuthenticityState.HEALTHY_DISAGREEMENT,
        )
        funding_confirmed = features.get("FundingConfirmed") == 1.0
        if surprise_confirmed and trajectory_confirmed and authenticity_confirmed and funding_confirmed:
            return WeaknessResolution.TRUE
        if surprise_confirmed and (trajectory_confirmed or authenticity_confirmed or funding_confirmed):
            return WeaknessResolution.PARTIAL
        return WeaknessResolution.FALSE

    def _identity_recovery(self, request: WeakToStrongInput) -> Tuple[IdentityRecoveryState, Optional[float]]:
        features = request.feature_snapshot.values
        theme_rank = features.get("ThemePeerRank")
        actual_gap = features.get("ActualAuctionGap")
        competitor_values = request.market_view.get_peer_latest_values(request.direct_competitor_group, "gap_pct")
        direct_rank = self._percentile_rank(actual_gap, competitor_values)
        improved = (
            theme_rank is not None
            and request.previous_theme_rank is not None
            and theme_rank - request.previous_theme_rank >= self.thresholds.get("identity_rank_improvement_min")
        )
        theme_confirmed = theme_rank is not None and theme_rank >= self.thresholds.get("identity_theme_rank_min")
        direct_confirmed = direct_rank is not None and direct_rank >= self.thresholds.get("identity_direct_rank_min")
        evidence = (theme_confirmed, direct_confirmed, improved, request.leadership_transfer_confirmed)
        if theme_confirmed and sum(evidence) >= 2:
            state = IdentityRecoveryState.TRUE
        elif any(evidence):
            state = IdentityRecoveryState.PARTIAL
        else:
            state = IdentityRecoveryState.FALSE
        score = self.thresholds.get({
            IdentityRecoveryState.TRUE: "score_identity_recovered",
            IdentityRecoveryState.PARTIAL: "score_identity_partial",
            IdentityRecoveryState.FALSE: "score_identity_false",
        }[state])
        return state, score

    def _repair_flags(
        self,
        request: WeakToStrongInput,
        repair_score: Optional[float],
        identity_state: IdentityRecoveryState,
        liquidity_score: Optional[float],
    ) -> Tuple[FakeStrongFlag, ...]:
        features = request.feature_snapshot.values
        actual_gap = features.get("ActualAuctionGap")
        post20 = features.get("Post20Delta")
        repair_signal = repair_score is not None and repair_score >= self.thresholds.get("repair_surprise_percentile_min") * 100.0
        flags = []
        if (
            repair_signal
            and actual_gap is not None
            and actual_gap >= self.thresholds.get("open_only_gap_min")
            and (post20 is None or post20 <= self.thresholds.get("repair_post20_delta_min") or features.get("FundingConfirmed") != 1.0)
        ):
            flags.append(FakeStrongFlag.OPEN_ONLY_REPAIR)
        if repair_signal and not request.theme_repair_confirmed:
            flags.append(FakeStrongFlag.ISOLATED_REPAIR)
        if identity_state == IdentityRecoveryState.FALSE:
            flags.append(FakeStrongFlag.NO_IDENTITY_RECOVERY)
        if (
            repair_signal
            and liquidity_score is not None
            and liquidity_score < self.thresholds.get("liquidity_confirmation_score_min")
        ):
            flags.append(FakeStrongFlag.LIQUIDITY_FAKE_REPAIR)
        if post20 is not None and post20 <= self.thresholds.get("repair_fade_delta_max"):
            flags.append(FakeStrongFlag.REPAIR_AND_FADE)
        return tuple(flags)

    def _recommend_grade(
        self,
        request: WeakToStrongInput,
        resolved: WeaknessResolution,
        identity: IdentityRecoveryState,
        flags: Sequence[FakeStrongFlag],
        aqs: float,
        hvs: Optional[float],
    ) -> CandidateGrade:
        if request.candidate_validation in (ValidationState.FALSIFIED, ValidationState.INVALID, ValidationState.HARD_INVALID):
            return CandidateGrade.DROP
        if not request.weakness_state.repairable:
            return CandidateGrade.DROP
        if auction_phase_at(request.market_view.as_of) not in (AuctionPhase.FINAL, AuctionPhase.OPEN_EXECUTION):
            return CandidateGrade.B_CONFIRMATION
        snapshot = request.feature_snapshot
        if snapshot.benchmark_status != BenchmarkStatus.SUFFICIENT or snapshot.surprise_status != BenchmarkStatus.SUFFICIENT:
            return CandidateGrade.B_CONFIRMATION
        if resolved == WeaknessResolution.FALSE:
            return CandidateGrade.C_NIGHT_DEGRADED
        severe_flags = {
            FakeStrongFlag.POST20_CONTINUOUS_DECAY,
            FakeStrongFlag.PRICE_WITHOUT_LIQUIDITY,
            FakeStrongFlag.LAST_SECOND_SPIKE,
            FakeStrongFlag.OPEN_ONLY_REPAIR,
            FakeStrongFlag.ISOLATED_REPAIR,
            FakeStrongFlag.NO_IDENTITY_RECOVERY,
            FakeStrongFlag.LIQUIDITY_FAKE_REPAIR,
            FakeStrongFlag.REPAIR_AND_FADE,
        }
        if severe_flags.intersection(flags):
            return CandidateGrade.B_CONFIRMATION
        if resolved == WeaknessResolution.PARTIAL or identity != IdentityRecoveryState.TRUE:
            return CandidateGrade.B_CONFIRMATION
        setup_requirements = (
            request.candidate_validation == ValidationState.VALID
            and resolved == WeaknessResolution.TRUE
            and identity == IdentityRecoveryState.TRUE
            and request.theme_repair_confirmed
            and request.feature_snapshot.authenticity_state in (
                AuthenticityState.AUTHENTIC,
                AuthenticityState.HEALTHY_DISAGREEMENT,
            )
        )
        if setup_requirements and hvs is not None and hvs >= self.thresholds.get("hvs_a_min"):
            if aqs >= self.thresholds.get("aqs_a1_min"):
                return CandidateGrade.A1
            if aqs >= self.thresholds.get("aqs_a2_min"):
                return CandidateGrade.A2
        if request.feature_snapshot.authenticity_state == AuthenticityState.HEALTHY_DISAGREEMENT:
            return CandidateGrade.B_DISAGREEMENT
        return CandidateGrade.B_HIGH_QUALITY if aqs >= self.thresholds.get("aqs_b_min") else CandidateGrade.B_CONFIRMATION

    def _hvs(
        self,
        resolved: WeaknessResolution,
        identity: IdentityRecoveryState,
        authenticity: AuthenticityState,
        validation: ValidationState,
        context_score: Optional[float],
    ) -> Optional[float]:
        if context_score is None:
            return None
        resolved_score = self.thresholds.get({
            WeaknessResolution.TRUE: "score_weakness_resolved",
            WeaknessResolution.PARTIAL: "score_weakness_partial",
            WeaknessResolution.FALSE: "score_weakness_false",
        }[resolved])
        identity_score = self.thresholds.get({
            IdentityRecoveryState.TRUE: "score_identity_recovered",
            IdentityRecoveryState.PARTIAL: "score_identity_partial",
            IdentityRecoveryState.FALSE: "score_identity_false",
        }[identity])
        authenticity_score = self._authenticity_score(authenticity)
        validation_score = {
            ValidationState.VALID: 100.0,
            ValidationState.PARTIAL: 50.0,
            ValidationState.UNVALIDATED: 25.0,
            ValidationState.FALSIFIED: 0.0,
            ValidationState.INVALID: 0.0,
            ValidationState.HARD_INVALID: 0.0,
        }[validation]
        if authenticity_score is None:
            return None
        return min(resolved_score, identity_score, authenticity_score, validation_score, context_score)

    def _repair_surprise_score(self, features: Mapping[str, Optional[float]]) -> Optional[float]:
        surprise = features.get("AuctionSurprisePercentile")
        if surprise is not None:
            return _clamp(surprise * 100.0)
        normalized = features.get("NormalizedEG")
        return None if normalized is None else _clamp(50.0 + normalized * 25.0)

    def _authenticity_score(self, state: AuthenticityState) -> Optional[float]:
        keys = {
            AuthenticityState.AUTHENTIC: "score_authentic",
            AuthenticityState.HEALTHY_DISAGREEMENT: "score_healthy_disagreement",
            AuthenticityState.SUSPICIOUS: "score_suspicious",
            AuthenticityState.FAKE_STRONG: "score_fake_strong",
        }
        return None if state not in keys else self.thresholds.get(keys[state])

    @staticmethod
    def _percentile_rank(value: Optional[float], population: Sequence[float]) -> Optional[float]:
        if value is None or not population:
            return None
        return sum(1 for item in population if item <= value) / len(population)

    @staticmethod
    def _mean_percentiles(features: Mapping[str, Optional[float]], *names: str) -> Optional[float]:
        available = [features.get(name) for name in names if features.get(name) is not None]
        return _clamp(sum(available) / len(available) * 100.0) if available else None  # type: ignore[arg-type]

    def _effective_weights(self, components: Mapping[str, Optional[float]]) -> Mapping[str, float]:
        available = {name: self.thresholds.get(self.WEIGHT_KEYS[name]) for name, value in components.items() if value is not None}
        if not available:
            raise ValueError("cannot calculate weak-to-strong AQS without available components")
        total = sum(available.values())
        return {name: weight / total * 100.0 for name, weight in available.items()}

    @staticmethod
    def _reason_codes(
        resolved: WeaknessResolution,
        identity: IdentityRecoveryState,
        flags: Sequence[FakeStrongFlag],
    ) -> Tuple[ReasonCode, ...]:
        reasons = [ReasonCode.WEAKNESS_RESOLVED if resolved == WeaknessResolution.TRUE else ReasonCode.WEAKNESS_UNRESOLVED]
        reasons.append(ReasonCode.IDENTITY_RECOVERED if identity == IdentityRecoveryState.TRUE else ReasonCode.IDENTITY_RECOVERY_FAILED)
        mapping = {
            FakeStrongFlag.OPEN_ONLY_REPAIR: ReasonCode.OPEN_ONLY_REPAIR,
            FakeStrongFlag.ISOLATED_REPAIR: ReasonCode.ISOLATED_REPAIR,
            FakeStrongFlag.LIQUIDITY_FAKE_REPAIR: ReasonCode.LIQUIDITY_FAKE_REPAIR,
            FakeStrongFlag.REPAIR_AND_FADE: ReasonCode.REPAIR_AND_FADE,
        }
        reasons.extend(mapping[flag] for flag in flags if flag in mapping)
        if FakeStrongFlag.REPAIR_AND_FADE in flags:
            reasons.append(ReasonCode.WEAKNESS_REAPPEARS)
        return tuple(dict.fromkeys(reasons))
