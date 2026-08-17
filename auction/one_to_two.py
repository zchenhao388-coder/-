from dataclasses import dataclass
from typing import Mapping, Optional

from auction.features import FeatureSnapshot
from auction.phase import auction_phase_at
from config.thresholds import ThresholdRegistry
from domain.enums import AuctionPhase, AuthenticityState, BenchmarkStatus, CandidateGrade, SetupType, ValidationState
from domain.models import SetupResult
from market.asof import AsOfMarketView


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class OneToTwoInput:
    ticker: str
    market_view: AsOfMarketView
    feature_snapshot: FeatureSnapshot
    candidate_validation: ValidationState
    context_strength: Optional[float]


class OneToTwoEngine:
    WEIGHT_KEYS = {
        "expectation": "weight_expectation",
        "authenticity": "weight_authenticity",
        "relative": "weight_relative_strength",
        "liquidity": "weight_liquidity",
        "context": "weight_context",
    }

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds
        self.thresholds.require(self.WEIGHT_KEYS.values())
        total = sum(self.thresholds.get(key) for key in self.WEIGHT_KEYS.values())
        if abs(total - 100.0) > 1e-9:
            raise ValueError(f"one-to-two AQS weights must sum to 100, got {total}")

    def evaluate(self, request: OneToTwoInput) -> SetupResult:
        snapshot = request.feature_snapshot
        if snapshot.ticker != request.ticker or snapshot.trade_date != request.market_view.trade_date:
            raise ValueError("feature snapshot does not match ticker/trade_date")
        request.market_view.assert_timestamp_visible(snapshot.computed_as_of)
        features = snapshot.values
        expectation = self._expectation_score(features)
        authenticity = self._authenticity_score(snapshot.authenticity_state)
        relative = self._mean_percentiles(features, "ThemePeerRank", "GlobalPeerRank", "HeightPeerRank")
        liquidity = self._mean_percentiles(
            features,
            "HistoricalAmountPercentile",
            "HistoricalVolumePercentile",
            "PeerAmountPercentile",
            "PeerVolumePercentile",
        )
        context = None if request.context_strength is None else _clamp(request.context_strength * 100.0)
        components = {
            "expectation": expectation,
            "authenticity": authenticity,
            "relative": relative,
            "liquidity": liquidity,
            "context": context,
        }
        effective_weights = self._effective_weights(components)
        aqs = sum(
            components[name] * effective_weights[name] / 100.0  # type: ignore[operator]
            for name in effective_weights
        )
        validation_score = {
            ValidationState.VALID: 100.0,
            ValidationState.PARTIAL: 50.0,
            ValidationState.UNVALIDATED: 25.0,
            ValidationState.FALSIFIED: 0.0,
            ValidationState.INVALID: 0.0,
            ValidationState.HARD_INVALID: 0.0,
        }[request.candidate_validation]
        hvs = None if authenticity is None or context is None else min(authenticity, context, validation_score)
        grade = self._recommend_grade(
            aqs,
            hvs,
            snapshot.authenticity_state,
            request.candidate_validation,
            auction_phase_at(request.market_view.as_of),
            snapshot.benchmark_status,
            snapshot.surprise_status,
        )
        return SetupResult(
            request.ticker,
            SetupType.ONE_TO_TWO,
            expectation,
            authenticity,
            relative,
            liquidity,
            context,
            hvs,
            aqs,
            request.candidate_validation,
            snapshot.authenticity_state,
            tuple(flag.value for flag in snapshot.fake_strong_flags),
            grade,
            effective_weights,
            snapshot.computed_as_of,
            snapshot.benchmark_status,
            snapshot.surprise_status,
        )

    @staticmethod
    def _mean_percentiles(features: Mapping[str, Optional[float]], *names: str) -> Optional[float]:
        available = [features.get(name) for name in names if features.get(name) is not None]
        return _clamp(sum(available) / len(available) * 100.0) if available else None  # type: ignore[arg-type]

    def _effective_weights(self, components: Mapping[str, Optional[float]]) -> Mapping[str, float]:
        available = {name: self.thresholds.get(self.WEIGHT_KEYS[name]) for name, value in components.items() if value is not None}
        if not available:
            raise ValueError("cannot calculate AQS without any available component")
        total = sum(available.values())
        return {name: weight / total * 100.0 for name, weight in available.items()}

    @staticmethod
    def _expectation_score(features: Mapping[str, Optional[float]]) -> Optional[float]:
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

    def _recommend_grade(
        self,
        aqs: float,
        hvs: Optional[float],
        authenticity: AuthenticityState,
        validation: ValidationState,
        phase: AuctionPhase,
        benchmark_status: BenchmarkStatus,
        surprise_status: BenchmarkStatus,
    ) -> CandidateGrade:
        if validation in (ValidationState.FALSIFIED, ValidationState.INVALID, ValidationState.HARD_INVALID):
            return CandidateGrade.DROP
        if phase not in (AuctionPhase.FINAL, AuctionPhase.OPEN_EXECUTION):
            return CandidateGrade.B_CONFIRMATION
        if benchmark_status != BenchmarkStatus.SUFFICIENT or surprise_status != BenchmarkStatus.SUFFICIENT:
            return CandidateGrade.B_HIGH_QUALITY if aqs >= self.thresholds.get("aqs_b_min") else CandidateGrade.B_CONFIRMATION
        a_allowed = authenticity in (AuthenticityState.AUTHENTIC, AuthenticityState.HEALTHY_DISAGREEMENT)
        if a_allowed and hvs is not None and hvs >= self.thresholds.get("hvs_a_min"):
            if aqs >= self.thresholds.get("aqs_a1_min"):
                return CandidateGrade.A1
            if aqs >= self.thresholds.get("aqs_a2_min"):
                return CandidateGrade.A2
        if authenticity in (AuthenticityState.HEALTHY_DISAGREEMENT, AuthenticityState.HEALTHY_DISAGREEMENT_PENDING):
            return CandidateGrade.B_DISAGREEMENT
        if aqs >= self.thresholds.get("aqs_b_min"):
            return CandidateGrade.B_HIGH_QUALITY
        return CandidateGrade.B_CONFIRMATION
