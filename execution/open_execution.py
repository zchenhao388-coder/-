from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional, Sequence, Tuple

from config.thresholds import ThresholdRegistry
from domain.context import night_plan_context_id
from domain.enums import (
    AuctionOpenConsistency,
    AuthenticityState,
    CandidateGrade,
    DataQualityState,
    ExecutionMode,
    ExecutionState,
    IdentityRecoveryState,
    LeadershipState,
    OpenPhase,
    SetupType,
    SignalTradability,
    ValidationState,
)
from domain.models import (
    DataQualityReport,
    DecisionTrace,
    NightPlan,
    OpenExecutionDecision,
    OpenExecutionPlan,
    OpenFeatureSnapshot,
    OpenTick,
    SetupResult,
)
from domain.reason_codes import ReasonCode
from market.asof import AsOfMarketView
from storage.hard_cancel import HardCancelStore


def open_phase_at(timestamp: datetime) -> OpenPhase:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("open execution timestamp must be timezone-aware")
    current = timestamp.time().replace(tzinfo=None)
    if current < time(9, 30):
        return OpenPhase.PRE_OPEN
    if current < time(9, 30, 15):
        return OpenPhase.OPEN_SHOCK
    if current < time(9, 31):
        return OpenPhase.OPEN_CONFIRM
    if current < time(9, 35):
        return OpenPhase.FOLLOW_THROUGH
    return OpenPhase.EXPIRED


class OpenExecutionPlanFactory:
    def create(
        self,
        night_plan: NightPlan,
        decision: DecisionTrace,
        setup_result: SetupResult,
        auction_price: Optional[float],
        auction_theme_rank: Optional[float],
    ) -> OpenExecutionPlan:
        is_a = decision.grade in (CandidateGrade.A1, CandidateGrade.A2)
        if is_a != (decision.execution_state == ExecutionState.ARMED):
            raise ValueError("09:25 A iff ARMED invariant is required before creating an open plan")
        if decision.grade == CandidateGrade.DROP or decision.grade.value.startswith("C_"):
            raise ValueError("C/DROP candidates do not receive OpenExecutionPlan")
        state = ExecutionState.ARMED if is_a else ExecutionState.WAITING
        expires_at = decision.decided_at.replace(hour=9, minute=35, second=0, microsecond=0)
        mode = self._mode(setup_result, is_a)
        triggers = (ReasonCode.OPEN_PRICE_HOLD.value, ReasonCode.OPEN_ACTIVE_BUY.value)
        hard_cancels = [
            ReasonCode.DATA_BROKEN.value,
            ReasonCode.OPEN_STRUCTURE_SEVERE_FAILURE.value,
            ReasonCode.SECTOR_HARD_FALSIFICATION.value,
            ReasonCode.REGULATORY_HARD_VETO.value,
        ]
        if setup_result.setup_type == SetupType.HIGH_BOARD:
            hard_cancels.extend((ReasonCode.LEADERSHIP_LOST.value, ReasonCode.HIGH_LEVEL_PANIC_EXPANSION.value))
        elif setup_result.setup_type == SetupType.WEAK_TO_STRONG:
            hard_cancels.extend((ReasonCode.WEAKNESS_REAPPEARS.value, ReasonCode.IDENTITY_RECOVERY_FAILED.value))
        upgrades = () if is_a else ("AUCTION_OPEN_CONSISTENCY_CONFIRMED", "SETUP_IDENTITY_CONFIRMED")
        return OpenExecutionPlan(
            setup_result.ticker,
            state,
            decision.decided_at,
            expires_at,
            triggers,
            tuple(hard_cancels),
            setup_type=setup_result.setup_type,
            auction_grade=decision.grade,
            execution_mode=mode,
            auction_price=auction_price,
            auction_theme_rank=auction_theme_rank,
            must_hold=("MARKET_PERMISSION", "SETUP_PERMISSION", "SIGNAL_VALID"),
            positive_triggers=triggers,
            soft_invalid=(ReasonCode.NO_CHASE.value,),
            upgrade_requirements=upgrades,
            context_id=night_plan_context_id(night_plan),
        )

    @staticmethod
    def _mode(setup: SetupResult, is_a: bool) -> ExecutionMode:
        if setup.setup_type == SetupType.ONE_TO_TWO:
            if setup.authenticity_state == AuthenticityState.HEALTHY_DISAGREEMENT:
                return ExecutionMode.PULLBACK_RECLAIM
            return ExecutionMode.OPEN_CONTINUATION if is_a else ExecutionMode.BREAKOUT_CONFIRM
        if setup.setup_type == SetupType.HIGH_BOARD:
            if setup.leadership_state == LeadershipState.CHALLENGED:
                return ExecutionMode.LEADERSHIP_RETAKE
            if setup.authenticity_state == AuthenticityState.HEALTHY_DISAGREEMENT:
                return ExecutionMode.LEADER_DIVERGENCE_RECLAIM
            return ExecutionMode.LEADER_CONTINUATION
        if setup.identity_recovery != IdentityRecoveryState.TRUE:
            return ExecutionMode.IDENTITY_BREAKOUT
        if setup.authenticity_state == AuthenticityState.HEALTHY_DISAGREEMENT:
            return ExecutionMode.REPAIR_PULLBACK_RECLAIM
        return ExecutionMode.REPAIR_CONTINUATION


