"""Bounded admission checks for SEC structural-quality evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from ...core.snapshot import SnapshotMode, SnapshotObservationRef
from .financials import SecStatementRow
from .pit import SecNormalizedFinancialFactVersion

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT = 4_096
_MAX_RECIPE_TEXT = 128
_MAX_DECIMAL_DIGITS = 1_024
_MAX_DECIMAL_EXPONENT = 10_000
_SERIALIZATION = "sec-financial-typed-rows-projection-v1"
_ROW_TEXT = (
    "statement_type",
    "standard_concept",
    "concept",
    "label",
    "value_native",
    "unit",
    "period_type",
    "dimension",
    "period_key",
    "context_ref",
    "unit_ref",
    "decimals_native",
    "period_source",
    "currency",
)
_RECIPE_TEXT = ("schema_version", "adapter_version", "normalization_version")


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _sha(value: object, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 identity")
    return value


def _limit(value: object, name: str, maximum: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds evaluator text limit")
    return value


def _limit_optional(value: object, name: str) -> None:
    if value is not None:
        _limit(value, name, _MAX_TEXT)


def _check_row(row: object) -> SecStatementRow:
    if type(row) is not SecStatementRow:
        raise TypeError("version row must be a SecStatementRow")
    for name in ("statement_type", "standard_concept", "concept", "label"):
        _limit(getattr(row, name), name, _MAX_TEXT)
    for name in _ROW_TEXT[4:]:
        _limit_optional(getattr(row, name), name)
    value = row.value
    if value is not None:
        if type(value) is not Decimal:
            raise TypeError("row value must be a Decimal or None")
        if not value.is_finite():
            raise ValueError("row value must be finite")
        decimal = value.as_tuple()
        if type(decimal.exponent) is not int:
            raise ValueError("row value must have a finite exponent")
        if (
            len(decimal.digits) > _MAX_DECIMAL_DIGITS
            or abs(decimal.exponent) > _MAX_DECIMAL_EXPONENT
        ):
            raise ValueError("row value exceeds evaluator decimal limit")
    if type(row.decimals) is not int and row.decimals is not None:
        raise TypeError("row decimals must be an integer or None")
    for name in ("period_start", "period_end"):
        value = getattr(row, name)
        if value is not None and type(value) is not date:
            raise TypeError(f"{name} must be a date or None")
    if type(row.is_point_in_time) is not bool:
        raise TypeError("row is_point_in_time must be a bool")
    rebuilt = SecStatementRow(
        row.statement_type,
        row.standard_concept,
        row.concept,
        row.label,
        row.value,
        row.value_native,
        row.unit,
        row.decimals,
        row.period_start,
        row.period_end,
        period_type=row.period_type,
        dimension=row.dimension,
        period_key=row.period_key,
        context_ref=row.context_ref,
        unit_ref=row.unit_ref,
        decimals_native=row.decimals_native,
        period_source=row.period_source,
        is_point_in_time=row.is_point_in_time,
    )
    if rebuilt != row:
        raise ValueError("version row is not canonical")
    return rebuilt


def _observation(observation: object) -> SnapshotObservationRef:
    if type(observation) is not SnapshotObservationRef:
        raise TypeError("version observation must be a SnapshotObservationRef")
    if not isinstance(observation.path, Path) or len(str(observation.path)) > _MAX_TEXT:
        raise ValueError("invalid observation path")
    for name in (
        "observation_identity",
        "snapshot_identity",
        "fact_version",
        "request_identity",
        "response_sha256",
    ):
        _sha(getattr(observation, name), name)
    if type(observation.mode) is not SnapshotMode:
        raise TypeError("invalid observation mode")
    if _limit(observation.provider, "provider", _MAX_RECIPE_TEXT) != "sec":
        raise ValueError("observation is not SEC")
    _limit(observation.endpoint, "endpoint", _MAX_TEXT)
    if (
        _limit(observation.serialization_identifier, "serialization_identifier", _MAX_RECIPE_TEXT)
        != _SERIALIZATION
    ):
        raise ValueError("observation is not a SEC typed-row projection")
    fetched = _utc(observation.snapshot_fetched_at, "snapshot_fetched_at")
    expected_snapshot = hashlib.sha256(
        (
            observation.request_identity
            + observation.response_sha256
            + observation.serialization_identifier
            + observation.mode.value
        ).encode()
    ).hexdigest()
    expected_fact = hashlib.sha256(
        json.dumps(
            {
                "request_identity": observation.request_identity,
                "response_sha256": observation.response_sha256,
                "serialization_identifier": observation.serialization_identifier,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    expected_observation = hashlib.sha256(
        json.dumps(
            {
                "snapshot_fetched_at": fetched.isoformat().replace("+00:00", "Z"),
                "snapshot_identity": observation.snapshot_identity,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if (
        observation.snapshot_identity,
        observation.fact_version,
        observation.observation_identity,
    ) != (expected_snapshot, expected_fact, expected_observation):
        raise ValueError("observation identity does not match its metadata")
    return observation


def admit(version: object) -> SecNormalizedFinancialFactVersion:
    """Validate bounded row/observation shape and immutable version bindings."""
    if type(version) is not SecNormalizedFinancialFactVersion:
        raise TypeError("versions must contain SecNormalizedFinancialFactVersion values")
    _check_row(version.row)
    _observation(version.observation)
    _limit(version.accession_number, "accession_number", _MAX_TEXT)
    for name in _RECIPE_TEXT:
        _limit(getattr(version, name), name, _MAX_RECIPE_TEXT)
    try:
        rebuilt = replace(version)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid normalized version binding") from exc
    if rebuilt != version:
        raise ValueError("normalized version identity does not match its content")
    return version
