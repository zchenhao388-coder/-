import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from queue import Queue
from threading import Event
from typing import Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from adapters.base import RealtimeAdapter
from adapters.mootdx import MootdxRealtimeAdapter
from adapters.tencent import TencentRealtimeAdapter
from app.probe_runtime import (
    IsolatedProbeRuntime,
    ProbeRunSummary,
    SourcePollResult,
    SourceProbeWorker,
)
from config.thresholds import ThresholdRegistry
from domain.enums import StorageLayer
from market.asof import AsOfMarketView
from market.source_validation import CrossSourceValidator
from storage.jsonl import LayeredResearchStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def capture(
    tickers: Sequence[str],
    storage_root: Path,
    samples: Optional[int],
    interval_seconds: float,
    capture_transactions: bool,
    *,
    end_time: Optional[datetime] = None,
    primary: Optional[RealtimeAdapter] = None,
    secondary: Optional[RealtimeAdapter] = None,
    stall_seconds: float = 1.5,
    gap_seconds: float = 3.0,
    watchdog_interval_seconds: float = 0.05,
    shutdown_grace_seconds: float = 3.0,
    clock: Optional[Callable[[], datetime]] = None,
    monotonic: Callable[[], float] = time.monotonic,
    print_reports: bool = True,
) -> ProbeRunSummary:
    """Capture two shadow sources without sharing a blocking execution path."""
    if not tickers:
        raise ValueError("at least one ticker is required")
    if samples is None and end_time is None:
        raise ValueError("samples or end_time is required")
    if samples is not None and samples <= 0:
        raise ValueError("samples must be positive")
    if interval_seconds < 0:
        raise ValueError("interval_seconds cannot be negative")
    if min(
        stall_seconds,
        gap_seconds,
        watchdog_interval_seconds,
        shutdown_grace_seconds,
    ) <= 0:
        raise ValueError("probe stability intervals must be positive")
    if end_time is not None and (end_time.tzinfo is None or end_time.utcoffset() is None):
        raise ValueError("end_time must be timezone-aware")
    storage_root = Path(storage_root)
    now = clock or (lambda: datetime.now(SHANGHAI))
    project_root = Path(__file__).parents[1]
    validator = CrossSourceValidator(
        ThresholdRegistry.load(project_root / "config/thresholds/source_validation.yaml")
    )
    derived_store = LayeredResearchStore(storage_root, schema_version="SOURCE_PROBE_V2")
    primary = primary or MootdxRealtimeAdapter()
    secondary = secondary or TencentRealtimeAdapter()
    result_queue = Queue()
    stop_event = Event()
    all_ticks = []

    def on_result(result: SourcePollResult) -> None:
        all_ticks.extend(result.ticks)
        as_of = now()
        if as_of < result.completed_at:
            as_of = result.completed_at
        latest_receive = max(
            (tick.receive_ts for tick in result.ticks if tick.receive_ts is not None),
            default=None,
        )
        if latest_receive is not None and as_of < latest_receive:
            as_of = latest_receive
        view = AsOfMarketView(as_of.date().isoformat(), as_of, ticks=tuple(all_ticks))
        for ticker in tickers:
            report = validator.evaluate(
                view,
                ticker,
                MootdxRealtimeAdapter.SOURCE,
                TencentRealtimeAdapter.SOURCE,
            )
            derived_store.append(
                StorageLayer.DERIVED,
                as_of.date().isoformat(),
                ticker,
                report,
                version_fingerprint="SOURCE_VALIDATION_V2",
            )
            if print_reports:
                print(
                    json.dumps(
                        asdict(report),
                        ensure_ascii=False,
                        default=_json_default,
                        sort_keys=True,
                    )
                )

    workers = (
        SourceProbeWorker(
            primary,
            tickers,
            storage_root,
            result_queue,
            stop_event,
            interval_seconds,
            samples,
            end_time,
            stall_seconds,
            gap_seconds,
            capture_transactions,
            now,
            monotonic,
        ),
        SourceProbeWorker(
            secondary,
            tickers,
            storage_root,
            result_queue,
            stop_event,
            interval_seconds,
            samples,
            end_time,
            stall_seconds,
            gap_seconds,
            False,
            now,
            monotonic,
        ),
    )
    runtime = IsolatedProbeRuntime(
        workers,
        result_queue,
        stop_event,
        end_time,
        watchdog_interval_seconds,
        shutdown_grace_seconds,
        on_result,
        now,
        monotonic,
    )
    return runtime.run()


