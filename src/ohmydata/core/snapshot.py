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
