from dataclasses import dataclass
from datetime import date, datetime
from math import exp
from typing import Any, Mapping, Sequence, Tuple

from config.thresholds import ThresholdRegistry
from domain.enums import BenchmarkStatus
from market.asof import AsOfMarketView


@dataclass(frozen=True)
class BenchmarkKey:
    setup_type: str
    regime: str
    theme_type: str
    leadership: str
    board_height_bucket: str
    board_form: str

    def hierarchy(self) -> Sequence[Tuple[str, ...]]:
        full = (
            self.setup_type,
            self.regime,
            self.theme_type,
            self.leadership,
            self.board_height_bucket,
            self.board_form,
        )
        return (full, full[:-1], full[:-2], full[:3], full[:2], full[:1], ())

    def feature_mapping(self) -> Mapping[str, str]:
        return {
            "setup_type": self.setup_type,
            "regime": self.regime,
            "theme_type": self.theme_type,
            "leadership": self.leadership,
            "board_height_bucket": self.board_height_bucket,
            "board_form": self.board_form,
        }


@dataclass(frozen=True)
class FeaturesAsOf:
    values: Mapping[str, Any]
    as_of: datetime
    source: str


@dataclass(frozen=True)
class BenchmarkObservation:
    observation_id: str
    trade_date: str
    ticker: str
    features_as_of: FeaturesAsOf
    information_available_at: datetime
    key: BenchmarkKey
    benchmark_key_source: str
    actual_next_auction_gap: float
    auction_amount: float
    auction_volume: float
    label_available_at: datetime
    source_version: str

    @property
    def available_at(self) -> datetime:
        return self.label_available_at

    @property
    def next_gap_pct(self) -> float:
        return self.actual_next_auction_gap


@dataclass(frozen=True)
class ExpectedAuctionDistribution:
    expected_gap_q10: float
    expected_gap_q25: float
    expected_gap_q50: float
    expected_gap_q75: float
    expected_gap_q90: float
    liquidity_amount_quantiles: Mapping[str, float]
    liquidity_volume_quantiles: Mapping[str, float]
    sample_size: int
    peer_similarity: float
    confidence: float
    parent_sample_size: int
    shrinkage_weight: float
    effective_sample_size: int
    minimum_sample_size: int
    status: BenchmarkStatus


@dataclass(frozen=True)
class PointInTimeExpectedAuction:
    ticker: str
    trade_date: str
    generated_at: datetime
    information_available_at: datetime
    benchmark_key: BenchmarkKey
    source_version: str
    distribution: ExpectedAuctionDistribution


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError("quantile requires data")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


class HierarchicalConditionalBenchmark:
    """Point-in-time hierarchical benchmark with minimum-sample fallback."""

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds

    def estimate(
        self,
        market_view: AsOfMarketView,
        key: BenchmarkKey,
        current_trade_date: str,
        information_available_at: datetime,
    ) -> ExpectedAuctionDistribution:
        market_view.assert_timestamp_visible(information_available_at)
        eligible = tuple(
            observation for observation in market_view.get_expectation_observations(information_available_at)
            if isinstance(observation, BenchmarkObservation)
            and self._provenance_valid(observation)
            and date.fromisoformat(observation.trade_date) < date.fromisoformat(current_trade_date)
            and observation.label_available_at <= information_available_at
        )
        if not eligible:
            raise ValueError("no point-in-time benchmark observations")

        levels = key.hierarchy()
        minimum = int(self.thresholds.get("minimum_local_sample_size"))
        matches_by_level = tuple(self._matches(level, eligible) for level in levels)
        selected_index = next(
            (index for index, matches in enumerate(matches_by_level) if len(matches) >= minimum),
            None,
        )
        insufficient = selected_index is None
        if selected_index is None:
            selected_index = max(
                (index for index, matches in enumerate(matches_by_level) if matches),
                key=lambda index: (len(matches_by_level[index]), index),
            )
        local = matches_by_level[selected_index]
        parent = local
        for broader in matches_by_level[selected_index + 1:]:
            if broader:
                parent = broader
                break

        prior_strength = self.thresholds.get("prior_strength")
        weight = len(local) / (len(local) + prior_strength)
        gap_q, amount_q, volume_q = {}, {}, {}
        for label, quantile in (("q10", .10), ("q25", .25), ("q50", .50), ("q75", .75), ("q90", .90)):
            gap_q[label] = self._shrink_quantile(local, parent, "actual_next_auction_gap", quantile, weight)
            amount_q[label] = self._shrink_quantile(local, parent, "auction_amount", quantile, weight)
            volume_q[label] = self._shrink_quantile(local, parent, "auction_volume", quantile, weight)

        specificity = 1.0 - selected_index / max(1, len(levels) - 1)
        sample_confidence = 1.0 - exp(-len(local) / prior_strength)
        insufficient_penalty = self.thresholds.get("insufficient_sample_confidence_multiplier") if insufficient else 1.0
        confidence = sample_confidence * (0.5 + 0.5 * specificity) * insufficient_penalty
        return ExpectedAuctionDistribution(
            gap_q["q10"],
            gap_q["q25"],
            gap_q["q50"],
            gap_q["q75"],
            gap_q["q90"],
            amount_q,
            volume_q,
            len(local),
            max(0.0, specificity),
            confidence,
            len(parent),
            weight,
            len(local),
            minimum,
            BenchmarkStatus.INSUFFICIENT if insufficient else BenchmarkStatus.SUFFICIENT,
        )

    def estimate_for_night_plan(
        self,
        market_view: AsOfMarketView,
        ticker: str,
        key: BenchmarkKey,
        generated_at: datetime,
        information_available_at: datetime,
    ) -> PointInTimeExpectedAuction:
        if generated_at > information_available_at:
            raise ValueError("expectation generated_at exceeds information cutoff")
        distribution = self.estimate(
            market_view,
            key,
            market_view.trade_date,
            information_available_at,
        )
        return PointInTimeExpectedAuction(
            ticker,
            market_view.trade_date,
            generated_at,
            information_available_at,
            key,
            "HIERARCHICAL_CONDITIONAL_BENCHMARK_V1",
            distribution,
        )

    @staticmethod
    def _provenance_valid(observation: BenchmarkObservation) -> bool:
        forbidden = {"actual_next_auction_gap", "next_gap_pct", "label", "outcome"}
        if forbidden.intersection(observation.features_as_of.values):
            raise ValueError("label data is forbidden in features_as_of")
        if any(observation.features_as_of.values.get(name) != value for name, value in observation.key.feature_mapping().items()):
            raise ValueError("benchmark key must be reproducible from features_as_of")
        if observation.features_as_of.as_of > observation.information_available_at:
            raise ValueError("feature snapshot exceeds information cutoff")
        if observation.label_available_at <= observation.information_available_at:
            raise ValueError("label must become available after feature cutoff")
        if not observation.features_as_of.source or not observation.benchmark_key_source or not observation.source_version:
            raise ValueError("observation provenance is incomplete")
        return True

    @staticmethod
    def _matches(
        level: Tuple[str, ...],
        observations: Sequence[BenchmarkObservation],
    ) -> Tuple[BenchmarkObservation, ...]:
        return tuple(
            observation for observation in observations
            if observation.key.hierarchy()[0][:len(level)] == level
        )

    @staticmethod
    def _shrink_quantile(local, parent, field, quantile, weight) -> float:
        local_q = _quantile([getattr(item, field) for item in local], quantile)
        parent_q = _quantile([getattr(item, field) for item in parent], quantile)
        return weight * local_q + (1.0 - weight) * parent_q
