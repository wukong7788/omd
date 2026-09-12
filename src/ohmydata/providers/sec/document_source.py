"""Retained, bounded document-source closures without full-submission SGML."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import islice

from ...core import RequestSpec, SnapshotObservationRef, SnapshotStore
from ._document_source_links import _basename, _validate_links
from ._event_discovery_models import _parse_stamp
from ._observed_financial_bundle_codec import _identity, _receipt, _stamp, _time, _utc
from .event_discovery import _strict_json, _submission_rows
from .sgml_financials import SecSgmlFinancialsRequest

_SCHEMA = "sec-document-source-package-v1"
_FACTORY = object()
_REQUIRED = frozenset(
    {"submissions", "index", "primary", "schema", "presentation", "labels", "instance"}
)
_ROLES = _REQUIRED | {"calculation", "definition"}
_MAX_TOTAL = 16 * 1024 * 1024
_MAX_PACKAGE = 256 * 1024
_ENDPOINT = "company-filing-document-source-package"
ObservationResolver = Callable[[str], tuple[SnapshotStore, SnapshotObservationRef]]


@dataclass(frozen=True)
class SecDocumentSource:
    """One caller-selected original observation; stores are never serialized."""

    role: str
    filename: str | None
    store: SnapshotStore
    observation: SnapshotObservationRef


@dataclass(frozen=True)
class SecDocumentSourcePackage:
    """Sealed source closure, not a financial production or quality approval."""

    observation: SnapshotObservationRef
    manifest: bytes
    _capability: object = field(repr=False, compare=False)
    _binding: str = field(repr=False, compare=False)
    package_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if self._capability is not _FACTORY or type(self.manifest) is not bytes:
            raise ValueError("SEC document package is created by source closure only")
        identity = hashlib.sha256(self.manifest).hexdigest()
        binding = _seal(self.observation, self.manifest)
        if self._binding != binding or self.observation.response_sha256 != identity:
            raise ValueError("SEC document package binding mismatch")
        object.__setattr__(self, "package_identity", identity)

    @property
    def known_by_at(self) -> datetime:
        return self.observation.snapshot_fetched_at


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _seal(observation: SnapshotObservationRef, manifest: bytes) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "receipt": _receipt(observation),
                "payload": hashlib.sha256(manifest).hexdigest(),
                "path": str(observation.path),
            }
        )
    ).hexdigest()


def _request(cik: str, accession_number: str, form: str) -> dict[str, str]:
    SecSgmlFinancialsRequest(
        "source-closure", cik, accession_number, form, ("income_statement",), False
    )
    return {"cik": cik, "accession_number": accession_number, "form": form}


def _source_request(source: SecDocumentSource, request: dict[str, str]) -> tuple[RequestSpec, str]:
    cik, accession = request["cik"], request["accession_number"]
    if source.role == "submissions":
        return RequestSpec("sec", "edgar_submissions", {"cik": cik}), "sec-submissions-json-v1"
    if source.role == "index":
        return RequestSpec(
            "sec", "company-filing-directory", {"cik": cik, "accession_number": accession}
        ), "sec-filing-directory-json-v1"
    return RequestSpec(
        "sec",
        "company-filing-document",
        {"cik": cik, "accession_number": accession, "filename": source.filename},
    ), "sec-filing-document-bytes-v1"


def _date(value: object) -> str:
    if type(value) is not str or date.fromisoformat(value).isoformat() != value:
        raise ValueError("invalid SEC filing date")
    return value


def _filing(payload: bytes, request: dict[str, str]) -> dict[str, str]:
    root = _strict_json(payload)
    if type(root.get("cik")) not in (str, int) or str(root["cik"]).zfill(10) != request["cik"]:
        raise ValueError("SEC submissions CIK mismatch")
    filings = root.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    accessions = recent.get("accessionNumber") if isinstance(recent, dict) else None
    if not isinstance(accessions, (list, tuple)) or len(accessions) > 10_000:
        raise ValueError("SEC source filing row limit exceeded or malformed")
    rows = _submission_rows(root, request["cik"], child=False)
    matches = [row for row in rows if row["accessionNumber"] == request["accession_number"]]
    if len(matches) != 1 or matches[0]["form"] != request["form"]:
        raise ValueError("SEC filing selection is missing, ambiguous or conflicting")
    row = matches[0]
    name = root.get("name")
    if type(name) is not str or not name.strip() or len(name) > 1024:
        raise ValueError("SEC company name is missing or invalid")
    accepted = _parse_stamp(row["acceptanceDateTime"], "acceptanceDateTime")
    return {
        "company_name": name,
        "filing_date": _date(row["filingDate"]),
        "period_end": _date(row["reportDate"]),
        "accepted_at": _stamp(accepted),
        "primary_document": _basename(row["primaryDocument"]),
    }


def _index(payload: bytes, request: dict[str, str], filenames: set[str]) -> None:
    root = _strict_json(payload)
    directory = root.get("directory")
    expected = (
        f"/Archives/edgar/data/{int(request['cik'])}/{request['accession_number'].replace('-', '')}"
    )
    if not isinstance(directory, dict) or directory.get("name") != expected:
        raise ValueError("SEC filing directory identity mismatch")
    items = directory.get("item")
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= 10_000:
        raise ValueError("invalid SEC directory entries")
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid SEC directory entry")  # noqa: TRY004 -- provider schema
        names.append(_basename(item.get("name")))
    if len(names) != len(set(names)) or not filenames <= set(names):
        raise ValueError("missing or duplicate SEC selected directory entry")


def _build(
    request: dict[str, str], sources: Iterable[SecDocumentSource], captured_at: datetime
) -> bytes:
    captured = _utc(captured_at, "captured_at")
    selected = tuple(islice(sources, 10))
    if not 7 <= len(selected) <= 9 or any(type(s) is not SecDocumentSource for s in selected):
        raise ValueError("invalid SEC document source count or type")
    roles = [s.role for s in selected]
    if len(set(roles)) != len(roles) or not _REQUIRED <= set(roles) <= _ROLES:
        raise ValueError("invalid SEC source roles")
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
            raise ValueError("SEC document source aggregate byte limit exceeded")
        limit = (4 if source.role in {"primary", "instance"} else 2) * 1024 * 1024
        replay = source.store.replay_observation(source.observation, spec, min(limit, remaining))
        payloads[source.role] = replay.payload
        used += len(replay.payload)
    if len(set(filenames.values())) != len(filenames):
        raise ValueError("SEC document roles must have distinct filenames")
    filing = _filing(payloads["submissions"], request)
    accepted = _time(filing["accepted_at"], "accepted_at")
    if any(not accepted <= s.observation.snapshot_fetched_at <= captured for s in selected):
        raise ValueError("SEC document source times are not causal")
    if filenames["primary"] != filing["primary_document"]:
        raise ValueError("SEC primary document does not match submissions")
    _index(payloads["index"], request, set(filenames.values()))
    _validate_links(
        payloads["primary"],
        {r: b for r, b in payloads.items() if r not in {"primary", "index", "submissions"}},
        filenames,
        request["cik"],
    )
    manifest = _canonical(
        {
            "schema": _SCHEMA,
            "request": request,
            "filing": filing,
            "captured_at": _stamp(captured),
            "sources": [
                {"role": s.role, "filename": s.filename, "receipt": _receipt(s.observation)}
                for s in sorted(selected, key=lambda item: item.role)
            ],
        }
    )
    if len(manifest) > _MAX_PACKAGE:
        raise ValueError("SEC document package envelope limit exceeded")
    return manifest


def produce_sec_document_source_package(
    *,
    store: SnapshotStore,
    cik: str,
    accession_number: str,
    form: str,
    sources: Iterable[SecDocumentSource],
    captured_at: datetime,
) -> SecDocumentSourcePackage:
    """Replay and close one filing's original document graph before retaining its manifest."""
    request = _request(cik, accession_number, form)
    manifest = _build(request, sources, captured_at)
    observation = store.observe(
        RequestSpec("sec", _ENDPOINT, request), manifest, _utc(captured_at, "captured_at"), _SCHEMA
    )
    return SecDocumentSourcePackage(observation, manifest, _FACTORY, _seal(observation, manifest))


