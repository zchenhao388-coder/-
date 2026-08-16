from dataclasses import dataclass
from datetime import datetime
from math import exp
from typing import Iterable, Mapping, Sequence, Tuple


@dataclass(frozen=True)
class BenchmarkKey:
    setup_type: str
    regime: str
    theme_type: str
    leadership: str
    board_height_bucket: str
    board_form: str

    def hierarchy(self) -> Sequence[Tuple[str, ...]]:
        full = (self.setup_type, self.regime, self.theme_type, self.leadership, self.board_height_bucket, self.board_form)
        return (full, full[:-1], full[:-2], full[:3], full[:2], full[:1], ())


@dataclass(frozen=True)
class BenchmarkObservation:
    key: BenchmarkKey
    next_gap_pct: float
    auction_amount: float
    auction_volume: float
    available_at: datetime


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
    def __init__(self, observations: Iterable[BenchmarkObservation], prior_strength: float):
        if prior_strength <= 0:
            raise ValueError("prior_strength must be positive")
        self.observations = tuple(observations)
        self.prior_strength = prior_strength

    def estimate(self, key: BenchmarkKey, information_available_at: datetime) -> ExpectedAuctionDistribution:
        eligible = tuple(observation for observation in self.observations if observation.available_at <= information_available_at)
        levels = key.hierarchy()
        leaf = self._matches(levels[0], eligible)
        parent = ()
        parent_level = len(levels) - 1
        for index, level in enumerate(levels[1:], start=1):
            matches = self._matches(level, eligible)
            if matches:
                parent = matches
                parent_level = index
                break
        if not leaf and parent:
            leaf = parent
        if not leaf:
            raise ValueError("no benchmark observations")
        if not parent:
            parent = leaf
        weight = len(leaf) / (len(leaf) + self.prior_strength)
        gap_q, amount_q, volume_q = {}, {}, {}
        for label, q in (("q10", .10), ("q25", .25), ("q50", .50), ("q75", .75), ("q90", .90)):
            gap_q[label] = self._shrink_quantile(leaf, parent, "next_gap_pct", q, weight)
            amount_q[label] = self._shrink_quantile(leaf, parent, "auction_amount", q, weight)
            volume_q[label] = self._shrink_quantile(leaf, parent, "auction_volume", q, weight)
        specificity = 1.0 - (parent_level / max(1, len(levels) - 1))
        peer_similarity = max(0.0, specificity)
        sample_confidence = 1.0 - exp(-len(leaf) / self.prior_strength)
        return ExpectedAuctionDistribution(
            gap_q["q10"], gap_q["q25"], gap_q["q50"], gap_q["q75"], gap_q["q90"],
            amount_q, volume_q, len(leaf), peer_similarity,
            sample_confidence * (0.5 + 0.5 * peer_similarity), len(parent), weight,
        )

    @staticmethod
    def _matches(level: Tuple[str, ...], observations: Sequence[BenchmarkObservation]) -> Sequence[BenchmarkObservation]:
        return tuple(observation for observation in observations if observation.key.hierarchy()[0][:len(level)] == level)

    @staticmethod
    def _shrink_quantile(leaf, parent, field, q, weight) -> float:
        leaf_q = _quantile([getattr(item, field) for item in leaf], q)
        parent_q = _quantile([getattr(item, field) for item in parent], q)
        return weight * leaf_q + (1.0 - weight) * parent_q