class OpenFeatureEngine:
    def compute(
        self,
        market_view: AsOfMarketView,
        ticker: str,
        auction_price: Optional[float],
        auction_theme_rank: Optional[float],
        theme_peer_group: str = "theme_open",
    ) -> OpenFeatureSnapshot:
        ticks = market_view.get_open_ticks(ticker)
        if not ticks:
            return OpenFeatureSnapshot(ticker, market_view.trade_date, market_view.as_of, {})
        latest = ticks[-1]
        last_price = latest.last_price
        vwap30 = self._vwap(ticks, 30)
        vwap60 = self._vwap(ticks, 60)
        open_return = self._pct_change(last_price, ticks[0].open_price or ticks[0].last_price)
        current_theme_rank = self._theme_rank(market_view, theme_peer_group, open_return)
        values = {
            "LastPrice": last_price,
            "OpenReturn": open_return,
            "Return5s": self._return(ticks, 5),
            "Return15s": self._return(ticks, 15),
            "Return30s": self._return(ticks, 30),
            "Return60s": self._return(ticks, 60),
            "MDD15s": self._mdd(ticks, 15),
            "MDD60s": self._mdd(ticks, 60),
            "Recovery30s": self._recovery(ticks, 30),
            "Recovery60s": self._recovery(ticks, 60),
            "VWAP30s": vwap30,
            "VWAP60s": vwap60,
            "PriceVsAuction": self._pct_change(last_price, auction_price),
            "PriceVsVWAP30s": self._pct_change(last_price, vwap30),
            "PriceVsVWAP60s": self._pct_change(last_price, vwap60),
            "ThemeRankChange": None if current_theme_rank is None or auction_theme_rank is None else current_theme_rank - auction_theme_rank,
            "PriceExtensionFromAuction": self._pct_change(last_price, auction_price),
            "TradeAmount": latest.trade_amount,
            "TradeVolume": latest.trade_volume,
        }
        return OpenFeatureSnapshot(ticker, market_view.trade_date, market_view.as_of, values)

    @staticmethod
    def _return(ticks: Sequence[OpenTick], seconds: int) -> Optional[float]:
        latest = ticks[-1]
        if latest.exchange_ts is None or latest.last_price is None:
            return None
        cutoff = latest.exchange_ts - timedelta(seconds=seconds)
        eligible = tuple(tick for tick in ticks if tick.exchange_ts is not None and tick.exchange_ts <= cutoff and tick.last_price is not None)
        if not eligible or eligible[-1].last_price in (None, 0):
            return None
        return (latest.last_price / eligible[-1].last_price - 1.0) * 100.0  # type: ignore[operator]

    @staticmethod
    def _window(ticks: Sequence[OpenTick], seconds: int) -> Tuple[OpenTick, ...]:
        latest = ticks[-1]
        if latest.exchange_ts is None:
            return ()
        cutoff = latest.exchange_ts - timedelta(seconds=seconds)
        return tuple(tick for tick in ticks if tick.exchange_ts is not None and tick.exchange_ts >= cutoff)

    @classmethod
    def _mdd(cls, ticks: Sequence[OpenTick], seconds: int) -> Optional[float]:
        prices = [tick.last_price for tick in cls._window(ticks, seconds) if tick.last_price is not None]
        if not prices:
            return None
        peak = prices[0]
        drawdown = 0.0
        for price in prices[1:]:
            drawdown = max(drawdown, (peak - price) / peak * 100.0 if peak else 0.0)
            peak = max(peak, price)
        return drawdown

    @classmethod
    def _recovery(cls, ticks: Sequence[OpenTick], seconds: int) -> Optional[float]:
        prices = [tick.last_price for tick in cls._window(ticks, seconds) if tick.last_price is not None]
        if len(prices) < 2:
            return None
        peak = prices[0]
        peak_index = 0
        best = None
        best_drawdown = 0.0
        for index, price in enumerate(prices[1:], start=1):
            if peak - price > best_drawdown:
                best_drawdown = peak - price
                best = (peak_index, index, peak, price)
            if price > peak:
                peak, peak_index = price, index
        if best is None:
            return None
        _, trough_index, peak, trough = best
        if trough_index == len(prices) - 1 or peak == trough:
            return 0.0
        return max(0.0, min(1.0, (prices[-1] - trough) / (peak - trough)))

    @classmethod
    def _vwap(cls, ticks: Sequence[OpenTick], seconds: int) -> Optional[float]:
        window = cls._window(ticks, seconds)
        amount = sum(tick.trade_amount for tick in window if tick.trade_amount is not None)
        volume = sum(tick.trade_volume for tick in window if tick.trade_volume is not None)
        return amount / volume if volume else None

    @staticmethod
    def _pct_change(value: Optional[float], reference: Optional[float]) -> Optional[float]:
        return None if value is None or reference in (None, 0) else (value / reference - 1.0) * 100.0

    @staticmethod
    def _theme_rank(market_view: AsOfMarketView, group: str, value: Optional[float]) -> Optional[float]:
        population = []
        for ticks in market_view.get_open_peer_ticks(group).values():
            if not ticks:
                continue
            start = ticks[0].open_price or ticks[0].last_price
            latest = ticks[-1].last_price
            peer_return = OpenFeatureEngine._pct_change(latest, start)
            if peer_return is not None:
                population.append(peer_return)
        if value is None or not population:
            return None
        return sum(1 for item in population if item <= value) / len(population)


