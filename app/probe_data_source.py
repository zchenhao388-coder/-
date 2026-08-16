import argparse
import json
from dataclasses import asdict
from datetime import datetime

from adapters.eastmoney import EastmoneySnapshotAdapter


def _default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe an experimental real quote adapter")
    parser.add_argument("ticker", nargs="?", default="600000.SH")
    args = parser.parse_args()
    adapter = EastmoneySnapshotAdapter()
    tick = adapter.fetch_one(args.ticker)
    print(json.dumps({"capability": asdict(adapter.capability), "tick": asdict(tick)}, ensure_ascii=False, indent=2, default=_default))


if __name__ == "__main__":
    main()
