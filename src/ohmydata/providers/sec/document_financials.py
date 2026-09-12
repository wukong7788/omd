"""Observed financial production from a completely retained document source graph."""

from __future__ import annotations

import hashlib
from copy import copy
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from ...core import RequestSpec, SnapshotObservationRef, SnapshotStore
from ._document_financial_contract import (
    DOCUMENT_CONFIG as _CONFIG,
)
from ._document_financial_contract import (
    DOCUMENT_ENDPOINT as _ENDPOINT,
)
from ._document_financial_contract import (
    DOCUMENT_PARSER as _PARSER,
)
from ._document_financial_contract import (
    DOCUMENT_SCHEMA as _SCHEMA,
)
from ._document_financial_contract import (
    EMBEDDED_CONFIG as _EMBEDDED_CONFIG,
)
from ._document_financial_contract import (
    EMBEDDED_ENDPOINT as _EMBEDDED_ENDPOINT,
)
from ._document_financial_contract import (
    EMBEDDED_PARSER as _EMBEDDED_PARSER,
)
from ._document_financial_contract import (
    EMBEDDED_SCHEMA as _EMBEDDED_SCHEMA,
)
from ._document_financial_contract import (
    EMBEDDED_SOURCE_ENDPOINT as _EMBEDDED_SOURCE_ENDPOINT,
)
from ._document_financial_contract import (
    EMBEDDED_SOURCE_SCHEMA as _EMBEDDED_SOURCE_SCHEMA,
)
from ._document_financial_contract import (
    DocumentSourcePackage,
    _binding,
    _configuration,
    _payload,
    _spec,
)
from ._document_financial_contract import (
    variant as _variant,
)
from ._observed_financial_bundle_codec import _identity, _receipt, _time, _utc
from ._observed_xbrl_units import decode_raw_units
from .document_source import (
    ObservationResolver,
    SecDocumentSourcePackage,
    _canonical,
    restore_sec_document_source_package,
)
from .embedded_document_source import (
    SecEmbeddedDocumentSourcePackage,
    restore_sec_embedded_document_source_package,
)
from .event_discovery import _strict_json
from .observed_xbrl_financials import SecObservedFinancialVintage
from .sgml_financials import (
    SecSgmlFinancialsRequest,
    _rows_from_documents,
    _rows_from_embedded_documents,
)

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


@dataclass(frozen=True)
class SecDocumentFinancialProduction:
    """Sealed native rows; no financial quality approval or market-known assertion."""

    request: SecSgmlFinancialsRequest
    source_package: DocumentSourcePackage
    output_observation: SnapshotObservationRef
    vintage: SecObservedFinancialVintage
    produced_at: datetime
    _capability: object = field(repr=False, compare=False)
    _binding_identity: str = field(repr=False, compare=False)
    production_identity: str = field(init=False)

    @property
    def parser_version(self) -> str:
        return _variant(self.source_package)[1]

    @property
    def configuration_version(self) -> str:
        return _variant(self.source_package)[2]

    @property
    def configuration_identity(self) -> str:
        _, parser, config, _, _ = _variant(self.source_package)
        return _configuration(self.request, config, parser)

    @property
    def output_schema_version(self) -> str:
        return _variant(self.source_package)[0]

    def __post_init__(self) -> None:
        if self._capability is not _FACTORY:
            raise ValueError("SEC document financial result requires production factory")
        if (
            type(self.request) is not SecSgmlFinancialsRequest
            or type(self.source_package)
            not in (SecDocumentSourcePackage, SecEmbeddedDocumentSourcePackage)
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
        schema, _, _, endpoint, source_serialization = _variant(self.source_package)
        if (
            self.source_package.observation.endpoint,
            self.source_package.observation.serialization_identifier,
        ) != (
            _EMBEDDED_SOURCE_ENDPOINT
            if schema == _EMBEDDED_SCHEMA
            else "company-filing-document-source-package",
            source_serialization,
        ):
            raise ValueError("SEC document financial source receipt mismatch")
        spec = _spec(self.request, endpoint)
        if (
            output.provider,
            output.endpoint,
            output.request_identity,
            output.serialization_identifier,
            output.snapshot_fetched_at,
        ) != ("sec", endpoint, spec.request_identity, schema, produced):
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
    embedded: bool = False,
) -> tuple[DocumentSourcePackage, SecObservedFinancialVintage, bytes]:
    if type(request) is not SecSgmlFinancialsRequest:
        raise TypeError("request must be SecSgmlFinancialsRequest")
    request.__post_init__()
    produced = _utc(produced_at, "produced_at")
    package = (
        restore_sec_embedded_document_source_package(
            store=package_store,
            observation=package_observation,
            resolve_observation=resolve_observation,
        )
        if embedded
        else restore_sec_document_source_package(
            store=package_store,
            observation=package_observation,
            resolve_observation=resolve_observation,
        )
    )
    _, _, _, _, source_serialization = _variant(package)
    if package.observation.serialization_identifier != source_serialization:
        raise ValueError("SEC document financial source serialization mismatch")
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
        if claim["role"] not in (_COMPONENTS if not embedded else {"schema", "instance"}):
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
        if claim["role"] == "instance":
            maximum = 12 * 1024 * 1024 if embedded else 4 * 1024 * 1024
        else:
            maximum = 2 * 1024 * 1024
        raw = store.replay_observation(observation, spec, maximum).payload
        components[_COMPONENTS[claim["role"]] if not embedded else claim["role"]] = raw.decode(
            "utf-8"
        )
    instance = components["instance"] if embedded else components["EX-101.INS"]
    units = decode_raw_units(
        instance.encode(),
        max_elements=200_000,
        max_depth=128,
        max_bytes=12 * 1024 * 1024 if embedded else 4 * 1024 * 1024,
    )
    native_rows = (
        _rows_from_embedded_documents(components["schema"], instance, request, 10_000)
        if embedded
        else _rows_from_documents(None, components, request, 10_000)
    )
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


