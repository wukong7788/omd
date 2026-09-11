"""Observed financial production from a completely retained document source graph."""

from __future__ import annotations

import hashlib
from copy import copy
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from ...core import RequestSpec, SnapshotObservationRef, SnapshotStore
from ._observed_financial_bundle_codec import _identity, _receipt, _stamp, _time, _utc
from ._observed_financial_output import _request_payload
from ._observed_xbrl_units import decode_raw_units
from .document_source import (
    ObservationResolver,
    SecDocumentSourcePackage,
    _canonical,
    restore_sec_document_source_package,
)
from .event_discovery import _strict_json
from .observed_xbrl_financials import SecObservedFinancialVintage
from .pit import _row_payload
from .sgml_financials import SecSgmlFinancialsRequest, _rows_from_documents

_SCHEMA = "sec-document-financial-rows-v1"
_PARSER = "sec-document-xbrl-financial-parser-v1-edgartools-5.56.0"
_CONFIG = "sec-document-financial-config-v1"
_ENDPOINT = "financial-document-observed-rows"
_MAX_BYTES = 8 * 1024 * 1024
_FACTORY = object()
_FIELDS = frozenset(
    {
        "schema",
        "request",
        "parser_version",
        "configuration_version",
        "configuration_identity",
        "source_package_receipt",
        "filing",
        "known_by_at",
        "produced_at",
        "rows",
    }
)
_COMPONENTS = {
    "schema": "EX-101.SCH",
    "presentation": "EX-101.PRE",
    "labels": "EX-101.LAB",
    "instance": "EX-101.INS",
    "calculation": "EX-101.CAL",
    "definition": "EX-101.DEF",
}


def _spec(request: SecSgmlFinancialsRequest) -> RequestSpec:
    return RequestSpec(
        "sec",
        _ENDPOINT,
        {"cik": request.cik, "accession_number": request.accession_number, "form": request.form},
    )


def _configuration(request: SecSgmlFinancialsRequest) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "configuration_version": _CONFIG,
                "parser_version": _PARSER,
                "statement_types": list(request.statement_types),
                "include_dimensions": request.include_dimensions,
            }
        )
    ).hexdigest()


def _payload(
    request: SecSgmlFinancialsRequest,
    package: SecDocumentSourcePackage,
    vintage: SecObservedFinancialVintage,
    produced: datetime,
) -> bytes:
    source = _strict_json(package.manifest)
    return _canonical(
        {
            "schema": _SCHEMA,
            "request": _request_payload(request),
            "parser_version": _PARSER,
            "configuration_version": _CONFIG,
            "configuration_identity": _configuration(request),
            "source_package_receipt": _receipt(package.observation),
            "filing": source["filing"],
            "known_by_at": _stamp(vintage.known_by_at),
            "produced_at": _stamp(produced),
            "rows": [_row_payload(row) for row in vintage.rows],
        }
    )


