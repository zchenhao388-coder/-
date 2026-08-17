import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from statistics import mean, pstdev
from typing import Mapping, Optional, Sequence, Tuple, Union

from config.thresholds import Threshold, ThresholdRegistry
from domain.enums import ThresholdStatus
from backtest.walk_forward import OutOfSampleMetrics


class CalibrationPhase(str, Enum):
    SINGLE_FEATURE = "SINGLE_FEATURE"
    INTERACTION = "INTERACTION"
    AQS_WEIGHT = "AQS_WEIGHT"


CALIBRATION_ROADMAP = {
    CalibrationPhase.SINGLE_FEATURE: (
        "EG", "Authenticity", "RecoveryRatio", "CloseLocation", "LiquidityPercentile", "ThemeRank",
    ),
    CalibrationPhase.INTERACTION: (
        "EG×Authenticity", "EG×Phase", "Authenticity×Phase", "ThemeWinner×Liquidity",
        "ExtremeConsensus×Phase", "HealthyDisagreement×Phase",
    ),
    CalibrationPhase.AQS_WEIGHT: ("AQS_WEIGHTS",),
}


@dataclass(frozen=True)
class DriftReport:
    name: str
    reference_sample_size: int
    current_sample_size: int
    standardized_mean_shift: Optional[float]
    drifted: bool


class PopulationDriftChecker:
    NUMERICAL_FLOOR = 1e-12

    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds

    def compare(self, name: str, reference: Sequence[float], current: Sequence[float]) -> DriftReport:
        if not reference or not current:
            return DriftReport(name, len(reference), len(current), None, True)
        pooled_scale = max((pstdev(reference) + pstdev(current)) / 2.0, self.NUMERICAL_FLOOR)
        shift = abs(mean(current) - mean(reference)) / pooled_scale
        return DriftReport(
            name,
            len(reference),
            len(current),
            shift,
            shift > self.thresholds.get("maximum_standardized_mean_drift"),
        )


@dataclass(frozen=True)
class CalibrationDecision:
    key: str
    proposed_value: float
    status: ThresholdStatus
    sample_size: int
    reason: str


class ThresholdCalibrator:
    def __init__(self, thresholds: ThresholdRegistry):
        self.thresholds = thresholds

    def evaluate(
        self,
        key: str,
        proposed_value: float,
        train_sample_size: int,
        validation: OutOfSampleMetrics,
        forward: OutOfSampleMetrics,
        drift: DriftReport,
    ) -> CalibrationDecision:
        enough = (
            train_sample_size >= int(self.thresholds.get("minimum_train_sample"))
            and validation.sample_size >= int(self.thresholds.get("minimum_validation_sample"))
            and forward.sample_size >= int(self.thresholds.get("minimum_forward_sample"))
        )
        if not enough:
            return CalibrationDecision(key, proposed_value, ThresholdStatus.SEED, train_sample_size, "INSUFFICIENT_SAMPLE")
        valid_returns = validation.average_return is not None and forward.average_return is not None
        passes = (
            valid_returns
            and validation.average_return >= self.thresholds.get("minimum_validation_average_return")
            and forward.average_return >= self.thresholds.get("minimum_forward_average_return")
            and not drift.drifted
        )
        return CalibrationDecision(
            key,
            proposed_value,
            ThresholdStatus.CALIBRATED if passes else ThresholdStatus.RETIRED,
            train_sample_size,
            "OUT_OF_SAMPLE_PASS" if passes else "OUT_OF_SAMPLE_OR_DRIFT_FAILED",
        )


@dataclass(frozen=True)
class ThresholdVersion:
    version_id: str
    parent_version_id: Optional[str]
    created_at: str
    thresholds: Mapping[str, Threshold]


class ThresholdVersionBuilder:
    def build(
        self,
        registry: ThresholdRegistry,
        decisions: Sequence[CalibrationDecision],
        parent_version_id: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> ThresholdVersion:
        values = dict(registry.as_mapping())
        calibrated_at = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        for decision in decisions:
            if decision.key not in values:
                raise KeyError(decision.key)
            previous = values[decision.key]
            value = decision.proposed_value if decision.status == ThresholdStatus.CALIBRATED else previous.value
            values[decision.key] = Threshold(
                decision.key,
                value,
                decision.status,
                previous.description,
                decision.sample_size,
                previous.regime_scope,
                calibrated_at if decision.status == ThresholdStatus.CALIBRATED else previous.last_calibrated_at,
                decision.reason,
            )
        identity = {
            "parent_version_id": parent_version_id,
            "thresholds": {
                key: self._threshold_payload(threshold)
                for key, threshold in sorted(values.items())
            },
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        version_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return ThresholdVersion(version_id, parent_version_id, calibrated_at, values)

    @staticmethod
    def _threshold_payload(threshold: Threshold):
        payload = asdict(threshold)
        payload["status"] = threshold.status.value
        return payload


class ThresholdVersionStore:
    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)

    def write(self, version: ThresholdVersion) -> Path:
        path = self.root / f"{version.version_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version_id": version.version_id,
            "parent_version_id": version.parent_version_id,
            "created_at": version.created_at,
            "thresholds": {
                key: ThresholdVersionBuilder._threshold_payload(threshold)
                for key, threshold in sorted(version.thresholds.items())
            },
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if path.exists() and path.read_text(encoding="utf-8") != serialized:
            raise ValueError("threshold version collision")
        if not path.exists():
            path.write_text(serialized, encoding="utf-8")
        return path

    def load(self, version_id: str) -> ThresholdVersion:
        payload = json.loads((self.root / f"{version_id}.json").read_text(encoding="utf-8"))
        thresholds = {
            key: Threshold(
                key=key,
                value=float(item["value"]),
                status=ThresholdStatus(item["status"]),
                description=item.get("description", ""),
                sample_size=item.get("sample_size"),
                regime_scope=item.get("regime_scope", "ALL"),
                last_calibrated_at=item.get("last_calibrated_at"),
                notes=item.get("notes", ""),
            )
            for key, item in payload["thresholds"].items()
        }
        return ThresholdVersion(
            payload["version_id"],
            payload.get("parent_version_id"),
            payload["created_at"],
            thresholds,
        )
