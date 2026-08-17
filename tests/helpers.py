from datetime import datetime
from typing import Mapping, Optional, Sequence

from auction.features import FeatureSnapshot
from domain.enums import AuthenticityState, BenchmarkStatus, SetupType
from domain.models import AuctionTick, NightPlan
from expectation.benchmark import BenchmarkKey, ExpectedAuctionDistribution, PointInTimeExpectedAuction
from market.asof import AsOfMarketView, HistoricalFeatureRecord


def ts(value: str) -> datetime:
    return datetime.fromisoformat(f"2026-08-14T{value}+08:00")


def make_ticks(points: Sequence[tuple], ticker: str = "000001.SZ"):
    result = []
    for index, (clock, gap) in enumerate(points, start=1):
        exchange = ts(clock)
        result.append(AuctionTick(
            exchange_ts=exchange,
            receive_ts=exchange,
            ticker=ticker,
            virtual_price=10.0 * (1 + gap / 100.0),
            gap_pct=gap,
            matched_volume=100_000.0 * index,
            matched_amount=1_000_000.0 * index,
            source="TEST",
        ))
    return result


def make_expectation(
    ticker: str = "000001.SZ",
    trade_date: str = "2026-08-14",
    q25: float = 2.0,
    q50: float = 3.0,
    q75: float = 4.0,
) -> PointInTimeExpectedAuction:
    distribution = ExpectedAuctionDistribution(
        q25 - 1.0,
        q25,
        q50,
        q75,
        q75 + 1.0,
        {"q10": 100.0, "q25": 200.0, "q50": 300.0, "q75": 400.0, "q90": 500.0},
        {"q10": 10.0, "q25": 20.0, "q50": 30.0, "q75": 40.0, "q90": 50.0},
        20,
        .9,
        .8,
        100,
        .5,
        20,
        5,
        BenchmarkStatus.SUFFICIENT,
    )
    return PointInTimeExpectedAuction(
        ticker,
        trade_date,
        datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
        datetime.fromisoformat("2026-08-13T23:59:59+08:00"),
        BenchmarkKey("ONE_TO_TWO", "RISK_ON", "MAIN", "LEADER", "H1", "CHANGE_HAND"),
        "TEST_EXPECTATION_V1",
        distribution,
    )


def make_view(
    ticks: Sequence[AuctionTick],
    as_of: Optional[datetime] = None,
    ticker: str = "000001.SZ",
    include_history: bool = True,
    include_peers: bool = True,
) -> AsOfMarketView:
    boundary = as_of or max(tick.exchange_ts for tick in ticks if tick.exchange_ts is not None)
    history = ()
    if include_history:
        available = datetime.fromisoformat("2026-08-13T18:00:00+08:00")
        history = (
            HistoricalFeatureRecord(ticker, "AuctionAmount", 10_000.0, "2026-08-12", available, "TEST"),
            HistoricalFeatureRecord(ticker, "AuctionAmount", 20_000.0, "2026-08-13", available, "TEST"),
            HistoricalFeatureRecord(ticker, "AuctionVolume", 1_000.0, "2026-08-12", available, "TEST"),
            HistoricalFeatureRecord(ticker, "AuctionVolume", 2_000.0, "2026-08-13", available, "TEST"),
        )
    groups = {
        "liquidity": (ticker,),
        "theme": (ticker,),
        "global": (ticker,),
        "height": (ticker,),
    } if include_peers else {}
    return AsOfMarketView(
        trade_date="2026-08-14",
        as_of=boundary,
        ticks=ticks,
        peer_groups=groups,
        historical_features=history,
    )


def make_snapshot(
    values: Mapping[str, Optional[float]],
    authenticity: AuthenticityState = AuthenticityState.AUTHENTIC,
    as_of: Optional[datetime] = None,
    ticker: str = "000001.SZ",
    benchmark_status: BenchmarkStatus = BenchmarkStatus.SUFFICIENT,
    surprise_status: BenchmarkStatus = BenchmarkStatus.SUFFICIENT,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        ticker,
        "2026-08-14",
        as_of or ts("09:25:00"),
        values,
        (),
        authenticity,
        benchmark_status,
        surprise_status,
    )


def make_night_plan(in_pool: bool = True) -> NightPlan:
    pool = ("000001.SZ",) if in_pool else ("000002.SZ",)
    return NightPlan(
        "2026-08-14",
        pool,
        {ticker: SetupType.ONE_TO_TWO for ticker in pool},
        datetime.fromisoformat("2026-08-13T20:00:00+08:00"),
        datetime.fromisoformat("2026-08-13T23:59:59+08:00"),
    )
