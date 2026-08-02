import hashlib
import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, cast

from .errors import SnapshotConflictError, SnapshotIntegrityError
from .specs import RequestSpec

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_KEYS = frozenset(
    {
        "manifest_schema_version",
        "provider",
        "endpoint",
        "canonical_request",
        "request_identity",
        "response_sha256",
        "response_byte_size",
        "serialization_identifier",
        "retrieved_at",
        "snapshot_identity",
        "mode",
    }
)
_OBSERVATION_KEYS = frozenset(
    {
        "observation_schema_version",
        "provider",
        "endpoint",
        "request_identity",
        "response_sha256",
        "serialization_identifier",
        "snapshot_identity",
        "fact_version",
        "mode",
        "snapshot_fetched_at",
        "observation_identity",
    }
)


class SnapshotMode(str, Enum):
    APPEND = "append"
    FROZEN = "frozen"


@dataclass(frozen=True)
class SnapshotRef:
    path: Path
    snapshot_identity: str
    mode: SnapshotMode
    provider: str
    endpoint: str
    request_identity: str
    response_sha256: str
    serialization_identifier: str

    @property
    def fact_version(self) -> str:
        payload = {
            "request_identity": self.request_identity,
            "response_sha256": self.response_sha256,
            "serialization_identifier": self.serialization_identifier,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SnapshotObservationRef:
    path: Path
    observation_identity: str
    snapshot_identity: str
    fact_version: str
    mode: SnapshotMode
    provider: str
    endpoint: str
    request_identity: str
    response_sha256: str
    serialization_identifier: str
    snapshot_fetched_at: datetime


@dataclass(frozen=True)
class SnapshotReplay:
    payload: bytes
    manifest: dict[str, Any]


@dataclass(frozen=True)
class _ValidatedSnapshot:
    ref: SnapshotRef
    replay: SnapshotReplay


def _safe(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value not in {".", ".."}
        and _IDENTIFIER.fullmatch(value) is not None
    )


def _identity(
    request_identity: str, response_sha256: str, serialization: str, mode: SnapshotMode
) -> str:
    return hashlib.sha256(
        (request_identity + response_sha256 + serialization + mode.value).encode()
    ).hexdigest()


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = Path(os.path.abspath(root))

    def _final_path(
        self,
        provider: str,
        endpoint: str,
        request_identity: str,
        mode: SnapshotMode,
        serialization: str,
        response_sha256: str,
    ) -> Path:
        base = self.root / provider / endpoint / request_identity / mode.value
        if mode is SnapshotMode.APPEND:
            return (
                base / hashlib.sha256(serialization.encode("utf-8")).hexdigest() / response_sha256
            )
        return base

    def _read_validated(self, path: Path) -> _ValidatedSnapshot:
        path = Path(os.path.abspath(path))
        try:
            manifest = json.loads(
                (path / "manifest.json").read_text(encoding="utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            payload = (path / "response.bin").read_bytes()
        except Exception as exc:
            raise SnapshotIntegrityError("invalid snapshot files") from exc
        if not isinstance(manifest, dict):
            raise SnapshotIntegrityError("manifest fields mismatch")
        manifest = cast(dict[str, Any], manifest)
        if frozenset(manifest) != _MANIFEST_KEYS:
            raise SnapshotIntegrityError("manifest fields mismatch")
        if (
            type(manifest["manifest_schema_version"]) is not int
            or manifest["manifest_schema_version"] != 1
        ):
            raise SnapshotIntegrityError("manifest schema mismatch")
        provider, endpoint, serialization = (
            manifest["provider"],
            manifest["endpoint"],
            manifest["serialization_identifier"],
        )
        if not _safe(provider) or not _safe(endpoint) or not _safe(serialization):
            raise SnapshotIntegrityError("unsafe identifier")
        mode_raw = manifest["mode"]
        if not isinstance(mode_raw, str) or mode_raw not in {m.value for m in SnapshotMode}:
            raise SnapshotIntegrityError("mode mismatch")
        mode = SnapshotMode(mode_raw)
        request_identity = manifest["request_identity"]
        response_sha = manifest["response_sha256"]
        snapshot_identity = manifest["snapshot_identity"]
        if not all(
            isinstance(x, str) and _HEX64.fullmatch(x)
            for x in (request_identity, response_sha, snapshot_identity)
        ):
            raise SnapshotIntegrityError("identity mismatch")
        canonical = manifest["canonical_request"]
        if not isinstance(canonical, dict):
            raise SnapshotIntegrityError("canonical request mismatch")
        canonical = cast(dict[str, Any], canonical)
        if set(canonical) != {
            "provider",
            "endpoint",
            "parameters",
            "fields",
        }:
            raise SnapshotIntegrityError("canonical request mismatch")
        if canonical["provider"] != provider or canonical["endpoint"] != endpoint:
            raise SnapshotIntegrityError("canonical request mismatch")
        if not isinstance(canonical["parameters"], dict) or not isinstance(
            canonical["fields"], list
        ):
            raise SnapshotIntegrityError("canonical request mismatch")
        try:
            canonical_bytes = json.dumps(
                canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except Exception as exc:
            raise SnapshotIntegrityError("canonical request mismatch") from exc
        if hashlib.sha256(canonical_bytes).hexdigest() != request_identity:
            raise SnapshotIntegrityError("canonical request mismatch")
        if type(manifest["response_byte_size"]) is not int or manifest["response_byte_size"] != len(
            payload
        ):
            raise SnapshotIntegrityError("response size mismatch")
        if hashlib.sha256(payload).hexdigest() != response_sha:
            raise SnapshotIntegrityError("response hash mismatch")
        if not isinstance(manifest["retrieved_at"], str):
            raise SnapshotIntegrityError("timestamp mismatch")
        try:
            timestamp = datetime.fromisoformat(manifest["retrieved_at"])
            if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(None):
                raise ValueError
        except Exception as exc:
            raise SnapshotIntegrityError("timestamp mismatch") from exc
        if _identity(request_identity, response_sha, serialization, mode) != snapshot_identity:
            raise SnapshotIntegrityError("snapshot identity mismatch")
        expected_path = self._final_path(
            provider, endpoint, request_identity, mode, serialization, response_sha
        )
        if path != expected_path:
            raise SnapshotIntegrityError("snapshot path mismatch")
        ref = SnapshotRef(
            path,
            snapshot_identity,
            mode,
            provider,
            endpoint,
            request_identity,
            response_sha,
            serialization,
        )
        return _ValidatedSnapshot(ref, SnapshotReplay(payload, dict(manifest)))

    @staticmethod
    def _observation_identity(snapshot_identity: str, fetched_at: datetime) -> str:
        value = fetched_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        encoded = json.dumps(
            {"snapshot_fetched_at": value, "snapshot_identity": snapshot_identity},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _observation_parent(self, ref: SnapshotRef) -> Path:
        return (
            self.root
            / ref.provider
            / ref.endpoint
            / ref.request_identity
            / "observations"
            / ref.snapshot_identity
        )

    def _read_observation(self, path: Path, snapshot: SnapshotRef) -> SnapshotObservationRef:
        path = Path(os.path.abspath(path))
        if path.is_dir():
            path = path / "observation.json"
        try:
            manifest = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except Exception as exc:
            raise SnapshotIntegrityError("invalid observation files") from exc
        if not isinstance(manifest, dict):
            raise SnapshotIntegrityError("observation manifest fields mismatch")
        manifest = cast(dict[str, Any], manifest)
        if frozenset(manifest) != _OBSERVATION_KEYS:
            raise SnapshotIntegrityError("observation manifest fields mismatch")
        if (
            type(manifest.get("observation_schema_version")) is not int
            or manifest["observation_schema_version"] != 1
        ):
            raise SnapshotIntegrityError("observation schema mismatch")
        if any(
            not isinstance(manifest.get(k), str)
            for k in _OBSERVATION_KEYS - {"observation_schema_version"}
        ):
            raise SnapshotIntegrityError("observation fields mismatch")
        if any(
            manifest[k] != value
            for k, value in {
                "provider": snapshot.provider,
                "endpoint": snapshot.endpoint,
                "request_identity": snapshot.request_identity,
                "response_sha256": snapshot.response_sha256,
                "serialization_identifier": snapshot.serialization_identifier,
                "snapshot_identity": snapshot.snapshot_identity,
                "fact_version": snapshot.fact_version,
                "mode": snapshot.mode.value,
            }.items()
        ):
            raise SnapshotIntegrityError("observation link mismatch")
        fetched_raw = manifest["snapshot_fetched_at"]
        if not fetched_raw.endswith("Z"):
            raise SnapshotIntegrityError("observation timestamp mismatch")
        try:
            fetched = datetime.fromisoformat(fetched_raw)
            if fetched.tzinfo is None or fetched.utcoffset() != UTC.utcoffset(None):
                raise ValueError
        except Exception as exc:
            raise SnapshotIntegrityError("observation timestamp mismatch") from exc
        if fetched < self.provider_first_observed_at(snapshot):
            raise SnapshotIntegrityError("observation timestamp predates first observation")
        identity = manifest["observation_identity"]
        if (
            not _HEX64.fullmatch(identity)
            or self._observation_identity(snapshot.snapshot_identity, fetched) != identity
        ):
            raise SnapshotIntegrityError("observation identity mismatch")
        expected = self._observation_parent(snapshot) / identity / "observation.json"
        if path != expected:
            raise SnapshotIntegrityError("observation path mismatch")
        return SnapshotObservationRef(
            path,
            identity,
            snapshot.snapshot_identity,
            snapshot.fact_version,
            snapshot.mode,
            snapshot.provider,
            snapshot.endpoint,
            snapshot.request_identity,
            snapshot.response_sha256,
            snapshot.serialization_identifier,
            fetched,
        )

    def _resolve_existing(self, final: Path, desired: SnapshotRef) -> SnapshotRef:
        winner = self._read_validated(final).ref
        same_request = (winner.provider, winner.endpoint, winner.request_identity, winner.mode) == (
            desired.provider,
            desired.endpoint,
            desired.request_identity,
            desired.mode,
        )
        same_content = (
            winner.response_sha256 == desired.response_sha256
            and winner.serialization_identifier == desired.serialization_identifier
        )
        if same_request and same_content:
            return winner
        if desired.mode is SnapshotMode.FROZEN:
            raise SnapshotConflictError("frozen snapshot conflict")
        raise SnapshotIntegrityError("append snapshot collision")

    def write(
        self,
        spec: RequestSpec,
        payload: bytes,
        retrieved_at: datetime,
        serialization: str,
        mode: SnapshotMode = SnapshotMode.APPEND,
    ) -> SnapshotRef:
        if (
            type(payload) is not bytes
            or type(retrieved_at) is not datetime
            or retrieved_at.tzinfo is None
            or retrieved_at.utcoffset() is None
        ):
            raise TypeError("invalid snapshot input")
        if not _safe(serialization):
            raise ValueError("invalid snapshot input")
        if type(mode) is not SnapshotMode:
            raise TypeError("invalid snapshot mode")
        response_sha = hashlib.sha256(payload).hexdigest()
        final = self._final_path(
            spec.provider, spec.endpoint, spec.request_identity, mode, serialization, response_sha
        )
        desired = SnapshotRef(
            final,
            _identity(spec.request_identity, response_sha, serialization, mode),
            mode,
            spec.provider,
            spec.endpoint,
            spec.request_identity,
            response_sha,
            serialization,
        )
        if final.exists():
            return self._resolve_existing(final, desired)
        manifest = {
            "manifest_schema_version": 1,
            "provider": spec.provider,
            "endpoint": spec.endpoint,
            "canonical_request": spec.canonical_payload,
            "request_identity": spec.request_identity,
            "response_sha256": response_sha,
            "response_byte_size": len(payload),
            "serialization_identifier": serialization,
            "retrieved_at": retrieved_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "snapshot_identity": desired.snapshot_identity,
            "mode": mode.value,
        }
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=final.parent))
        try:
            (tmp / "response.bin").write_bytes(payload)
            (tmp / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                os.rename(tmp, final)
            except OSError:
                if final.exists():
                    return self._resolve_existing(final, desired)
                raise
            return self._read_validated(final).ref
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    def replay(self, ref: SnapshotRef, expected: RequestSpec | None = None) -> SnapshotReplay:
        validated = self._read_validated(ref.path)
        if validated.ref != ref:
            raise SnapshotIntegrityError("snapshot reference mismatch")
        if expected is not None and (
            validated.ref.request_identity != expected.request_identity
            or validated.replay.manifest["canonical_request"] != expected.canonical_payload
        ):
            raise SnapshotIntegrityError("request mismatch")
        return SnapshotReplay(validated.replay.payload, deepcopy(validated.replay.manifest))

    def provider_first_observed_at(self, snapshot_ref: SnapshotRef) -> datetime:
        validated = self._read_validated(snapshot_ref.path)
        if validated.ref != snapshot_ref:
            raise SnapshotIntegrityError("snapshot reference mismatch")
        value = validated.replay.manifest["retrieved_at"]
        return datetime.fromisoformat(value).astimezone(UTC)

    def observe(
        self,
        spec: RequestSpec,
        payload: bytes,
        snapshot_fetched_at: datetime,
        serialization: str,
        mode: SnapshotMode = SnapshotMode.APPEND,
    ) -> SnapshotObservationRef:
        if (
            type(snapshot_fetched_at) is not datetime
            or snapshot_fetched_at.tzinfo is None
            or snapshot_fetched_at.utcoffset() is None
        ):
            raise TypeError("invalid observation input")
        fetched = snapshot_fetched_at.astimezone(UTC)
        snapshot = self.write(spec, payload, fetched, serialization, mode)
        first = self.provider_first_observed_at(snapshot)
        if fetched < first:
            raise SnapshotIntegrityError("observation timestamp predates first observation")
        identity = self._observation_identity(snapshot.snapshot_identity, fetched)
        final = self._observation_parent(snapshot) / identity / "observation.json"
        if final.exists():
            return self._read_observation(final, snapshot)
        manifest = {
            "observation_schema_version": 1,
            "provider": snapshot.provider,
            "endpoint": snapshot.endpoint,
            "request_identity": snapshot.request_identity,
            "response_sha256": snapshot.response_sha256,
            "serialization_identifier": snapshot.serialization_identifier,
            "snapshot_identity": snapshot.snapshot_identity,
            "fact_version": snapshot.fact_version,
            "mode": snapshot.mode.value,
            "snapshot_fetched_at": fetched.isoformat().replace("+00:00", "Z"),
            "observation_identity": identity,
        }
        self._observation_parent(snapshot).mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=self._observation_parent(snapshot)))
        try:
            (tmp / "observation.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                os.rename(tmp, final.parent)
            except OSError:
                if final.exists():
                    return self._read_observation(final, snapshot)
                raise
            return self._read_observation(final, snapshot)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

    def observations(self, snapshot_ref: SnapshotRef) -> tuple[SnapshotObservationRef, ...]:
        validated = self._read_validated(snapshot_ref.path)
        if validated.ref != snapshot_ref:
            raise SnapshotIntegrityError("snapshot reference mismatch")
        parent = self._observation_parent(snapshot_ref)
        if not parent.exists():
            return ()
        if not parent.is_dir():
            raise SnapshotIntegrityError("observation ledger mismatch")
        refs: list[SnapshotObservationRef] = []
        for child in parent.iterdir():
            if not child.is_dir() or {p.name for p in child.iterdir()} != {"observation.json"}:
                raise SnapshotIntegrityError("observation ledger mismatch")
            refs.append(self._read_observation(child / "observation.json", snapshot_ref))
        return tuple(
            sorted(refs, key=lambda item: (item.snapshot_fetched_at, item.observation_identity))
        )

    def replay_observation(
        self, observation_ref: SnapshotObservationRef, expected: RequestSpec | None = None
    ) -> SnapshotReplay:
        if type(observation_ref.mode) is not SnapshotMode:
            raise SnapshotIntegrityError("observation reference mismatch")
        if any(
            not _safe(value)
            for value in (
                observation_ref.provider,
                observation_ref.endpoint,
                observation_ref.serialization_identifier,
            )
        ) or any(
            _HEX64.fullmatch(value) is None
            for value in (
                observation_ref.request_identity,
                observation_ref.response_sha256,
                observation_ref.snapshot_identity,
                observation_ref.fact_version,
                observation_ref.observation_identity,
            )
        ):
            raise SnapshotIntegrityError("observation reference mismatch")
        if (
            type(observation_ref.snapshot_fetched_at) is not datetime
            or observation_ref.snapshot_fetched_at.tzinfo is None
            or observation_ref.snapshot_fetched_at.utcoffset() is None
        ):
            raise SnapshotIntegrityError("observation reference mismatch")
        expected_obs = (
            self.root
            / observation_ref.provider
            / observation_ref.endpoint
            / observation_ref.request_identity
            / "observations"
            / observation_ref.snapshot_identity
            / observation_ref.observation_identity
            / "observation.json"
        )
        if Path(os.path.abspath(observation_ref.path)) != expected_obs:
            raise SnapshotIntegrityError("observation path mismatch")
        snapshot_path = self._final_path(
            observation_ref.provider,
            observation_ref.endpoint,
            observation_ref.request_identity,
            observation_ref.mode,
            observation_ref.serialization_identifier,
            observation_ref.response_sha256,
        )
        if not snapshot_path.exists():
            raise SnapshotIntegrityError("linked snapshot missing")
        snapshot = self._read_validated(snapshot_path).ref
        validated_obs = self._read_observation(observation_ref.path, snapshot)
        if validated_obs != observation_ref:
            raise SnapshotIntegrityError("observation reference mismatch")
        return self.replay(snapshot, expected)
