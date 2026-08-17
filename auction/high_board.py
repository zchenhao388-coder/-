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
    HighBoardEcosystem,
    LeadershipState,
    SetupType,
    ValidationState,
)
from domain.models import SetupResult
from domain.reason_codes import ReasonCode
from market.asof import AsOfMarketView


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class HighBoardPeerGroups:
    high_board: str = "high_board"
    next_height: str = "next_height"
    middle_board: str = "middle_board"
    broken_high_board: str = "broken_high_board"


@dataclass(frozen=True)
class HighBoardEcosystemResult:
    state: HighBoardEcosystem
    observation_count: int
    positive_fraction: Optional[float]
    negative_fraction: Optional[float]
    panic_fraction: Optional[float]


@dataclass(frozen=True)
class HighBoardInput:
    ticker: str
    market_view: AsOfMarketView
    feature_snapshot: FeatureSnapshot
    candidate_validation: ValidationState
    board_height: int
    is_highest_board: bool
    leader_identity_strength: Optional[float]
    regulatory_headroom: Optional[float]
    theme_leadership_retained: bool
    board_form: str
    peer_groups: HighBoardPeerGroups = HighBoardPeerGroups()


class HighBoardEcosystemEngine:
    """Point-in-time high-board ecosystem classifier over canonical peer groups."""

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds

    def evaluate(self, market_view: AsOfMarketView, groups: HighBoardPeerGroups) -> HighBoardEcosystemResult:
        active = self._values(market_view, groups.high_board, groups.next_height, groups.middle_board)
        broken = self._values(market_view, groups.broken_high_board)
        all_values = active + broken
        if not all_values:
            return HighBoardEcosystemResult(HighBoardEcosystem.NEUTRAL, 0, None, None, None)

        positive = self._fraction(active, lambda value: value >= self.thresholds.get("ecosystem_positive_gap_pct"))
        negative = self._fraction(all_values, lambda value: value <= self.thresholds.get("ecosystem_negative_gap_pct"))
        panic_population = broken + self._values(market_view, groups.high_board, groups.next_height)
        panic = self._fraction(panic_population, lambda value: value <= self.thresholds.get("ecosystem_panic_gap_pct"))

        if panic is not None and panic >= self.thresholds.get("ecosystem_panic_fraction_min"):
            state = HighBoardEcosystem.SEVERE_NEGATIVE
        elif negative is not None and negative >= self.thresholds.get("ecosystem_negative_fraction_min"):
            state = HighBoardEcosystem.NEGATIVE
        elif positive is not None and positive >= self.thresholds.get("ecosystem_positive_fraction_min"):
            state = HighBoardEcosystem.POSITIVE
        else:
            state = HighBoardEcosystem.NEUTRAL
        return HighBoardEcosystemResult(state, len(all_values), positive, negative, panic)

    @staticmethod
    def _values(market_view: AsOfMarketView, *groups: str) -> Tuple[float, ...]:
        return tuple(value for group in groups for value in market_view.get_peer_latest_values(group, "gap_pct"))

    @staticmethod
    def _fraction(values: Sequence[float], predicate) -> Optional[float]:
        return None if not values else sum(1 for value in values if predicate(value)) / len(values)


