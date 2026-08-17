import re
from datetime import datetime
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from adapters.base import DataCapability, RealtimeAdapter
from adapters.probe import RawSourcePayload
from domain.models import AuctionTick


class TencentResponseError(RuntimeError):
    pass


class TencentRealtimeAdapter(RealtimeAdapter):
    """Tencent quote probe used only for shadow cross-source validation."""

    SOURCE = "tencent_realtime_probe"
    ENDPOINT = "https://qt.gtimg.cn/q="
    PROVENANCE = {
        "receive_ts": "local timezone-aware receive clock after HTTP response",
        "provider_ts": "Tencent field 30 (YYYYMMDDHHMMSS); not exchange-origin verified",
        "exchange_ts": "N/A; provider timestamp is not promoted to exchange timestamp",
        "virtual_price": "field 3 price; auction indicative-price semantics unverified",
        "gap_pct": "field 32 when present, otherwise derived from fields 3 and 4",
        "matched_volume": "field 6 * 100 provisional shares; auction matched semantics unverified",
        "matched_amount": "third component of field 35 provisional yuan; auction semantics unverified",
        "orderbook": "fields 9-28, five bid/ask levels; volume provisionally lots * 100",
    }

    def __init__(
        self,
        opener: Optional[Callable[[Request, float], bytes]] = None,
        timeout_seconds: float = 10.0,
        receive_clock: Optional[Callable[[], datetime]] = None,
    ):
        self._opener = opener or self._open
        self.timeout_seconds = timeout_seconds
        self._receive_clock = receive_clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        self._connected = False
        self._subscriptions = ()
        self._latest = {}
        self._last_receive_ts = None
        self._last_error = None

    @property
    def capability(self) -> DataCapability:
        return DataCapability(
            adapter_name=self.SOURCE,
            core_fields={
                "exchange_ts": False,
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
                "bid": True,
                "ask": True,
                "orderbook": True,
            },
            field_provenance=self.PROVENANCE,
            timestamp_semantics="provider quote timestamp only; exchange-origin timestamp unverified",
            auction_semantics_verified=False,
            notes=(
                "Undocumented public HTTP quote response; schema may change without notice.",
                "Used as a secondary price/basic-quote cross-check only.",
                "Full 09:15-09:25 field semantics, refresh frequency and corrections remain unverified.",
                "Never enable execution from this adapter without separate licensed evidence.",
            ),
            allowed_uses=("FIELD_PROBE", "RAW_CAPTURE", "SHADOW_RESEARCH"),
            execution_enabled=False,
            license_verified=False,
            sla_verified=False,
            semantic_evidence_version=None,
        )

    def connect(self) -> None:
        self._connected = True

    def subscribe(self, tickers: Sequence[str]) -> None:
        self._require_connected()
        self._subscriptions = tuple(dict.fromkeys(self._canonical_ticker(item) for item in tickers))

    def unsubscribe(self, tickers: Sequence[str]) -> None:
        remove = {self._canonical_ticker(item) for item in tickers}
        self._subscriptions = tuple(item for item in self._subscriptions if item not in remove)

    def get_latest(self, ticker: str) -> Optional[AuctionTick]:
        return self._latest.get(self._canonical_ticker(ticker))

    def stream(self) -> Iterable[AuctionTick]:
        yield from self.poll_once()

    def poll_once(self) -> Tuple[AuctionTick, ...]:
        self._require_connected()
        if not self._subscriptions:
            return ()
        symbols = ",".join(self._provider_symbol(item) for item in self._subscriptions)
        request = Request(
            f"{self.ENDPOINT}{symbols}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
        )
        try:
            body = self._opener(request, self.timeout_seconds).decode("gbk")
            receive_ts = self._now()
            ticks = tuple(self._parse_line(line, receive_ts) for line in body.split(";") if line.strip())
        except Exception as error:
            self._last_error = f"{type(error).__name__}: {error}"
            raise
        self._last_error = None
        self._last_receive_ts = receive_ts
        for tick in ticks:
            self._latest[tick.ticker] = tick
        return ticks

    def raw_quote_payloads(self, ticks: Sequence[AuctionTick]) -> Tuple[RawSourcePayload, ...]:
        return tuple(
            RawSourcePayload(
                source=self.SOURCE,
                event_type="QUOTE",
                ticker=tick.ticker,
                receive_ts=tick.receive_ts,  # type: ignore[arg-type]
                provider_ts=tick.provider_ts,
                exchange_ts=tick.exchange_ts,
                payload={} if tick.raw is None else tick.raw,
                provenance=self.PROVENANCE,
            )
            for tick in ticks
        )

    def load(
        self,
        tickers: Sequence[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[AuctionTick]:
        self.subscribe(tickers)
        for tick in self.poll_once():
            boundary = tick.receive_ts
            if boundary is None:
                continue
            if start is not None and boundary < start:
                continue
            if end is not None and boundary > end:
                continue
            yield tick

    def health(self) -> Mapping[str, object]:
        return {
            "source": self.SOURCE,
            "connected": self._connected,
            "subscriptions": self._subscriptions,
            "last_receive_ts": self._last_receive_ts,
            "last_error": self._last_error,
            "execution_enabled": False,
        }

    def close(self) -> None:
        self._connected = False

    def _parse_line(self, line: str, receive_ts: datetime) -> AuctionTick:
        match = re.fullmatch(r'\s*v_([a-z]{2}\d{6})="(.*)"\s*', line, flags=re.IGNORECASE)
        if match is None:
            raise TencentResponseError("unexpected Tencent quote line")
        provider_symbol, payload = match.groups()
        fields = payload.split("~")
        if len(fields) < 36:
            raise TencentResponseError(f"Tencent quote has insufficient fields: {len(fields)}")
        ticker = self._canonical_ticker(provider_symbol)
        price = self._price(self._field(fields, 3))
        last_close = self._price(self._field(fields, 4))
        gap_pct = self._number(self._field(fields, 32))
        if gap_pct is None and price is not None and last_close is not None:
            gap_pct = (price / last_close - 1.0) * 100.0
        volume_lots = self._number(self._field(fields, 6))
        amount = None
        combined = self._field(fields, 35)
        if combined:
            parts = combined.split("/")
            if len(parts) >= 3:
                amount = self._number(parts[2])
        orderbook = {
            "volume_unit": "PROVISIONAL_SHARES_FROM_PROVIDER_LOTS_X100",
            "bids": tuple(
                {"level": level, "price": self._price(self._field(fields, 7 + level * 2)), "volume": self._lot_volume(self._field(fields, 8 + level * 2))}
                for level in range(1, 6)
            ),
            "asks": tuple(
                {"level": level, "price": self._price(self._field(fields, 17 + level * 2)), "volume": self._lot_volume(self._field(fields, 18 + level * 2))}
                for level in range(1, 6)
            ),
        }
        return AuctionTick(
            exchange_ts=None,
            receive_ts=receive_ts,
            ticker=ticker,
            virtual_price=price,
            gap_pct=gap_pct,
            matched_volume=None if volume_lots is None else volume_lots * 100.0,
            matched_amount=amount,
            unmatched_side=None,
            unmatched_volume=None,
            bid=self._price(self._field(fields, 9)),
            ask=self._price(self._field(fields, 19)),
            orderbook=orderbook,
            provider_ts=self._parse_provider_time(self._field(fields, 30)),
            source=self.SOURCE,
            raw={
                "provider_symbol": provider_symbol,
                "raw_text": line,
                "fields": tuple(fields),
                "mapping_version": "TENCENT_PROBE_V1",
                "auction_semantics_verified": False,
                "exchange_timestamp_verified": False,
            },
        )

    def _now(self) -> datetime:
        current = self._receive_clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("Tencent receive clock must be timezone-aware")
        return current

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("Tencent adapter is not connected")

    @staticmethod
    def _open(request: Request, timeout: float) -> bytes:
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    @staticmethod
    def _field(fields: Sequence[str], index: int) -> Optional[str]:
        if index >= len(fields) or fields[index] in ("", "-"):
            return None
        return fields[index]

    @staticmethod
    def _number(value) -> Optional[float]:
        if value in (None, "", "-"):
            return None
        return float(value)

    @classmethod
    def _price(cls, value) -> Optional[float]:
        number = cls._number(value)
        return None if number is None or number <= 0 else number

    @classmethod
    def _lot_volume(cls, value) -> Optional[float]:
        lots = cls._number(value)
        return None if lots is None else lots * 100.0

    @staticmethod
    def _parse_provider_time(value: Optional[str]) -> Optional[datetime]:
        if value is None:
            return None
        try:
            parsed = datetime.strptime(value, "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))

    @staticmethod
    def _provider_symbol(ticker: str) -> str:
        code, exchange = ticker.split(".")
        return f"{exchange.lower()}{code}"

    @staticmethod
    def _canonical_ticker(ticker: str) -> str:
        upper = ticker.upper()
        if upper.endswith((".SH", ".SZ", ".BJ")):
            return upper
        if upper.startswith(("SH", "SZ", "BJ")):
            return f"{upper[2:]}.{upper[:2]}"
        code = upper.zfill(6)
        exchange = "SH" if code.startswith(("5", "6", "9")) else "BJ" if code.startswith(("4", "8")) else "SZ"
        return f"{code}.{exchange}"
