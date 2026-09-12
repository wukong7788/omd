"""Exact source/output domains for sealed document financial productions."""

from __future__ import annotations

import hashlib
from datetime import datetime

from ...core import RequestSpec, SnapshotObservationRef
from ._observed_financial_bundle_codec import _receipt, _stamp
from ._observed_financial_output import _request_payload
from .document_source import SecDocumentSourcePackage, _canonical
from .embedded_document_source import SecEmbeddedDocumentSourcePackage
from .event_discovery import _strict_json
from .observed_xbrl_financials import SecObservedFinancialVintage
from .pit import _row_payload
from .sgml_financials import SecSgmlFinancialsRequest

DOCUMENT_SCHEMA = "sec-document-financial-rows-v1"
DOCUMENT_PARSER = "sec-document-xbrl-financial-parser-v1-edgartools-5.56.0"
DOCUMENT_CONFIG = "sec-document-financial-config-v1"
DOCUMENT_ENDPOINT = "financial-document-observed-rows"
DOCUMENT_SOURCE_SCHEMA = "sec-document-source-package-v1"
DOCUMENT_SOURCE_ENDPOINT = "company-filing-document-source-package"
EMBEDDED_SCHEMA = "sec-embedded-document-financial-rows-v1"
EMBEDDED_PARSER = "sec-schema-embedded-financial-parser-v1-edgartools-5.56.0"
EMBEDDED_CONFIG = "sec-embedded-document-financial-config-v1"
EMBEDDED_ENDPOINT = "financial-embedded-document-observed-rows"
EMBEDDED_SOURCE_SCHEMA = "sec-document-embedded-source-package-v1"
EMBEDDED_SOURCE_ENDPOINT = "company-filing-embedded-document-source-package"

DocumentSourcePackage = SecDocumentSourcePackage | SecEmbeddedDocumentSourcePackage


def variant(package: DocumentSourcePackage) -> tuple[str, str, str, str, str]:
    if type(package) is SecDocumentSourcePackage:
        return (
            DOCUMENT_SCHEMA,
            DOCUMENT_PARSER,
            DOCUMENT_CONFIG,
            DOCUMENT_ENDPOINT,
            DOCUMENT_SOURCE_SCHEMA,
        )
    if type(package) is SecEmbeddedDocumentSourcePackage:
        return (
            EMBEDDED_SCHEMA,
            EMBEDDED_PARSER,
            EMBEDDED_CONFIG,
            EMBEDDED_ENDPOINT,
            EMBEDDED_SOURCE_SCHEMA,
        )
    raise TypeError("invalid SEC document financial source package")


def _spec(request: SecSgmlFinancialsRequest, endpoint: str = DOCUMENT_ENDPOINT) -> RequestSpec:
    return RequestSpec(
        "sec",
        endpoint,
        {"cik": request.cik, "accession_number": request.accession_number, "form": request.form},
    )


def _configuration(
    request: SecSgmlFinancialsRequest, config: str = DOCUMENT_CONFIG, parser: str = DOCUMENT_PARSER
) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "configuration_version": config,
                "parser_version": parser,
                "statement_types": list(request.statement_types),
                "include_dimensions": request.include_dimensions,
            }
        )
    ).hexdigest()


def _payload(
    request: SecSgmlFinancialsRequest,
    package: DocumentSourcePackage,
    vintage: SecObservedFinancialVintage,
    produced: datetime,
) -> bytes:
    schema, parser, config, _, _ = variant(package)
    source = _strict_json(package.manifest)
    return _canonical(
        {
            "schema": schema,
            "request": _request_payload(request),
            "parser_version": parser,
            "configuration_version": config,
            "configuration_identity": _configuration(request, config, parser),
            "source_package_receipt": _receipt(package.observation),
            "filing": source["filing"],
            "known_by_at": _stamp(vintage.known_by_at),
            "produced_at": _stamp(produced),
            "rows": [_row_payload(row) for row in vintage.rows],
        }
    )


def _binding(
    request: SecSgmlFinancialsRequest,
    package: DocumentSourcePackage,
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
