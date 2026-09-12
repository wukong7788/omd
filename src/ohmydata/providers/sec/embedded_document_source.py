"""Sealed, experimental SEC embedded-linkbase document-source closures."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import islice
from xml.etree import ElementTree
from xml.parsers import expat

from ...core import RequestSpec, SnapshotObservationRef, SnapshotStore
from ._document_source_links import _basename, _PrimaryReferences
from ._observed_financial_bundle_codec import _identity, _receipt, _stamp, _time, _utc
from .document_source import (
    SecDocumentSource,
    _canonical,
    _filing,
    _index,
    _request,
    _seal,
    _source_request,
)
from .event_discovery import _strict_json
from .sgml_financials import _validate_instance_identity

_SCHEMA = "sec-document-embedded-source-package-v1"
_VIEW_POLICY = "sec-schema-embedded-linkbase-view-v1"
_ENDPOINT = "company-filing-embedded-document-source-package"
_ROLES = frozenset({"submissions", "index", "primary", "schema", "instance"})
_MAX_MANIFEST = 256 * 1024
_MAX_TOTAL = 24 * 1024 * 1024
_MAX_BY_ROLE = {"primary": 8 * 1024 * 1024, "instance": 12 * 1024 * 1024}
_DEFAULT_MAX = 2 * 1024 * 1024
_LINK = "http://www.xbrl.org/2003/linkbase"
_XLINK = "http://www.w3.org/1999/xlink"
_XS = "http://www.w3.org/2001/XMLSchema"
_XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
_FACTORY = object()
ObservationResolver = Callable[[str], tuple[SnapshotStore, SnapshotObservationRef]]


@dataclass(frozen=True)
class SecEmbeddedDocumentSourcePackage:
    """Experimental source closure for a filing with embedded schema linkbases."""

    observation: SnapshotObservationRef
    manifest: bytes
    _capability: object = field(repr=False, compare=False)
    _binding: str = field(repr=False, compare=False)
    package_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if self._capability is not _FACTORY or type(self.manifest) is not bytes:
            raise ValueError("SEC embedded document package is created by source closure only")
        identity = hashlib.sha256(self.manifest).hexdigest()
        if (
            self._binding != _seal(self.observation, self.manifest)
            or self.observation.response_sha256 != identity
        ):
            raise ValueError("SEC embedded document package binding mismatch")
        object.__setattr__(self, "package_identity", identity)

    @property
    def known_by_at(self) -> datetime:
        return self.observation.snapshot_fetched_at


def _text(payload: bytes, role: str) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"SEC embedded {role} must be UTF-8") from exc
    if re.search(r"<!DOCTYPE|<!ENTITY", text, flags=re.IGNORECASE):
        raise ValueError("unsafe SEC embedded document declaration")
    return text


def _xml(payload: bytes, role: str) -> tuple[ElementTree.Element, int]:
    try:
        root = ElementTree.fromstring(_text(payload, role))
    except ElementTree.ParseError as exc:
        raise ValueError(f"malformed SEC embedded {role} XML") from exc
    count = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > 200_000:
            raise ValueError("SEC embedded document XML element limit exceeded")
        if depth > 128:
            raise ValueError("SEC embedded document XML depth limit exceeded")
        if _XML_BASE in element.attrib:
            raise ValueError("SEC document base URI overrides are unsupported")
        stack.extend((child, depth + 1) for child in element)
    return root, count


def _validate_schema(schema: ElementTree.Element) -> int:
    if schema.tag != f"{{{_XS}}}schema":
        raise ValueError("SEC embedded schema root must be xs:schema")
    appinfo = [item for item in schema.iter(f"{{{_XS}}}appinfo")]
    if len(appinfo) != 1:
        raise ValueError("SEC embedded schema requires exactly one xs:appinfo")
    embedded = [item for item in appinfo[0] if item.tag == f"{{{_LINK}}}linkbase"]
    linkbases = list(schema.iter(f"{{{_LINK}}}linkbase"))
    if len(embedded) != 1 or linkbases != embedded:
        raise ValueError("SEC embedded schema requires one direct link:linkbase")
    if any(item.tag == f"{{{_LINK}}}linkbaseRef" for item in schema.iter()):
        raise ValueError("external SEC filing linkbase references are unsupported")
    children = list(embedded[0])
    known = {
        f"{{{_LINK}}}{name}"
        for name in ("labelLink", "presentationLink", "calculationLink", "definitionLink")
    }
    if any(item.tag in known and item not in children for item in schema.iter()):
        raise ValueError("SEC embedded schema link content must be direct")
    for required in ("labelLink", "presentationLink"):
        if not any(item.tag == f"{{{_LINK}}}{required}" for item in children):
            raise ValueError(f"SEC embedded schema requires direct link:{required}")
    return len(list(schema.iter()))


def _validate_schema_prefix(text: str) -> None:
    """Keep the retained schema compatible with the pinned parser's link prefix."""
    parser = expat.ParserCreate(namespace_separator="|")
    parser.namespace_prefixes = True
    found = False

    def start(name: str, _: dict[str, str]) -> None:
        nonlocal found
        if name == f"{_LINK}|linkbase|link":
            found = True

    parser.StartElementHandler = start
    try:
        parser.Parse(text, True)
    except expat.ExpatError as exc:
        raise ValueError("malformed SEC embedded schema XML") from exc
    if not found:
        raise ValueError("SEC embedded schema requires the pinned link:linkbase namespace form")


