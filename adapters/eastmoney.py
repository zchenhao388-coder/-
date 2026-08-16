import json
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from domain.models import AuctionTick

from .base import BaseAdapter, DataCapability


class AdapterResponseError(RuntimeError):
    pass


class EastmoneySnapshotAdapter(BaseAdapter):
    """Experimental public snapshot adapter for field availability probes."""

    ENDPOINT = "https://push2delay.eastmoney.com/api/qt/stock/get"
    FIELDS = "f43,f47,f48,f57,f58,f60,f71,f86,f124"

    def __init__(
        self,
        opener: Optional[Callable[[Request, float], bytes]] = None,
        timeout_seconds: float = 10.0,
        receive_clock: Optional[Callable[[], datetime]] = None,
    ):
        self.timeout_seconds = timeout_seconds
        self._opener = opener or self._open
        self._receive_clock = receive_clock or (lambda: datetime.now(timezone.utc))

    @property
    def capability(self) -> DataCapability:
        return DataCapability(
            adapter_name="eastmoney_snapshot_experimental",
            core_fields={
                "exchange_ts": True,
                "receive_ts": True,
                "ticker": True,
                "virtual_price": True,
                "gap_pct": True,
                "matched_volume": True,
                "matched_amount": True,
            },
            enhanced_fields={
                "unmatched_side": False,
                "unmatched_volume": False,
                "bid": False,
                "ask": False,
                "orderbook": False,
            },
            field_provenance={
                "exchange_ts": "f86 provider quote timestamp",
                "receive_ts": "local UTC receive clock",
                "virtual_price": "f43; interpretation is phase-dependent",
                "gap_pct": "derived from f43 and f60",
                "matched_volume": "f47 converted from lots to shares",
                "matched_amount": "f48",
            },
            timestamp_semantics="f86 provider quote timestamp; exchange-origin not contractually documented",
            auction_semantics_verified=False,
            notes=(
                "Public undocumented endpoint; schema may change without notice.",
                "Weekend/after-close probe verifies field shape only.",
                "09:15-09:25 virtual-price and virtual-match semantics require a trading-day capture.",
                "Do not enable production execution from this adapter.",
            ),
        )

    def load(
        self,
        tickers: Sequence[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[AuctionTick]:
        for ticker in tickers:
            tick = self.fetch_one(ticker)
            if tick.exchange_ts is None:
                yield tick
                continue
            if start is not None and tick.exchange_ts < start:
                continue
            if end is not None and tick.exchange_ts > end:
                continue
            yield tick

    def fetch_one(self, ticker: str) -> AuctionTick:
        canonical = self._canonical_ticker(ticker)
        query = urlencode({"secid": self._secid(canonical), "fields": self.FIELDS})
        request = Request(
            f"{self.ENDPOINT}?{query}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
        )
        payload = json.loads(self._opener(request, self.timeout_seconds).decode("utf-8"))
        data = payload.get("data")
        if payload.get("rc") != 0 or not isinstance(data, Mapping):
            raise AdapterResponseError(f"unexpected provider response: rc={payload.get('rc')!r}")
        receive_ts = self._receive_clock()
        price = self._scaled(data.get("f43"), 100.0)
        previous_close = self._scaled(data.get("f60"), 100.0)
        gap_pct = None
        if price is not None and previous_close not in (None, 0):
            gap_pct = (price / previous_close - 1.0) * 100.0
        raw_volume = self._number(data.get("f47"))
        exchange_epoch = self._number(data.get("f86"))
        exchange_ts = None
        if exchange_epoch not in (None, 0):
            exchange_ts = datetime.fromtimestamp(exchange_epoch, tz=ZoneInfo("Asia/Shanghai"))
        return AuctionTick(
            exchange_ts=exchange_ts,
            receive_ts=receive_ts,
            ticker=canonical,
            virtual_price=price,
            gap_pct=gap_pct,
            matched_volume=None if raw_volume is None else raw_volume * 100.0,
            matched_amount=self._number(data.get("f48")),
            source=self.capability.adapter_name,
            raw=dict(data),
        )

    @staticmethod
    def _open(request: Request, timeout: float) -> bytes:
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    @staticmethod
    def _canonical_ticker(ticker: str) -> str:
        upper = ticker.upper()
        if upper.endswith((".SH", ".SZ", ".BJ")):
            return upper
        if upper.startswith(("SH", "SZ", "BJ")):
            return f"{upper[2:]}.{upper[:2]}"
        exchange = "SH" if upper.startswith(("5", "6", "9")) else "BJ" if upper.startswith(("4", "8")) else "SZ"
        return f"{upper}.{exchange}"

    @staticmethod
    def _secid(ticker: str) -> str:
        code, exchange = ticker.split(".")
        market = "1" if exchange == "SH" else "0"
        return f"{market}.{code}"

    @staticmethod
    def _number(value) -> Optional[float]:
        if value in (None, "", "-"):
            return None
        return float(value)

    @classmethod
    def _scaled(cls, value, divisor: float) -> Optional[float]:
        number = cls._number(value)
        return None if number is None else number / divisor
