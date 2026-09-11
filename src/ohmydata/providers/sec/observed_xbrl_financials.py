"""Bounded offline financial rows from locally observed SEC XBRL packages."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from ...core import RequestSpec, SnapshotObservationRef, SnapshotStore
from ._observed_financial_receipts import receipt_binding
from .financials import SecStatementRow
from .observed_xbrl_package import decode_sec_observed_xbrl_package
from .pit import _row_payload
from .sgml_financials import SecSgmlFinancialsRequest, _documents, _header, _rows_from_documents

_OUTPUT_SERIALIZATION = "sec-financial-observed-rows-v1"
_PACKAGE_SERIALIZATION = "sec-observed-xbrl-package-v1"
_SOURCE_SERIALIZATION = "sec-filing-sgml-v1"
_PARSER_VERSION = "sec-observed-xbrl-financial-parser-v1-edgartools-5.56.0"
_CONFIG_VERSION = "sec-observed-xbrl-financial-config-v1"
_MAX_BYTES = 8 * 1024 * 1024
_FACTORY = object()


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _limit(value: int, name: str, maximum: int) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be a positive integer no greater than {maximum}")
    return value


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _identity(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class SecObservedFinancialEvidence:
    """Validated local-observation timing and receipt binding for one production."""

    source_observation: SnapshotObservationRef
    package_observation: SnapshotObservationRef
    accepted_at: datetime
    known_by_at: datetime
    _capability: object = field(repr=False, compare=False)
    _binding_identity: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._capability is not _FACTORY:
            raise ValueError("SEC observed financial evidence is created by production only")
        accepted, known = (
            _utc(self.accepted_at, "accepted_at"),
            _utc(self.known_by_at, "known_by_at"),
        )
        if known != max(
            self.source_observation.snapshot_fetched_at,
            self.package_observation.snapshot_fetched_at,
        ):
            raise ValueError("SEC observed financial known_by_at does not bind receipts")
        if self._binding_identity != _evidence_binding(
            self.source_observation, self.package_observation, accepted, known
        ):
            raise ValueError("SEC observed financial evidence binding mismatch")
        object.__setattr__(self, "accepted_at", accepted)
        object.__setattr__(self, "known_by_at", known)


def _evidence_binding(
    source: SnapshotObservationRef,
    package: SnapshotObservationRef,
    accepted: datetime,
    known: datetime,
) -> str:
    return _identity(
        {
            "source_receipt": receipt_binding(source),
            "package_receipt": receipt_binding(package),
            "accepted_at": _stamp(accepted),
            "known_by_at": _stamp(known),
        }
    )


@dataclass(frozen=True)
class SecObservedFinancialVintage:
    """A filing's native rows with acceptance and local known-by timestamps."""

    symbol: str
    cik: str
    company_name: str
    form: str
    accession_number: str
    filing_date: date
    period_end: date
    accepted_at: datetime
    known_by_at: datetime
    is_amendment: bool
    rows: tuple[SecStatementRow, ...]
    vintage_identity: str = field(init=False)

    def __post_init__(self) -> None:
        accepted, known = (
            _utc(self.accepted_at, "accepted_at"),
            _utc(self.known_by_at, "known_by_at"),
        )
        if (
            type(self.rows) is not tuple
            or not self.rows
            or any(not isinstance(row, SecStatementRow) for row in self.rows)
        ):
            raise ValueError("rows must be a nonempty tuple of SecStatementRow")
        object.__setattr__(self, "accepted_at", accepted)
        object.__setattr__(self, "known_by_at", known)
        object.__setattr__(
            self,
            "vintage_identity",
            _identity(
                {
                    "symbol": self.symbol,
                    "cik": self.cik,
                    "company_name": self.company_name,
                    "form": self.form,
                    "accession_number": self.accession_number,
                    "filing_date": self.filing_date.isoformat(),
                    "period_end": self.period_end.isoformat(),
                    "accepted_at": _stamp(accepted),
                    "known_by_at": _stamp(known),
                    "is_amendment": self.is_amendment,
                    "rows": [_row_payload(row) for row in self.rows],
                }
            ),
        )


