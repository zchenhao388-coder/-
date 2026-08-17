import json
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence, Union

from domain.enums import StorageLayer
from domain.models import AuctionTick
from adapters.probe import RawSourcePayload


def _json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


class RawTickJsonlStore:
    """Append-only raw/canonical tick storage for replay."""

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)

    def append(self, tick: AuctionTick) -> Path:
        timestamp = tick.exchange_ts or tick.receive_ts
        partition = "unknown-date" if timestamp is None else timestamp.date().isoformat()
        path = self.root / partition / f"{tick.ticker}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(tick), ensure_ascii=False, default=_json_default, separators=(",", ":")))
            handle.write("\n")
        return path


class RawPayloadJsonlStore:
    """Append-only source-native payload storage with explicit provenance."""

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)

    def append(self, event: RawSourcePayload) -> Path:
        trade_date = (event.exchange_ts or event.receive_ts).date().isoformat()
        safe_source = event.source.replace("/", "_")
        safe_event = event.event_type.lower().replace("/", "_")
        path = self.root / safe_source / trade_date / safe_event / f"{event.ticker}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), ensure_ascii=False, default=_json_default, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
        return path


class LayeredResearchStore:
    """Append-only raw/derived/decision/outcome partitions."""

    def __init__(self, root: Union[str, Path], schema_version: str = "V1"):
        self.root = Path(root)
        self.schema_version = schema_version

    def append(
        self,
        layer: StorageLayer,
        trade_date: str,
        ticker: str,
        record: Any,
        version_fingerprint: str,
    ) -> Path:
        if not version_fingerprint:
            raise ValueError("version_fingerprint is required")
        payload = asdict(record) if hasattr(record, "__dataclass_fields__") else record
        envelope = {
            "schema_version": self.schema_version,
            "layer": layer.value,
            "trade_date": trade_date,
            "ticker": ticker,
            "version_fingerprint": version_fingerprint,
            "payload": payload,
        }
        path = self.root / layer.value / trade_date / f"{ticker}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False, default=_json_default, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
        return path

    def read(self, layer: StorageLayer, trade_date: str, ticker: str) -> Sequence[Mapping[str, Any]]:
        path = self.root / layer.value / trade_date / f"{ticker}.jsonl"
        if not path.exists():
            return ()
        with path.open("r", encoding="utf-8") as handle:
            return tuple(json.loads(line) for line in handle if line.strip())
