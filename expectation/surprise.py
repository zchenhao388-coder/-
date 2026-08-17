from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Sequence, Tuple

from config.thresholds import ThresholdRegistry
from domain.enums import BenchmarkStatus
from expectation.benchmark import BenchmarkKey
from expectation.observations import AuctionGapObservation
from market.asof import AsOfMarketView


@dataclass(frozen=True)
class AuctionSurpriseResult:
    percentile: Optional[float]
    effective_sample_size: int
    minimum_sample_size: int
    status: BenchmarkStatus


class PointInTimeAuctionSurprisePercentile:
    """Historical actual-gap percentile with the benchmark minimum contract."""

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds

    def percentile(
        self,
        market_view: AsOfMarketView,
        benchmark_key: BenchmarkKey,
        actual_gap: float,
        information_available_at: datetime,
    ) -> AuctionSurpriseResult:
        observations = tuple(
            observation for observation in market_view.get_auction_gap_observations(information_available_at)
            if isinstance(observation, AuctionGapObservation)
            and date.fromisoformat(observation.trade_date) < date.fromisoformat(market_view.trade_date)
            and observation.available_at <= information_available_at
        )
        minimum = int(self.thresholds.get("minimum_local_sample_size"))
        selected, sufficient = self._select_population(observations, benchmark_key, minimum)
        if not selected:
            return AuctionSurpriseResult(None, 0, minimum, BenchmarkStatus.INSUFFICIENT)
        percentile = sum(1 for observation in selected if observation.actual_gap <= actual_gap) / len(selected)
        return AuctionSurpriseResult(
            percentile,
            len(selected),
            minimum,
            BenchmarkStatus.SUFFICIENT if sufficient else BenchmarkStatus.INSUFFICIENT,
        )

    @staticmethod
    def _select_population(
        observations: Sequence[AuctionGapObservation],
        key: BenchmarkKey,
        minimum: int,
    ) -> Tuple[Tuple[AuctionGapObservation, ...], bool]:
        matches_by_level = tuple(
            tuple(
                observation for observation in observations
                if observation.benchmark_key.hierarchy()[0][:len(level)] == level
            )
            for level in key.hierarchy()
        )
        for matches in matches_by_level:
            if len(matches) >= minimum:
                return matches, True
        for matches in reversed(matches_by_level):
            if matches:
                return matches, False
        return (), False
