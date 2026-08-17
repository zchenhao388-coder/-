from dataclasses import dataclass
from datetime import date, datetime

from domain.models import NightPlan
from expectation.benchmark import BenchmarkKey, BenchmarkObservation, FeaturesAsOf


@dataclass(frozen=True)
class AuctionGapObservation:
    observation_id: str
    trade_date: str
    available_at: datetime
    source: str
    provenance: str
    benchmark_key: BenchmarkKey
    actual_gap: float


class PointInTimeObservationBuilder:
    """Build labelled history while keeping T features separate from T+1 labels."""

    def build_benchmark_observation(
        self,
        night_plan: NightPlan,
        observation_id: str,
        auction_trade_date: str,
        ticker: str,
        features_as_of: FeaturesAsOf,
        benchmark_key: BenchmarkKey,
        benchmark_key_source: str,
        actual_next_auction_gap: float,
        auction_amount: float,
        auction_volume: float,
        label_available_at: datetime,
        source_version: str,
    ) -> BenchmarkObservation:
        if ticker not in night_plan.candidate_pool:
            raise ValueError("historical observation ticker must belong to its NightPlan")
        if features_as_of.as_of > night_plan.information_available_at:
            raise ValueError("features_as_of exceeds NightPlan information cutoff")
        if night_plan.generated_at > night_plan.information_available_at:
            raise ValueError("NightPlan was generated after its information cutoff")
        date.fromisoformat(auction_trade_date)
        date.fromisoformat(night_plan.trade_date)
        if auction_trade_date != night_plan.trade_date:
            raise ValueError("label trade date must equal the NightPlan target trade date")
        if label_available_at <= night_plan.information_available_at:
            raise ValueError("T+1 label cannot be available at T feature time")
        for name, value in (
            ("observation_id", observation_id),
            ("benchmark_key_source", benchmark_key_source),
            ("source_version", source_version),
        ):
            if not value:
                raise ValueError(f"{name} is required")
        forbidden = {"actual_next_auction_gap", "next_gap_pct", "label", "outcome"}
        if forbidden.intersection(features_as_of.values):
            raise ValueError("T+1 label cannot enter T features_as_of")
        if any(features_as_of.values.get(name) != value for name, value in benchmark_key.feature_mapping().items()):
            raise ValueError("benchmark_key must be derived from stored T features_as_of")
        if not features_as_of.source:
            raise ValueError("features_as_of source is required")
        return BenchmarkObservation(
            observation_id,
            auction_trade_date,
            ticker,
            features_as_of,
            night_plan.information_available_at,
            benchmark_key,
            benchmark_key_source,
            actual_next_auction_gap,
            auction_amount,
            auction_volume,
            label_available_at,
            source_version,
        )

    @staticmethod
    def build_gap_observation(observation: BenchmarkObservation) -> AuctionGapObservation:
        return AuctionGapObservation(
            observation.observation_id,
            observation.trade_date,
            observation.label_available_at,
            observation.source_version,
            observation.benchmark_key_source,
            observation.key,
            observation.actual_next_auction_gap,
        )
