"""Offline SEC financial production versions and explicit PIT selection.

This module deliberately works from a caller-supplied, snapshot-backed typed-row
projection.  It validates that projection's internal binding to an SEC accession
and a caller-attested source artifact identity; it does not validate original
SEC XBRL or SGML bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from ...core import (
    AvailabilityBasis,
    AvailabilityEvidence,
    AvailabilityPrecision,
    SnapshotObservationRef,
    SnapshotStore,
)
from .financials import SecCompanyFinancialVintage, SecStatementRow

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PROJECTION_SCHEMA = "sec-financial-typed-rows-projection-v1"
_SERIALIZATION = "sec-financial-typed-rows-projection-v1"
_FACTORY_CAPABILITY = object()


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _sha(value: str, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 identity")
    return value


def _version(value: str, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _canonical(value: Any) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is Decimal:
        return {"decimal": str(value)}
    if type(value) is datetime:
        return {"datetime": _utc(value, "datetime").isoformat().replace("+00:00", "Z")}
    if type(value) is date:
        return {"date": value.isoformat()}
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _row_payload(row: SecStatementRow) -> dict[str, Any]:
    return _canonical(row.to_dict())


def _normalized_content_payload(
    *,
    observation: SnapshotObservationRef,
    accession_number: str,
    source_artifact_identity: str,
    source_available_at: datetime,
    vintage_identity: str,
    row_ordinal: int,
    row: SecStatementRow,
    schema_version: str,
    adapter_version: str,
    normalization_version: str,
    configuration_identity: str,
) -> dict[str, Any]:
    return {
        "fact_version": observation.fact_version,
        "observation_identity": observation.observation_identity,
        "accession_number": accession_number,
        "source_artifact_identity": source_artifact_identity,
        "source_available_at": source_available_at,
        "vintage_identity": vintage_identity,
        "row_ordinal": row_ordinal,
        "row": _row_payload(row),
        "schema_version": schema_version,
        "adapter_version": adapter_version,
        "normalization_version": normalization_version,
        "configuration_identity": configuration_identity,
    }


def serialize_sec_typed_rows_projection(
    vintage: SecCompanyFinancialVintage,
    *,
    source_artifact_identity: str,
    source_available_at: datetime,
) -> bytes:
    """Serialize a caller-attested typed-row projection for a SnapshotStore.

    ``source_artifact_identity`` is an identity supplied by the caller for the
    original source artifact.  The result is only a typed projection of it.
    """
    _sha(source_artifact_identity, "source_artifact_identity")
    available = _utc(source_available_at, "source_available_at")
    payload = {
        "schema": _PROJECTION_SCHEMA,
        "accession_number": vintage.accession_number,
        "source_artifact_identity": source_artifact_identity,
        "source_available_at": available.isoformat().replace("+00:00", "Z"),
        "vintage_identity": vintage.vintage_identity,
        "rows": [_row_payload(row) for row in vintage.rows],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode_projection(payload: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid SEC typed-row projection") from exc
    if not isinstance(decoded, dict) or set(decoded) != {
        "schema",
        "accession_number",
        "source_artifact_identity",
        "source_available_at",
        "vintage_identity",
        "rows",
    }:
        raise ValueError("invalid SEC typed-row projection fields")
    if decoded["schema"] != _PROJECTION_SCHEMA or not isinstance(decoded["accession_number"], str):
        raise ValueError("invalid SEC typed-row projection identity")
    _sha(decoded["source_artifact_identity"], "source_artifact_identity")
    _sha(decoded["vintage_identity"], "vintage_identity")
    try:
        value = datetime.fromisoformat(decoded["source_available_at"].removesuffix("Z") + "+00:00")
        _utc(value, "source_available_at")
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("invalid SEC typed-row projection availability") from exc
    if not isinstance(decoded["rows"], list):
        raise TypeError("invalid SEC typed-row projection rows")
    return decoded


class SecPitMode(str, Enum):
    MARKET_KNOWN = "MARKET_KNOWN"
    SYSTEM_REPLAY = "SYSTEM_REPLAY"


class SecQualityStatus(str, Enum):
    PASS = "PASS"
    QUARANTINED = "QUARANTINED"
    REVOKED = "REVOKED"


@dataclass(frozen=True)
class SecPitPolicy:
    """Versions chosen explicitly for a PIT query; there are no latest defaults."""

    schema_version: str
    adapter_version: str
    normalization_version: str
    configuration_identity: str
    quality_policy_version: str
    quality_cutoff: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.schema_version, "schema_version"),
            (self.adapter_version, "adapter_version"),
            (self.normalization_version, "normalization_version"),
            (self.quality_policy_version, "quality_policy_version"),
        ):
            _version(value, name)
        _sha(self.configuration_identity, "configuration_identity")
        object.__setattr__(self, "quality_cutoff", _utc(self.quality_cutoff, "quality_cutoff"))


@dataclass(frozen=True)
class SecNormalizedFinancialFactVersion:
    """One normalized typed SEC row, bound to a replay-verified observation."""

    observation: SnapshotObservationRef
    accession_number: str
    source_artifact_identity: str
    source_available_at: datetime
    vintage_identity: str
    row_ordinal: int
    row: SecStatementRow
    schema_version: str
    adapter_version: str
    normalization_version: str
    configuration_identity: str
    recorded_at: datetime
    _factory_capability: object = field(repr=False, compare=False)
    _projection_binding_identity: str = field(repr=False, compare=False)
    content_identity: str = field(init=False)
    normalized_version_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self._factory_capability is not _FACTORY_CAPABILITY:
            raise ValueError("use from_projection to create SEC normalized versions")
        if not self.accession_number:
            raise ValueError("accession_number is required")
        _sha(self.source_artifact_identity, "source_artifact_identity")
        _sha(self.vintage_identity, "vintage_identity")
        if type(self.row_ordinal) is not int or self.row_ordinal < 0:
            raise ValueError("row_ordinal must be a non-negative integer")
        for value, name in (
            (self.schema_version, "schema_version"),
            (self.adapter_version, "adapter_version"),
            (self.normalization_version, "normalization_version"),
        ):
            _version(value, name)
        _sha(self.configuration_identity, "configuration_identity")
        available = _utc(self.source_available_at, "source_available_at")
        recorded = _utc(self.recorded_at, "recorded_at")
        if recorded < self.observation.snapshot_fetched_at:
            raise ValueError("recorded_at must not precede the source observation")
        content = _normalized_content_payload(
            observation=self.observation,
            accession_number=self.accession_number,
            source_artifact_identity=self.source_artifact_identity,
            source_available_at=available,
            vintage_identity=self.vintage_identity,
            row_ordinal=self.row_ordinal,
            row=self.row,
            schema_version=self.schema_version,
            adapter_version=self.adapter_version,
            normalization_version=self.normalization_version,
            configuration_identity=self.configuration_identity,
        )
        expected_binding = _hash(
            {
                "content": content,
                "recorded_at": recorded,
                "projection_payload_sha256": self.observation.response_sha256,
            }
        )
        if self._projection_binding_identity != expected_binding:
            raise ValueError("SEC normalized version projection binding mismatch")
        object.__setattr__(self, "source_available_at", available)
        object.__setattr__(self, "recorded_at", recorded)
        content_identity = _hash(content)
        object.__setattr__(self, "content_identity", content_identity)
        object.__setattr__(
            self,
            "normalized_version_id",
            _hash({"content_identity": content_identity, "recorded_at": recorded}),
        )

    @classmethod
    def from_projection(
        cls,
        *,
        store: SnapshotStore,
        observation: SnapshotObservationRef,
        availability: AvailabilityEvidence,
        vintage: SecCompanyFinancialVintage,
        row_ordinal: int,
        schema_version: str,
        adapter_version: str,
        normalization_version: str,
        configuration_identity: str,
        recorded_at: datetime,
    ) -> SecNormalizedFinancialFactVersion:
        """Build only after replaying the exact typed projection snapshot."""
        if observation.provider != "sec" or observation.serialization_identifier != _SERIALIZATION:
            raise ValueError("observation is not a SEC typed-row projection")
        replay = store.replay_observation(observation)
        projection = _decode_projection(replay.payload)
        if availability.snapshot_fetched_at != observation.snapshot_fetched_at:
            raise ValueError("availability is not bound to observation")
        first_observed = datetime.fromisoformat(
            str(replay.manifest["retrieved_at"]).removesuffix("Z") + "+00:00"
        ).astimezone(UTC)
        if availability.provider_first_observed_at != first_observed:
            raise ValueError("availability is not bound to source snapshot")
        if (
            availability.availability_basis is not AvailabilityBasis.SOURCE_DECLARED
            or availability.availability_precision is not AvailabilityPrecision.TIMESTAMP
            or type(availability.source_available_at) is not datetime
        ):
            raise ValueError("SEC market source evidence must be caller-declared and timestamped")
        source_at = _utc(availability.source_available_at, "source_available_at")
        if source_at > observation.snapshot_fetched_at:
            raise ValueError("source availability cannot follow the source observation")
        projected_at = datetime.fromisoformat(
            projection["source_available_at"].removesuffix("Z") + "+00:00"
        ).astimezone(UTC)
        if projected_at != source_at:
            raise ValueError("projection availability assertion does not match evidence")
        if projection["accession_number"] != vintage.accession_number:
            raise ValueError("projection accession does not match typed vintage")
        if projection["vintage_identity"] != vintage.vintage_identity:
            raise ValueError("projection vintage identity does not match typed vintage")
        if row_ordinal < 0 or row_ordinal >= len(vintage.rows):
            raise ValueError("row_ordinal is outside typed vintage")
        row = vintage.rows[row_ordinal]
        if projection["rows"][row_ordinal] != _row_payload(row):
            raise ValueError("projection row does not match typed vintage")
        content = _normalized_content_payload(
            observation=observation,
            accession_number=vintage.accession_number,
            source_artifact_identity=projection["source_artifact_identity"],
            source_available_at=source_at,
            vintage_identity=vintage.vintage_identity,
            row_ordinal=row_ordinal,
            row=row,
            schema_version=schema_version,
            adapter_version=adapter_version,
            normalization_version=normalization_version,
            configuration_identity=configuration_identity,
        )
        return cls(
            observation=observation,
            accession_number=vintage.accession_number,
            source_artifact_identity=projection["source_artifact_identity"],
            source_available_at=source_at,
            vintage_identity=vintage.vintage_identity,
            row_ordinal=row_ordinal,
            row=row,
            schema_version=schema_version,
            adapter_version=adapter_version,
            normalization_version=normalization_version,
            configuration_identity=configuration_identity,
            recorded_at=recorded_at,
            _factory_capability=_FACTORY_CAPABILITY,
            _projection_binding_identity=_hash(
                {
                    "content": content,
                    "recorded_at": _utc(recorded_at, "recorded_at"),
                    "projection_payload_sha256": observation.response_sha256,
                }
            ),
        )


@dataclass(frozen=True)
class SecQualityRecord:
    normalized_version_id: str
    quality_policy_version: str
    status: SecQualityStatus
    recorded_at: datetime
    supersedes_quality_record_id: str | None = None
    quality_record_id: str = field(init=False)

    def __post_init__(self) -> None:
        _sha(self.normalized_version_id, "normalized_version_id")
        _version(self.quality_policy_version, "quality_policy_version")
        if type(self.status) is not SecQualityStatus:
            raise TypeError("status must be a SecQualityStatus")
        recorded = _utc(self.recorded_at, "recorded_at")
        if self.supersedes_quality_record_id is not None:
            _sha(self.supersedes_quality_record_id, "supersedes_quality_record_id")
        object.__setattr__(self, "recorded_at", recorded)
        object.__setattr__(
            self,
            "quality_record_id",
            _hash(
                {
                    "normalized_version_id": self.normalized_version_id,
                    "quality_policy_version": self.quality_policy_version,
                    "status": self.status.value,
                    "recorded_at": recorded,
                    "supersedes_quality_record_id": self.supersedes_quality_record_id,
                }
            ),
        )


@dataclass(frozen=True)
class SecConsumerCommit:
    normalized_version_id: str
    quality_record_id: str
    consumer_dataset_identity: str
    committed_at: datetime
    commit_id: str = field(init=False)

    def __post_init__(self) -> None:
        _sha(self.normalized_version_id, "normalized_version_id")
        _sha(self.quality_record_id, "quality_record_id")
        _sha(self.consumer_dataset_identity, "consumer_dataset_identity")
        committed = _utc(self.committed_at, "committed_at")
        object.__setattr__(self, "committed_at", committed)
        object.__setattr__(
            self,
            "commit_id",
            _hash(
                {
                    "normalized_version_id": self.normalized_version_id,
                    "quality_record_id": self.quality_record_id,
                    "consumer_dataset_identity": self.consumer_dataset_identity,
                    "committed_at": committed,
                }
            ),
        )


@dataclass(frozen=True)
class SecPitResult:
    version: SecNormalizedFinancialFactVersion
    quality_record: SecQualityRecord
    consumer_commit: SecConsumerCommit | None


def select_sec_financial_versions(
    versions: Iterable[SecNormalizedFinancialFactVersion],
    *,
    mode: SecPitMode,
    knowledge_cutoff: datetime,
    policy: SecPitPolicy,
    quality_records: Iterable[SecQualityRecord],
    consumer_commits: Iterable[SecConsumerCommit] = (),
) -> tuple[SecPitResult, ...]:
    """Return all eligible versions for an explicit mode and version policy."""
    from ._pit_query import select_sec_financial_versions as _select

    return _select(
        versions,
        mode=mode,
        knowledge_cutoff=knowledge_cutoff,
        policy=policy,
        quality_records=quality_records,
        consumer_commits=consumer_commits,
    )


__all__ = [
    "SecConsumerCommit",
    "SecNormalizedFinancialFactVersion",
    "SecPitMode",
    "SecPitPolicy",
    "SecPitResult",
    "SecQualityRecord",
    "SecQualityStatus",
    "select_sec_financial_versions",
    "serialize_sec_typed_rows_projection",
]
