from datetime import datetime
from typing import Sequence

from config.thresholds import ThresholdRegistry
from domain.enums import DataQualityState
from domain.models import AuctionTick, DataQualityReport


class DataQualityService:
    CORE_FIELDS = (
        "exchange_ts",
        "receive_ts",
        "virtual_price",
        "gap_pct",
        "matched_volume",
        "matched_amount",
    )

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds

    def evaluate(self, ticks: Sequence[AuctionTick], as_of: datetime) -> DataQualityReport:
        if not ticks:
            return DataQualityReport(DataQualityState.BROKEN, self.CORE_FIELDS, ("NO_TICKS",), 0.0)
        latest = max(ticks, key=lambda tick: float("-inf") if tick.exchange_ts is None else tick.exchange_ts.timestamp())
        missing = tuple(name for name in self.CORE_FIELDS if getattr(latest, name) is None)
        completeness = (len(self.CORE_FIELDS) - len(missing)) / len(self.CORE_FIELDS)
        reasons = []
        if latest.exchange_ts is not None and latest.exchange_ts > as_of:
            reasons.append("FUTURE_TICK")
        elif latest.exchange_ts is not None:
            age = (as_of - latest.exchange_ts).total_seconds()
            if age > self.thresholds.get("max_tick_age_seconds"):
                reasons.append("STALE_EXCHANGE_TICK")
        if latest.exchange_ts is not None and latest.receive_ts is not None:
            lag = (latest.receive_ts - latest.exchange_ts).total_seconds()
            if lag < 0:
                reasons.append("RECEIVE_BEFORE_EXCHANGE")
            elif lag > self.thresholds.get("max_receive_lag_seconds"):
                reasons.append("STALE_RECEIVE")
        observed_timestamps = [t.exchange_ts for t in ticks if t.exchange_ts is not None]
        if any(right < left for left, right in zip(observed_timestamps, observed_timestamps[1:])):
            reasons.append("EXCHANGE_TIMESTAMP_NON_MONOTONIC")
        broken_floor = self.thresholds.get("dqs_broken_completeness")
        degraded_floor = self.thresholds.get("dqs_degraded_completeness")
        if completeness < broken_floor or "FUTURE_TICK" in reasons:
            state = DataQualityState.BROKEN
        elif completeness < degraded_floor or reasons:
            state = DataQualityState.DEGRADED
        else:
            state = DataQualityState.GOOD
        return DataQualityReport(state, missing, tuple(reasons), completeness)
