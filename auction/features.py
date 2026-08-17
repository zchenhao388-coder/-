from dataclasses import dataclass
from datetime import datetime, time, timedelta
from statistics import median
from typing import Dict, Mapping, Optional, Protocol, Sequence, Tuple

from auction.phase import auction_phase_at
from config.thresholds import ThresholdRegistry
from domain.enums import AuctionPhase, AuthenticityState, BenchmarkStatus, FakeStrongFlag
from domain.models import AuctionTick
from expectation.benchmark import BenchmarkKey, PointInTimeExpectedAuction
from expectation.surprise import AuctionSurpriseResult
from market.asof import AsOfMarketView


class AuctionSurprisePercentile(Protocol):
    def percentile(
        self,
        market_view: AsOfMarketView,
        benchmark_key: BenchmarkKey,
        actual_gap: float,
        information_available_at: datetime,
    ) -> AuctionSurpriseResult:
        ...


@dataclass(frozen=True)
class FeatureSnapshot:
    ticker: str
    trade_date: str
    computed_as_of: datetime
    values: Mapping[str, Optional[float]]
    fake_strong_flags: Sequence[FakeStrongFlag]
    authenticity_state: AuthenticityState
    benchmark_status: BenchmarkStatus
    surprise_status: BenchmarkStatus


def _percentile_rank(value: Optional[float], population: Sequence[float]) -> Optional[float]:
    if value is None or not population:
        return None
    return sum(1 for item in population if item <= value) / len(population)


def _value_at_or_before(ticks: Sequence[AuctionTick], target: time) -> Optional[AuctionTick]:
    eligible = [tick for tick in ticks if tick.exchange_ts is not None and tick.exchange_ts.time() <= target]
    return eligible[-1] if eligible else None


