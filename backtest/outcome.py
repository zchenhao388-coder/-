from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence, Tuple

from config.thresholds import ThresholdRegistry
from domain.enums import CandidateGrade, ExecutionState, OutcomeClass, OutcomeDiagnostic, SetupType
from domain.models import OpenExecutionDecision, OpenTick, Outcome
from market.asof import AsOfMarketView


@dataclass(frozen=True)
class SetupOutcomeEvidence:
    leadership_retained_close: Optional[bool] = None
    high_board_blowup: Optional[bool] = None
    weakness_resolved_at_925: Optional[bool] = None
    weakness_resolved_at_931: Optional[bool] = None
    identity_recovered: Optional[bool] = None
    leadership_transfer_success: Optional[bool] = None
    repair_retention_30m: Optional[float] = None


@dataclass(frozen=True)
class OutcomeInput:
    ticker: str
    setup_type: SetupType
    decision_grade: CandidateGrade
    trade_view: AsOfMarketView
    open_decision: OpenExecutionDecision
    limit_up_price: Optional[float]
    label_view: Optional[AsOfMarketView] = None
    setup_evidence: SetupOutcomeEvidence = SetupOutcomeEvidence()


class OutcomeEngine:
    """Point-in-time outcome labels over visible continuous-market data."""

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds

    def compute(self, request: OutcomeInput) -> Outcome:
        if request.trade_view.trade_date != request.open_decision.features.trade_date:
            raise ValueError("outcome trade view and execution decision must share trade_date")
        request.trade_view.assert_timestamp_visible(request.open_decision.decided_at)
        ticks = tuple(
            tick for tick in request.trade_view.get_open_ticks(request.ticker)
            if tick.exchange_ts is not None and tick.exchange_ts >= request.open_decision.decided_at
        )
        executed = request.open_decision.state == ExecutionState.EXECUTE
        entry_price = request.open_decision.trigger_price if executed else None
        entry_time = request.open_decision.decided_at if executed else None
        return_1m = self._return_at(ticks, entry_time, entry_price, 1)
        return_5m = self._return_at(ticks, entry_time, entry_price, 5)
        return_30m = self._return_at(ticks, entry_time, entry_price, 30)
        close_price = ticks[-1].last_price if ticks else None
        return_close = self._pct_change(close_price, entry_price)
        mfe_5m, mae_5m = self._excursions(ticks, entry_time, entry_price, 5)
        mfe_day, mae_day = self._excursions(ticks, entry_time, entry_price, None)
        touched = self._touched_limit(ticks, request.limit_up_price)
        closed = None if request.limit_up_price is None or close_price is None else close_price >= request.limit_up_price
        second_board_quality = None if touched is None else (1.0 if closed else .5 if touched else 0.0)
        fast_failure = return_5m is not None and return_5m <= self.thresholds.get("fast_failure_return_5m_max_pct")
        outcome_class = self._classify(closed, return_close, mae_day, fast_failure)
        d2_gap = None
        if request.label_view is not None:
            d2_gap = request.label_view.get_precomputed_feature(request.ticker, "D2OpenGap")
        nuclear = None if d2_gap is None else d2_gap <= self.thresholds.get("nuclear_open_gap_max_pct")
        evidence = request.setup_evidence
        return Outcome(
            ticker=request.ticker,
            trade_date=request.trade_view.trade_date,
            decision_grade=request.decision_grade,
            executed=executed,
            entry_price=entry_price,
            return_1m=return_1m,
            return_5m=return_5m,
            return_close=return_close,
            max_favorable_excursion=mfe_day,
            max_adverse_excursion=mae_day,
            touched_limit_up=touched,
            closed_limit_up=closed,
            second_board_quality=second_board_quality,
            executable_at_open=executed,
            return_30m=return_30m,
            mfe_5m=mfe_5m,
            mfe_day=mfe_day,
            mae_5m=mae_5m,
            mae_day=mae_day,
            strategy_return=return_close,
            d2_open_gap=d2_gap,
            fast_failure=fast_failure,
            outcome_class=outcome_class,
            leadership_retained_close=evidence.leadership_retained_close if request.setup_type == SetupType.HIGH_BOARD else None,
            high_board_blowup=evidence.high_board_blowup if request.setup_type == SetupType.HIGH_BOARD else None,
            next_day_nuclear_open=nuclear if request.setup_type == SetupType.HIGH_BOARD else None,
            weakness_resolved_at_925=evidence.weakness_resolved_at_925 if request.setup_type == SetupType.WEAK_TO_STRONG else None,
            weakness_resolved_at_931=evidence.weakness_resolved_at_931 if request.setup_type == SetupType.WEAK_TO_STRONG else None,
            identity_recovered=evidence.identity_recovered if request.setup_type == SetupType.WEAK_TO_STRONG else None,
            leadership_transfer_success=evidence.leadership_transfer_success if request.setup_type == SetupType.WEAK_TO_STRONG else None,
            repair_retention_30m=evidence.repair_retention_30m if request.setup_type == SetupType.WEAK_TO_STRONG else None,
        )

    @staticmethod
    def _return_at(
        ticks: Sequence[OpenTick],
        entry_time: Optional[datetime],
        entry_price: Optional[float],
        minutes: int,
    ) -> Optional[float]:
        if entry_time is None or entry_price in (None, 0):
            return None
        target = entry_time + timedelta(minutes=minutes)
        eligible = tuple(tick for tick in ticks if tick.exchange_ts is not None and tick.exchange_ts <= target and tick.last_price is not None)
        if not eligible or eligible[-1].exchange_ts is None or eligible[-1].exchange_ts < target:
            return None
        return OutcomeEngine._pct_change(eligible[-1].last_price, entry_price)

    @staticmethod
    def _excursions(
        ticks: Sequence[OpenTick],
        entry_time: Optional[datetime],
        entry_price: Optional[float],
        minutes: Optional[int],
    ) -> Tuple[Optional[float], Optional[float]]:
        if entry_time is None or entry_price in (None, 0):
            return None, None
        cutoff = None if minutes is None else entry_time + timedelta(minutes=minutes)
        prices = [
            tick.last_price for tick in ticks
            if tick.last_price is not None and tick.exchange_ts is not None and (cutoff is None or tick.exchange_ts <= cutoff)
        ]
        if not prices:
            return None, None
        returns = [(price / entry_price - 1.0) * 100.0 for price in prices]
        return max(returns), abs(min(0.0, min(returns)))

    @staticmethod
    def _touched_limit(ticks: Sequence[OpenTick], limit_up_price: Optional[float]) -> Optional[bool]:
        if limit_up_price is None:
            return None
        visible = [value for tick in ticks for value in (tick.high, tick.last_price) if value is not None]
        return any(value >= limit_up_price for value in visible)

    @staticmethod
    def _pct_change(value: Optional[float], reference: Optional[float]) -> Optional[float]:
        return None if value is None or reference in (None, 0) else (value / reference - 1.0) * 100.0

    def _classify(
        self,
        closed_limit: Optional[bool],
        return_close: Optional[float],
        mae_day: Optional[float],
        fast_failure: bool,
    ) -> Optional[OutcomeClass]:
        if return_close is None:
            return None
        if fast_failure:
            return OutcomeClass.FAST_FAILURE
        if closed_limit or (
            return_close >= self.thresholds.get("clean_success_return_min_pct")
            and mae_day is not None
            and mae_day <= self.thresholds.get("clean_success_mae_max_pct")
        ):
            return OutcomeClass.CLEAN_SUCCESS
        if return_close >= self.thresholds.get("clean_success_return_min_pct"):
            return OutcomeClass.UGLY_SUCCESS
        if return_close >= self.thresholds.get("good_non_limit_return_min_pct"):
            return OutcomeClass.GOOD_NON_LIMIT
        if abs(return_close) <= self.thresholds.get("flat_no_edge_abs_return_max_pct"):
            return OutcomeClass.FLAT_NO_EDGE
        return OutcomeClass.SLOW_FAILURE


