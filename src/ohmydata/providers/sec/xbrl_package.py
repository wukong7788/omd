"""Canonical retained traditional-XBRL component packages for SEC replay."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.etree import ElementTree

from ...core import (
    AvailabilityBasis,
    AvailabilityEvidence,
    AvailabilityPrecision,
    SnapshotObservationRef,
)

_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_CIK = re.compile(r"^[0-9]{10}$")
_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SERIALIZATION = "sec-xbrl-package-v1"
_MAX_BYTES = 8 * 1024 * 1024
_MAX_COMPONENT_BYTES = 2 * 1024 * 1024
_MAX_ELEMENTS = 200_000
_REQUIRED = frozenset({"schema", "presentation", "labels", "instance"})
_OPTIONAL = frozenset({"calculation", "definition"})
_COMPONENTS = _REQUIRED | _OPTIONAL


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _filing(cik: object, accession_number: object, form: object) -> tuple[str, str, str]:
    if type(cik) is not str or _CIK.fullmatch(cik) is None:
        raise ValueError("cik must be a 10-digit string")
    if type(accession_number) is not str or _ACCESSION.fullmatch(accession_number) is None:
        raise ValueError("invalid accession_number")
    if type(form) is not str or form not in _FORMS:
        raise ValueError("unsupported form")
    return cik, accession_number, form


def _hex(value: object, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 identity")
    return value


def _xml(value: bytes, name: str) -> int:
    if len(value) > _MAX_COMPONENT_BYTES:
        raise ValueError("SEC XBRL package component limit exceeded")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SEC XBRL package component must be UTF-8") from exc
    if re.search(r"<!DOCTYPE|<!ENTITY", text, flags=re.IGNORECASE):
        raise ValueError("unsafe XML declaration")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"malformed SEC XBRL package {name} XML") from exc
    count = 0
    stack = [(root, 1)]
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > _MAX_ELEMENTS:
            raise ValueError("SEC XBRL package XML element limit exceeded")
        if depth > 128:
            raise ValueError("SEC XBRL package XML depth limit exceeded")
        stack.extend((child, depth + 1) for child in item)
    return count


@dataclass(frozen=True)
class SecXbrlPackageComponents:
    """Original traditional-XBRL component bytes retained in one envelope."""

    schema: bytes
    presentation: bytes
    labels: bytes
    instance: bytes
    calculation: bytes | None = None
    definition: bytes | None = None

    def __post_init__(self) -> None:
        total = 0
        for name, value in self.to_mapping().items():
            if type(value) is not bytes:
                raise TypeError(f"{name} must be bytes")
            total += _xml(value, name)
        if total > _MAX_ELEMENTS:
            raise ValueError("SEC XBRL package XML aggregate element limit exceeded")

    def to_mapping(self) -> dict[str, bytes]:
        result = {
            "schema": self.schema,
            "presentation": self.presentation,
            "labels": self.labels,
            "instance": self.instance,
        }
        if self.calculation is not None:
            result["calculation"] = self.calculation
        if self.definition is not None:
            result["definition"] = self.definition
        return result


@dataclass(frozen=True)
class SecXbrlPackage:
    """Decoded package bound to a retained full-SGML observation."""

    sgml_fact_version: str
    sgml_observation_identity: str
    cik: str
    accession_number: str
    form: str
    source_available_at: datetime
    components: SecXbrlPackageComponents

    def __post_init__(self) -> None:
        _hex(self.sgml_fact_version, "sgml_fact_version")
        _hex(self.sgml_observation_identity, "sgml_observation_identity")
        _filing(self.cik, self.accession_number, self.form)
        object.__setattr__(
            self, "source_available_at", _utc(self.source_available_at, "source_available_at")
        )
        if not isinstance(self.components, SecXbrlPackageComponents):
            raise TypeError("components must be SecXbrlPackageComponents")


@dataclass(frozen=True)
class SecXbrlPackageAvailability:
    """Caller-supplied declared availability bound to one package receipt."""

    package_observation: SnapshotObservationRef
    evidence: AvailabilityEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.package_observation, SnapshotObservationRef):
            raise TypeError("package_observation must be SnapshotObservationRef")
        if not isinstance(self.evidence, AvailabilityEvidence):
            raise TypeError("evidence must be AvailabilityEvidence")
        if (
            self.evidence.availability_basis is not AvailabilityBasis.SOURCE_DECLARED
            or self.evidence.availability_precision is not AvailabilityPrecision.TIMESTAMP
            or type(self.evidence.source_available_at) is not datetime
        ):
            raise ValueError(
                "package availability must be SOURCE_DECLARED with TIMESTAMP precision"
            )
        if self.evidence.snapshot_fetched_at != self.package_observation.snapshot_fetched_at:
            raise ValueError("package availability does not bind the package observation")


def serialize_sec_xbrl_package(
    *,
    sgml_observation: SnapshotObservationRef,
    cik: str,
    accession_number: str,
    form: str,
    source_available_at: datetime,
    components: SecXbrlPackageComponents,
) -> bytes:
    """Encode original extracted traditional-XBRL components for offline replay."""
    if not isinstance(sgml_observation, SnapshotObservationRef):
        raise TypeError("sgml_observation must be SnapshotObservationRef")
    cik, accession_number, form = _filing(cik, accession_number, form)
    if not isinstance(components, SecXbrlPackageComponents):
        raise TypeError("components must be SecXbrlPackageComponents")
    available = _utc(source_available_at, "source_available_at")
    payload = {
        "schema": _SERIALIZATION,
        "filing": {
            "cik": cik,
            "accession_number": accession_number,
            "form": form,
            "sgml_fact_version": _hex(sgml_observation.fact_version, "sgml_fact_version"),
            "sgml_observation_identity": _hex(
                sgml_observation.observation_identity, "sgml_observation_identity"
            ),
        },
        "source_available_at": available.isoformat().replace("+00:00", "Z"),
        "components": {
            name: base64.b64encode(value).decode("ascii")
            for name, value in sorted(components.to_mapping().items())
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_BYTES:
        raise ValueError("SEC XBRL package exceeds limit")
    return encoded


def decode_sec_xbrl_package(payload: bytes) -> SecXbrlPackage:
    """Strictly decode one canonical retained package envelope."""
    if type(payload) is not bytes or len(payload) > _MAX_BYTES:
        raise ValueError("SEC XBRL package exceeds limit")

    class _DuplicateKey(ValueError):
        pass

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise _DuplicateKey("duplicate SEC XBRL package JSON key")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")),
        )
    except _DuplicateKey:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid SEC XBRL package") from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"schema", "filing", "source_available_at", "components"}
        or decoded["schema"] != _SERIALIZATION
    ):
        raise ValueError("invalid SEC XBRL package envelope")
    if json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode("utf-8") != payload:
        raise ValueError("noncanonical SEC XBRL package envelope")
    filing = decoded["filing"]
    components = decoded["components"]
    if (
        not isinstance(filing, Mapping)
        or set(filing)
        != {"cik", "accession_number", "form", "sgml_fact_version", "sgml_observation_identity"}
        or not isinstance(components, Mapping)
    ):
        raise ValueError("invalid SEC XBRL package envelope")
    if not _REQUIRED <= set(components) or not set(components) <= _COMPONENTS:
        raise ValueError("invalid SEC XBRL package components")
    raw: dict[str, bytes] = {}
    for name, value in components.items():
        if type(name) is not str or type(value) is not str:
            raise ValueError("invalid SEC XBRL package component")
        try:
            item = base64.b64decode(value.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError("invalid SEC XBRL package base64") from exc
        if base64.b64encode(item).decode("ascii") != value:
            raise ValueError("noncanonical SEC XBRL package base64")
        raw[name] = item
    source_at = decoded["source_available_at"]
    if type(source_at) is not str or not source_at.endswith("Z"):
        raise ValueError("invalid SEC XBRL package availability")
    try:
        available = datetime.fromisoformat(source_at.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("invalid SEC XBRL package availability") from exc
    return SecXbrlPackage(
        sgml_fact_version=filing["sgml_fact_version"],
        sgml_observation_identity=filing["sgml_observation_identity"],
        cik=filing["cik"],
        accession_number=filing["accession_number"],
        form=filing["form"],
        source_available_at=available,
        components=SecXbrlPackageComponents(
            schema=raw["schema"],
            presentation=raw["presentation"],
            labels=raw["labels"],
            instance=raw["instance"],
            calculation=raw.get("calculation"),
            definition=raw.get("definition"),
        ),
    )


__all__ = [
    "SecXbrlPackage",
    "SecXbrlPackageAvailability",
    "SecXbrlPackageComponents",
    "decode_sec_xbrl_package",
    "serialize_sec_xbrl_package",
]