@dataclass(frozen=True)
class SecObservedFinancialProduction:
    """Result persisted at endpoint ``financial-observed-rows`` for offline replay."""

    request: SecSgmlFinancialsRequest
    evidence: SecObservedFinancialEvidence
    output_observation: SnapshotObservationRef
    vintage: SecObservedFinancialVintage
    produced_at: datetime
    _capability: object = field(repr=False, compare=False)
    _binding_identity: str = field(repr=False, compare=False)
    production_identity: str = field(init=False)

    @property
    def output_schema_version(self) -> str:
        return _OUTPUT_SERIALIZATION

    @property
    def parser_version(self) -> str:
        return _PARSER_VERSION

    @property
    def configuration_version(self) -> str:
        return _CONFIG_VERSION

    @property
    def configuration_identity(self) -> str:
        return _configuration_identity(self.request)

    def __post_init__(self) -> None:
        if self._capability is not _FACTORY:
            raise ValueError("SEC observed financial production is created by production only")
        # Revalidate nested seals: frozen dataclasses can still be illicitly
        # changed with object.__setattr__ after producer construction.
        self.evidence.__post_init__()
        self.vintage.__post_init__()
        produced = _utc(self.produced_at, "produced_at")
        _validate_output_observation(
            self.output_observation,
            self.request,
            self.evidence,
            self.vintage,
            produced,
        )
        binding = _production_binding(
            self.request, self.evidence, self.output_observation, self.vintage, produced
        )
        if self._binding_identity != binding:
            raise ValueError("SEC observed financial production binding mismatch")
        object.__setattr__(self, "produced_at", produced)
        object.__setattr__(
            self,
            "production_identity",
            _identity(
                {
                    "domain": "sec-observed-financial-production-v1",
                    "canonical_output_identity": self.output_observation.response_sha256,
                    "output_observation_identity": self.output_observation.observation_identity,
                }
            ),
        )


def _validate_output_observation(
    output: SnapshotObservationRef,
    request: SecSgmlFinancialsRequest,
    evidence: SecObservedFinancialEvidence,
    vintage: SecObservedFinancialVintage,
    produced: datetime,
) -> None:
    expected = RequestSpec(
        "sec",
        "financial-observed-rows",
        {"cik": request.cik, "accession_number": request.accession_number, "form": request.form},
    )
    if (
        output.provider != expected.provider
        or output.endpoint != expected.endpoint
        or output.request_identity != expected.request_identity
        or output.serialization_identifier != _OUTPUT_SERIALIZATION
        or output.snapshot_fetched_at != produced
    ):
        raise ValueError("SEC observed financial output receipt does not match production")
    if (
        output.response_sha256
        != hashlib.sha256(
            _serialize_result(
                request=request,
                evidence=evidence,
                vintage=vintage,
                produced_at=produced,
                configuration_identity=_configuration_identity(request),
            )
        ).hexdigest()
    ):
        raise ValueError("SEC observed financial output receipt does not bind result bytes")
    if (
        vintage.accepted_at != evidence.accepted_at
        or vintage.known_by_at != evidence.known_by_at
        or evidence.accepted_at > evidence.source_observation.snapshot_fetched_at
        or evidence.known_by_at > produced
    ):
        raise ValueError("SEC observed financial production identities or times are invalid")


def _production_binding(
    request: SecSgmlFinancialsRequest,
    evidence: SecObservedFinancialEvidence,
    output: SnapshotObservationRef,
    vintage: SecObservedFinancialVintage,
    produced: datetime,
) -> str:
    return _identity(
        {
            "domain": "sec-observed-financial-production-binding-v1",
            "request": _request_payload(request),
            "evidence": evidence._binding_identity,
            "output_receipt": receipt_binding(output),
            "vintage": {
                "identity": vintage.vintage_identity,
                "accepted_at": _stamp(vintage.accepted_at),
                "known_by_at": _stamp(vintage.known_by_at),
                "rows": [_row_payload(row) for row in vintage.rows],
            },
            "produced_at": _stamp(produced),
        }
    )


