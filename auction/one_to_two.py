from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from config.thresholds import ThresholdRegistry
from domain.enums import AuthenticityState, CandidateGrade, SetupType, ValidationState
from domain.models import SetupResult


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class OneToTwoInput:
    ticker: str
    features: Mapping[str, Optional[float]]
    authenticity_state: AuthenticityState
    flags: Sequence[str]
    candidate_validation: ValidationState
    context_strength: float


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
        expectation = self._expectation_score(request.features)
        authenticity = self._authenticity_score(request.authenticity_state)
        relative = self._mean_percentiles(request.features, "ThemePeerRank", "GlobalPeerRank", "HeightPeerRank")
        liquidity = self._mean_percentiles(request.features, "HistoricalAmountPercentile", "HistoricalVolumePercentile", "PeerAmountPercentile", "PeerVolumePercentile")
        context = _clamp(request.context_strength * 100.0)
        components = {"expectation": expectation, "authenticity": authenticity, "relative": relative, "liquidity": liquidity, "context": context}
        aqs = sum(components[name] * self.thresholds.get(self.WEIGHT_KEYS[name]) / 100.0 for name in components)
        validation_score = {
            ValidationState.VALID: 100.0,
            ValidationState.PARTIAL: 50.0,
            ValidationState.UNVALIDATED: 25.0,
            ValidationState.INVALID: 0.0,
            ValidationState.HARD_INVALID: 0.0,
        }[request.candidate_validation]
        hvs = min(authenticity, context, validation_score)
        grade = self._recommend_grade(aqs, hvs, request.authenticity_state, request.candidate_validation)
        return SetupResult(request.ticker, SetupType.ONE_TO_TWO, expectation, authenticity, relative, liquidity, context, hvs, aqs, request.candidate_validation, request.authenticity_state, tuple(request.flags), grade)

    @staticmethod
    def _mean_percentiles(features: Mapping[str, Optional[float]], *names: str) -> float:
        available = [features.get(name) for name in names if features.get(name) is not None]
        return _clamp(sum(available) / len(available) * 100.0) if available else 0.0

    def _expectation_score(self, features) -> float:
        surprise = features.get("AuctionSurprisePercentile")
        if surprise is not None:
            return _clamp(surprise * 100.0)
        normalized = features.get("NormalizedEG")
        return 50.0 if normalized is None else _clamp(50.0 + normalized * 25.0)

    def _authenticity_score(self, state) -> float:
        keys = {
            AuthenticityState.AUTHENTIC: "score_authentic",
            AuthenticityState.HEALTHY_DISAGREEMENT: "score_healthy_disagreement",
            AuthenticityState.SUSPICIOUS: "score_suspicious",
            AuthenticityState.FAKE_STRONG: "score_fake_strong",
            AuthenticityState.UNKNOWN: "score_suspicious",
        }
        return self.thresholds.get(keys[state])

    def _recommend_grade(self, aqs, hvs, authenticity, validation) -> CandidateGrade:
        if validation in (ValidationState.INVALID, ValidationState.HARD_INVALID):
            return CandidateGrade.DROP
        a_allowed = authenticity in (AuthenticityState.AUTHENTIC, AuthenticityState.HEALTHY_DISAGREEMENT)
        if a_allowed and hvs >= self.thresholds.get("hvs_a_min"):
            if aqs >= self.thresholds.get("aqs_a1_min"):
                return CandidateGrade.A1
            if aqs >= self.thresholds.get("aqs_a2_min"):
                return CandidateGrade.A2
        if authenticity == AuthenticityState.HEALTHY_DISAGREEMENT:
            return CandidateGrade.B_DISAGREEMENT
        if aqs >= self.thresholds.get("aqs_b_min"):
            return CandidateGrade.B_HIGH_QUALITY
        return CandidateGrade.B_CONFIRMATION