def _build(
    request: dict[str, str], sources: Iterable[SecDocumentSource], captured_at: datetime
) -> bytes:
    captured = _utc(captured_at, "captured_at")
    selected = tuple(islice(sources, 6))
    if len(selected) != 5 or any(type(source) is not SecDocumentSource for source in selected):
        raise ValueError("invalid SEC embedded document source count or type")
    if {source.role for source in selected} != _ROLES:
        raise ValueError("invalid SEC embedded document source roles")
    if len({source.observation.observation_identity for source in selected}) != 5:
        raise ValueError("SEC embedded document source observations must be distinct")
    payloads: dict[str, bytes] = {}
    filenames: dict[str, str] = {}
    used = 0
    for source in sorted(selected, key=lambda item: item.role):
        if (
            type(source.store) is not SnapshotStore
            or type(source.observation) is not SnapshotObservationRef
        ):
            raise TypeError("SEC source requires SnapshotStore and SnapshotObservationRef")
        if source.role in {"submissions", "index"}:
            if source.filename is not None:
                raise ValueError("metadata source filename must be null")
        else:
            filenames[source.role] = _basename(source.filename)
        spec, serialization = _source_request(source, request)
        if source.observation.serialization_identifier != serialization:
            raise ValueError("SEC document source serialization mismatch")
        remaining = _MAX_TOTAL - used
        if remaining <= 0:
            raise ValueError("SEC embedded document aggregate byte limit exceeded")
        replay = source.store.replay_observation(
            source.observation, spec, min(_MAX_BY_ROLE.get(source.role, _DEFAULT_MAX), remaining)
        )
        payloads[source.role] = replay.payload
        used += len(replay.payload)
    if len(set(filenames.values())) != 3:
        raise ValueError("SEC embedded document filenames must be distinct")
    filing = _filing(payloads["submissions"], request)
    accepted = _time(filing["accepted_at"], "accepted_at")
    if any(
        not accepted <= source.observation.snapshot_fetched_at <= captured for source in selected
    ):
        raise ValueError("SEC embedded document source times are not causal")
    if filenames["primary"] != filing["primary_document"]:
        raise ValueError("SEC primary document does not match submissions")
    _index(payloads["index"], request, set(filenames.values()))
    primary = _PrimaryReferences()
    primary.feed(_text(payloads["primary"], "primary"))
    primary.close()
    if primary.stack or primary.references != [filenames["schema"]]:
        raise ValueError("SEC primary schemaRef does not bind selected schema")
    schema_text = _text(payloads["schema"], "schema")
    _validate_schema_prefix(schema_text)
    schema, schema_count = _xml(payloads["schema"], "schema")
    instance, instance_count = _xml(payloads["instance"], "instance")
    if primary.elements + schema_count + instance_count > 200_000:
        raise ValueError("SEC embedded document XML aggregate element limit exceeded")
    _validate_schema(schema)
    refs = [item for item in instance.iter() if item.tag.rsplit("}", 1)[-1] == "schemaRef"]
    if (
        len(refs) != 1
        or refs[0].tag != f"{{{_LINK}}}schemaRef"
        or _basename(refs[0].get(f"{{{_XLINK}}}href")) != filenames["schema"]
    ):
        raise ValueError("SEC instance schemaRef does not bind selected schema")
    _validate_instance_identity(_text(payloads["instance"], "instance"), request["cik"])
    manifest = _canonical(
        {
            "schema": _SCHEMA,
            "request": request,
            "filing": filing,
            "captured_at": _stamp(captured),
            "view_policy": _VIEW_POLICY,
            "sources": [
                {
                    "role": source.role,
                    "filename": source.filename,
                    "receipt": _receipt(source.observation),
                }
                for source in sorted(selected, key=lambda item: item.role)
            ],
        }
    )
    if len(manifest) > _MAX_MANIFEST:
        raise ValueError("SEC embedded document package envelope limit exceeded")
    return manifest


