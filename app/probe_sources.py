import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from adapters.mootdx import MootdxRealtimeAdapter
from adapters.tencent import TencentRealtimeAdapter
from config.thresholds import ThresholdRegistry
from domain.enums import StorageLayer
from market.asof import AsOfMarketView
from market.source_validation import CrossSourceValidator
from storage.jsonl import LayeredResearchStore, RawPayloadJsonlStore, RawTickJsonlStore


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def capture(
    tickers: Sequence[str],
    storage_root: Path,
    samples: int,
    interval_seconds: float,
    capture_transactions: bool,
) -> None:
    project_root = Path(__file__).parents[1]
    validator = CrossSourceValidator(
        ThresholdRegistry.load(project_root / "config/thresholds/source_validation.yaml")
    )
    raw_store = RawPayloadJsonlStore(storage_root / "raw")
    derived_store = LayeredResearchStore(storage_root, schema_version="SOURCE_PROBE_V2")
    primary = MootdxRealtimeAdapter()
    secondary = TencentRealtimeAdapter()
    primary.connect()
    secondary.connect()
    primary.subscribe(tickers)
    secondary.subscribe(tickers)
    all_ticks = []
    try:
        for index in range(samples):
            primary_ticks = primary.poll_once()
            secondary_ticks = secondary.poll_once()
            all_ticks.extend(primary_ticks)
            all_ticks.extend(secondary_ticks)
            for source, ticks in ((primary, primary_ticks), (secondary, secondary_ticks)):
                canonical_store = RawTickJsonlStore(storage_root / "raw/canonical" / source.capability.adapter_name)
                for tick in ticks:
                    canonical_store.append(tick)
                for event in source.raw_quote_payloads(ticks):
                    raw_store.append(event)
            as_of = datetime.now(ZoneInfo("Asia/Shanghai"))
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
                print(json.dumps(asdict(report), ensure_ascii=False, default=_json_default, sort_keys=True))
            if index + 1 < samples:
                time.sleep(interval_seconds)
        if capture_transactions:
            trade_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            for ticker in tickers:
                for event in primary.capture_transactions(ticker, trade_date):
                    raw_store.append(event)
    finally:
        primary.close()
        secondary.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture Mootdx/Tencent probe data; never execution data")
    parser.add_argument("tickers", nargs="+", help="Canonical tickers, for example 000001.SZ 600000.SH")
    parser.add_argument("--storage-root", default="storage", type=Path)
    parser.add_argument("--samples", default=1, type=int)
    parser.add_argument("--interval-seconds", default=1.0, type=float)
    parser.add_argument("--transactions", action="store_true")
    args = parser.parse_args()
    if args.samples <= 0 or args.interval_seconds < 0:
        parser.error("samples must be positive and interval-seconds cannot be negative")
    capture(args.tickers, args.storage_root, args.samples, args.interval_seconds, args.transactions)


if __name__ == "__main__":
    main()