class HighBoardEngine:
    """Milestone 6 high-board setup engine; permissions remain Compiler-owned."""

    AQS_WEIGHT_KEYS = {
        "expectation_hvs": "weight_expectation_hvs",
        "authenticity": "weight_authenticity",
        "relative": "weight_relative_strength",
        "liquidity": "weight_liquidity",
        "ecosystem": "weight_ecosystem",
    }
    HVS_WEIGHT_KEYS = {
        "expectation": "hvs_weight_expectation",
        "leadership": "hvs_weight_leadership",
        "ecosystem": "hvs_weight_ecosystem",
        "theme": "hvs_weight_theme",
    }

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds
        self.thresholds.require((*self.AQS_WEIGHT_KEYS.values(), *self.HVS_WEIGHT_KEYS.values()))
        self._require_weight_total(self.AQS_WEIGHT_KEYS, "high-board AQS")
        self._require_weight_total(self.HVS_WEIGHT_KEYS, "high-board HVS")
        self.ecosystem_engine = HighBoardEcosystemEngine(thresholds)

    def evaluate(self, request: HighBoardInput) -> SetupResult:
        snapshot = request.feature_snapshot
        if snapshot.ticker != request.ticker or snapshot.trade_date != request.market_view.trade_date:
            raise ValueError("feature snapshot does not match ticker/trade_date")
        request.market_view.assert_timestamp_visible(snapshot.computed_as_of)
        if request.board_height < 2:
            raise ValueError("HIGH_BOARD requires board_height >= 2")

        features = snapshot.values
        ecosystem = self.ecosystem_engine.evaluate(request.market_view, request.peer_groups)
        leadership = self._leadership_state(request)
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
        ecosystem_score = self._ecosystem_score(ecosystem.state)
        leadership_score = self._leadership_score(leadership)
        theme_score = 100.0 if request.theme_leadership_retained else 0.0
        hvs_components = {
            "expectation": expectation,
            "leadership": leadership_score,
            "ecosystem": ecosystem_score,
            "theme": theme_score,
        }
        hvs = self._weighted_score(hvs_components, self.HVS_WEIGHT_KEYS)
        aqs_components = {
            "expectation_hvs": self._mean_available(expectation, hvs),
            "authenticity": authenticity,
            "relative": relative,
            "liquidity": liquidity,
            "ecosystem": ecosystem_score,
        }
        effective_weights = self._effective_weights(aqs_components, self.AQS_WEIGHT_KEYS)
        aqs = self._score_with_weights(aqs_components, effective_weights)
        flags = tuple(snapshot.fake_strong_flags) + self._high_board_flags(request, ecosystem, relative, liquidity)
        reason_codes = self._reason_codes(features, ecosystem.state, leadership, flags)
        grade = self._recommend_grade(request, ecosystem, leadership, flags, aqs, hvs)
        return SetupResult(
            ticker=request.ticker,
            setup_type=SetupType.HIGH_BOARD,
            expectation_score=expectation,
            authenticity_score=authenticity,
            relative_strength_score=relative,
            liquidity_score=liquidity,
            context_score=ecosystem_score,
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
            leadership_state=leadership,
            reason_codes=tuple(code.value for code in reason_codes),
            ecosystem_state=ecosystem.state.value,
        )

    def _leadership_state(self, request: HighBoardInput) -> LeadershipState:
        features = request.feature_snapshot.values
        rank = features.get("HeightPeerRank")
        post20_delta = features.get("Post20Delta")
        if not request.theme_leadership_retained:
            return LeadershipState.LOST
        if post20_delta is not None and post20_delta <= self.thresholds.get("leadership_lost_post20_delta"):
            return LeadershipState.LOST
        if rank is not None and rank <= self.thresholds.get("leadership_lost_rank_max"):
            return LeadershipState.LOST
        identity = request.leader_identity_strength
        if (
            request.is_highest_board
            and rank is not None
            and rank >= self.thresholds.get("leadership_dominant_rank_min")
            and identity is not None
            and identity >= self.thresholds.get("leader_identity_dominant_min")
        ):
            return LeadershipState.DOMINANT
        if (
            rank is not None
            and rank >= self.thresholds.get("leadership_retained_rank_min")
            and identity is not None
            and identity >= self.thresholds.get("leader_identity_retained_min")
        ):
            return LeadershipState.RETAINED
        return LeadershipState.CHALLENGED

    def _high_board_flags(
        self,
        request: HighBoardInput,
        ecosystem: HighBoardEcosystemResult,
        relative: Optional[float],
        liquidity: Optional[float],
    ) -> Tuple[FakeStrongFlag, ...]:
        features = request.feature_snapshot.values
        actual_gap = features.get("ActualAuctionGap")
        phase = (request.market_view.get_market_context().phase if request.market_view.get_market_context() else "")
        flags = []
        if (
            actual_gap is not None
            and actual_gap >= self.thresholds.get("leader_isolation_gap_min")
            and ecosystem.state in (HighBoardEcosystem.NEGATIVE, HighBoardEcosystem.SEVERE_NEGATIVE)
        ):
            flags.append(FakeStrongFlag.FAKE_LEADER_ISOLATION)
        if (
            actual_gap is not None
            and actual_gap >= self.thresholds.get("height_premium_gap_min")
            and (
                (relative is not None and relative <= self.thresholds.get("height_premium_relative_rank_max"))
                or (liquidity is not None and liquidity <= self.thresholds.get("height_premium_liquidity_max"))
            )
        ):
            flags.append(FakeStrongFlag.FAKE_HEIGHT_PREMIUM)
        phase_consensus_risk = phase.upper() in {"CLIMAX", "DIVERGENCE", "DECLINE"}
        if (
            request.board_form.upper() == "ONE_WORD"
            and actual_gap is not None
            and actual_gap >= self.thresholds.get("one_word_consensus_gap_min")
            and (phase_consensus_risk or (liquidity is not None and liquidity <= self.thresholds.get("one_word_consensus_liquidity_max")))
        ):
            flags.append(FakeStrongFlag.FAKE_ONE_WORD_CONSENSUS)
        return tuple(flags)

    def _recommend_grade(
        self,
        request: HighBoardInput,
        ecosystem: HighBoardEcosystemResult,
        leadership: LeadershipState,
        flags: Sequence[FakeStrongFlag],
        aqs: float,
        hvs: Optional[float],
    ) -> CandidateGrade:
        if request.candidate_validation in (ValidationState.FALSIFIED, ValidationState.INVALID, ValidationState.HARD_INVALID):
            return CandidateGrade.DROP
        if auction_phase_at(request.market_view.as_of) not in (AuctionPhase.FINAL, AuctionPhase.OPEN_EXECUTION):
            return CandidateGrade.B_CONFIRMATION
        snapshot = request.feature_snapshot
        if snapshot.benchmark_status != BenchmarkStatus.SUFFICIENT or snapshot.surprise_status != BenchmarkStatus.SUFFICIENT:
            return CandidateGrade.B_ECOSYSTEM_PENDING
        if leadership == LeadershipState.LOST:
            return CandidateGrade.C_LEADERSHIP_LOST
        if ecosystem.state in (HighBoardEcosystem.NEGATIVE, HighBoardEcosystem.SEVERE_NEGATIVE):
            return CandidateGrade.C_HIGHBOARD_DEGRADED
        if FakeStrongFlag.FAKE_ONE_WORD_CONSENSUS in flags:
            return CandidateGrade.B_CONSENSUS_RISK
        severe_fake = {
            FakeStrongFlag.POST20_CONTINUOUS_DECAY,
            FakeStrongFlag.PRICE_WITHOUT_LIQUIDITY,
            FakeStrongFlag.LAST_SECOND_SPIKE,
            FakeStrongFlag.FAKE_LEADER_ISOLATION,
            FakeStrongFlag.FAKE_HEIGHT_PREMIUM,
        }
        if severe_fake.intersection(flags):
            return CandidateGrade.B_CONFIRMATION
        if ecosystem.observation_count < int(self.thresholds.get("ecosystem_min_observations")):
            return CandidateGrade.B_ECOSYSTEM_PENDING
        if leadership == LeadershipState.CHALLENGED:
            return CandidateGrade.B_LEADERSHIP_CHALLENGED
        if request.feature_snapshot.authenticity_state == AuthenticityState.HEALTHY_DISAGREEMENT:
            return CandidateGrade.B_DISAGREEMENT
        setup_requirements = (
            request.candidate_validation == ValidationState.VALID
            and request.feature_snapshot.authenticity_state == AuthenticityState.AUTHENTIC
            and request.regulatory_headroom is not None
            and request.regulatory_headroom >= self.thresholds.get("regulatory_headroom_min")
            and request.theme_leadership_retained
            and leadership in (LeadershipState.DOMINANT, LeadershipState.RETAINED)
            and ecosystem.state in (HighBoardEcosystem.POSITIVE, HighBoardEcosystem.NEUTRAL)
        )
        if setup_requirements and hvs is not None and hvs >= self.thresholds.get("hvs_a_min"):
            if aqs >= self.thresholds.get("aqs_a1_min"):
                return CandidateGrade.A1
            if aqs >= self.thresholds.get("aqs_a2_min"):
                return CandidateGrade.A2
        return CandidateGrade.B_HIGH_QUALITY if aqs >= self.thresholds.get("aqs_b_min") else CandidateGrade.B_CONFIRMATION

    def _reason_codes(
        self,
        features: Mapping[str, Optional[float]],
        ecosystem: HighBoardEcosystem,
        leadership: LeadershipState,
        flags: Sequence[FakeStrongFlag],
    ) -> Tuple[ReasonCode, ...]:
        reasons = []
        if features.get("NormalizedEG") is not None and features["NormalizedEG"] > 0:  # type: ignore[operator]
            reasons.append(ReasonCode.POSITIVE_SURPRISE)
        if ecosystem == HighBoardEcosystem.POSITIVE:
            reasons.append(ReasonCode.ECOSYSTEM_POSITIVE)
        elif ecosystem in (HighBoardEcosystem.NEGATIVE, HighBoardEcosystem.SEVERE_NEGATIVE):
            reasons.append(ReasonCode.ECOSYSTEM_NEGATIVE)
        if leadership in (LeadershipState.DOMINANT, LeadershipState.RETAINED):
            reasons.append(ReasonCode.LEADERSHIP_RETAINED)
        elif leadership == LeadershipState.CHALLENGED:
            reasons.append(ReasonCode.LEADERSHIP_CHALLENGED)
        elif leadership == LeadershipState.LOST:
            reasons.append(ReasonCode.LEADERSHIP_LOST)
        mapping = {
            FakeStrongFlag.FAKE_LEADER_ISOLATION: ReasonCode.FAKE_LEADER_ISOLATION,
            FakeStrongFlag.FAKE_HEIGHT_PREMIUM: ReasonCode.FAKE_HEIGHT_PREMIUM,
            FakeStrongFlag.FAKE_ONE_WORD_CONSENSUS: ReasonCode.FAKE_ONE_WORD_CONSENSUS,
        }
        reasons.extend(mapping[flag] for flag in flags if flag in mapping)
        if FakeStrongFlag.FAKE_ONE_WORD_CONSENSUS in flags:
            reasons.append(ReasonCode.FINAL_CONSENSUS_RISK)
        if ecosystem == HighBoardEcosystem.SEVERE_NEGATIVE:
            reasons.append(ReasonCode.HIGH_LEVEL_PANIC_EXPANSION)
        return tuple(dict.fromkeys(reasons))

    def _expectation_score(self, features: Mapping[str, Optional[float]]) -> Optional[float]:
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
    def _mean_percentiles(features: Mapping[str, Optional[float]], *names: str) -> Optional[float]:
        available = [features.get(name) for name in names if features.get(name) is not None]
        return _clamp(sum(available) / len(available) * 100.0) if available else None  # type: ignore[arg-type]

    @staticmethod
    def _mean_available(*values: Optional[float]) -> Optional[float]:
        available = [value for value in values if value is not None]
        return sum(available) / len(available) if available else None

    def _ecosystem_score(self, state: HighBoardEcosystem) -> float:
        return self.thresholds.get({
            HighBoardEcosystem.POSITIVE: "ecosystem_score_positive",
            HighBoardEcosystem.NEUTRAL: "ecosystem_score_neutral",
            HighBoardEcosystem.NEGATIVE: "ecosystem_score_negative",
            HighBoardEcosystem.SEVERE_NEGATIVE: "ecosystem_score_severe_negative",
        }[state])

    def _leadership_score(self, state: LeadershipState) -> float:
        return self.thresholds.get({
            LeadershipState.DOMINANT: "leadership_score_dominant",
            LeadershipState.RETAINED: "leadership_score_retained",
            LeadershipState.CHALLENGED: "leadership_score_challenged",
            LeadershipState.LOST: "leadership_score_lost",
        }[state])

    def _weighted_score(self, components, keys) -> Optional[float]:
        weights = self._effective_weights(components, keys)
        return None if not weights else self._score_with_weights(components, weights)

    def _effective_weights(self, components, keys) -> Mapping[str, float]:
        available = {name: self.thresholds.get(keys[name]) for name, value in components.items() if value is not None}
        if not available:
            return {}
        total = sum(available.values())
        return {name: weight / total * 100.0 for name, weight in available.items()}

    @staticmethod
    def _score_with_weights(components, weights) -> float:
        return sum(components[name] * weight / 100.0 for name, weight in weights.items())

    def _require_weight_total(self, keys: Mapping[str, str], label: str) -> None:
        total = sum(self.thresholds.get(key) for key in keys.values())
        if abs(total - 100.0) > 1e-9:
            raise ValueError(f"{label} weights must sum to 100, got {total}")
