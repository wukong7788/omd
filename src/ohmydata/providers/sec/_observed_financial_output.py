"""Canonical output and integrity bindings for observed financial productions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ...core import RequestSpec, SnapshotObservationRef
from ._observed_financial_receipts import receipt_binding
from .pit import _row_payload
from .sgml_financials import SecSgmlFinancialsRequest

if TYPE_CHECKING:
    from .observed_xbrl_financials import SecObservedFinancialEvidence, SecObservedFinancialVintage

_OUTPUT_SERIALIZATION = "sec-financial-observed-rows-v1"
_PARSER_VERSION_V1 = "sec-observed-xbrl-financial-parser-v1-edgartools-5.56.0"
_CONFIG_VERSION = "sec-observed-xbrl-financial-config-v1"


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _identity(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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


def _validate_output_observation(
    output: SnapshotObservationRef,
    request: SecSgmlFinancialsRequest,
    evidence: SecObservedFinancialEvidence,
    vintage: SecObservedFinancialVintage,
    produced: datetime,
    parser_version: str,
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
                configuration_identity=_configuration_identity(request, parser_version),
                parser_version=parser_version,
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


def _configuration_identity(request: SecSgmlFinancialsRequest, parser_version: str) -> str:
    return _identity(
        {
            "parser_version": parser_version,
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
    parser_version: str,
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
        "parser_version": parser_version,
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