def _binding(
    request: SecSgmlFinancialsRequest,
    package: SecDocumentSourcePackage,
    output: SnapshotObservationRef,
    vintage: SecObservedFinancialVintage,
    produced: datetime,
) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "domain": "sec-document-financial-binding-v1",
                "request": _request_payload(request),
                "source_binding": package._binding,
                "output_receipt": _receipt(output),
                "output_path": str(output.path),
                "vintage_identity": vintage.vintage_identity,
                "produced_at": _stamp(produced),
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class SecDocumentFinancialProduction:
    """Sealed native rows; no financial quality approval or market-known assertion."""

    request: SecSgmlFinancialsRequest
    source_package: SecDocumentSourcePackage
    output_observation: SnapshotObservationRef
    vintage: SecObservedFinancialVintage
    produced_at: datetime
    _capability: object = field(repr=False, compare=False)
    _binding_identity: str = field(repr=False, compare=False)
    production_identity: str = field(init=False)

    @property
    def parser_version(self) -> str:
        return _PARSER

    @property
    def configuration_version(self) -> str:
        return _CONFIG

    @property
    def configuration_identity(self) -> str:
        return _configuration(self.request)

    @property
    def output_schema_version(self) -> str:
        return _SCHEMA

    def __post_init__(self) -> None:
        if self._capability is not _FACTORY:
            raise ValueError("SEC document financial result requires production factory")
        if (
            type(self.request) is not SecSgmlFinancialsRequest
            or type(self.source_package) is not SecDocumentSourcePackage
            or type(self.vintage) is not SecObservedFinancialVintage
            or type(self.output_observation) is not SnapshotObservationRef
        ):
            raise TypeError("invalid SEC document financial sealed value types")
        self.request.__post_init__()
        source_copy, vintage_copy = copy(self.source_package), copy(self.vintage)
        source_copy.__post_init__()
        vintage_copy.__post_init__()
        if (
            source_copy.package_identity != self.source_package.package_identity
            or vintage_copy.vintage_identity != self.vintage.vintage_identity
        ):
            raise ValueError("SEC document financial nested identity mismatch")
        produced = _utc(self.produced_at, "produced_at")
        source = _strict_json(self.source_package.manifest)
        filing = source["filing"]
        if source["request"] != {
            "cik": self.request.cik,
            "accession_number": self.request.accession_number,
            "form": self.request.form,
        }:
            raise ValueError("SEC financial request does not match source package")
        expected_vintage = (
            self.request.symbol,
            self.request.cik,
            filing["company_name"],
            self.request.form,
            self.request.accession_number,
            date.fromisoformat(filing["filing_date"]),
            date.fromisoformat(filing["period_end"]),
            _time(filing["accepted_at"], "accepted_at"),
            self.source_package.known_by_at,
            self.request.form.endswith("/A"),
        )
        actual_vintage = (
            self.vintage.symbol,
            self.vintage.cik,
            self.vintage.company_name,
            self.vintage.form,
            self.vintage.accession_number,
            self.vintage.filing_date,
            self.vintage.period_end,
            self.vintage.accepted_at,
            self.vintage.known_by_at,
            self.vintage.is_amendment,
        )
        if actual_vintage != expected_vintage or self.source_package.known_by_at > produced:
            raise ValueError("SEC document financial metadata or times do not match sources")
        output = self.output_observation
        spec = _spec(self.request)
        if (
            output.provider,
            output.endpoint,
            output.request_identity,
            output.serialization_identifier,
            output.snapshot_fetched_at,
        ) != ("sec", _ENDPOINT, spec.request_identity, _SCHEMA, produced):
            raise ValueError("SEC document financial output receipt mismatch")
        payload = _payload(self.request, self.source_package, self.vintage, produced)
        if (
            len(payload) > _MAX_BYTES
            or hashlib.sha256(payload).hexdigest() != output.response_sha256
        ):
            raise ValueError("SEC document financial output bytes mismatch")
        if self._binding_identity != _binding(
            self.request, self.source_package, output, self.vintage, produced
        ):
            raise ValueError("SEC document financial production binding mismatch")
        object.__setattr__(self, "produced_at", produced)
        object.__setattr__(
            self,
            "production_identity",
            hashlib.sha256(
                _canonical(
                    {
                        "domain": "sec-document-financial-production-v1",
                        "output_sha256": output.response_sha256,
                        "output_observation_identity": output.observation_identity,
                    }
                )
            ).hexdigest(),
        )


def _resolve(
    receipt: object, resolver: ObservationResolver
) -> tuple[SnapshotStore, SnapshotObservationRef]:
    if not isinstance(receipt, dict):
        raise ValueError("invalid SEC document financial source receipt")  # noqa: TRY004 -- schema
    identity = _identity(receipt.get("observation_identity"), "observation_identity")
    store, observation = resolver(identity)
    if type(store) is not SnapshotStore or type(observation) is not SnapshotObservationRef:
        raise TypeError("resolver must return SnapshotStore and SnapshotObservationRef")
    if _canonical(_receipt(observation)) != _canonical(receipt):
        raise ValueError("SEC document financial resolved receipt mismatch")
    return store, observation


