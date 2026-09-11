"""Caller declarations and defensive copies for dated invalidation."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from ._event_discovery_models import _canonical, _stamp, _utc
from .errors import SchemaMismatchError
from .event_dependencies import SecDataVersionId, SecDataVersionKind, SecDependencyEdge
from .pit import SecPitMode


def _text(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be text")
    if not value or len(value) > 1024 or len(value.encode("utf-8")) > 1024:
        raise ValueError(f"{name} must contain 1..1024 UTF-8 bytes")
    if value != value.strip() or any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError(f"{name} contains whitespace or control characters")
    return value


def _identity(value: object, name: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 identity")
    return value


def _cik(value: object, name: str) -> str:
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]{0,9}", value) is None:
        raise ValueError(f"{name} must be canonical CIK digits")
    return value


def _optional_time(value: datetime | None, name: str) -> datetime | None:
    return None if value is None else _utc(value, name)


def _copy_version(value: object, name: str) -> SecDataVersionId:
    if type(value) is not SecDataVersionId:
        raise TypeError(f"{name} must be a typed version identity")
    return SecDataVersionId(value.kind, value.identity)


def _copy_edge(value: object) -> SecDependencyEdge:
    if type(value) is not SecDependencyEdge:
        raise TypeError("invalid dependency edge")
    declared_identity = value.edge_identity
    copy = SecDependencyEdge(
        _copy_version(value.input_version, "input_version"),
        _copy_version(value.output_version, "output_version"),
        value.canonical_cik,
        value.recipe_identity,
        value.recorded_at,
    )
    if declared_identity != copy.edge_identity:
        raise SchemaMismatchError("dated dependency edge identity mismatch")
    return copy


@dataclass(frozen=True)
class SecDatedInputChange:
    """A caller declaration that an old input version was superseded."""

    old_input_version: SecDataVersionId
    new_input_version: SecDataVersionId | None
    canonical_cik: str
    valuation_from: datetime
    valuation_to: datetime
    market_available_at: datetime | None
    system_available_at: datetime | None
    evidence_reference: str
    recorded_at: datetime
    change_identity: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "old_input_version", _copy_version(self.old_input_version, "old_input_version")
        )
        if self.new_input_version is not None:
            object.__setattr__(
                self,
                "new_input_version",
                _copy_version(self.new_input_version, "new_input_version"),
            )
            if self.new_input_version == self.old_input_version:
                raise ValueError("new_input_version must differ from old_input_version")
        _cik(self.canonical_cik, "canonical_cik")
        start = _utc(self.valuation_from, "valuation_from")
        end = _utc(self.valuation_to, "valuation_to")
        if start >= end:
            raise ValueError("valuation interval must be nonempty and half-open")
        object.__setattr__(self, "valuation_from", start)
        object.__setattr__(self, "valuation_to", end)
        object.__setattr__(
            self,
            "market_available_at",
            _optional_time(self.market_available_at, "market_available_at"),
        )
        object.__setattr__(
            self,
            "system_available_at",
            _optional_time(self.system_available_at, "system_available_at"),
        )
        _text(self.evidence_reference, "evidence_reference")
        object.__setattr__(self, "recorded_at", _utc(self.recorded_at, "recorded_at"))
        object.__setattr__(
            self,
            "change_identity",
            hashlib.sha256(_canonical(self.canonical_payload())).hexdigest(),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": "sec-dated-input-change-v1",
            "old_input_version": self.old_input_version.canonical_payload(),
            "new_input_version": None
            if self.new_input_version is None
            else self.new_input_version.canonical_payload(),
            "canonical_cik": self.canonical_cik,
            "valuation_from": _stamp(self.valuation_from),
            "valuation_to": _stamp(self.valuation_to),
            "market_available_at": None
            if self.market_available_at is None
            else _stamp(self.market_available_at),
            "system_available_at": None
            if self.system_available_at is None
            else _stamp(self.system_available_at),
            "evidence_reference": self.evidence_reference,
            "recorded_at": _stamp(self.recorded_at),
        }


@dataclass(frozen=True)
class SecDatedInvalidationTarget:
    """One exact, caller-declared dated derived-metric target."""

    output_version: SecDataVersionId
    canonical_cik: str
    instrument_id: str
    instrument_binding_identity: str
    recipe_identity: str
    valuation_at: datetime
    knowledge_cutoff: datetime
    mode: SecPitMode
    recorded_at: datetime
    target_identity: str = field(init=False)

    def __post_init__(self) -> None:
        output = _copy_version(self.output_version, "output_version")
        if output.kind is not SecDataVersionKind.DERIVED_METRIC:
            raise ValueError("dated target output must be DERIVED_METRIC")
        object.__setattr__(self, "output_version", output)
        _cik(self.canonical_cik, "canonical_cik")
        _text(self.instrument_id, "instrument_id")
        _identity(self.instrument_binding_identity, "instrument_binding_identity")
        _identity(self.recipe_identity, "recipe_identity")
        valuation_at = _utc(self.valuation_at, "valuation_at")
        cutoff = _utc(self.knowledge_cutoff, "knowledge_cutoff")
        if cutoff > valuation_at:
            raise ValueError("knowledge_cutoff cannot exceed valuation_at")
        object.__setattr__(self, "valuation_at", valuation_at)
        object.__setattr__(self, "knowledge_cutoff", cutoff)
        if type(self.mode) is not SecPitMode:
            raise TypeError("mode must be SecPitMode")
        object.__setattr__(self, "recorded_at", _utc(self.recorded_at, "recorded_at"))
        object.__setattr__(
            self,
            "target_identity",
            hashlib.sha256(_canonical(self.canonical_payload())).hexdigest(),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": "sec-dated-invalidation-target-v1",
            "output_version": self.output_version.canonical_payload(),
            "canonical_cik": self.canonical_cik,
            "instrument_id": self.instrument_id,
            "instrument_binding_identity": self.instrument_binding_identity,
            "recipe_identity": self.recipe_identity,
            "valuation_at": _stamp(self.valuation_at),
            "knowledge_cutoff": _stamp(self.knowledge_cutoff),
            "mode": self.mode.value,
            "recorded_at": _stamp(self.recorded_at),
        }


@dataclass(frozen=True)
class SecDatedInvalidationProof:
    change_identity: str
    target_identity: str
    edge_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identity(self.change_identity, "change_identity")
        _identity(self.target_identity, "target_identity")
        if type(self.edge_ids) is not tuple or not self.edge_ids:
            raise ValueError("proof requires a nonempty edge path")
        for edge_id in self.edge_ids:
            _identity(edge_id, "proof edge identity")


def _copy_change(value: object) -> SecDatedInputChange:
    if type(value) is not SecDatedInputChange:
        raise TypeError("invalid dated input change")
    copy = SecDatedInputChange(
        value.old_input_version,
        value.new_input_version,
        value.canonical_cik,
        value.valuation_from,
        value.valuation_to,
        value.market_available_at,
        value.system_available_at,
        value.evidence_reference,
        value.recorded_at,
    )
    if value.change_identity != copy.change_identity:
        raise SchemaMismatchError("dated input change identity mismatch")
    return copy


def _copy_target(value: object) -> SecDatedInvalidationTarget:
    if type(value) is not SecDatedInvalidationTarget:
        raise TypeError("invalid dated invalidation target")
    copy = SecDatedInvalidationTarget(
        value.output_version,
        value.canonical_cik,
        value.instrument_id,
        value.instrument_binding_identity,
        value.recipe_identity,
        value.valuation_at,
        value.knowledge_cutoff,
        value.mode,
        value.recorded_at,
    )
    if value.target_identity != copy.target_identity:
        raise SchemaMismatchError("dated invalidation target identity mismatch")
    return copy
