"""Provider-neutral append-only source-fact registry."""
# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from itertools import pairwise
from pathlib import Path
from typing import Any

from ._vintage_lock import file_lock

_REGISTRY_LOCKS: dict[str, threading.RLock] = {}


class SourceResolutionStatus(str, Enum):
    VERIFIED_SOURCE_OBSERVATION = "VERIFIED_SOURCE_OBSERVATION"
    UNKNOWN_FAIL_CLOSED = "UNKNOWN_FAIL_CLOSED"
    NOT_YET_OBSERVED = "NOT_YET_OBSERVED"
    CURRENT_ONLY_HISTORICAL_UNPROVEN = "CURRENT_ONLY_HISTORICAL_UNPROVEN"
    INCOMPLETE_SOURCE_OBSERVATION = "INCOMPLETE_SOURCE_OBSERVATION"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SourceFactObservation:
    provider: str
    endpoint: str
    source_key: str
    source_event_date: str | None = None
    source_effective_from: str | None = None
    source_effective_to: str | None = None
    source_available_at: datetime | None = None
    availability_basis: str | None = None
    availability_precision: str | None = None
    provider_first_observed_at: datetime | None = None
    snapshot_fetched_at: datetime | None = None
    snapshot_identity: str = ""
    observation_identity: str = ""
    request_identity: str = ""
    payload_sha256: str = ""
    source_value_sha256: str = ""
    fact_version: str = ""
    revision_status: str = "CURRENT"
    supersedes_fact_version: str | None = None
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.provider or not self.endpoint or not self.source_key:
            raise ValueError("provider, endpoint, and source_key are required")
        for name in (
            "snapshot_identity",
            "observation_identity",
            "request_identity",
            "payload_sha256",
            "fact_version",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError(f"{name} must be a SHA-256 identity")
        if len(self.source_value_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.source_value_sha256
        ):
            raise ValueError("source_value_sha256 must be a SHA-256 identity")
        for name in ("source_available_at", "provider_first_observed_at", "snapshot_fetched_at"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if (
            self.provider_first_observed_at
            and self.snapshot_fetched_at
            and self.snapshot_fetched_at < self.provider_first_observed_at
        ):
            raise ValueError("observation predates provider first observation")
        if (
            self.source_available_at
            and self.snapshot_fetched_at
            and self.source_available_at > self.snapshot_fetched_at
        ):
            raise ValueError("source availability is inconsistent")
        object.__setattr__(self, "quality_flags", tuple(sorted(set(self.quality_flags))))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("source_available_at", "provider_first_observed_at", "snapshot_fetched_at"):
            data[key] = _iso(getattr(self, key))
        data["quality_flags"] = list(self.quality_flags)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceFactObservation:
        values = dict(data)
        for key in ("source_available_at", "provider_first_observed_at", "snapshot_fetched_at"):
            if values.get(key):
                values[key] = datetime.fromisoformat(values[key].removesuffix("Z") + "+00:00")
        values["quality_flags"] = tuple(values.get("quality_flags", ()))
        return cls(**values)


@dataclass(frozen=True)
class SourceFactRevisionRef:
    source_key: str
    prior_fact_version: str
    next_fact_version: str

    def __post_init__(self) -> None:
        if not self.source_key or any(
            len(v) != 64 or any(c not in "0123456789abcdef" for c in v)
            for v in (self.prior_fact_version, self.next_fact_version)
        ):
            raise ValueError("invalid revision reference")
        if self.prior_fact_version == self.next_fact_version:
            raise ValueError("revision must change fact version")


@dataclass(frozen=True)
class SourceFactRegistryManifest:
    schema_version: int
    registry_identity: str
    observation_count: int
    observations: tuple[dict[str, Any], ...]


class SourceFactRegistry:
    """Atomic, deterministic JSON registry; records are append-only."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "manifest.json"
        self._records: dict[tuple[str, str, str, str, str], SourceFactObservation] = {}
        self._lock = threading.RLock()
        self._shared_lock = _REGISTRY_LOCKS.setdefault(str(self.root.resolve()), threading.RLock())
        if self.path.exists():
            self._load()

    @staticmethod
    def _identity(records: list[dict[str, Any]]) -> str:
        raw = json.dumps(
            records, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _write_immutable(path: Path, data: bytes) -> None:
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("immutable content collision")
            return
        fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("immutable content collision")
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError("invalid registry manifest") from exc
        generations = sorted((self.root / "manifests").glob("*.json"))
        authoritative_generation: str | None = None
        if "current_registry_identity" in data:
            head_identity = data.get("current_registry_identity")
            generation = data.get("generation")
            if not isinstance(generation, str) or Path(generation).name != generation:
                raise ValueError("registry head pointer mismatch")
            target = self.root / "manifests" / generation
            if not target.exists():
                raise ValueError("registry head generation missing")
            data = json.loads(target.read_text(encoding="utf-8"))
            if data.get("registry_identity") != head_identity:
                raise ValueError("registry head identity mismatch")
            authoritative_generation = generation
        if generations:
            # Follow the authoritative previous-identity links only. Unreachable
            # orphan generations (for example from an interrupted publication)
            # must not poison a later valid head.
            by_identity: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
            for generation in generations:
                try:
                    historical = json.loads(generation.read_text(encoding="utf-8"))
                    hist_records = historical.get("observations", [])
                    if historical.get("registry_identity") != self._identity(hist_records):
                        raise ValueError("registry generation tampering detected")
                    identity = historical.get("registry_identity")
                    if not isinstance(identity, str):
                        continue
                    by_identity.setdefault(identity, []).append((generation, historical))
                except (ValueError, TypeError, KeyError):
                    # Unreachable interrupted/orphan files are ignored; any
                    # malformed generation referenced by the authoritative chain
                    # is rejected below.
                    continue
            reachable: list[dict[str, Any]] = []
            current = data
            seen: set[str] = set()
            while True:
                identity = current.get("registry_identity")
                if not isinstance(identity, str) or identity in seen:
                    raise ValueError("registry generation chain cycle")
                seen.add(identity)
                reachable.append(current)
                previous = current.get("previous_registry_identity")
                if not previous:
                    break
                if previous not in by_identity or len(by_identity[previous]) != 1:
                    raise ValueError("registry generation chain missing")
                current = by_identity[previous][0][1]
            for prior, current in pairwise(reversed(reachable)):
                prior_map = {self._identity([r]): r for r in prior.get("observations", [])}
                current_map = {self._identity([r]): r for r in current.get("observations", [])}
                if not set(prior_map).issubset(current_map) or any(
                    prior_map[d] != current_map[d] for d in prior_map
                ):
                    raise ValueError("registry generation removal or mutation")
                if current.get("observation_count", 0) <= prior.get("observation_count", 0):
                    raise ValueError("registry generation count did not increase")
            try:
                authoritative = json.loads(
                    (self.root / "manifests" / authoritative_generation).read_text(encoding="utf-8")
                    if authoritative_generation
                    else generations[-1].read_text(encoding="utf-8")
                )
            except Exception as exc:
                raise ValueError("invalid immutable registry generation") from exc
            if data != authoritative:
                raise ValueError("registry head tampering detected")
            data = authoritative
        records = data.get("observations")
        if not isinstance(records, list) or data.get("registry_identity") != self._identity(
            records
        ):
            raise ValueError("registry tampering detected")
        record_dir = self.root / "records"
        if record_dir.exists():
            for item in records:
                encoded = json.dumps(
                    item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                path = record_dir / f"{digest}.json"
                if not path.exists() or path.read_text(encoding="utf-8") != encoded:
                    raise ValueError("registry record tampering detected")
        self._records = {}
        for item in records:
            obs = SourceFactObservation.from_dict(item)
            key = (obs.endpoint, obs.source_key)
            self._records[
                (obs.provider, key[0], key[1], obs.fact_version, obs.observation_identity)
            ] = obs

    @property
    def observations(self) -> tuple[SourceFactObservation, ...]:
        return tuple(
            sorted(
                self._records.values(),
                key=lambda x: (
                    x.endpoint,
                    x.provider,
                    x.source_key,
                    x.snapshot_fetched_at or datetime.min.replace(tzinfo=UTC),
                    x.fact_version,
                ),
            )
        )

    def register(
        self, observation: SourceFactObservation, *, store: Any = None, observation_ref: Any = None
    ) -> SourceFactObservation:
        if store is None or observation_ref is None:
            raise ValueError("store and observation_ref are required")
        with self._shared_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / ".registry.lock").open("a+b") as lock, file_lock(lock):
                return self._register_locked(
                    observation, store=store, observation_ref=observation_ref
                )

    def _register_locked(
        self, observation: SourceFactObservation, *, store: Any = None, observation_ref: Any = None
    ) -> SourceFactObservation:
        allowed_statuses = {
            "CURRENT",
            "CURRENT_OBSERVATION_ONLY",
            "UNCHANGED_FROM_PREVIOUS",
            "REVISED",
            "REVISED_FROM_PREVIOUS",
            "INCOMPLETE",
            "RETRIEVAL_COMPLETENESS_UNPROVEN",
        }
        if observation.revision_status not in allowed_statuses:
            raise ValueError("invalid revision status")
        if store is not None or observation_ref is not None:
            if store is None or observation_ref is None:
                raise ValueError("store and observation_ref must be supplied together")
            replay = store.replay_observation(observation_ref)
            manifest = replay.manifest
            direct = {
                "provider": observation_ref.provider,
                "endpoint": observation_ref.endpoint,
                "request_identity": observation_ref.request_identity,
                "payload_sha256": observation_ref.response_sha256,
                "snapshot_identity": observation_ref.snapshot_identity,
                "observation_identity": observation_ref.observation_identity,
                "fact_version": observation_ref.fact_version,
                "snapshot_fetched_at": _iso(observation_ref.snapshot_fetched_at),
            }
            actual = {
                "provider": observation.provider,
                "endpoint": observation.endpoint,
                "request_identity": observation.request_identity,
                "payload_sha256": observation.payload_sha256,
                "snapshot_identity": observation.snapshot_identity,
                "observation_identity": observation.observation_identity,
                "fact_version": observation.fact_version,
                "snapshot_fetched_at": _iso(observation.snapshot_fetched_at),
            }
            if (
                actual != direct
                or manifest.get("response_sha256") != observation_ref.response_sha256
            ):
                raise ValueError("observation timestamp mismatch")
        with self._lock:
            if self.path.exists():
                self._load()
        key = (observation.endpoint, observation.source_key)
        existing = [
            v
            for (p, e, s, _, _), v in self._records.items()
            if (p, e, s) == (observation.provider, *key)
        ]
        same_observation = [
            v for v in existing if v.observation_identity == observation.observation_identity
        ]
        if same_observation:
            if same_observation[0].to_dict() == observation.to_dict():
                return observation
            raise ValueError("observation identity collision")
        if existing:
            prior = max(
                existing, key=lambda x: x.snapshot_fetched_at or datetime.min.replace(tzinfo=UTC)
            )
            if observation.source_value_sha256 == prior.source_value_sha256:
                observation = replace(
                    observation,
                    revision_status="UNCHANGED_FROM_PREVIOUS",
                    supersedes_fact_version=None,
                )
            elif observation.supersedes_fact_version is None:
                observation = replace(
                    observation,
                    revision_status="REVISED_FROM_PREVIOUS",
                    supersedes_fact_version=prior.fact_version,
                )
            if (
                observation.supersedes_fact_version is not None
                and observation.supersedes_fact_version != prior.fact_version
            ):
                raise ValueError("conflicting revision lineage")
            if (
                observation.snapshot_fetched_at
                and prior.snapshot_fetched_at
                and observation.snapshot_fetched_at < prior.snapshot_fetched_at
            ):
                raise ValueError("backdated observation")
        elif observation.supersedes_fact_version is not None:
            raise ValueError("fabricated lineage")
        self._records[
            (
                observation.provider,
                key[0],
                key[1],
                observation.fact_version,
                observation.observation_identity,
            )
        ] = observation
        self._publish()
        return observation

    def _publish(self) -> None:
        records = [x.to_dict() for x in self.observations]
        manifest_dir = self.root / "manifests"
        previous_identity = None
        if self.path.exists():
            pointer = json.loads(self.path.read_text(encoding="utf-8"))
            head_name = pointer.get("generation")
            if isinstance(head_name, str):
                prior = json.loads((manifest_dir / head_name).read_text(encoding="utf-8"))
                previous_identity = prior.get("registry_identity")
            # Deterministic ordering may interleave concurrent source keys; lineage is
            # validated by the prior-generation identity chain below.
        payload = {
            "schema_version": 1,
            "observation_count": len(records),
            "observations": records,
            "registry_identity": self._identity(records),
            "previous_registry_identity": previous_identity,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        # Immutable content-addressed generations are the durable source of truth.
        record_dir = self.root / "records"
        record_dir.mkdir(exist_ok=True)
        manifest_dir.mkdir(exist_ok=True)
        for record in records:
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            target = record_dir / f"{digest}.json"
            if not target.exists():
                self._write_immutable(target, encoded.encode())
        encoded_manifest = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        generation = (
            manifest_dir
            / f"{len(list(manifest_dir.glob('*.json'))):020d}-{payload['registry_identity']}.json"
        )
        if not generation.exists():
            self._write_immutable(generation, encoded_manifest.encode())
        pointer = {
            "schema_version": 1,
            "current_registry_identity": payload["registry_identity"],
            "generation": generation.name,
        }
        fd, name = tempfile.mkstemp(prefix=".manifest-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    pointer, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def manifest(self) -> SourceFactRegistryManifest:
        records = tuple(x.to_dict() for x in self.observations)
        return SourceFactRegistryManifest(1, self._identity(list(records)), len(records), records)

    def verify(self) -> bool:
        if not self.path.exists():
            return False
        self._load()
        return True


__all__ = [
    "SourceFactObservation",
    "SourceFactRegistry",
    "SourceFactRegistryManifest",
    "SourceFactRevisionRef",
    "SourceResolutionStatus",
]
