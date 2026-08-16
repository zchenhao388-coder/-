import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Union

from domain.enums import ThresholdStatus


@dataclass(frozen=True)
class Threshold:
    key: str
    value: float
    status: ThresholdStatus
    description: str = ""


class ThresholdRegistry:
    def __init__(self, values: Mapping[str, Threshold]):
        self._values = dict(values)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "ThresholdRegistry":
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        values: Dict[str, Threshold] = {}
        for key, item in raw["thresholds"].items():
            values[key] = Threshold(
                key=key,
                value=float(item["value"]),
                status=ThresholdStatus(item["status"]),
                description=item.get("description", ""),
            )
        return cls(values)

    def get(self, key: str) -> float:
        threshold = self._values[key]
        if threshold.status == ThresholdStatus.RETIRED:
            raise ValueError(f"threshold {key} is retired")
        return threshold.value

    def metadata(self, key: str) -> Threshold:
        return self._values[key]

    def require(self, keys: Iterable[str]) -> None:
        missing = sorted(set(keys) - set(self._values))
        if missing:
            raise KeyError(f"missing thresholds: {missing}")