def produce_sec_embedded_document_source_package(
    *,
    store: SnapshotStore,
    cik: str,
    accession_number: str,
    form: str,
    sources: Iterable[SecDocumentSource],
    captured_at: datetime,
) -> SecEmbeddedDocumentSourcePackage:
    """Replay and seal an experimental embedded-linkbase filing source closure."""
    request = _request(cik, accession_number, form)
    manifest = _build(request, sources, captured_at)
    observation = store.observe(
        RequestSpec("sec", _ENDPOINT, request), manifest, _utc(captured_at, "captured_at"), _SCHEMA
    )
    return SecEmbeddedDocumentSourcePackage(
        observation, manifest, _FACTORY, _seal(observation, manifest)
    )


def restore_sec_embedded_document_source_package(
    *,
    store: SnapshotStore,
    observation: SnapshotObservationRef,
    resolve_observation: ObservationResolver,
) -> SecEmbeddedDocumentSourcePackage:
    """Rebuild the experimental closure from every raw receipt without writes."""
    if observation.serialization_identifier != _SCHEMA:
        raise ValueError("SEC embedded document package serialization mismatch")
    replay = store.replay_observation(observation, max_payload_bytes=_MAX_MANIFEST)
    root = _strict_json(replay.payload)
    if (
        set(root) != {"schema", "request", "filing", "captured_at", "view_policy", "sources"}
        or root["schema"] != _SCHEMA
        or root["view_policy"] != _VIEW_POLICY
    ):
        raise ValueError("invalid SEC embedded document package envelope")
    request_data = root["request"]
    if not isinstance(request_data, dict) or set(request_data) != {
        "cik",
        "accession_number",
        "form",
    }:
        raise ValueError("invalid SEC embedded document package request")
    request = _request(**request_data)
    store.replay_observation(observation, RequestSpec("sec", _ENDPOINT, request), _MAX_MANIFEST)
    captured = _time(root["captured_at"], "captured_at")
    if captured != observation.snapshot_fetched_at:
        raise ValueError("SEC embedded document package capture time mismatch")
    claims = root["sources"]
    if not isinstance(claims, list) or len(claims) != 5:
        raise ValueError("invalid SEC embedded source receipt count")
    sources = []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"role", "filename", "receipt"}:
            raise ValueError("invalid SEC embedded source receipt")
        receipt = claim["receipt"]
        if not isinstance(receipt, dict) or type(receipt.get("observation_identity")) is not str:
            raise ValueError("invalid SEC embedded source observation identity")
        dependency_store, dependency = resolve_observation(
            _identity(receipt["observation_identity"], "observation_identity")
        )
        if (
            type(dependency_store) is not SnapshotStore
            or type(dependency) is not SnapshotObservationRef
            or _canonical(_receipt(dependency)) != _canonical(receipt)
        ):
            raise ValueError("SEC resolved source receipt mismatch")
        sources.append(
            SecDocumentSource(claim["role"], claim["filename"], dependency_store, dependency)
        )
    manifest = _build(request, sources, captured)
    if manifest != replay.payload:
        raise ValueError("SEC embedded document package rebuilt bytes mismatch")
    return SecEmbeddedDocumentSourcePackage(
        observation, manifest, _FACTORY, _seal(observation, manifest)
    )