class AuctionFeatureEngine:
    def __init__(self, thresholds: ThresholdRegistry, surprise: Optional[AuctionSurprisePercentile] = None):
        self.thresholds = thresholds
        self.surprise = surprise

    def compute(
        self,
        market_view: AsOfMarketView,
        ticker: str,
        expectation: PointInTimeExpectedAuction,
        liquidity_peer_group: Optional[str] = None,
        theme_peer_group: Optional[str] = None,
        global_peer_group: Optional[str] = None,
        height_peer_group: Optional[str] = None,
        theme_validated: bool = True,
    ) -> FeatureSnapshot:
        self._validate_expectation(market_view, ticker, expectation)
        ordered = market_view.get_ticks(ticker)
        if not ordered:
            return FeatureSnapshot(
                ticker,
                market_view.trade_date,
                market_view.as_of,
                {},
                (),
                AuthenticityState.UNKNOWN,
                expectation.distribution.status,
                BenchmarkStatus.INSUFFICIENT,
            )

        latest = ordered[-1]
        actual_gap = latest.gap_pct
        distribution = expectation.distribution
        expected_q50 = distribution.expected_gap_q50
        iqr = distribution.expected_gap_q75 - distribution.expected_gap_q25
        eg = None if actual_gap is None else actual_gap - expected_q50
        normalized_eg = None
        if eg is not None:
            epsilon = self.thresholds.get("normalized_eg_epsilon")
            normalized_eg = eg / max(abs(iqr), epsilon)
        surprise_result = AuctionSurpriseResult(
            None,
            0,
            distribution.minimum_sample_size,
            BenchmarkStatus.INSUFFICIENT,
        )
        if actual_gap is not None and self.surprise is not None:
            surprise_result = self.surprise.percentile(
                market_view,
                expectation.benchmark_key,
                actual_gap,
                expectation.information_available_at,
            )

        pre20 = tuple(tick for tick in ordered if tick.exchange_ts.time() < time(9, 20))  # type: ignore[union-attr]
        post20 = tuple(tick for tick in ordered if tick.exchange_ts.time() >= time(9, 20))  # type: ignore[union-attr]
        pre20_gaps = [tick.gap_pct for tick in pre20 if tick.gap_pct is not None]
        post20_gaps = [tick.gap_pct for tick in post20 if tick.gap_pct is not None]
        pre20_peak = max(pre20_gaps) if pre20_gaps else None
        at20_tick = _value_at_or_before(ordered, time(9, 20))
        at20_gap = at20_tick.gap_pct if at20_tick is not None else None
        pre20_decay = None if pre20_peak is None or at20_gap is None else pre20_peak - at20_gap
        first_post_gap = post20_gaps[0] if post20_gaps else None
        post20_delta = None if actual_gap is None or first_post_gap is None else actual_gap - first_post_gap
        post20_mdd = self._ordered_max_drawdown(post20_gaps)
        close_location = self._close_location(post20_gaps)
        recovery_ratio = self._recovery_ratio(post20_gaps)
        late30_delta, late30_slope = self._late_delta_slope(ordered, 30)
        late60_delta, late60_slope = self._late_delta_slope(ordered, 60)

        amount = latest.matched_amount
        volume = latest.matched_volume
        historical_amounts = market_view.get_historical_features(ticker, "AuctionAmount")
        historical_volumes = market_view.get_historical_features(ticker, "AuctionVolume")
        peer_amounts = market_view.get_peer_latest_values(liquidity_peer_group, "matched_amount") if liquidity_peer_group else ()
        peer_volumes = market_view.get_peer_latest_values(liquidity_peer_group, "matched_volume") if liquidity_peer_group else ()
        theme_peer_gaps = market_view.get_peer_latest_values(theme_peer_group, "gap_pct") if theme_peer_group else ()
        global_peer_gaps = market_view.get_peer_latest_values(global_peer_group, "gap_pct") if global_peer_group else ()
        height_peer_gaps = market_view.get_peer_latest_values(height_peer_group, "gap_pct") if height_peer_group else ()
        amount_hist_pct = _percentile_rank(amount, historical_amounts)
        volume_hist_pct = _percentile_rank(volume, historical_volumes)
        amount_peer_pct = _percentile_rank(amount, peer_amounts)
        volume_peer_pct = _percentile_rank(volume, peer_volumes)
        amount_ratio = self._ratio_to_median(amount, historical_amounts)
        volume_ratio = self._ratio_to_median(volume, historical_volumes)
        theme_rank = _percentile_rank(actual_gap, theme_peer_gaps)
        global_rank = _percentile_rank(actual_gap, global_peer_gaps)
        height_rank = _percentile_rank(actual_gap, height_peer_gaps)
        esr = None if pre20_peak in (None, 0) or actual_gap is None else actual_gap / pre20_peak

        flags = []
        if pre20_decay is not None and pre20_decay >= self.thresholds.get("pre20_mirage_decay_pct"):
            flags.append(FakeStrongFlag.PRE20_MIRAGE)
        decay_fraction = self._decay_fraction(post20_gaps)
        if post20_mdd is not None and post20_mdd >= self.thresholds.get("post20_decay_pct") and decay_fraction >= self.thresholds.get("continuous_decay_fraction"):
            flags.append(FakeStrongFlag.POST20_CONTINUOUS_DECAY)
        liquidity_pct = self._mean_available(amount_hist_pct, volume_hist_pct)
        if actual_gap is not None and actual_gap >= self.thresholds.get("price_strength_gap_pct") and liquidity_pct is not None and liquidity_pct < self.thresholds.get("liquidity_low_percentile"):
            flags.append(FakeStrongFlag.PRICE_WITHOUT_LIQUIDITY)
        if not theme_validated and theme_rank is not None and theme_rank >= 1.0 - self.thresholds.get("isolated_strength_rank_pct"):
            flags.append(FakeStrongFlag.ISOLATED_STRENGTH)
        if late30_delta is not None and late30_delta >= self.thresholds.get("last_second_spike_pct") and liquidity_pct is not None and liquidity_pct < self.thresholds.get("liquidity_low_percentile"):
            flags.append(FakeStrongFlag.LAST_SECOND_SPIKE)

        relative_rank = self._mean_available(theme_rank, global_rank, height_rank)
        funding_confirmed = self._funding_confirmed(post20)
        phase = auction_phase_at(market_view.as_of)
        authenticity = self._authenticity(
            phase,
            post20_mdd,
            recovery_ratio,
            late30_slope,
            funding_confirmed,
            relative_rank,
            flags,
        )
        values: Dict[str, Optional[float]] = {
            "ActualAuctionGap": actual_gap,
            "ExpectedGapQ50": expected_q50,
            "EG": eg,
            "NormalizedEG": normalized_eg,
            "AuctionSurprisePercentile": surprise_result.percentile,
            "BenchmarkEffectiveSampleSize": float(distribution.effective_sample_size),
            "BenchmarkMinimumSampleSize": float(distribution.minimum_sample_size),
            "SurpriseEffectiveSampleSize": float(surprise_result.effective_sample_size),
            "SurpriseMinimumSampleSize": float(surprise_result.minimum_sample_size),
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
            "FundingConfirmed": 1.0 if funding_confirmed else 0.0,
        }
        return FeatureSnapshot(
            ticker,
            market_view.trade_date,
            market_view.as_of,
            values,
            tuple(flags),
            authenticity,
            distribution.status,
            surprise_result.status,
        )

    @staticmethod
    def _validate_expectation(
        market_view: AsOfMarketView,
        ticker: str,
        expectation: PointInTimeExpectedAuction,
    ) -> None:
        if expectation.ticker != ticker or expectation.trade_date != market_view.trade_date:
            raise ValueError("expectation ticker/trade_date must match the market view")
        if expectation.generated_at > expectation.information_available_at:
            raise ValueError("expectation cannot be generated after its information cutoff")
        if not expectation.source_version:
            raise ValueError("expectation source_version is required")
        market_view.assert_timestamp_visible(expectation.information_available_at)

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
        return None if high == low else (values[-1] - low) / (high - low)

    @staticmethod
    def _recovery_ratio(values: Sequence[float]) -> Optional[float]:
        details = AuctionFeatureEngine._drawdown_details(values)
        if details is None:
            return None
        peak_index, trough_index, peak, trough = details
        if trough_index >= len(values) - 1:
            return 0.0
        return max(0.0, min(1.0, (values[-1] - trough) / (peak - trough)))

    @staticmethod
    def _drawdown_details(values: Sequence[float]) -> Optional[Tuple[int, int, float, float]]:
        if len(values) < 2:
            return None
        running_peak = values[0]
        running_peak_index = 0
        best = None
        best_drawdown = 0.0
        for index, value in enumerate(values[1:], start=1):
            drawdown = running_peak - value
            if drawdown > best_drawdown:
                best_drawdown = drawdown
                best = (running_peak_index, index, running_peak, value)
            if value > running_peak:
                running_peak = value
                running_peak_index = index
        return best

    @staticmethod
    def _late_delta_slope(ticks: Sequence[AuctionTick], seconds: int) -> Tuple[Optional[float], Optional[float]]:
        latest = ticks[-1]
        if latest.exchange_ts is None or latest.gap_pct is None:
            return None, None
        cutoff = latest.exchange_ts - timedelta(seconds=seconds)
        candidates = [tick for tick in ticks if tick.exchange_ts is not None and tick.exchange_ts <= cutoff and tick.gap_pct is not None]
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
        available = [value for value in values if value is not None]
        return sum(available) / len(available) if available else None

    @staticmethod
    def _ratio_to_median(value: Optional[float], history: Sequence[float]) -> Optional[float]:
        if value is None or not history:
            return None
        center = median(history)
        return value / center if center else None

    @staticmethod
    def _funding_confirmed(post20_ticks: Sequence[AuctionTick]) -> bool:
        gaps = [tick.gap_pct for tick in post20_ticks]
        if any(gap is None for gap in gaps):
            return False
        details = AuctionFeatureEngine._drawdown_details([float(gap) for gap in gaps])
        if details is None:
            return False
        _, trough_index, _, _ = details
        trough_tick = post20_ticks[trough_index]
        latest = post20_ticks[-1]
        amount_confirmed = (
            trough_tick.matched_amount is not None
            and latest.matched_amount is not None
            and latest.matched_amount > trough_tick.matched_amount
        )
        volume_confirmed = (
            trough_tick.matched_volume is not None
            and latest.matched_volume is not None
            and latest.matched_volume > trough_tick.matched_volume
        )
        return amount_confirmed and volume_confirmed

    def _authenticity(
        self,
        phase: AuctionPhase,
        drawdown: Optional[float],
        recovery: Optional[float],
        late_slope: Optional[float],
        funding_confirmed: bool,
        relative_rank: Optional[float],
        flags: Sequence[FakeStrongFlag],
    ) -> AuthenticityState:
        if phase in (AuctionPhase.PRE_AUCTION, AuctionPhase.SCOUTING):
            return AuthenticityState.UNKNOWN
        hard_fake = {
            FakeStrongFlag.POST20_CONTINUOUS_DECAY,
            FakeStrongFlag.PRICE_WITHOUT_LIQUIDITY,
            FakeStrongFlag.LAST_SECOND_SPIKE,
        }
        if hard_fake.intersection(flags):
            return AuthenticityState.FAKE_STRONG
        price_recovered = (
            drawdown is not None
            and recovery is not None
            and drawdown >= self.thresholds.get("healthy_min_drawdown_pct")
            and recovery >= self.thresholds.get("healthy_recovery_ratio")
        )
        if price_recovered:
            relative_ok = relative_rank is not None and relative_rank >= self.thresholds.get("healthy_min_relative_rank")
            if late_slope is not None and late_slope > 0 and funding_confirmed and relative_ok:
                return AuthenticityState.HEALTHY_DISAGREEMENT
            return AuthenticityState.HEALTHY_DISAGREEMENT_PENDING
        if FakeStrongFlag.PRE20_MIRAGE in flags or FakeStrongFlag.ISOLATED_STRENGTH in flags:
            return AuthenticityState.SUSPICIOUS
        return AuthenticityState.AUTHENTIC
