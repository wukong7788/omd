"""Immutable provider-native raw fact envelopes."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any

from .availability import AvailabilityBasis, AvailabilityEvidence, AvailabilityPrecision
from .snapshot import SnapshotMode, SnapshotObservationRef, SnapshotStore

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FLAG = re.compile(r"^[A-Z][A-Z0-9_]*$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SECRET = re.compile(
    r"(^|_)(token|secret|password|api_key|access_key|authorization|cookie)$", re.IGNORECASE
)
_BUILTIN = frozenset({"PIT_UNPROVEN", "DATE_ONLY_AVAILABILITY", "REVISION_UNCLASSIFIED"})


class RawFactRevisionStatus(str, Enum):
    UNCLASSIFIED = "UNCLASSIFIED"
    UNCHANGED_FROM_PREVIOUS = "UNCHANGED_FROM_PREVIOUS"
    REVISED_FROM_PREVIOUS = "REVISED_FROM_PREVIOUS"


class RawFactQualityFlag(str, Enum):
    PIT_UNPROVEN = "PIT_UNPROVEN"
    DATE_ONLY_AVAILABILITY = "DATE_ONLY_AVAILABILITY"
    REVISION_UNCLASSIFIED = "REVISION_UNCLASSIFIED"


def _utc(value: Any, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _safe_identifier(value: Any, name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError(f"{name} must be a safe identifier")
    return value


def _encode(value: Any) -> Any:
    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value
    if type(value) is float:
        if math.isnan(value):
            return {"__float__": "nan"}
        if math.isinf(value):
            return {"__float__": "+inf" if value > 0 else "-inf"}
        return value
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("datetime values must be timezone-aware")
        return {"__datetime__": value.astimezone(UTC).isoformat().replace("+00:00", "Z")}
    if type(value) is date:
        return {"__date__": value.isoformat()}
    raise TypeError("native fields must contain supported scalar values")


def _validate_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(fields, Mapping):  # type: ignore[reportUnnecessaryIsInstance]
        raise TypeError("native_fields must be a mapping")
    copied: dict[str, Any] = {}
    for key, value in fields.items():
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", key) if type(key) is str else ""
        if type(key) is not str or not key or _SECRET.search(normalized):
            raise ValueError("native field name is unsafe")
        _encode(value)
        copied[key] = value
    return copied


def _canonical(fields: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    encoded = {key: _encode(fields[key]) for key in sorted(fields)}
    payload = json.dumps(encoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded, hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RawFactEnvelope:
    observation: SnapshotObservationRef
    availability: AvailabilityEvidence
    native_fields: Mapping[str, Any]
    primary_key_fields: tuple[str, ...]
    entity_fields: tuple[str, ...]
    native_schema_version: str
    adapter_version: str
    row_payload_sha256: str = field(init=False)
    revision_status: RawFactRevisionStatus = field(
        default=RawFactRevisionStatus.UNCLASSIFIED, init=False
    )
    previous_fact_version: str | None = field(default=None, init=False)
    previous_row_payload_sha256: str | None = field(default=None, init=False)
    quality_flags: tuple[str, ...] = ()

    ENVELOPE_SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        if not isinstance(self.observation, SnapshotObservationRef):  # type: ignore[reportUnnecessaryIsInstance]
            raise TypeError("observation must be a SnapshotObservationRef")
        if not isinstance(self.availability, AvailabilityEvidence):  # type: ignore[reportUnnecessaryIsInstance]
            raise TypeError("availability must be AvailabilityEvidence")
        for value, name in (
            (self.observation.provider, "provider"),
            (self.observation.endpoint, "endpoint"),
            (self.observation.serialization_identifier, "serialization_identifier"),
        ):
            _safe_identifier(value, name)
        for value, name in (
            (self.observation.request_identity, "request_identity"),
            (self.observation.response_sha256, "response_sha256"),
            (self.observation.snapshot_identity, "snapshot_identity"),
            (self.observation.fact_version, "fact_version"),
        ):
            if type(value) is not str or not _HEX64.fullmatch(value):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if type(self.observation.observation_identity) is not str or not _HEX64.fullmatch(
            self.observation.observation_identity
        ):
            raise ValueError("observation_identity must be lowercase SHA-256")
        if type(self.observation.mode) is not SnapshotMode:
            raise ValueError("observation mode is invalid")
        if self.observation.snapshot_fetched_at.utcoffset() != UTC.utcoffset(None):
            raise ValueError("observation timestamp must be UTC")
        expected_fact = hashlib.sha256(
            json.dumps(
                {
                    "request_identity": self.observation.request_identity,
                    "response_sha256": self.observation.response_sha256,
                    "serialization_identifier": self.observation.serialization_identifier,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        expected_snapshot = hashlib.sha256(
            (
                self.observation.request_identity
                + self.observation.response_sha256
                + self.observation.serialization_identifier
                + self.observation.mode.value
            ).encode()
        ).hexdigest()
        fetched = _utc(self.observation.snapshot_fetched_at, "snapshot_fetched_at")
        expected_observation = hashlib.sha256(
            json.dumps(
                {
                    "snapshot_fetched_at": fetched.isoformat().replace("+00:00", "Z"),
                    "snapshot_identity": self.observation.snapshot_identity,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if (
            self.observation.fact_version != expected_fact
            or self.observation.snapshot_identity != expected_snapshot
            or self.observation.observation_identity != expected_observation
        ):
            raise ValueError("observation identity mismatch")
        if self.availability.snapshot_fetched_at != _utc(
            self.observation.snapshot_fetched_at, "snapshot_fetched_at"
        ):
            raise ValueError("availability snapshot timestamp mismatch")
        if not isinstance(self.native_fields, Mapping):  # type: ignore[reportUnnecessaryIsInstance]
            raise TypeError("native_fields must be a mapping")
        fields = _validate_fields(self.native_fields)
        _, digest = _canonical(fields)
        object.__setattr__(self, "row_payload_sha256", digest)
        keys = tuple(self.primary_key_fields)
        entities = tuple(self.entity_fields)
        if (
            not keys
            or len(set(keys)) != len(keys)
            or not all(type(k) is str and k in fields for k in keys)
        ):
            raise ValueError("invalid primary key fields")
        if (
            not entities
            or len(set(entities)) != len(entities)
            or any(k not in keys for k in entities)
        ):
            raise ValueError("invalid entity fields")
        for key in (*keys, *entities):
            value = fields[key]
            if value is None or (type(value) is float and (not math.isfinite(value))):
                raise ValueError("identity values must be non-null and finite")
        schema = _safe_identifier(self.native_schema_version, "native_schema_version")
        adapter = _safe_identifier(self.adapter_version, "adapter_version")
        if type(self.revision_status) is not RawFactRevisionStatus:
            raise TypeError("revision_status must be RawFactRevisionStatus")
        for lineage in (self.previous_fact_version, self.previous_row_payload_sha256):
            if lineage is not None and (type(lineage) is not str or not _HEX64.fullmatch(lineage)):
                raise ValueError("invalid revision lineage")
        flags = tuple(self.quality_flags)
        if len(set(flags)) != len(flags) or any(
            type(flag) is not str or _FLAG.fullmatch(flag) is None for flag in flags
        ):
            raise ValueError("invalid quality flag")
        if _BUILTIN.intersection(flags):
            raise ValueError("built-in quality flags are computed")
        computed: list[str] = []
        if not self.availability.pit_proven:
            computed.append(RawFactQualityFlag.PIT_UNPROVEN.value)
        if self.availability.availability_precision is AvailabilityPrecision.DATE:
            computed.append(RawFactQualityFlag.DATE_ONLY_AVAILABILITY.value)
        if self.revision_status is RawFactRevisionStatus.UNCLASSIFIED:
            computed.append(RawFactQualityFlag.REVISION_UNCLASSIFIED.value)
        object.__setattr__(self, "native_fields", MappingProxyType(fields))
        object.__setattr__(self, "primary_key_fields", keys)
        object.__setattr__(self, "entity_fields", entities)
        object.__setattr__(self, "native_schema_version", schema)
        object.__setattr__(self, "adapter_version", adapter)
        object.__setattr__(self, "quality_flags", tuple(computed) + flags)

    @classmethod
    def from_observation(
        cls,
        store: SnapshotStore,
        observation: SnapshotObservationRef,
        *,
        native_fields: Mapping[str, Any],
        primary_key_fields: tuple[str, ...],
        entity_fields: tuple[str, ...],
        native_schema_version: str,
        adapter_version: str,
        source_available_at: datetime | date | None = None,
        availability_basis: AvailabilityBasis = AvailabilityBasis.PROVIDER_FIRST_OBSERVED,
        availability_precision: AvailabilityPrecision = AvailabilityPrecision.UNKNOWN,
        additional_quality_flags: tuple[str, ...] = (),
    ) -> RawFactEnvelope:
        availability = AvailabilityEvidence.from_observation(
            store,
            observation,
            source_available_at=source_available_at,
            availability_basis=availability_basis,
            availability_precision=availability_precision,
        )
        return cls(
            observation,
            availability,
            native_fields,
            primary_key_fields,
            entity_fields,
            native_schema_version,
            adapter_version,
            quality_flags=tuple(additional_quality_flags),
        )

    @property
    def provider(self) -> str:
        return self.observation.provider

    @property
    def endpoint(self) -> str:
        return self.observation.endpoint

    @property
    def request_identity(self) -> str:
        return self.observation.request_identity

    @property
    def payload_hash(self) -> str:
        return self.observation.response_sha256

    @property
    def snapshot_id(self) -> str:
        return self.observation.snapshot_identity

    @property
    def fact_version(self) -> str:
        return self.observation.fact_version

    @property
    def serialization_identifier(self) -> str:
        return self.observation.serialization_identifier

    @property
    def snapshot_fetched_at(self) -> datetime:
        return _utc(self.observation.snapshot_fetched_at, "snapshot_fetched_at")

    def classify_against(self, previous: RawFactEnvelope) -> RawFactEnvelope:
        if not isinstance(previous, RawFactEnvelope):  # type: ignore[reportUnnecessaryIsInstance]
            raise TypeError("previous must be RawFactEnvelope")
        if (
            self.provider,
            self.endpoint,
            self.primary_key_fields,
            self.entity_fields,
            self.native_schema_version,
            self.adapter_version,
        ) != (
            previous.provider,
            previous.endpoint,
            previous.primary_key_fields,
            previous.entity_fields,
            previous.native_schema_version,
            previous.adapter_version,
        ):
            raise ValueError("incompatible prior envelope")
        if any(
            type(self.native_fields[k]) is not type(previous.native_fields[k])
            or self.native_fields[k] != previous.native_fields[k]
            for k in self.primary_key_fields
        ):
            raise ValueError("primary key mismatch")
        if previous.snapshot_fetched_at > self.snapshot_fetched_at:
            raise ValueError("prior observation is in the future")
        if (
            self.row_payload_sha256 != previous.row_payload_sha256
            and self.fact_version == previous.fact_version
        ):
            raise ValueError("changed row under same fact version")
        status = (
            RawFactRevisionStatus.UNCHANGED_FROM_PREVIOUS
            if self.row_payload_sha256 == previous.row_payload_sha256
            else RawFactRevisionStatus.REVISED_FROM_PREVIOUS
        )
        result = RawFactEnvelope(
            self.observation,
            self.availability,
            self.native_fields,
            self.primary_key_fields,
            self.entity_fields,
            self.native_schema_version,
            self.adapter_version,
            quality_flags=tuple(f for f in self.quality_flags if f not in _BUILTIN),
        )
        object.__setattr__(result, "revision_status", status)
        object.__setattr__(result, "previous_fact_version", previous.fact_version)
        object.__setattr__(result, "previous_row_payload_sha256", previous.row_payload_sha256)
        flags = [f for f in result.quality_flags if f not in _BUILTIN]
        if not result.availability.pit_proven:
            flags.insert(0, RawFactQualityFlag.PIT_UNPROVEN.value)
        if result.availability.availability_precision is AvailabilityPrecision.DATE:
            flags.insert(
                1 if not result.availability.pit_proven else 0,
                RawFactQualityFlag.DATE_ONLY_AVAILABILITY.value,
            )
        object.__setattr__(result, "quality_flags", tuple(flags))
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope_schema_version": 1,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "request_identity": self.request_identity,
            "payload_hash": self.payload_hash,
            "snapshot_id": self.snapshot_id,
            "observation_identity": self.observation.observation_identity,
            "fact_version": self.fact_version,
            "serialization_identifier": self.serialization_identifier,
            "snapshot_fetched_at": self.snapshot_fetched_at.isoformat().replace("+00:00", "Z"),
            "native_fields": {k: _encode(v) for k, v in self.native_fields.items()},
            "primary_key_fields": list(self.primary_key_fields),
            "entity_fields": list(self.entity_fields),
            "native_schema_version": self.native_schema_version,
            "adapter_version": self.adapter_version,
            "row_payload_sha256": self.row_payload_sha256,
            "revision_status": self.revision_status.value,
            "previous_fact_version": self.previous_fact_version,
            "previous_row_payload_sha256": self.previous_row_payload_sha256,
            "quality_flags": list(self.quality_flags),
            "availability": self.availability.to_dict(),
        }