def _parse_end_time(value: str, reference: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed_time = None
        for pattern in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.strptime(value, pattern).time()
                break
            except ValueError:
                continue
        if parsed_time is None:
            raise argparse.ArgumentTypeError(
                "end time must be ISO-8601 or local HH:MM[:SS]"
            )
        parsed = datetime.combine(reference.date(), parsed_time, tzinfo=SHANGHAI)
    else:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture fault-isolated Mootdx/Tencent probe data; never execution data"
    )
    parser.add_argument(
        "tickers",
        nargs="+",
        help="Canonical tickers, for example 000001.SZ 600000.SH",
    )
    parser.add_argument("--storage-root", default="storage", type=Path)
    parser.add_argument("--samples", type=int)
    parser.add_argument("--interval-seconds", default=1.0, type=float)
    parser.add_argument("--transactions", action="store_true")
    deadline = parser.add_mutually_exclusive_group()
    deadline.add_argument("--end-time", help="hard local/ISO end time, for example 09:25:05")
    deadline.add_argument("--duration-seconds", type=float)
    parser.add_argument("--mootdx-timeout-seconds", default=2.0, type=float)
    parser.add_argument("--tencent-timeout-seconds", default=2.0, type=float)
    parser.add_argument("--stall-seconds", default=1.5, type=float)
    parser.add_argument("--gap-seconds", default=3.0, type=float)
    parser.add_argument("--watchdog-interval-seconds", default=0.05, type=float)
    parser.add_argument("--shutdown-grace-seconds", default=3.0, type=float)
    args = parser.parse_args()
    reference = datetime.now(SHANGHAI)
    end_time = None
    if args.end_time:
        end_time = _parse_end_time(args.end_time, reference)
    elif args.duration_seconds is not None:
        if args.duration_seconds <= 0:
            parser.error("duration-seconds must be positive")
        end_time = reference + timedelta(seconds=args.duration_seconds)
    samples = args.samples
    if samples is None and end_time is None:
        samples = 1
    if samples is not None and samples <= 0:
        parser.error("samples must be positive")
    if args.interval_seconds < 0:
        parser.error("interval-seconds cannot be negative")
    if min(
        args.mootdx_timeout_seconds,
        args.tencent_timeout_seconds,
        args.stall_seconds,
        args.gap_seconds,
        args.watchdog_interval_seconds,
        args.shutdown_grace_seconds,
    ) <= 0:
        parser.error("timeouts and stability intervals must be positive")
    if end_time is not None and end_time <= reference:
        parser.error("end-time must be in the future")
    summary = capture(
        args.tickers,
        args.storage_root,
        samples,
        args.interval_seconds,
        args.transactions,
        end_time=end_time,
        primary=MootdxRealtimeAdapter(timeout_seconds=args.mootdx_timeout_seconds),
        secondary=TencentRealtimeAdapter(timeout_seconds=args.tencent_timeout_seconds),
        stall_seconds=args.stall_seconds,
        gap_seconds=args.gap_seconds,
        watchdog_interval_seconds=args.watchdog_interval_seconds,
        shutdown_grace_seconds=args.shutdown_grace_seconds,
    )
    print(
        json.dumps(
            {"event": "PROBE_RUN_SUMMARY", **asdict(summary)},
            ensure_ascii=False,
            default=_json_default,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