@dataclass(frozen=True)
class OpenValidationInput:
    plan: OpenExecutionPlan
    night_plan: NightPlan
    market_view: AsOfMarketView
    data_quality: DataQualityReport
    regulatory_validation: ValidationState
    sector_validation: ValidationState
    leadership_state: LeadershipState = LeadershipState.UNKNOWN
    weakness_reappeared: bool = False
    identity_recovery_failed: bool = False
    high_level_panic: bool = False
    upgrade_requirements_met: bool = False


class OpenExecutionEngine:
    def __init__(self, thresholds: ThresholdRegistry, hard_cancel_store: HardCancelStore):
        self.thresholds = thresholds
        self.hard_cancel_store = hard_cancel_store
        self.features = OpenFeatureEngine()

    def evaluate(self, request: OpenValidationInput) -> OpenExecutionDecision:
        self._validate(request)
        plan = request.plan
        view = request.market_view
        context_id = night_plan_context_id(request.night_plan)
        key = (request.night_plan.trade_date, plan.ticker, context_id)
        features = self.features.compute(view, plan.ticker, plan.auction_price, plan.auction_theme_rank)
        phase = open_phase_at(view.as_of)

        if self.hard_cancel_store.contains(*key):
            return self._decision(plan, view, phase, ExecutionState.CANCELLED, AuctionOpenConsistency.FALSIFIED, SignalTradability.INVALID, features, (ReasonCode.HARD_CANCEL_STICKY,))
        if plan.state in (ExecutionState.EXECUTE, ExecutionState.CANCELLED, ExecutionState.EXPIRED):
            consistency = AuctionOpenConsistency.CONFIRMED if plan.state == ExecutionState.EXECUTE else AuctionOpenConsistency.FALSIFIED
            tradability = SignalTradability.VALID_AND_TRADABLE if plan.state == ExecutionState.EXECUTE else SignalTradability.INVALID
            return self._decision(plan, view, phase, plan.state, consistency, tradability, features, ())
        if phase == OpenPhase.EXPIRED:
            return self._decision(plan, view, phase, ExecutionState.EXPIRED, AuctionOpenConsistency.PARTIAL, SignalTradability.INVALID, features, (ReasonCode.OPEN_SIGNAL_EXPIRED,))

        hard_reason = self._hard_cancel_reason(request, features)
        if hard_reason is not None:
            self.hard_cancel_store.record(*key, hard_reason.value)
            return self._decision(plan, view, phase, ExecutionState.CANCELLED, AuctionOpenConsistency.FALSIFIED, SignalTradability.INVALID, features, (hard_reason,))

        consistency = self._consistency(request, features)
        tradability = self._tradability(consistency, features)
        reasons = []
        price_vs_auction = features.values.get("PriceVsAuction")
        if price_vs_auction is not None and price_vs_auction >= self.thresholds.get("open_hold_return_min_pct"):
            reasons.append(ReasonCode.OPEN_PRICE_HOLD)
        if features.values.get("TradeAmount") is not None and features.values["TradeAmount"] >= self.thresholds.get("open_active_amount_min"):  # type: ignore[operator]
            reasons.append(ReasonCode.OPEN_ACTIVE_BUY)

        if request.data_quality.state == DataQualityState.DEGRADED:
            return self._decision(plan, view, phase, ExecutionState.WAIT, consistency, tradability, features, tuple(reasons))
        if tradability == SignalTradability.VALID_BUT_EXTENDED:
            reasons.append(ReasonCode.NO_CHASE)
            return self._decision(plan, view, phase, ExecutionState.WAIT, consistency, tradability, features, tuple(reasons))
        if consistency == AuctionOpenConsistency.FALSIFIED or tradability == SignalTradability.INVALID:
            self.hard_cancel_store.record(*key, ReasonCode.OPEN_STRUCTURE_SEVERE_FAILURE.value)
            return self._decision(plan, view, phase, ExecutionState.CANCELLED, consistency, tradability, features, (ReasonCode.OPEN_STRUCTURE_SEVERE_FAILURE,))
        if plan.state == ExecutionState.WAITING and not request.upgrade_requirements_met:
            reasons.append(ReasonCode.UPGRADE_REQUIREMENT_PENDING)
            return self._decision(plan, view, phase, ExecutionState.WAIT, consistency, tradability, features, tuple(reasons))
        if consistency == AuctionOpenConsistency.CONFIRMED and tradability == SignalTradability.VALID_AND_TRADABLE:
            return self._decision(plan, view, phase, ExecutionState.EXECUTE, consistency, tradability, features, tuple(reasons))
        return self._decision(plan, view, phase, ExecutionState.WAIT, consistency, tradability, features, tuple(reasons))

    def _consistency(self, request: OpenValidationInput, features: OpenFeatureSnapshot) -> AuctionOpenConsistency:
        values = features.values
        price_hold = values.get("PriceVsAuction")
        return15 = values.get("Return15s")
        active = values.get("TradeAmount")
        if request.sector_validation in (ValidationState.FALSIFIED, ValidationState.INVALID, ValidationState.HARD_INVALID):
            return AuctionOpenConsistency.FALSIFIED
        if price_hold is not None and price_hold <= self.thresholds.get("open_dump_return_max_pct"):
            return AuctionOpenConsistency.FALSIFIED
        confirmed = (
            price_hold is not None
            and price_hold >= self.thresholds.get("open_hold_return_min_pct")
            and return15 is not None
            and return15 >= self.thresholds.get("open_confirm_return_min_pct")
            and active is not None
            and active >= self.thresholds.get("open_active_amount_min")
            and request.sector_validation == ValidationState.VALID
        )
        if confirmed:
            return AuctionOpenConsistency.CONFIRMED
        return AuctionOpenConsistency.PARTIAL

    def _tradability(self, consistency: AuctionOpenConsistency, features: OpenFeatureSnapshot) -> SignalTradability:
        if consistency == AuctionOpenConsistency.FALSIFIED or not features.values:
            return SignalTradability.INVALID
        extension = features.values.get("PriceExtensionFromAuction")
        if extension is not None and extension >= self.thresholds.get("open_extension_max_pct"):
            return SignalTradability.VALID_BUT_EXTENDED
        return SignalTradability.VALID_AND_TRADABLE

    def _hard_cancel_reason(self, request: OpenValidationInput, features: OpenFeatureSnapshot) -> Optional[ReasonCode]:
        if request.data_quality.state == DataQualityState.BROKEN:
            return ReasonCode.DATA_BROKEN
        if request.regulatory_validation in (ValidationState.FALSIFIED, ValidationState.INVALID, ValidationState.HARD_INVALID):
            return ReasonCode.REGULATORY_HARD_VETO
        if request.sector_validation in (ValidationState.FALSIFIED, ValidationState.INVALID, ValidationState.HARD_INVALID):
            return ReasonCode.SECTOR_HARD_FALSIFICATION
        if request.plan.setup_type == SetupType.HIGH_BOARD:
            if request.leadership_state == LeadershipState.LOST:
                return ReasonCode.LEADERSHIP_LOST
            if request.high_level_panic:
                return ReasonCode.HIGH_LEVEL_PANIC_EXPANSION
        if request.plan.setup_type == SetupType.WEAK_TO_STRONG:
            if request.weakness_reappeared:
                return ReasonCode.WEAKNESS_REAPPEARS
            if request.identity_recovery_failed:
                return ReasonCode.IDENTITY_RECOVERY_FAILED
        price_vs_auction = features.values.get("PriceVsAuction")
        if price_vs_auction is not None and price_vs_auction <= self.thresholds.get("open_dump_return_max_pct"):
            return ReasonCode.OPEN_STRUCTURE_SEVERE_FAILURE
        return None

    @staticmethod
    def _validate(request: OpenValidationInput) -> None:
        if request.market_view.trade_date != request.night_plan.trade_date:
            raise ValueError("open view and NightPlan trade_date must match")
        expected_context = night_plan_context_id(request.night_plan)
        if request.plan.context_id != expected_context:
            raise ValueError("OpenExecutionPlan context does not match NightPlan")
        if request.market_view.as_of < request.plan.valid_from:
            raise ValueError("open view precedes plan validity")

    @staticmethod
    def _decision(plan, view, phase, state, consistency, tradability, features, reasons) -> OpenExecutionDecision:
        ticks = view.get_open_ticks(plan.ticker)
        price = ticks[-1].last_price if ticks else None
        return OpenExecutionDecision(
            plan.ticker,
            view.as_of,
            phase,
            state,
            consistency,
            tradability,
            features,
            tuple(reason.value if isinstance(reason, ReasonCode) else str(reason) for reason in reasons),
            price if state == ExecutionState.EXECUTE else None,
        )
