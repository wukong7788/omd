"""Canonical local-observation envelopes for retained SEC XBRL components.

These envelopes record only what the caller observed locally.  They do not
assert when a filing first became public.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from xml.etree import ElementTree

from ...core import SnapshotObservationRef
from .xbrl_package import SecXbrlPackageComponents

_SCHEMA = "sec-observed-xbrl-package-v1"
_MAX_BYTES = 8 * 1024 * 1024
_MAX_COMPONENT_BYTES = 2 * 1024 * 1024
_MAX_ELEMENTS = 200_000
_MAX_DEPTH = 128
_REQUIRED = frozenset({"schema", "presentation", "labels", "instance"})
_OPTIONAL = frozenset({"calculation", "definition"})
_COMPONENTS = _REQUIRED | _OPTIONAL
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _limit(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be a positive integer no greater than {maximum}")
    return value


def _hex(value: object, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 identity")
    return value


def _validate_components(
    components: SecXbrlPackageComponents,
    *,
    max_component_bytes: int,
    max_elements: int,
    max_depth: int,
) -> None:
    total = 0
    for name, value in components.to_mapping().items():
        if len(value) > max_component_bytes:
            raise ValueError("SEC observed XBRL component limit exceeded")
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("SEC observed XBRL component must be UTF-8") from exc
        if re.search(r"<!DOCTYPE|<!ENTITY", text, flags=re.IGNORECASE):
            raise ValueError("unsafe SEC observed XBRL XML declaration")
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise ValueError(f"malformed SEC observed XBRL {name} XML") from exc
        stack = [(root, 1)]
        while stack:
            element, depth = stack.pop()
            total += 1
            if total > max_elements:
                raise ValueError("SEC observed XBRL XML aggregate element limit exceeded")
            if depth > max_depth:
                raise ValueError("SEC observed XBRL XML depth limit exceeded")
            stack.extend((child, depth + 1) for child in element)


@dataclass(frozen=True)
class SecObservedXbrlPackage:
    """Components bound to the exact local SGML observation used to assemble them."""

    sgml_fact_version: str
    sgml_observation_identity: str
    cik: str
    accession_number: str
    form: str
    components: SecXbrlPackageComponents

    def __post_init__(self) -> None:
        _hex(self.sgml_fact_version, "sgml_fact_version")
        _hex(self.sgml_observation_identity, "sgml_observation_identity")
        if not isinstance(self.components, SecXbrlPackageComponents):
            raise TypeError("components must be SecXbrlPackageComponents")
        # Reuse the established filing validators without importing its legacy envelope.
        from .sgml_financials import SecSgmlFinancialsRequest

        SecSgmlFinancialsRequest(
            "observed", self.cik, self.accession_number, self.form, ("income_statement",), False
        )


def serialize_sec_observed_xbrl_package(
    *,
    sgml_observation: SnapshotObservationRef,
    cik: str,
    accession_number: str,
    form: str,
    components: SecXbrlPackageComponents,
    max_envelope_bytes: int = _MAX_BYTES,
    max_component_bytes: int = _MAX_COMPONENT_BYTES,
    max_xml_elements: int = _MAX_ELEMENTS,
    max_xml_depth: int = _MAX_DEPTH,
) -> bytes:
    """Encode one bounded local observed-package envelope without availability claims."""
    _limit(max_envelope_bytes, "max_envelope_bytes", _MAX_BYTES)
    _limit(max_component_bytes, "max_component_bytes", _MAX_COMPONENT_BYTES)
    _limit(max_xml_elements, "max_xml_elements", _MAX_ELEMENTS)
    _limit(max_xml_depth, "max_xml_depth", _MAX_DEPTH)
    if not isinstance(sgml_observation, SnapshotObservationRef):
        raise TypeError("sgml_observation must be SnapshotObservationRef")
    package = SecObservedXbrlPackage(
        sgml_observation.fact_version,
        sgml_observation.observation_identity,
        cik,
        accession_number,
        form,
        components,
    )
    _validate_components(
        package.components,
        max_component_bytes=max_component_bytes,
        max_elements=max_xml_elements,
        max_depth=max_xml_depth,
    )
    payload = {
        "schema": _SCHEMA,
        "filing": {
            "cik": package.cik,
            "accession_number": package.accession_number,
            "form": package.form,
            "sgml_fact_version": package.sgml_fact_version,
            "sgml_observation_identity": package.sgml_observation_identity,
        },
        "components": {
            name: base64.b64encode(value).decode("ascii")
            for name, value in sorted(package.components.to_mapping().items())
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_envelope_bytes:
        raise ValueError("SEC observed XBRL package exceeds limit")
    return encoded


def decode_sec_observed_xbrl_package(
    payload: bytes,
    *,
    max_envelope_bytes: int = _MAX_BYTES,
    max_component_bytes: int = _MAX_COMPONENT_BYTES,
    max_xml_elements: int = _MAX_ELEMENTS,
    max_xml_depth: int = _MAX_DEPTH,
) -> SecObservedXbrlPackage:
    """Strictly decode one canonical observed-package envelope."""
    _limit(max_envelope_bytes, "max_envelope_bytes", _MAX_BYTES)
    _limit(max_component_bytes, "max_component_bytes", _MAX_COMPONENT_BYTES)
    _limit(max_xml_elements, "max_xml_elements", _MAX_ELEMENTS)
    _limit(max_xml_depth, "max_xml_depth", _MAX_DEPTH)
    if type(payload) is not bytes or len(payload) > max_envelope_bytes:
        raise ValueError("SEC observed XBRL package exceeds limit")

    class _DuplicateKey(ValueError):
        pass

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise _DuplicateKey("duplicate SEC observed XBRL package JSON key")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except _DuplicateKey:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid SEC observed XBRL package") from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"schema", "filing", "components"}
        or decoded["schema"] != _SCHEMA
    ):
        raise ValueError("invalid SEC observed XBRL package envelope")
    if json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode("utf-8") != payload:
        raise ValueError("noncanonical SEC observed XBRL package envelope")
    filing, encoded_components = decoded["filing"], decoded["components"]
    fields = {"cik", "accession_number", "form", "sgml_fact_version", "sgml_observation_identity"}
    if (
        not isinstance(filing, Mapping)
        or set(filing) != fields
        or not isinstance(encoded_components, Mapping)
    ):
        raise ValueError("invalid SEC observed XBRL package envelope")
    if not _REQUIRED <= set(encoded_components) or not set(encoded_components) <= _COMPONENTS:
        raise ValueError("invalid SEC observed XBRL package components")
    raw: dict[str, bytes] = {}
    for name, value in encoded_components.items():
        if type(name) is not str or type(value) is not str:
            raise ValueError("invalid SEC observed XBRL package component")
        try:
            item = base64.b64decode(value.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError("invalid SEC observed XBRL package base64") from exc
        if base64.b64encode(item).decode("ascii") != value:
            raise ValueError("noncanonical SEC observed XBRL package base64")
        raw[name] = item
    components = SecXbrlPackageComponents(
        raw["schema"],
        raw["presentation"],
        raw["labels"],
        raw["instance"],
        raw.get("calculation"),
        raw.get("definition"),
    )
    _validate_components(
        components,
        max_component_bytes=max_component_bytes,
        max_elements=max_xml_elements,
        max_depth=max_xml_depth,
    )
    return SecObservedXbrlPackage(
        filing["sgml_fact_version"],
        filing["sgml_observation_identity"],
        filing["cik"],
        filing["accession_number"],
        filing["form"],
        components,
    )


__all__ = [
    "SecObservedXbrlPackage",
    "decode_sec_observed_xbrl_package",
    "serialize_sec_observed_xbrl_package",
]
