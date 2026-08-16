import json
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Union

from domain.models import AuctionTick


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
        partition = "unknown-date" if tick.exchange_ts is None else tick.exchange_ts.date().isoformat()
        path = self.root / partition / f"{tick.ticker}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(tick), ensure_ascii=False, default=_json_default, separators=(",", ":")))
            handle.write("\n")
        return path