def _request_payload(request: SecSgmlFinancialsRequest) -> dict[str, Any]:
    return {
        "symbol": request.symbol,
        "cik": request.cik,
        "accession_number": request.accession_number,
        "form": request.form,
        "statement_types": list(request.statement_types),
        "include_dimensions": request.include_dimensions,
    }


def _configuration_identity(request: SecSgmlFinancialsRequest) -> str:
    return _identity(
        {
            "parser_version": _PARSER_VERSION,
            "configuration_version": _CONFIG_VERSION,
            "statement_types": request.statement_types,
            "include_dimensions": request.include_dimensions,
        }
    )


def _serialize_result(
    *,
    request: SecSgmlFinancialsRequest,
    evidence: SecObservedFinancialEvidence,
    vintage: SecObservedFinancialVintage,
    produced_at: datetime,
    configuration_identity: str,
) -> bytes:
    payload = {
        "schema": _OUTPUT_SERIALIZATION,
        "request": _request_payload(request),
        "source_receipt": {
            "observation_identity": evidence.source_observation.observation_identity,
            "fact_version": evidence.source_observation.fact_version,
        },
        "package_receipt": {
            "observation_identity": evidence.package_observation.observation_identity,
            "fact_version": evidence.package_observation.fact_version,
        },
        "parser_version": _PARSER_VERSION,
        "configuration_version": _CONFIG_VERSION,
        "configuration_identity": configuration_identity,
        "accepted_at": _stamp(vintage.accepted_at),
        "known_by_at": _stamp(vintage.known_by_at),
        "produced_at": _stamp(produced_at),
        "filing": {
            "cik": vintage.cik,
            "accession_number": vintage.accession_number,
            "form": vintage.form,
            "company_name": vintage.company_name,
            "filing_date": vintage.filing_date.isoformat(),
            "period_end": vintage.period_end.isoformat(),
            "is_amendment": vintage.is_amendment,
            "vintage_identity": vintage.vintage_identity,
        },
        "rows": [_row_payload(row) for row in vintage.rows],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def produce_sec_financials_from_observed_xbrl_package(
    *,
    source_store: SnapshotStore,
    source_observation: SnapshotObservationRef,
    package_store: SnapshotStore,
    package_observation: SnapshotObservationRef,
    output_store: SnapshotStore,
    request: SecSgmlFinancialsRequest,
    produced_at: datetime,
    max_raw_bytes: int = _MAX_BYTES,
    max_package_bytes: int = _MAX_BYTES,
    max_result_bytes: int = _MAX_BYTES,
    max_component_bytes: int = 2 * 1024 * 1024,
    max_xml_elements: int = 200_000,
    max_xml_depth: int = 128,
    max_rows: int = 10_000,
) -> SecObservedFinancialProduction:
    """Replay two receipts once and persist bounded rows at ``financial-observed-rows``.

    This local observation product intentionally does not establish public availability.
    """
    for value, name, maximum in (
        (max_raw_bytes, "max_raw_bytes", _MAX_BYTES),
        (max_package_bytes, "max_package_bytes", _MAX_BYTES),
        (max_result_bytes, "max_result_bytes", _MAX_BYTES),
        (max_component_bytes, "max_component_bytes", 2 * 1024 * 1024),
        (max_xml_elements, "max_xml_elements", 200_000),
        (max_xml_depth, "max_xml_depth", 128),
        (max_rows, "max_rows", 10_000),
    ):
        _limit(value, name, maximum)
    produced = _utc(produced_at, "produced_at")
    expected_source = RequestSpec(
        "sec",
        "company-filing-sgml",
        {"cik": request.cik, "accession_number": request.accession_number, "form": request.form},
    )
    expected_package = RequestSpec(
        "sec",
        "company-filing-observed-xbrl-package",
        {"cik": request.cik, "accession_number": request.accession_number, "form": request.form},
    )
    if source_observation.serialization_identifier != _SOURCE_SERIALIZATION:
        raise ValueError("source observation is not SEC filing SGML")
    if package_observation.serialization_identifier != _PACKAGE_SERIALIZATION:
        raise ValueError("package observation is not SEC observed XBRL package")
    source_replay = source_store.replay_observation(
        source_observation, expected_source, max_raw_bytes
    )
    package_replay = package_store.replay_observation(
        package_observation, expected_package, max_package_bytes
    )
    try:
        raw = source_replay.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SEC filing SGML must be UTF-8") from exc
    accepted, filed, period, company_name = _header(raw, request)
    package = decode_sec_observed_xbrl_package(
        package_replay.payload,
        max_envelope_bytes=max_package_bytes,
        max_component_bytes=max_component_bytes,
        max_xml_elements=max_xml_elements,
        max_xml_depth=max_xml_depth,
    )
    if (package.cik, package.accession_number, package.form) != (
        request.cik,
        request.accession_number,
        request.form,
    ):
        raise ValueError("SEC observed XBRL package filing does not match request")
    if (
        package.sgml_fact_version != source_observation.fact_version
        or package.sgml_observation_identity != source_observation.observation_identity
    ):
        raise ValueError("SEC observed XBRL package does not bind source observation")
    known = max(source_observation.snapshot_fetched_at, package_observation.snapshot_fetched_at)
    if accepted > source_observation.snapshot_fetched_at or known > produced:
        raise ValueError("SEC observed financial times are not causal")
    evidence = SecObservedFinancialEvidence(
        source_observation,
        package_observation,
        accepted,
        known,
        _FACTORY,
        _evidence_binding(source_observation, package_observation, accepted, known),
    )
    documents = {
        "EX-101.SCH": package.components.schema.decode("utf-8"),
        "EX-101.PRE": package.components.presentation.decode("utf-8"),
        "EX-101.LAB": package.components.labels.decode("utf-8"),
        "EX-101.INS": package.components.instance.decode("utf-8"),
    }
    if package.components.calculation is not None:
        documents["EX-101.CAL"] = package.components.calculation.decode("utf-8")
    if package.components.definition is not None:
        documents["EX-101.DEF"] = package.components.definition.decode("utf-8")
    _documents(raw, require_traditional=False)
    rows = _rows_from_documents(raw, documents, request, max_rows)
    if len(rows) > max_rows:
        raise ValueError("SEC observed financial row limit exceeded")
    vintage = SecObservedFinancialVintage(
        request.symbol,
        request.cik,
        company_name,
        request.form,
        request.accession_number,
        filed,
        period,
        accepted,
        known,
        request.form.endswith("/A"),
        rows,
    )
    configuration_identity = _configuration_identity(request)
    payload = _serialize_result(
        request=request,
        evidence=evidence,
        vintage=vintage,
        produced_at=produced,
        configuration_identity=configuration_identity,
    )
    if len(payload) > max_result_bytes:
        raise ValueError("SEC observed financial result exceeds limit")
    output = output_store.observe(
        RequestSpec(
            "sec",
            "financial-observed-rows",
            {
                "cik": request.cik,
                "accession_number": request.accession_number,
                "form": request.form,
            },
        ),
        payload,
        produced,
        _OUTPUT_SERIALIZATION,
    )
    return SecObservedFinancialProduction(
        request,
        evidence,
        output,
        vintage,
        produced,
        _FACTORY,
        _production_binding(request, evidence, output, vintage, produced),
    )


__all__ = [
    "SecObservedFinancialEvidence",
    "SecObservedFinancialProduction",
    "SecObservedFinancialVintage",
    "produce_sec_financials_from_observed_xbrl_package",
]