def produce_sec_financials_from_embedded_document_source(
    *,
    package_store: SnapshotStore,
    package_observation: SnapshotObservationRef,
    resolve_observation: ObservationResolver,
    output_store: SnapshotStore,
    request: SecSgmlFinancialsRequest,
    produced_at: datetime,
) -> SecDocumentFinancialProduction:
    """Produce experimental rows only from an embedded-linkbase source closure."""
    package, vintage, payload = _build(
        package_store=package_store,
        package_observation=package_observation,
        resolve_observation=resolve_observation,
        request=request,
        produced_at=produced_at,
        embedded=True,
    )
    produced = _utc(produced_at, "produced_at")
    output = output_store.observe(
        _spec(request, _EMBEDDED_ENDPOINT), payload, produced, _EMBEDDED_SCHEMA
    )
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
    if output_observation.serialization_identifier not in {_SCHEMA, _EMBEDDED_SCHEMA}:
        raise ValueError("SEC document financial serialization mismatch")
    replay = output_store.replay_observation(output_observation, max_payload_bytes=_MAX_BYTES)
    decoded = _strict_json(replay.payload)
    if set(decoded) != _FIELDS or decoded["schema"] not in {_SCHEMA, _EMBEDDED_SCHEMA}:
        raise ValueError("invalid SEC document financial envelope")
    embedded = decoded["schema"] == _EMBEDDED_SCHEMA
    expected_schema = _EMBEDDED_SCHEMA if embedded else _SCHEMA
    expected_parser = _EMBEDDED_PARSER if embedded else _PARSER
    expected_config = _EMBEDDED_CONFIG if embedded else _CONFIG
    expected_endpoint = _EMBEDDED_ENDPOINT if embedded else _ENDPOINT
    if output_observation.serialization_identifier != expected_schema:
        raise ValueError("SEC document financial output schema mismatch")
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
    if (
        decoded["parser_version"],
        decoded["configuration_version"],
        decoded["configuration_identity"],
    ) != (
        expected_parser,
        expected_config,
        _configuration(request, expected_config, expected_parser),
    ):
        raise ValueError("SEC document financial parser or configuration mismatch")
    output_store.replay_observation(
        output_observation, _spec(request, expected_endpoint), _MAX_BYTES
    )
    produced = _time(decoded["produced_at"], "produced_at")
    if produced != output_observation.snapshot_fetched_at:
        raise ValueError("SEC document financial observed production time mismatch")
    store, observation = _resolve(decoded["source_package_receipt"], resolve_observation)
    if (
        observation.provider,
        observation.endpoint,
        observation.serialization_identifier,
    ) != (
        "sec",
        _EMBEDDED_SOURCE_ENDPOINT if embedded else "company-filing-document-source-package",
        _EMBEDDED_SOURCE_SCHEMA if embedded else "sec-document-source-package-v1",
    ):
        raise ValueError("SEC document financial output/source domain mismatch")
    package, vintage, payload = _build(
        package_store=store,
        package_observation=observation,
        resolve_observation=resolve_observation,
        request=request,
        produced_at=produced,
        embedded=embedded,
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
