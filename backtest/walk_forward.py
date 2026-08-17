from dataclasses import dataclass
from datetime import date, datetime, time
from statistics import mean
from typing import Callable, Mapping, Optional, Protocol, Sequence, Tuple

from domain.enums import OutcomeClass, SetupType


@dataclass(frozen=True)
class BacktestObservation:
    observation_id: str
    trade_date: str
    ticker: str
    setup_type: SetupType
    features: Mapping[str, float]
    features_available_at: datetime
    label_available_at: datetime
    strategy_return: float
    outcome_class: OutcomeClass

    def __post_init__(self) -> None:
        forbidden = {"label", "outcome", "strategy_return", "future_return"}
        if forbidden.intersection(self.features):
            raise ValueError("labels/outcomes are forbidden from backtest features")
        if self.features_available_at.tzinfo is None or self.label_available_at.tzinfo is None:
            raise ValueError("backtest availability timestamps must be timezone-aware")
        if self.features_available_at >= self.label_available_at:
            raise ValueError("feature availability must precede label availability")
        if self.features_available_at.date() > date.fromisoformat(self.trade_date):
            raise ValueError("features cannot become available after the observation trade_date")


@dataclass(frozen=True)
class WalkForwardConfig:
    train_months: int = 6
    validation_months: int = 1
    forward_months: int = 1
    step_months: int = 1

    def __post_init__(self) -> None:
        if min(self.train_months, self.validation_months, self.forward_months, self.step_months) <= 0:
            raise ValueError("walk-forward month lengths must be positive")


@dataclass(frozen=True)
class WalkForwardWindow:
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    forward_start: date
    forward_end: date


@dataclass(frozen=True)
class OutOfSampleMetrics:
    sample_size: int
    selected_count: int
    hit_rate: Optional[float]
    average_return: Optional[float]
    fast_failure_rate: Optional[float]


@dataclass(frozen=True)
class WalkForwardResult:
    window: WalkForwardWindow
    train_sample_size: int
    validation: OutOfSampleMetrics
    forward: OutOfSampleMetrics


class PredictionModel(Protocol):
    def __call__(self, features: Mapping[str, float]) -> bool:
        ...


class WalkForwardSplitter:
    def __init__(self, config: WalkForwardConfig):
        self.config = config

    def split(self, observations: Sequence[BacktestObservation]) -> Tuple[WalkForwardWindow, ...]:
        if not observations:
            return ()
        first = min(date.fromisoformat(item.trade_date) for item in observations).replace(day=1)
        last_exclusive = self._add_months(max(date.fromisoformat(item.trade_date) for item in observations).replace(day=1), 1)
        windows = []
        train_start = first
        while True:
            train_end = self._add_months(train_start, self.config.train_months)
            validation_end = self._add_months(train_end, self.config.validation_months)
            forward_end = self._add_months(validation_end, self.config.forward_months)
            if forward_end > last_exclusive:
                break
            windows.append(WalkForwardWindow(train_start, train_end, train_end, validation_end, validation_end, forward_end))
            train_start = self._add_months(train_start, self.config.step_months)
        return tuple(windows)

    @staticmethod
    def _add_months(value: date, months: int) -> date:
        offset = value.year * 12 + value.month - 1 + months
        return date(offset // 12, offset % 12 + 1, 1)


class PointInTimeDataset:
    def __init__(self, observations: Sequence[BacktestObservation]):
        ids = [item.observation_id for item in observations]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate backtest observation_id")
        self.observations = tuple(sorted(observations, key=lambda item: (item.trade_date, item.ticker)))

    def select(self, start: date, end: date, label_cutoff: date) -> Tuple[BacktestObservation, ...]:
        selected = []
        for item in self.observations:
            item_date = date.fromisoformat(item.trade_date)
            cutoff = datetime.combine(label_cutoff, time.min, tzinfo=item.label_available_at.tzinfo)
            if start <= item_date < end and item.label_available_at < cutoff:
                selected.append(item)
        return tuple(selected)


class WalkForwardBacktester:
    """Chronological train/validation/forward evaluation; never random-splits."""

    def __init__(self, splitter: WalkForwardSplitter):
        self.splitter = splitter

    def run(
        self,
        observations: Sequence[BacktestObservation],
        trainer: Callable[[Sequence[BacktestObservation]], PredictionModel],
    ) -> Tuple[WalkForwardResult, ...]:
        dataset = PointInTimeDataset(observations)
        results = []
        for window in self.splitter.split(observations):
            train = dataset.select(window.train_start, window.train_end, window.validation_start)
            validation = dataset.select(window.validation_start, window.validation_end, window.forward_start)
            forward = dataset.select(window.forward_start, window.forward_end, window.forward_end)
            model = trainer(train)
            results.append(WalkForwardResult(
                window,
                len(train),
                self._metrics(validation, model),
                self._metrics(forward, model),
            ))
        return tuple(results)

    @staticmethod
    def _metrics(observations: Sequence[BacktestObservation], model: PredictionModel) -> OutOfSampleMetrics:
        selected = tuple(item for item in observations if model(dict(item.features)))
        if not selected:
            return OutOfSampleMetrics(len(observations), 0, None, None, None)
        returns = tuple(item.strategy_return for item in selected)
        fast_failures = sum(1 for item in selected if item.outcome_class == OutcomeClass.FAST_FAILURE)
        return OutOfSampleMetrics(
            len(observations),
            len(selected),
            sum(1 for value in returns if value > 0) / len(returns),
            mean(returns),
            fast_failures / len(selected),
        )
