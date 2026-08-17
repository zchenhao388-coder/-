from typing import Optional

from config.thresholds import ThresholdRegistry
from domain.enums import DataQualityState, SourceAgreementState
from domain.models import SourceFieldComparison, SourceValidationReport
from domain.reason_codes import ReasonCode
from market.asof import AsOfMarketView


class CrossSourceValidator:
    """Point-in-time primary/secondary comparison for DQS only."""

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds

    def evaluate(
        self,
        market_view: AsOfMarketView,
        ticker: str,
        primary_source: str,
        secondary_source: str,
    ) -> SourceValidationReport:
        primary = market_view.get_probe_ticks(ticker, primary_source)
        secondary = market_view.get_probe_ticks(ticker, secondary_source)
        if not primary or not secondary:
            return SourceValidationReport(
                ticker=ticker,
                as_of=market_view.as_of,
                primary_source=primary_source,
                secondary_source=secondary_source,
                state=SourceAgreementState.INSUFFICIENT,
                dqs_state=DataQualityState.DEGRADED,
                comparisons=(),
                reason_codes=(ReasonCode.SOURCE_CROSSCHECK_UNAVAILABLE.value,),
                execution_eligible=False,
            )
        primary_tick = primary[-1]
        secondary_tick = secondary[-1]
        receive_skew = abs((primary_tick.receive_ts - secondary_tick.receive_ts).total_seconds())  # type: ignore[operator]
        provider_skew = None
        if primary_tick.provider_ts is not None and secondary_tick.provider_ts is not None:
            provider_skew = abs((primary_tick.provider_ts - secondary_tick.provider_ts).total_seconds())
        comparisons = (
            self._compare(
                "virtual_price",
                primary_tick.virtual_price,
                secondary_tick.virtual_price,
                self.thresholds.get("source_price_degraded_pct"),
                self.thresholds.get("source_price_broken_pct"),
                relative=True,
            ),
            self._compare(
                "gap_pct",
                primary_tick.gap_pct,
                secondary_tick.gap_pct,
                self.thresholds.get("source_gap_degraded_points"),
                self.thresholds.get("source_gap_broken_points"),
                relative=False,
            ),
        )
        states = {item.state for item in comparisons}
        reasons = []
        if (
            receive_skew > self.thresholds.get("source_skew_broken_seconds")
            or provider_skew is not None
            and provider_skew > self.thresholds.get("source_provider_skew_broken_seconds")
        ):
            state = SourceAgreementState.CRITICAL
            dqs_state = DataQualityState.BROKEN
            reasons.append(ReasonCode.SOURCE_TIME_SKEW.value)
        elif SourceAgreementState.CRITICAL in states:
            state = SourceAgreementState.CRITICAL
            dqs_state = DataQualityState.BROKEN
            reasons.append(ReasonCode.SOURCE_DISAGREEMENT.value)
        elif (
            receive_skew > self.thresholds.get("source_skew_degraded_seconds")
            or provider_skew is not None
            and provider_skew > self.thresholds.get("source_provider_skew_degraded_seconds")
            or SourceAgreementState.DISAGREEMENT in states
        ):
            state = SourceAgreementState.DISAGREEMENT
            dqs_state = DataQualityState.DEGRADED
            reasons.append(
                ReasonCode.SOURCE_TIME_SKEW.value
                if (
                    receive_skew > self.thresholds.get("source_skew_degraded_seconds")
                    or provider_skew is not None
                    and provider_skew > self.thresholds.get("source_provider_skew_degraded_seconds")
                )
                else ReasonCode.SOURCE_DISAGREEMENT.value
            )
        elif SourceAgreementState.INSUFFICIENT in states:
            state = SourceAgreementState.INSUFFICIENT
            dqs_state = DataQualityState.DEGRADED
            reasons.append(ReasonCode.SOURCE_CROSSCHECK_UNAVAILABLE.value)
        else:
            state = SourceAgreementState.AGREED
            dqs_state = DataQualityState.GOOD
        return SourceValidationReport(
            ticker=ticker,
            as_of=market_view.as_of,
            primary_source=primary_source,
            secondary_source=secondary_source,
            state=state,
            dqs_state=dqs_state,
            comparisons=comparisons,
            reason_codes=tuple(reasons),
            execution_eligible=False,
            receive_time_skew_seconds=receive_skew,
            provider_time_skew_seconds=provider_skew,
        )

    @staticmethod
    def _compare(
        field: str,
        primary: Optional[float],
        secondary: Optional[float],
        degraded: float,
        broken: float,
        relative: bool,
    ) -> SourceFieldComparison:
        if primary is None or secondary is None:
            return SourceFieldComparison(
                field, primary, secondary, None, None, SourceAgreementState.INSUFFICIENT
            )
        absolute = abs(primary - secondary)
        relative_pct = None
        measure = absolute
        if relative:
            denominator = max(abs(primary), abs(secondary))
            relative_pct = None if denominator == 0 else absolute / denominator * 100.0
            measure = float("inf") if relative_pct is None else relative_pct
        if measure >= broken:
            state = SourceAgreementState.CRITICAL
        elif measure >= degraded:
            state = SourceAgreementState.DISAGREEMENT
        else:
            state = SourceAgreementState.AGREED
        return SourceFieldComparison(field, primary, secondary, absolute, relative_pct, state)