def restore_sec_document_source_package(
    *,
    store: SnapshotStore,
    observation: SnapshotObservationRef,
    resolve_observation: ObservationResolver,
) -> SecDocumentSourcePackage:
    """Rebuild the complete source graph without writing or trusting manifest claims."""
    if observation.serialization_identifier != _SCHEMA:
        raise ValueError("SEC document package serialization mismatch")
    replay = store.replay_observation(observation, max_payload_bytes=_MAX_PACKAGE)
    root = _strict_json(replay.payload)
    if (
        set(root) != {"schema", "request", "filing", "captured_at", "sources"}
        or root["schema"] != _SCHEMA
    ):
        raise ValueError("invalid SEC document package envelope")
    request_data = root["request"]
    if not isinstance(request_data, dict) or set(request_data) != {
        "cik",
        "accession_number",
        "form",
    }:
        raise ValueError("invalid SEC document package request")
    request = _request(**request_data)
    store.replay_observation(observation, RequestSpec("sec", _ENDPOINT, request), _MAX_PACKAGE)
    captured = _time(root["captured_at"], "captured_at")
    if captured != observation.snapshot_fetched_at:
        raise ValueError("SEC document package capture time mismatch")
    claims = root["sources"]
    if not isinstance(claims, (list, tuple)) or not 7 <= len(claims) <= 9:
        raise ValueError("invalid SEC source receipt count")
    sources = []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"role", "filename", "receipt"}:
            raise ValueError("invalid SEC source receipt")
        receipt = claim["receipt"]
        if not isinstance(receipt, dict) or type(receipt.get("observation_identity")) is not str:
            raise ValueError("invalid SEC source observation identity")
        identity = _identity(receipt["observation_identity"], "observation_identity")
        dependency_store, dependency = resolve_observation(identity)
        if type(dependency) is not SnapshotObservationRef or _canonical(
            _receipt(dependency)
        ) != _canonical(receipt):
            raise ValueError("SEC resolved source receipt mismatch")
        sources.append(
            SecDocumentSource(claim["role"], claim["filename"], dependency_store, dependency)
        )
    manifest = _build(request, sources, captured)
    if manifest != replay.payload:
        raise ValueError("SEC document package rebuilt bytes mismatch")
    return SecDocumentSourcePackage(observation, manifest, _FACTORY, _seal(observation, manifest))
