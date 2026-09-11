"""Strict reconstruction of work journal input records."""

from __future__ import annotations

from typing import Any

from ._event_discovery_models import _parse_stamp
from ._event_work_models import (
    SecEventWorkCommand,
    SecEventWorkErrorClass,
    SecEventWorkPhase,
    SecEventWorkSpec,
)
from .errors import ResourceLimitError, SchemaMismatchError
from .event_dependencies import SecDataVersionId, SecDataVersionKind, SecDependencyEdge


def _array(value: object, limit: int) -> list[Any]:
    if type(value) is not list:
        raise SchemaMismatchError("work collection must be a JSON array")
    if len(value) > limit:
        raise ResourceLimitError("work collection limit exceeded")
    return value


def _version(value: Any) -> SecDataVersionId:
    if type(value) is not dict or set(value) != {"kind", "identity"}:
        raise SchemaMismatchError("invalid work version reference")
    return SecDataVersionId(SecDataVersionKind(value["kind"]), value["identity"])


def _edge(value: Any) -> SecDependencyEdge:
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema",
            "input_version",
            "output_version",
            "canonical_cik",
            "recipe_identity",
            "recorded_at",
        }
        or value["schema"] != "sec-dependency-edge-v1"
    ):
        raise SchemaMismatchError("invalid work dependency edge")
    return SecDependencyEdge(
        _version(value["input_version"]),
        _version(value["output_version"]),
        value["canonical_cik"],
        value["recipe_identity"],
        _parse_stamp(value["recorded_at"], "edge time"),
    )


def decode_spec(value: Any) -> SecEventWorkSpec:
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema",
            "event_key",
            "metadata_digest",
            "processing_version",
            "configuration_identity",
            "input_version_ids",
            "max_attempts",
        }
        or value["schema"] != "sec-event-work-spec-v1"
    ):
        raise SchemaMismatchError("invalid work specification")
    try:
        return SecEventWorkSpec(
            value["event_key"],
            value["metadata_digest"],
            value["processing_version"],
            value["configuration_identity"],
            tuple(_version(item) for item in _array(value["input_version_ids"], 1024)),
            value["max_attempts"],
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise SchemaMismatchError("invalid work specification") from exc


def decode_command(value: Any) -> SecEventWorkCommand:
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema",
            "command_id",
            "recorded_at",
            "target_state",
            "error_class",
            "reason_code",
            "retry_at",
            "output_version_ids",
            "quality_evidence_ids",
            "dependency_edges",
        }
        or value["schema"] != "sec-event-work-command-v1"
    ):
        raise SchemaMismatchError("invalid work command")
    try:
        return SecEventWorkCommand(
            value["command_id"],
            _parse_stamp(value["recorded_at"], "command time"),
            SecEventWorkPhase(value["target_state"]),
            None if value["error_class"] is None else SecEventWorkErrorClass(value["error_class"]),
            value["reason_code"],
            None if value["retry_at"] is None else _parse_stamp(value["retry_at"], "retry time"),
            tuple(_version(item) for item in _array(value["output_version_ids"], 1024)),
            tuple(_array(value["quality_evidence_ids"], 1024)),
            tuple(_edge(item) for item in _array(value["dependency_edges"], 10_000)),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise SchemaMismatchError("invalid work command") from exc
