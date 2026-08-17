from datetime import datetime
from queue import Empty, Queue
from threading import Lock, Thread
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from adapters.base import DataCapability, RealtimeAdapter
from adapters.probe import RawSourcePayload
from domain.models import AuctionTick


class MootdxUnavailable(RuntimeError):
    pass


class MootdxPollTimeout(TimeoutError):
    """Raised when a public Mootdx call exceeds the probe timeout."""


class MootdxRealtimeAdapter(RealtimeAdapter):
    """Mootdx quote/transaction probe.

    The public TDX feed has no accepted exchange-timestamp or auction-field
    contract in this project.  It therefore remains probe/shadow-only and
    deliberately emits ``exchange_ts=None``.
    """

    SOURCE = "mootdx_realtime_probe"
    PROVENANCE = {
        "receive_ts": "local timezone-aware clock immediately after provider response completes",
        "provider_ts": "mootdx servertime/time combined with local Shanghai trade date",
        "exchange_ts": "N/A; provider time is not accepted as exchange-origin time",
        "virtual_price": "price; auction indicative-price semantics pending trading-day validation",
        "gap_pct": "derived from price and last_close",
        "matched_volume": "vol * 100 provisional shares; auction matched-volume semantics unverified",
        "matched_amount": "amount provisional; auction matched-amount semantics unverified",
        "orderbook": "bid/ask level 1-5 with provider lot volumes normalized provisionally to shares",
    }

    def __init__(
        self,
        client_factory: Optional[Callable[[], object]] = None,
        receive_clock: Optional[Callable[[], datetime]] = None,
        timeout_seconds: float = 2.0,
    ):
        if timeout_seconds <= 0:
            raise ValueError("Mootdx timeout_seconds must be positive")
        self._client_factory = client_factory
        self._receive_clock = receive_clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        self.timeout_seconds = float(timeout_seconds)
        self._client = None
        self._subscriptions = ()
        self._latest = {}
        self._last_receive_ts = None
        self._last_error = None
        self._active_call = None
        self._active_call_lock = Lock()

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
                "transactions": True,
            },
            field_provenance=self.PROVENANCE,
            timestamp_semantics="provider server time only; exchange-origin timestamp unverified",
            auction_semantics_verified=False,
            notes=(
                "Mootdx upstream states the project is for learning/exchange, not commercial use.",
                "Public TDX server availability and field semantics have no project-accepted SLA.",
                "Quote vol/amount and price require full 09:15-09:25 trading-day validation.",
                "Never enable execution from this adapter without separate licensed evidence.",
            ),
            allowed_uses=("FIELD_PROBE", "RAW_CAPTURE", "SHADOW_RESEARCH"),
            execution_enabled=False,
            license_verified=False,
            sla_verified=False,
            semantic_evidence_version=None,
        )

    def connect(self) -> None:
        if self._client is not None:
            return
        if self._client_factory is not None:
            self._client = self._client_factory()
            return
        try:
            from mootdx.quotes import Quotes
        except ImportError as error:
            raise MootdxUnavailable(
                "mootdx is not installed; install the optional data-sources dependency"
            ) from error
        self._client = Quotes.factory(market="std", multithread=True, heartbeat=True)

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
        client = self._require_connected()
        if not self._subscriptions:
            return ()
        codes = [ticker.split(".")[0] for ticker in self._subscriptions]
        try:
            payload, receive_ts = self._provider_call(
                "quotes",
                lambda: client.quotes(symbol=codes),
            )
            records = self._records(payload)
            ticks = tuple(self._map_quote(record, receive_ts) for record in records)
        except Exception as error:
            self._last_error = f"{type(error).__name__}: {error}"
            raise
        self._last_error = None
        self._last_receive_ts = receive_ts
        for tick in ticks:
            self._latest[tick.ticker] = tick
        return ticks

    def capture_transactions(self, ticker: str, trade_date: str) -> Tuple[RawSourcePayload, ...]:
        client = self._require_connected()
        canonical = self._canonical_ticker(ticker)
        payload, receive_ts = self._provider_call(
            "transaction",
            lambda: client.transaction(
                symbol=canonical.split(".")[0],
                date=trade_date.replace("-", ""),
            ),
        )
        return tuple(
            RawSourcePayload(
                source=self.SOURCE,
                event_type="TRANSACTION",
                ticker=canonical,
                receive_ts=receive_ts,
                provider_ts=self._parse_provider_time(record.get("time"), receive_ts),
                exchange_ts=None,
                payload=record,
                provenance={
                    "provider_ts": "transaction time combined with receive-date; not exchange-verified",
                    "exchange_ts": "N/A",
                    "payload": "unmodified Mootdx transaction record after scalar normalization",
                },
            )
            for record in self._records(payload)
        )

    def raw_quote_payloads(self, ticks: Sequence[AuctionTick]) -> Tuple[RawSourcePayload, ...]:
        result = []
        for tick in ticks:
            payload = {} if tick.raw is None else tick.raw.get("provider_payload", {})
            result.append(RawSourcePayload(
                source=self.SOURCE,
                event_type="QUOTE",
                ticker=tick.ticker,
                receive_ts=tick.receive_ts,  # type: ignore[arg-type]
                provider_ts=tick.provider_ts,
                exchange_ts=tick.exchange_ts,
                payload=payload,
                provenance=self.PROVENANCE,
            ))
        return tuple(result)

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
        with self._active_call_lock:
            call_in_flight = self._active_call is not None and self._active_call.is_alive()
        return {
            "source": self.SOURCE,
            "connected": self._client is not None,
            "subscriptions": self._subscriptions,
            "last_receive_ts": self._last_receive_ts,
            "last_error": self._last_error,
            "timeout_seconds": self.timeout_seconds,
            "call_in_flight": call_in_flight,
            "execution_enabled": False,
        }

    def close(self) -> None:
        client = self._client
        self._client = None
        close = getattr(client, "close", None)
        if callable(close):
            completed = Queue(maxsize=1)

            def close_client() -> None:
                try:
                    close()
                    completed.put_nowait(None)
                except BaseException as error:  # pragma: no branch - stored for health only
                    completed.put_nowait(error)

            thread = Thread(target=close_client, name="mootdx-close", daemon=True)
            thread.start()
            try:
                result = completed.get(timeout=self.timeout_seconds)
            except Empty:
                self._last_error = (
                    f"MootdxPollTimeout: close exceeded {self.timeout_seconds:.3f}s"
                )
            else:
                if isinstance(result, BaseException):
                    self._last_error = f"{type(result).__name__}: {result}"

    def _map_quote(self, record: Mapping[str, object], receive_ts: datetime) -> AuctionTick:
        code = str(record.get("code") or "").zfill(6)
        ticker = next((item for item in self._subscriptions if item.startswith(f"{code}.")), None)
        if ticker is None:
            ticker = self._canonical_ticker(code, self._optional_int(record.get("market")))
        price = self._price(record.get("price"))
        last_close = self._price(record.get("last_close"))
        gap_pct = None if price is None or last_close is None else (price / last_close - 1.0) * 100.0
        provider_ts = self._parse_provider_time(record.get("servertime"), receive_ts)
        volume_lots = self._number(self._first(record, "vol", "volume"))
        orderbook = {
            "volume_unit": "PROVISIONAL_SHARES_FROM_PROVIDER_LOTS_X100",
            "bids": tuple(
                {"level": level, "price": self._price(record.get(f"bid{level}")), "volume": self._lot_volume(record.get(f"bid_vol{level}"))}
                for level in range(1, 6)
            ),
            "asks": tuple(
                {"level": level, "price": self._price(record.get(f"ask{level}")), "volume": self._lot_volume(record.get(f"ask_vol{level}"))}
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
            matched_amount=self._number(record.get("amount")),
            unmatched_side=None,
            unmatched_volume=None,
            bid=self._price(record.get("bid1")),
            ask=self._price(record.get("ask1")),
            orderbook=orderbook,
            provider_ts=provider_ts,
            source=self.SOURCE,
            raw={
                "provider_payload": dict(record),
                "mapping_version": "MOOTDX_PROBE_V1",
                "auction_semantics_verified": False,
                "exchange_timestamp_verified": False,
            },
        )

    def _now(self) -> datetime:
        current = self._receive_clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("Mootdx receive clock must be timezone-aware")
        return current

    def _provider_call(
        self,
        operation: str,
        callback: Callable[[], object],
    ) -> Tuple[object, datetime]:
        """Bound a blocking Mootdx call without creating non-daemon shutdown debt.

        Public TDX clients do not expose a reliable per-request timeout.  The
        probe therefore runs at most one provider call in a daemon thread and
        fails closed when it exceeds the configured deadline.  While that call
        remains blocked, later polls fail immediately instead of creating an
        unbounded thread backlog.
        """
        with self._active_call_lock:
            active = self._active_call
            if active is not None and active.is_alive():
                raise MootdxPollTimeout(
                    f"Mootdx {operation} skipped because the previous provider call is still in flight"
                )
            result_queue = Queue(maxsize=1)

            def invoke() -> None:
                try:
                    result = callback()
                    completed_at = self._now()
                    result_queue.put_nowait((True, result, completed_at))
                except BaseException as error:
                    result_queue.put_nowait((False, error, None))

            thread = Thread(
                target=invoke,
                name=f"mootdx-{operation}",
                daemon=True,
            )
            self._active_call = thread
            thread.start()
        try:
            succeeded, value, completed_at = result_queue.get(timeout=self.timeout_seconds)
        except Empty as error:
            raise MootdxPollTimeout(
                f"Mootdx {operation} exceeded {self.timeout_seconds:.3f}s"
            ) from error
        finally:
            if not thread.is_alive():
                with self._active_call_lock:
                    if self._active_call is thread:
                        self._active_call = None
        if not succeeded:
            if not isinstance(value, BaseException):
                raise RuntimeError("Mootdx provider call failed without an exception")
            raise value
        if not isinstance(completed_at, datetime):
            raise RuntimeError("Mootdx provider call completed without a receive timestamp")
        return value, completed_at

    def _require_connected(self):
        if self._client is None:
            raise RuntimeError("Mootdx adapter is not connected")
        return self._client

    @staticmethod
    def _records(payload: object) -> Tuple[Mapping[str, object], ...]:
        if payload is None:
            return ()
        if hasattr(payload, "to_dict"):
            payload = payload.to_dict(orient="records")
        elif isinstance(payload, Mapping):
            payload = (payload,)
        result = []
        for item in payload:  # type: ignore[union-attr]
            if not isinstance(item, Mapping):
                raise TypeError("Mootdx payload rows must be mappings")
            result.append({str(key): MootdxRealtimeAdapter._scalar(value) for key, value in item.items()})
        return tuple(result)

    @staticmethod
    def _scalar(value):
        item = getattr(value, "item", None)
        return item() if callable(item) else value

    @staticmethod
    def _first(record: Mapping[str, object], *names: str):
        for name in names:
            if name in record and record[name] is not None:
                return record[name]
        return None

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
    def _optional_int(value) -> Optional[int]:
        return None if value is None else int(value)

    @staticmethod
    def _canonical_ticker(ticker: str, provider_market: Optional[int] = None) -> str:
        upper = ticker.upper()
        if upper.endswith((".SH", ".SZ", ".BJ")):
            return upper
        if upper.startswith(("SH", "SZ", "BJ")):
            return f"{upper[2:]}.{upper[:2]}"
        code = upper.zfill(6)
        if provider_market == 1 or code.startswith(("5", "6", "9")):
            exchange = "SH"
        elif code.startswith(("4", "8")):
            exchange = "BJ"
        else:
            exchange = "SZ"
        return f"{code}.{exchange}"

    @staticmethod
    def _parse_provider_time(value, receive_ts: datetime) -> Optional[datetime]:
        if value in (None, "", "-"):
            return None
        text = str(value).strip()
        formats = ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M", "%H%M%S")
        for pattern in formats:
            try:
                parsed = datetime.strptime(text, pattern).time()
                local_date = receive_ts.astimezone(ZoneInfo("Asia/Shanghai")).date()
                return datetime.combine(local_date, parsed, tzinfo=ZoneInfo("Asia/Shanghai"))
            except ValueError:
                continue
        return None
