from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional, Sequence

from auction.features import AuctionFeatureEngine, FeatureSnapshot
from auction.one_to_two import OneToTwoEngine, OneToTwoInput
from auction.phase import auction_phase_at
from domain.enums import AuctionPhase, ValidationState
from domain.models import DataQualityReport, DecisionTrace, MarketContext, NightPlan, SetupResult
from execution.compiler import CompilerInput, DecisionCompiler
from expectation.benchmark import BenchmarkKey, PointInTimeExpectedAuction
from market.asof import HistoricalFeatureRecord, MarketContextRecord
from market.dqs import DataQualityService
from replay.clock import ReplayEngine


@dataclass(frozen=True)
class TimedDecisionInputs:
    available_at: datetime
    regulatory_validation: ValidationState
    market_validation: ValidationState
    candidate_validation: ValidationState
    setup_requirements_met: bool
    cross_setup_rank: Optional[int]
    context_strength: Optional[float]


@dataclass(frozen=True)
class PipelineCheckpointResult:
    phase: AuctionPhase
    data_quality: DataQualityReport
    features: FeatureSnapshot
    setup_result: SetupResult
    decision: DecisionTrace


class AuctionDecisionPipeline:
    """Formal M1–M5 point-in-time orchestration entry."""

    def __init__(
        self,
        feature_engine: AuctionFeatureEngine,
        setup_engine: OneToTwoEngine,
        dqs: DataQualityService,
        compiler: DecisionCompiler,
    ):
        self.feature_engine = feature_engine
        self.setup_engine = setup_engine
        self.dqs = dqs
        self.compiler = compiler

    def evaluate_checkpoint(
        self,
        night_plan: NightPlan,
        replay: ReplayEngine,
        ticker: str,
        expectation: PointInTimeExpectedAuction,
        decision_inputs: TimedDecisionInputs,
        market_context_records: Sequence[MarketContextRecord],
        historical_features: Sequence[HistoricalFeatureRecord] = (),
        expectation_observations: Sequence[object] = (),
        auction_gap_observations: Sequence[object] = (),
        peer_groups: Optional[Mapping[str, Sequence[str]]] = None,
    ) -> PipelineCheckpointResult:
        if night_plan.trade_date != replay.clock.now.date().isoformat():
            raise ValueError("NightPlan trade_date must match replay trade date")
        self._validate_night_expectation(night_plan, expectation)
        view = replay.market_view(
            trade_date=night_plan.trade_date,
            peer_groups=peer_groups or {},
            historical_features=historical_features,
            expectation_observations=expectation_observations,
            auction_gap_observations=auction_gap_observations,
            market_context_records=market_context_records,
        )
        view.assert_timestamp_visible(decision_inputs.available_at)
        context = view.get_market_context()
        if context is None:
            raise ValueError("point-in-time MarketContext is unavailable")
        features = self.feature_engine.compute(
            view,
            ticker,
            expectation,
            liquidity_peer_group="liquidity",
            theme_peer_group="theme",
            global_peer_group="global",
            height_peer_group="height",
            theme_validated=context.theme_validation == ValidationState.VALID,
        )
        setup_result = self.setup_engine.evaluate(OneToTwoInput(
            ticker,
            view,
            features,
            decision_inputs.candidate_validation,
            decision_inputs.context_strength,
        ))
        data_quality = self.dqs.evaluate(view.get_ticks(ticker), view.as_of)
        decision = self.compiler.compile(CompilerInput(
            trade_date=night_plan.trade_date,
            decided_at=view.as_of,
            night_plan=night_plan,
            market_context=context,
            setup_result=setup_result,
            regulatory_validation=decision_inputs.regulatory_validation,
            data_quality=data_quality,
            market_validation=decision_inputs.market_validation,
            candidate_validation=decision_inputs.candidate_validation,
            setup_requirements_met=decision_inputs.setup_requirements_met,
            auction_percentile=features.values.get("AuctionSurprisePercentile"),
            cross_setup_rank=decision_inputs.cross_setup_rank,
        ))
        return PipelineCheckpointResult(auction_phase_at(view.as_of), data_quality, features, setup_result, decision)

    @staticmethod
    def _validate_night_expectation(
        night_plan: NightPlan,
        expectation: PointInTimeExpectedAuction,
    ) -> None:
        if expectation.trade_date != night_plan.trade_date:
            raise ValueError("expectation trade date does not match NightPlan")
        if expectation.information_available_at > night_plan.information_available_at:
            raise ValueError("expectation uses information after the NightPlan cutoff")
        if expectation.generated_at > night_plan.information_available_at:
            raise ValueError("expectation was generated after the NightPlan cutoff")
        if not expectation.source_version:
            raise ValueError("expectation source_version is required")
