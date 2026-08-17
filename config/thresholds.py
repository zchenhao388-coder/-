import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple, Union

from domain.enums import ThresholdStatus


@dataclass(frozen=True)
class Threshold:
    key: str
    value: float
    status: ThresholdStatus
    description: str = ""
    sample_size: Optional[int] = None
    regime_scope: str = "ALL"
    last_calibrated_at: Optional[str] = None
    notes: str = ""


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
                sample_size=item.get("sample_size"),
                regime_scope=item.get("regime_scope", "ALL"),
                last_calibrated_at=item.get("last_calibrated_at"),
                notes=item.get("notes", ""),
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

    def items(self) -> Tuple[Tuple[str, Threshold], ...]:
        return tuple(sorted(self._values.items()))

    def as_mapping(self) -> Mapping[str, Threshold]:
        return dict(self._values)
