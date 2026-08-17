"""Fault-isolated runtime for non-execution market-data probes."""

from dataclasses import dataclass
from datetime import datetime
from queue import Empty, Queue
from threading import Event, Lock, Thread
import time
from typing import Callable, Mapping, Optional, Sequence, Tuple

from adapters.base import RealtimeAdapter
from adapters.probe import RawSourcePayload
from domain.models import AuctionTick
from storage.jsonl import RawPayloadJsonlStore, RawTickJsonlStore


RUNTIME_PROVENANCE = {
    "receive_ts": "local timezone-aware probe runtime clock",
    "provider_ts": "N/A; control-plane event",
    "exchange_ts": "N/A; control-plane event",
    "payload": "structured probe telemetry; not market data and never execution eligible",
}


@dataclass(frozen=True)
class SourcePollResult:
    source: str
    completed_at: datetime
    ticks: Tuple[AuctionTick, ...]


@dataclass(frozen=True)
class ProbeRunSummary:
    started_at: datetime
    ended_at: datetime
    end_time: Optional[datetime]
    deadline_reached: bool
    sources: Mapping[str, Mapping[str, object]]


class SourceProbeWorker:
    """Own one adapter lifecycle so one provider cannot block another."""

    CONTROL_TICKER = "_SOURCE_"

    def __init__(
        self,
        adapter: RealtimeAdapter,
        tickers: Sequence[str],
        storage_root,
        result_queue: Queue,
        stop_event: Event,
        interval_seconds: float,
        max_samples: Optional[int],
        end_time: Optional[datetime],
        stall_seconds: float,
        gap_seconds: float,
        capture_transactions: bool,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float],
    ):
        self.adapter = adapter
        self.source = adapter.capability.adapter_name
        self.tickers = tuple(tickers)
        self.raw_store = RawPayloadJsonlStore(storage_root / "raw")
        self.canonical_store = RawTickJsonlStore(
            storage_root / "raw" / "canonical" / self.source
        )
        self.result_queue = result_queue
        self.stop_event = stop_event
        self.interval_seconds = interval_seconds
        self.max_samples = max_samples
        self.end_time = end_time
        self.stall_seconds = stall_seconds
        self.gap_seconds = gap_seconds
        self.capture_transactions = capture_transactions
        self.clock = clock
        self.monotonic = monotonic
        self.thread = Thread(
            target=self._run,
            name=f"probe-{self.source}",
            daemon=True,
        )
        self._lock = Lock()
        self._operation = None
        self._operation_started_at = None
        self._last_activity_at = None
        self._watchdog_reported = False
        self._finished = False
        self._termination_reason = None
        self._attempts = 0
        self._successes = 0
        self._errors = 0
        self._stalls = 0
        self._gaps = 0
        self._watchdog_events = 0
        self._ticks = 0

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: Optional[float]) -> None:
        self.thread.join(timeout)

    def snapshot(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "source": self.source,
                "attempts": self._attempts,
                "successes": self._successes,
                "errors": self._errors,
                "stalls": self._stalls,
                "gaps": self._gaps,
                "watchdog_events": self._watchdog_events,
                "ticks": self._ticks,
                "last_activity_at": self._last_activity_at,
                "operation": self._operation,
                "operation_started_at": self._operation_started_at,
                "finished": self._finished,
                "termination_reason": self._termination_reason,
                "thread_alive": self.thread.is_alive(),
            }

    def claim_watchdog_stall(
        self,
        as_of: datetime,
    ) -> Optional[Mapping[str, object]]:
        with self._lock:
            started_at = self._operation_started_at
            if (
                self._operation is None
                or started_at is None
                or self._watchdog_reported
            ):
                return None
            age = (as_of - started_at).total_seconds()
            if age < self.stall_seconds:
                return None
            self._watchdog_reported = True
            self._watchdog_events += 1
            return {
                "status": "STALL",
                "operation": self._operation,
                "operation_started_at": started_at.isoformat(),
                "detected_at": as_of.isoformat(),
                "elapsed_seconds": age,
                "stall_threshold_seconds": self.stall_seconds,
                "execution_eligible": False,
            }

    def emit_watchdog_stall(
        self,
        as_of: datetime,
        payload: Mapping[str, object],
    ) -> None:
        self._emit("WATCHDOG_STALL", as_of, payload)

    def emit_forced_stop(self, as_of: datetime, grace_seconds: float) -> None:
        self._emit(
            "FORCED_STOP",
            as_of,
            {
                "status": "STILL_RUNNING_AFTER_DEADLINE",
                "shutdown_grace_seconds": grace_seconds,
                "execution_eligible": False,
            },
        )

    def _run(self) -> None:
        started_at = self._aware_now()
        self._emit(
            "SOURCE_STARTED",
            started_at,
            {
                "status": "STARTED",
                "end_time": None if self.end_time is None else self.end_time.isoformat(),
                "max_samples": self.max_samples,
                "interval_seconds": self.interval_seconds,
                "execution_eligible": False,
            },
        )
        termination_reason = "UNKNOWN"
        try:
            self._begin_operation("CONNECT", started_at)
            try:
                self.adapter.connect()
                self.adapter.subscribe(self.tickers)
            except BaseException as error:
                completed_at = self._aware_now()
                self._finish_operation(completed_at)
                self._record_error("CONNECT_ERROR", error, started_at, completed_at, 0)
                termination_reason = "CONNECT_ERROR"
                return
            self._finish_operation(self._aware_now())
            last_completed_at = None
            next_poll_at = self.monotonic()
            while True:
                now = self._aware_now()
                if self.end_time is not None and now >= self.end_time:
                    termination_reason = "DEADLINE_REACHED"
                    break
                if self.stop_event.is_set():
                    termination_reason = "STOP_REQUESTED"
                    break
                with self._lock:
                    attempts = self._attempts
                if self.max_samples is not None and attempts >= self.max_samples:
                    termination_reason = "SAMPLE_LIMIT_REACHED"
                    break
                wait_seconds = max(0.0, next_poll_at - self.monotonic())
                if self.end_time is not None:
                    wait_seconds = min(
                        wait_seconds,
                        max(0.0, (self.end_time - now).total_seconds()),
                    )
                if wait_seconds > 0 and self.stop_event.wait(wait_seconds):
                    if self.end_time is not None and self._aware_now() >= self.end_time:
                        termination_reason = "DEADLINE_REACHED"
                    else:
                        termination_reason = "STOP_REQUESTED"
                    break
                poll_started_at = self._aware_now()
                self._begin_operation("POLL", poll_started_at)
                with self._lock:
                    self._attempts += 1
                    attempt = self._attempts
                try:
                    ticks = tuple(self.adapter.poll_once())  # type: ignore[attr-defined]
                    completed_at = self._aware_now()
                    for tick in ticks:
                        self.canonical_store.append(tick)
                    raw_payloads = getattr(self.adapter, "raw_quote_payloads")
                    for event in raw_payloads(ticks):
                        self.raw_store.append(event)
                    self.result_queue.put(
                        SourcePollResult(self.source, completed_at, ticks)
                    )
                    with self._lock:
                        self._successes += 1
                        self._ticks += len(ticks)
                    status = "OK"
                    error_type = None
                except BaseException as error:
                    completed_at = self._aware_now()
                    event_type = "STALL" if isinstance(error, TimeoutError) else "POLL_ERROR"
                    self._record_error(
                        event_type,
                        error,
                        poll_started_at,
                        completed_at,
                        attempt,
                    )
                    status = event_type
                    error_type = type(error).__name__
                    ticks = ()
                self._finish_operation(completed_at)
                elapsed = (completed_at - poll_started_at).total_seconds()
                if last_completed_at is not None:
                    gap = (completed_at - last_completed_at).total_seconds()
                    if gap > self.gap_seconds:
                        with self._lock:
                            self._gaps += 1
                        self._emit(
                            "GAP",
                            completed_at,
                            {
                                "status": "GAP",
                                "previous_completion_at": last_completed_at.isoformat(),
                                "current_completion_at": completed_at.isoformat(),
                                "gap_seconds": gap,
                                "gap_threshold_seconds": self.gap_seconds,
                                "execution_eligible": False,
                            },
                        )
                last_completed_at = completed_at
                self._emit(
                    "HEARTBEAT",
                    completed_at,
                    {
                        "status": status,
                        "attempt": attempt,
                        "tick_count": len(ticks),
                        "poll_elapsed_seconds": elapsed,
                        "error_type": error_type,
                        "execution_eligible": False,
                    },
                )
                next_poll_at = self.monotonic() + self.interval_seconds
            self._capture_transactions_if_allowed(termination_reason)
        finally:
            self._begin_operation("CLOSE", self._aware_now())
            try:
                self.adapter.close()
            except BaseException as error:
                self._record_error(
                    "CLOSE_ERROR",
                    error,
                    self._operation_started_at or self._aware_now(),
                    self._aware_now(),
                    0,
                )
            completed_at = self._aware_now()
            self._finish_operation(completed_at)
            with self._lock:
                self._finished = True
                self._termination_reason = termination_reason
            self._emit(
                "SOURCE_STOPPED",
                completed_at,
                {
                    "status": "STOPPED",
                    "termination_reason": termination_reason,
                    "execution_eligible": False,
                },
            )

    def _capture_transactions_if_allowed(self, termination_reason: str) -> None:
        if not self.capture_transactions or not hasattr(self.adapter, "capture_transactions"):
            return
        now = self._aware_now()
        if self.end_time is not None and now >= self.end_time:
            self._emit(
                "TRANSACTION_SKIPPED",
                now,
                {
                    "reason": "END_TIME_REACHED",
                    "termination_reason": termination_reason,
                    "execution_eligible": False,
                },
            )
            return
        trade_date = now.date().isoformat()
        for ticker in self.tickers:
            started_at = self._aware_now()
            self._begin_operation("TRANSACTION", started_at)
            try:
                events = getattr(self.adapter, "capture_transactions")(ticker, trade_date)
                for event in events:
                    self.raw_store.append(event)
            except BaseException as error:
                self._record_error(
                    "TRANSACTION_ERROR",
                    error,
                    started_at,
                    self._aware_now(),
                    0,
                )
            finally:
                self._finish_operation(self._aware_now())

    def _record_error(
        self,
        event_type: str,
        error: BaseException,
        started_at: datetime,
        completed_at: datetime,
        attempt: int,
    ) -> None:
        with self._lock:
            self._errors += 1
            if event_type == "STALL":
                self._stalls += 1
        self._emit(
            event_type,
            completed_at,
            {
                "status": event_type,
                "attempt": attempt,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "operation_started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "elapsed_seconds": (completed_at - started_at).total_seconds(),
                "execution_eligible": False,
            },
        )

    def _emit(
        self,
        event_type: str,
        receive_ts: datetime,
        payload: Mapping[str, object],
    ) -> None:
        self.raw_store.append(
            RawSourcePayload(
                source=self.source,
                event_type=event_type,
                ticker=self.CONTROL_TICKER,
                receive_ts=receive_ts,
                provider_ts=None,
                exchange_ts=None,
                payload=payload,
                provenance=RUNTIME_PROVENANCE,
            )
        )

    def _begin_operation(self, operation: str, started_at: datetime) -> None:
        with self._lock:
            self._operation = operation
            self._operation_started_at = started_at
            self._last_activity_at = started_at
            self._watchdog_reported = False

    def _finish_operation(self, completed_at: datetime) -> None:
        with self._lock:
            self._operation = None
            self._operation_started_at = None
            self._last_activity_at = completed_at
            self._watchdog_reported = False

    def _aware_now(self) -> datetime:
        current = self.clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("probe runtime clock must be timezone-aware")
        return current


