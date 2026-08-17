import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple, Union

from domain.context import night_plan_context_id
from domain.models import AuctionTick, NightPlan
from replay.clock import ReplayEngine, VirtualClock


class ReplayIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactFingerprint:
    logical_name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ReplayManifest:
    schema_version: str
    trade_date: str
    night_plan_context_id: str
    raw_artifacts: Sequence[ArtifactFingerprint]
    config_artifacts: Sequence[ArtifactFingerprint]
    engine_versions: Mapping[str, str]
    code_version: str
    manifest_id: str
    created_at: str


class ReplayManifestBuilder:
    SCHEMA_VERSION = "REPLAY_MANIFEST_V1"

    def build(
        self,
        night_plan: NightPlan,
        raw_files: Sequence[Union[str, Path]],
        config_files: Sequence[Union[str, Path]],
        engine_versions: Mapping[str, str],
        code_version: str,
        created_at: Optional[datetime] = None,
    ) -> ReplayManifest:
        if not code_version or not engine_versions:
            raise ValueError("code_version and engine_versions are required")
        raw = self._fingerprints(raw_files, "raw")
        configs = self._fingerprints(config_files, "config")
        identity = {
            "schema_version": self.SCHEMA_VERSION,
            "trade_date": night_plan.trade_date,
            "night_plan_context_id": night_plan_context_id(night_plan),
            "raw_artifacts": [asdict(item) for item in raw],
            "config_artifacts": [asdict(item) for item in configs],
            "engine_versions": dict(sorted(engine_versions.items())),
            "code_version": code_version,
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        manifest_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        timestamp = created_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return ReplayManifest(
            schema_version=self.SCHEMA_VERSION,
            trade_date=night_plan.trade_date,
            night_plan_context_id=identity["night_plan_context_id"],
            raw_artifacts=raw,
            config_artifacts=configs,
            engine_versions=dict(sorted(engine_versions.items())),
            code_version=code_version,
            manifest_id=manifest_id,
            created_at=timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    @staticmethod
    def _fingerprints(paths: Sequence[Union[str, Path]], prefix: str) -> Tuple[ArtifactFingerprint, ...]:
        results = []
        seen = set()
        for raw_path in sorted((Path(path) for path in paths), key=lambda path: path.name):
            if not raw_path.is_file():
                raise FileNotFoundError(raw_path)
            logical_name = f"{prefix}/{raw_path.name}"
            if logical_name in seen:
                raise ValueError(f"duplicate replay artifact name: {logical_name}")
            seen.add(logical_name)
            payload = raw_path.read_bytes()
            results.append(ArtifactFingerprint(logical_name, hashlib.sha256(payload).hexdigest(), len(payload)))
        return tuple(results)


class ReplayManifestVerifier:
    def verify(
        self,
        manifest: ReplayManifest,
        raw_files: Sequence[Union[str, Path]],
        config_files: Sequence[Union[str, Path]],
    ) -> None:
        builder = ReplayManifestBuilder()
        actual_raw = builder._fingerprints(raw_files, "raw")
        actual_config = builder._fingerprints(config_files, "config")
        if tuple(manifest.raw_artifacts) != actual_raw:
            raise ReplayIntegrityError("raw replay artifacts do not match manifest")
        if tuple(manifest.config_artifacts) != actual_config:
            raise ReplayIntegrityError("replay configuration does not match manifest")


class ReplayManifestStore:
    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)

    def write(self, manifest: ReplayManifest) -> Path:
        path = self.root / manifest.trade_date / f"{manifest.manifest_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(asdict(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            incoming = json.loads(serialized)
            existing.pop("created_at", None)
            incoming.pop("created_at", None)
            if existing != incoming:
                raise ReplayIntegrityError("manifest id collision with different content")
        if not path.exists():
            path.write_text(serialized, encoding="utf-8")
        return path


class VerifiedReplayFactory:
    """ReplayEngine construction is conditional on immutable artifact verification."""

    def __init__(self, verifier: Optional[ReplayManifestVerifier] = None):
        self.verifier = verifier or ReplayManifestVerifier()

    def create(
        self,
        manifest: ReplayManifest,
        raw_files: Sequence[Union[str, Path]],
        config_files: Sequence[Union[str, Path]],
        ticks: Sequence[AuctionTick],
        clock: VirtualClock,
    ) -> ReplayEngine:
        self.verifier.verify(manifest, raw_files, config_files)
        if clock.now.date().isoformat() != manifest.trade_date:
            raise ReplayIntegrityError("virtual clock trade_date does not match replay manifest")
        return ReplayEngine(ticks, clock)