def _build(
    *,
    package_store: SnapshotStore,
    package_observation: SnapshotObservationRef,
    resolve_observation: ObservationResolver,
    request: SecSgmlFinancialsRequest,
    produced_at: datetime,
) -> tuple[SecDocumentSourcePackage, SecObservedFinancialVintage, bytes]:
    if type(request) is not SecSgmlFinancialsRequest:
        raise TypeError("request must be SecSgmlFinancialsRequest")
    request.__post_init__()
    produced = _utc(produced_at, "produced_at")
    package = restore_sec_document_source_package(
        store=package_store,
        observation=package_observation,
        resolve_observation=resolve_observation,
    )
    source = _strict_json(package.manifest)
    if source["request"] != {
        "cik": request.cik,
        "accession_number": request.accession_number,
        "form": request.form,
    }:
        raise ValueError("SEC financial request does not match source package")
    if package.known_by_at > produced:
        raise ValueError("SEC financial production precedes source package")
    components: dict[str, str] = {}
    for claim in source["sources"]:
        if claim["role"] not in _COMPONENTS:
            continue
        store, observation = _resolve(claim["receipt"], resolve_observation)
        spec = RequestSpec(
            "sec",
            "company-filing-document",
            {
                "cik": request.cik,
                "accession_number": request.accession_number,
                "filename": claim["filename"],
            },
        )
        raw = store.replay_observation(observation, spec, 2 * 1024 * 1024).payload
        components[_COMPONENTS[claim["role"]]] = raw.decode("utf-8")
    units = decode_raw_units(components["EX-101.INS"].encode(), max_elements=200_000, max_depth=128)
    native_rows = _rows_from_documents(None, components, request, 10_000)
    try:
        rows = tuple(replace(row, unit=units[row.unit_ref or ""]) for row in native_rows)
    except KeyError as exc:
        raise ValueError("selected SEC document row has no raw unit definition") from exc
    filing = source["filing"]
    vintage = SecObservedFinancialVintage(
        request.symbol,
        request.cik,
        filing["company_name"],
        request.form,
        request.accession_number,
        date.fromisoformat(filing["filing_date"]),
        date.fromisoformat(filing["period_end"]),
        _time(filing["accepted_at"], "accepted_at"),
        package.known_by_at,
        request.form.endswith("/A"),
        rows,
    )
    payload = _payload(request, package, vintage, produced)
    if len(payload) > _MAX_BYTES:
        raise ValueError("SEC document financial result byte limit exceeded")
    return package, vintage, payload


def produce_sec_financials_from_document_source(
    *,
    package_store: SnapshotStore,
    package_observation: SnapshotObservationRef,
    resolve_observation: ObservationResolver,
    output_store: SnapshotStore,
    request: SecSgmlFinancialsRequest,
    produced_at: datetime,
) -> SecDocumentFinancialProduction:
    """Rebuild source closure, parse native rows and retain one distinct output observation."""
    package, vintage, payload = _build(
        package_store=package_store,
        package_observation=package_observation,
        resolve_observation=resolve_observation,
        request=request,
        produced_at=produced_at,
    )
    produced = _utc(produced_at, "produced_at")
    output = output_store.observe(_spec(request), payload, produced, _SCHEMA)
    return SecDocumentFinancialProduction(
        request,
        package,
        output,
        vintage,
        produced,
        _FACTORY,
        _binding(request, package, output, vintage, produced),
    )


def restore_sec_document_financial_production(
    *,
    output_store: SnapshotStore,
    output_observation: SnapshotObservationRef,
    resolve_observation: ObservationResolver,
) -> SecDocumentFinancialProduction:
    """Replay all retained sources and compare freshly rebuilt output bytes without writes."""
    if output_observation.serialization_identifier != _SCHEMA:
        raise ValueError("SEC document financial serialization mismatch")
    replay = output_store.replay_observation(output_observation, max_payload_bytes=_MAX_BYTES)
    decoded = _strict_json(replay.payload)
    if set(decoded) != _FIELDS or decoded["schema"] != _SCHEMA:
        raise ValueError("invalid SEC document financial envelope")
    req = decoded["request"]
    if not isinstance(req, dict) or set(req) != {
        "symbol",
        "cik",
        "accession_number",
        "form",
        "statement_types",
        "include_dimensions",
    }:
        raise ValueError("invalid SEC document financial request")
    if not isinstance(req["statement_types"], (list, tuple)):
        raise ValueError("invalid SEC document statement selection")  # noqa: TRY004 -- schema
    request = SecSgmlFinancialsRequest(
        req["symbol"],
        req["cik"],
        req["accession_number"],
        req["form"],
        tuple(req["statement_types"]),
        req["include_dimensions"],
    )
    output_store.replay_observation(output_observation, _spec(request), _MAX_BYTES)
    produced = _time(decoded["produced_at"], "produced_at")
    if produced != output_observation.snapshot_fetched_at:
        raise ValueError("SEC document financial observed production time mismatch")
    store, observation = _resolve(decoded["source_package_receipt"], resolve_observation)
    package, vintage, payload = _build(
        package_store=store,
        package_observation=observation,
        resolve_observation=resolve_observation,
        request=request,
        produced_at=produced,
    )
    if payload != replay.payload:
        raise ValueError("SEC document financial rebuilt bytes mismatch")
    return SecDocumentFinancialProduction(
        request,
        package,
        output_observation,
        vintage,
        produced,
        _FACTORY,
        _binding(request, package, output_observation, vintage, produced),
    )