class IsolatedProbeRuntime:
    """Supervise independent provider workers with a hard wall-clock end."""

    def __init__(
        self,
        workers: Sequence[SourceProbeWorker],
        result_queue: Queue,
        stop_event: Event,
        end_time: Optional[datetime],
        watchdog_interval_seconds: float,
        shutdown_grace_seconds: float,
        on_result: Callable[[SourcePollResult], None],
        clock: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.workers = tuple(workers)
        self.result_queue = result_queue
        self.stop_event = stop_event
        self.end_time = end_time
        self.watchdog_interval_seconds = watchdog_interval_seconds
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self.on_result = on_result
        self.clock = clock
        self.monotonic = monotonic

    def run(self) -> ProbeRunSummary:
        started_at = self._aware_now()
        for worker in self.workers:
            worker.start()
        deadline_reached = False
        try:
            while not all(not worker.thread.is_alive() for worker in self.workers):
                now = self._aware_now()
                if self.end_time is not None and now >= self.end_time:
                    deadline_reached = True
                    self.stop_event.set()
                    break
                timeout = self.watchdog_interval_seconds
                if self.end_time is not None:
                    timeout = min(timeout, max(0.0, (self.end_time - now).total_seconds()))
                try:
                    result = self.result_queue.get(timeout=timeout)
                except Empty:
                    pass
                else:
                    self.on_result(result)
                self._check_watchdogs()
        except KeyboardInterrupt:
            self.stop_event.set()
            raise
        finally:
            self.stop_event.set()
            shutdown_deadline = self.monotonic() + self.shutdown_grace_seconds
            for worker in self.workers:
                worker.join(max(0.0, shutdown_deadline - self.monotonic()))
            ended_at = self._aware_now()
            if self.end_time is not None and ended_at >= self.end_time:
                deadline_reached = True
            for worker in self.workers:
                if worker.thread.is_alive():
                    worker.emit_forced_stop(ended_at, self.shutdown_grace_seconds)
            self._drain_results()
        return ProbeRunSummary(
            started_at=started_at,
            ended_at=ended_at,
            end_time=self.end_time,
            deadline_reached=deadline_reached,
            sources={worker.source: worker.snapshot() for worker in self.workers},
        )

    def _check_watchdogs(self) -> None:
        now = self._aware_now()
        for worker in self.workers:
            payload = worker.claim_watchdog_stall(now)
            if payload is not None:
                worker.emit_watchdog_stall(now, payload)

    def _drain_results(self) -> None:
        while True:
            try:
                result = self.result_queue.get_nowait()
            except Empty:
                return
            self.on_result(result)

    def _aware_now(self) -> datetime:
        current = self.clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("probe runtime clock must be timezone-aware")
        return current