def diagnose_outcome(
    outcome: Outcome,
    setup_type: SetupType,
    final_state: ExecutionState,
    reason_codes: Sequence[str] = (),
) -> Tuple[OutcomeDiagnostic, ...]:
    diagnostics = []
    failure = outcome.outcome_class in (OutcomeClass.FAST_FAILURE, OutcomeClass.SLOW_FAILURE)
    success = outcome.outcome_class in (OutcomeClass.CLEAN_SUCCESS, OutcomeClass.UGLY_SUCCESS, OutcomeClass.GOOD_NON_LIMIT)
    if outcome.executed and failure:
        diagnostics.append(OutcomeDiagnostic.FP_EXECUTION)
        if "POSITIVE_SURPRISE" in reason_codes:
            diagnostics.append(OutcomeDiagnostic.FP_EXPECTATION)
        if "LEADERSHIP_RETAINED" in reason_codes or "ECOSYSTEM_POSITIVE" in reason_codes:
            diagnostics.append(OutcomeDiagnostic.FP_CONTEXT)
    if not outcome.executed and success:
        if outcome.decision_grade == CandidateGrade.C_AUCTION_EMERGENT:
            diagnostics.append(OutcomeDiagnostic.FN_NIGHT_SELECTION)
        elif final_state in (ExecutionState.CANCELLED, ExecutionState.HARD_CANCELLED):
            diagnostics.append(OutcomeDiagnostic.FN_OPEN_CANCEL)
        else:
            diagnostics.append(OutcomeDiagnostic.FN_AUCTION_FILTER)
    return tuple(dict.fromkeys(diagnostics))
