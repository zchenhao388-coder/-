from dataclasses import dataclass
from typing import Mapping, Sequence, Tuple

from domain.enums import AccountAuctionState, CandidateGrade, ExecutionState, SetupType
from domain.models import DecisionTrace, NightPlan, SetupResult


@dataclass(frozen=True)
class ResolvedSetupCandidate:
    ticker: str
    primary_result: SetupResult
    secondary_results: Sequence[SetupResult]
    supporting_evidence: Sequence[str]
    cross_setup_rank: int
    setup_quality_percentile: float


class CrossSetupResolver:
    """One ticker, one primary setup; secondary setups are evidence only."""

    GRADE_PRIORITY: Mapping[CandidateGrade, int] = {
        CandidateGrade.A1: 100,
        CandidateGrade.A2: 90,
        CandidateGrade.B_HIGH_QUALITY: 80,
        CandidateGrade.B_DISAGREEMENT: 75,
        CandidateGrade.B_LEADERSHIP_CHALLENGED: 70,
        CandidateGrade.B_CONSENSUS_RISK: 65,
        CandidateGrade.B_ECOSYSTEM_PENDING: 60,
        CandidateGrade.B_CONFIRMATION: 55,
        CandidateGrade.C_NIGHT_DEGRADED: 30,
        CandidateGrade.C_HIGHBOARD_DEGRADED: 25,
        CandidateGrade.C_LEADERSHIP_LOST: 20,
        CandidateGrade.C_AUCTION_EMERGENT: 10,
        CandidateGrade.DROP: 0,
    }

    def resolve(self, night_plan: NightPlan, results: Sequence[SetupResult]) -> Tuple[ResolvedSetupCandidate, ...]:
        grouped = {}
        for result in results:
            if result.ticker not in night_plan.candidate_pool:
                continue
            key = (result.ticker, result.setup_type)
            if key in grouped:
                raise ValueError(f"duplicate setup result for {result.ticker}/{result.setup_type.value}")
            grouped[key] = result

        selected = []
        for ticker in night_plan.candidate_pool:
            ticker_results = tuple(result for (item_ticker, _), result in grouped.items() if item_ticker == ticker)
            if not ticker_results:
                continue
            primary_type = night_plan.setup_by_ticker.get(ticker)
            primary = grouped.get((ticker, primary_type)) if primary_type is not None else None
            if primary is None:
                raise ValueError(f"primary setup result is missing for {ticker}")
            secondary = tuple(result for result in ticker_results if result.setup_type != primary_type)
            evidence = tuple(dict.fromkeys(
                item
                for result in secondary
                for item in (*result.reason_codes, *result.flags)
            ))
            selected.append((primary, secondary, evidence))

        ordered = sorted(selected, key=lambda item: self._sort_key(item[0]), reverse=True)
        count = len(ordered)
        return tuple(
            ResolvedSetupCandidate(
                primary.ticker,
                primary,
                secondary,
                evidence,
                index,
                (count - index + 1) / count,
            )
            for index, (primary, secondary, evidence) in enumerate(ordered, start=1)
        )

    def account_state(self, decisions: Sequence[DecisionTrace]) -> AccountAuctionState:
        if any(
            decision.grade in (CandidateGrade.A1, CandidateGrade.A2)
            and decision.execution_state == ExecutionState.ARMED
            for decision in decisions
        ):
            return AccountAuctionState.EXECUTION_READY
        grades = tuple(decision.grade for decision in decisions)
        if any(grade.value.startswith("B_") for grade in grades):
            return AccountAuctionState.WAIT_OPEN
        return AccountAuctionState.CASH

    def _sort_key(self, result: SetupResult):
        return (
            self.GRADE_PRIORITY[result.grade_recommendation],
            result.hvs if result.hvs is not None else -1.0,
            result.aqs,
            self._setup_priority(result.setup_type),
            result.ticker,
        )

    @staticmethod
    def _setup_priority(setup_type: SetupType) -> int:
        return {
            SetupType.HIGH_BOARD: 3,
            SetupType.ONE_TO_TWO: 2,
            SetupType.WEAK_TO_STRONG: 1,
        }[setup_type]
