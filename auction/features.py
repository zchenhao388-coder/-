from dataclasses import dataclass
from datetime import datetime, time, timedelta
from statistics import median
from typing import Dict, Mapping, Optional, Protocol, Sequence, Tuple

from config.thresholds import ThresholdRegistry
from domain.enums import AuthenticityState, FakeStrongFlag
from domain.models import AuctionTick


class AuctionSurprisePercentile(Protocol):
    def percentile(self, ticker: str, normalized_eg: float, as_of: datetime) -> Optional[float]:
        ...


@dataclass(frozen=True)
class FeatureSnapshot:
    values: Mapping[str, Optional[float]]
    fake_strong_flags: Sequence[FakeStrongFlag]
    authenticity_state: AuthenticityState


def _percentile_rank(value: Optional[float], population: Sequence[float]) -> Optional[float]:
    if value is None or not population:
        return None
    return sum(1 for item in population if item <= value) / len(population)


def _value_at_or_before(ticks: Sequence[AuctionTick], target: time) -> Optional[AuctionTick]:
    eligible = [t for t in ticks if t.exchange_ts is not None and t.exchange_ts.time() <= target]
    return eligible[-1] if eligible else None


class AuctionFeatureEngine:
    def __init__(self, thresholds: ThresholdRegistry, surprise: Optional[AuctionSurprisePercentile] = None):
        self.thresholds = thresholds
        self.surprise = surprise

    def compute(
        self,
        ticker: str,
        ticks: Sequence[AuctionTick],
        expected_gap_q50: Optional[float],
        expected_gap_q25: Optional[float],
        expected_gap_q75: Optional[float],
        historical_amounts: Sequence[float] = (),
        historical_volumes: Sequence[float] = (),
        peer_amounts: Sequence[float] = (),
        peer_volumes: Sequence[float] = (),
        theme_peer_gaps: Sequence[float] = (),
        global_peer_gaps: Sequence[float] = (),
        height_peer_gaps: Sequence[float] = (),
        theme_validated: bool = True,
    ) -> FeatureSnapshot:
        ordered = tuple(sorted((t for t in ticks if t.exchange_ts is not None), key=lambda t: t.exchange_ts))
        if not ordered:
            return FeatureSnapshot({}, (), AuthenticityState.UNKNOWN)
        latest = ordered[-1]
        eg = latest.gap_pct
        iqr = None
        if expected_gap_q25 is not None and expected_gap_q75 is not None:
            iqr = expected_gap_q75 - expected_gap_q25
        normalized_eg = None
        if eg is not None and expected_gap_q50 is not None and iqr not in (None, 0):
            normalized_eg = (eg - expected_gap_q50) / iqr
        surprise_pct = None
        if normalized_eg is not None and self.surprise is not None:
            surprise_pct = self.surprise.percentile(ticker, normalized_eg, latest.exchange_ts)  # type: ignore[arg-type]

        pre20 = tuple(t for t in ordered if t.exchange_ts.time() < time(9, 20))  # type: ignore[union-attr]
        post20 = tuple(t for t in ordered if t.exchange_ts.time() >= time(9, 20))  # type: ignore[union-attr]
        pre20_gaps = [t.gap_pct for t in pre20 if t.gap_pct is not None]
        post20_gaps = [t.gap_pct for t in post20 if t.gap_pct is not None]
        pre20_peak = max(pre20_gaps) if pre20_gaps else None
        at20_tick = _value_at_or_before(ordered, time(9, 20))
        at20_gap = at20_tick.gap_pct if at20_tick is not None else None
        pre20_decay = None if pre20_peak is None or at20_gap is None else pre20_peak - at20_gap
        first_post_gap = post20_gaps[0] if post20_gaps else None
        post20_delta = None if eg is None or first_post_gap is None else eg - first_post_gap
        post20_mdd = self._ordered_max_drawdown(post20_gaps)
        close_location = self._close_location(post20_gaps)
        recovery_ratio = self._recovery_ratio(post20_gaps)
        late30_delta, late30_slope = self._late_delta_slope(ordered, 30)
        late60_delta, late60_slope = self._late_delta_slope(ordered, 60)

        amount = latest.matched_amount
        volume = latest.matched_volume
        amount_hist_pct = _percentile_rank(amount, historical_amounts)
        volume_hist_pct = _percentile_rank(volume, historical_volumes)
        amount_peer_pct = _percentile_rank(amount, peer_amounts)
        volume_peer_pct = _percentile_rank(volume, peer_volumes)
        amount_ratio = None
        if amount is not None and historical_amounts:
            center = median(historical_amounts)
            amount_ratio = amount / center if center else None
        volume_ratio = None
        if volume is not None and historical_volumes:
            center = median(historical_volumes)
            volume_ratio = volume / center if center else None
        theme_rank = _percentile_rank(eg, theme_peer_gaps)
        global_rank = _percentile_rank(eg, global_peer_gaps)
        height_rank = _percentile_rank(eg, height_peer_gaps)
        esr = None if pre20_peak in (None, 0) or eg is None else eg / pre20_peak

        flags = []
        if pre20_decay is not None and pre20_decay >= self.thresholds.get("pre20_mirage_decay_pct"):
            flags.append(FakeStrongFlag.PRE20_MIRAGE)
        decay_fraction = self._decay_fraction(post20_gaps)
        if post20_mdd is not None and post20_mdd >= self.thresholds.get("post20_decay_pct") and decay_fraction >= self.thresholds.get("continuous_decay_fraction"):
            flags.append(FakeStrongFlag.POST20_CONTINUOUS_DECAY)
        liquidity_pct = self._mean_available(amount_hist_pct, volume_hist_pct)
        if eg is not None and eg >= self.thresholds.get("price_strength_gap_pct") and liquidity_pct is not None and liquidity_pct < self.thresholds.get("liquidity_low_percentile"):
            flags.append(FakeStrongFlag.PRICE_WITHOUT_LIQUIDITY)
        if not theme_validated and theme_rank is not None and theme_rank >= 1.0 - self.thresholds.get("isolated_strength_rank_pct"):
            flags.append(FakeStrongFlag.ISOLATED_STRENGTH)
        if late30_delta is not None and late30_delta >= self.thresholds.get("last_second_spike_pct") and liquidity_pct is not None and liquidity_pct < self.thresholds.get("liquidity_low_percentile"):
            flags.append(FakeStrongFlag.LAST_SECOND_SPIKE)

        authenticity = self._authenticity(post20_mdd, recovery_ratio, flags)
        values: Dict[str, Optional[float]] = {
            "EG": eg,
            "NormalizedEG": normalized_eg,
            "AuctionSurprisePercentile": surprise_pct,
            "Pre20Peak": pre20_peak,
            "Pre20Decay": pre20_decay,
            "ESR": esr,
            "Post20Delta": post20_delta,
            "Post20MDD": post20_mdd,
            "CloseLocation": close_location,
            "RecoveryRatio": recovery_ratio,
            "Late30Delta": late30_delta,
            "Late30Slope": late30_slope,
            "Late60Delta": late60_delta,
            "Late60Slope": late60_slope,
            "AuctionAmount": amount,
            "AuctionVolume": volume,
            "AuctionAmountRatio": amount_ratio,
            "AuctionVolumeRatio": volume_ratio,
            "HistoricalAmountPercentile": amount_hist_pct,
            "HistoricalVolumePercentile": volume_hist_pct,
            "PeerAmountPercentile": amount_peer_pct,
            "PeerVolumePercentile": volume_peer_pct,
            "ThemePeerRank": theme_rank,
            "GlobalPeerRank": global_rank,
            "HeightPeerRank": height_rank,
        }
        return FeatureSnapshot(values, tuple(flags), authenticity)

    @staticmethod
    def _ordered_max_drawdown(values: Sequence[float]) -> Optional[float]:
        if not values:
            return None
        running_peak = values[0]
        max_drawdown = 0.0
        for value in values[1:]:
            max_drawdown = max(max_drawdown, running_peak - value)
            running_peak = max(running_peak, value)
        return max_drawdown

    @staticmethod
    def _close_location(values: Sequence[float]) -> Optional[float]:
        if not values:
            return None
        low, high = min(values), max(values)
        return 1.0 if high == low else (values[-1] - low) / (high - low)

    @staticmethod
    def _recovery_ratio(values: Sequence[float]) -> Optional[float]:
        if not values:
            return None
        peak_index = max(range(len(values)), key=lambda index: values[index])
        tail = values[peak_index:]
        low = min(tail)
        peak = values[peak_index]
        return 1.0 if peak == low else (values[-1] - low) / (peak - low)

    @staticmethod
    def _late_delta_slope(ticks: Sequence[AuctionTick], seconds: int) -> Tuple[Optional[float], Optional[float]]:
        latest = ticks[-1]
        if latest.exchange_ts is None or latest.gap_pct is None:
            return None, None
        cutoff = latest.exchange_ts - timedelta(seconds=seconds)
        candidates = [t for t in ticks if t.exchange_ts is not None and t.exchange_ts <= cutoff and t.gap_pct is not None]
        if not candidates:
            return None, None
        start = candidates[-1]
        elapsed = (latest.exchange_ts - start.exchange_ts).total_seconds()  # type: ignore[operator]
        delta = latest.gap_pct - start.gap_pct  # type: ignore[operator]
        return delta, (delta / elapsed if elapsed else None)

    @staticmethod
    def _decay_fraction(values: Sequence[float]) -> float:
        if len(values) < 2:
            return 0.0
        changes = [right - left for left, right in zip(values, values[1:])]
        return sum(1 for change in changes if change < 0) / len(changes)

    @staticmethod
    def _mean_available(*values: Optional[float]) -> Optional[float]:
        available = [v for v in values if v is not None]
        return sum(available) / len(available) if available else None

    def _authenticity(self, drawdown, recovery, flags) -> AuthenticityState:
        hard_fake = {FakeStrongFlag.POST20_CONTINUOUS_DECAY, FakeStrongFlag.PRICE_WITHOUT_LIQUIDITY, FakeStrongFlag.LAST_SECOND_SPIKE}
        if hard_fake.intersection(flags):
            return AuthenticityState.FAKE_STRONG
        if drawdown is not None and recovery is not None and drawdown >= self.thresholds.get("healthy_min_drawdown_pct") and recovery >= self.thresholds.get("healthy_recovery_ratio"):
            return AuthenticityState.HEALTHY_DISAGREEMENT
        if FakeStrongFlag.PRE20_MIRAGE in flags or FakeStrongFlag.ISOLATED_STRENGTH in flags:
            return AuthenticityState.SUSPICIOUS
        return AuthenticityState.AUTHENTIC
