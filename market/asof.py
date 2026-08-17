from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from domain.models import AuctionTick, MarketContext, OpenTick
from replay.clock import FutureDataAccessError


@dataclass(frozen=True)
class HistoricalFeatureRecord:
    ticker: str
    name: str
    value: float
    trade_date: str
    available_at: datetime
    source: str


@dataclass(frozen=True)
class PrecomputedFeatureRecord:
    ticker: str
    name: str
    value: float
    available_at: datetime


@dataclass(frozen=True)
class MarketContextRecord:
    context: MarketContext
    available_at: datetime


class AsOfMarketView:
    """The only strategy-facing, point-in-time market-data boundary.

    The view may be built over a full replay dataset, but every public accessor
    enforces the view's immutable ``as_of`` boundary. Core engines accept this
    object rather than arbitrary tick or precomputed-feature sequences.
    """

    def __init__(
        self,
        trade_date: str,
        as_of: datetime,
        ticks: Sequence[AuctionTick] = (),
        peer_groups: Optional[Mapping[str, Sequence[str]]] = None,
        historical_features: Sequence[HistoricalFeatureRecord] = (),
        expectation_observations: Sequence[Any] = (),
        auction_gap_observations: Sequence[Any] = (),
        market_context_records: Sequence[MarketContextRecord] = (),
        precomputed_features: Sequence[PrecomputedFeatureRecord] = (),
        open_ticks: Sequence[OpenTick] = (),
    ):
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("AsOfMarketView requires a timezone-aware as_of")
        if as_of.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != trade_date:
            raise ValueError("AsOfMarketView trade_date must equal as_of date")
        date.fromisoformat(trade_date)
        for tick in (*ticks, *open_ticks):
            for timestamp in (tick.exchange_ts, tick.receive_ts):
                if timestamp is not None and (timestamp.tzinfo is None or timestamp.utcoffset() is None):
                    raise ValueError("market ticks require timezone-aware timestamps")
        self.trade_date = trade_date
        self.as_of = as_of
        self.__ticks = tuple(ticks)
        self.__peer_groups = {name: tuple(tickers) for name, tickers in (peer_groups or {}).items()}
        self.__historical_features = tuple(historical_features)
        self.__expectation_observations = tuple(expectation_observations)
        self.__auction_gap_observations = tuple(auction_gap_observations)
        self.__market_context_records = tuple(market_context_records)
        self.__precomputed_features = tuple(precomputed_features)
        self.__open_ticks = tuple(open_ticks)

    def at(self, as_of: datetime) -> "AsOfMarketView":
        if as_of > self.as_of:
            raise FutureDataAccessError(f"requested view at {as_of!r}; boundary is {self.as_of!r}")
        return AsOfMarketView(
            trade_date=self.trade_date,
            as_of=as_of,
            ticks=self.__ticks,
            peer_groups=self.__peer_groups,
            historical_features=self.__historical_features,
            expectation_observations=self.__expectation_observations,
            auction_gap_observations=self.__auction_gap_observations,
            market_context_records=self.__market_context_records,
            precomputed_features=self.__precomputed_features,
            open_ticks=self.__open_ticks,
        )

    def get_ticks(self, ticker: str) -> Tuple[AuctionTick, ...]:
        visible = (
            tick for tick in self.__ticks
            if tick.ticker == ticker
            and tick.exchange_ts is not None
            and tick.receive_ts is not None
            and tick.exchange_ts <= self.as_of
            and tick.receive_ts <= self.as_of
        )
        return tuple(sorted(visible, key=lambda tick: tick.exchange_ts))  # type: ignore[arg-type]

    def get_probe_ticks(self, ticker: str, source: Optional[str] = None) -> Tuple[AuctionTick, ...]:
        """Return receive-time-bounded probe data that is never execution-grade.

        This accessor exists for field validation and cross-source monitoring.
        Strategy features continue to use ``get_ticks``, which requires both a
        reliable exchange timestamp and a receive timestamp.
        """
        visible = (
            tick for tick in self.__ticks
            if tick.ticker == ticker
            and (source is None or tick.source == source)
            and tick.receive_ts is not None
            and tick.receive_ts <= self.as_of
            and (tick.exchange_ts is None or tick.exchange_ts <= self.as_of)
        )
        return tuple(sorted(visible, key=lambda tick: tick.receive_ts))  # type: ignore[arg-type]

    def get_peer_ticks(self, group: str) -> Mapping[str, Tuple[AuctionTick, ...]]:
        return {ticker: self.get_ticks(ticker) for ticker in self.__peer_groups.get(group, ())}

    def get_peer_latest_values(self, group: str, field: str) -> Tuple[float, ...]:
        result = []
        for ticks in self.get_peer_ticks(group).values():
            if not ticks:
                continue
            value = getattr(ticks[-1], field)
            if value is not None:
                result.append(float(value))
        return tuple(result)

    def get_open_ticks(self, ticker: str) -> Tuple[OpenTick, ...]:
        visible = (
            tick for tick in self.__open_ticks
            if tick.ticker == ticker
            and tick.exchange_ts is not None
            and tick.receive_ts is not None
            and tick.exchange_ts <= self.as_of
            and tick.receive_ts <= self.as_of
        )
        return tuple(sorted(visible, key=lambda tick: tick.exchange_ts))  # type: ignore[arg-type]

    def get_open_peer_latest_values(self, group: str, field: str) -> Tuple[float, ...]:
        result = []
        for ticker in self.__peer_groups.get(group, ()):
            ticks = self.get_open_ticks(ticker)
            if not ticks:
                continue
            value = getattr(ticks[-1], field)
            if value is not None:
                result.append(float(value))
        return tuple(result)

    def get_open_peer_ticks(self, group: str) -> Mapping[str, Tuple[OpenTick, ...]]:
        return {ticker: self.get_open_ticks(ticker) for ticker in self.__peer_groups.get(group, ())}

    def get_historical_features(self, ticker: str, name: str) -> Tuple[float, ...]:
        return tuple(
            record.value for record in self.__historical_features
            if record.ticker == ticker
            and record.name == name
            and date.fromisoformat(record.trade_date) < date.fromisoformat(self.trade_date)
            and record.available_at <= self.as_of
        )

    def get_expectation_observations(self, available_at: Optional[datetime] = None) -> Tuple[Any, ...]:
        cutoff = self._bounded_cutoff(available_at)
        return tuple(
            item for item in self.__expectation_observations
            if self._availability(item) is not None and self._availability(item) <= cutoff
        )

    def get_auction_gap_observations(self, available_at: Optional[datetime] = None) -> Tuple[Any, ...]:
        cutoff = self._bounded_cutoff(available_at)
        return tuple(
            item for item in self.__auction_gap_observations
            if self._availability(item) is not None and self._availability(item) <= cutoff
        )

    def get_market_context(self) -> Optional[MarketContext]:
        eligible = tuple(record for record in self.__market_context_records if record.available_at <= self.as_of)
        if not eligible:
            return None
        return max(eligible, key=lambda record: record.available_at).context

    def get_precomputed_feature(self, ticker: str, name: str) -> Optional[float]:
        eligible = tuple(
            record for record in self.__precomputed_features
            if record.ticker == ticker and record.name == name and record.available_at <= self.as_of
        )
        if not eligible:
            return None
        return max(eligible, key=lambda record: record.available_at).value

    def assert_timestamp_visible(self, timestamp: datetime) -> None:
        if timestamp > self.as_of:
            raise FutureDataAccessError(f"data at {timestamp!r} is not visible at {self.as_of!r}")

    def _bounded_cutoff(self, requested: Optional[datetime]) -> datetime:
        if requested is None:
            return self.as_of
        self.assert_timestamp_visible(requested)
        return requested

    @staticmethod
    def _availability(item: Any) -> Optional[datetime]:
        return getattr(item, "available_at", None) or getattr(item, "label_available_at", None)
