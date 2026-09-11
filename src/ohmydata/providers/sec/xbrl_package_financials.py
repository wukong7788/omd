"""Offline financial production from retained SEC XBRL component packages."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from ...core import AvailabilityBasis, AvailabilityEvidence, AvailabilityPrecision, RequestSpec
from ...core.snapshot import SnapshotObservationRef, SnapshotStore
from ._observed_xbrl_units import decode_raw_units
from ._pit_projection import _decode_projection
from .financials import SecCompanyFinancialVintage, SecStatementRow
from .pit import (
    SecNormalizedFinancialFactVersion,
    _version_from_replayed_projection,
    serialize_sec_typed_rows_projection,
)
from .sgml_financials import (
    _NORMALIZATION_VERSION,
    _NORMALIZED_SCHEMA_VERSION,
    _PROJECTION_SERIALIZATION,
    _SERIALIZATION,
    SecSgmlFinancialsRequest,
    _documents,
    _hash,
    _header,
    _positive,
    _rows_from_documents,
    _utc,
)
from .xbrl_package import SecXbrlPackageAvailability, decode_sec_xbrl_package

_PACKAGE_PARSER_VERSION_V1 = "sec-xbrl-package-financial-parser-v1-edgartools-5.56.0"
_PACKAGE_PARSER_VERSION_V2 = "sec-xbrl-package-financial-parser-v2-edgartools-5.56.0"
_PACKAGE_PARSER_VERSIONS = frozenset({_PACKAGE_PARSER_VERSION_V1, _PACKAGE_PARSER_VERSION_V2})
_PACKAGE_ADAPTER_VERSIONS = {
    _PACKAGE_PARSER_VERSION_V1: "sec-xbrl-package-financial-adapter-v1",
    _PACKAGE_PARSER_VERSION_V2: "sec-xbrl-package-financial-adapter-v2",
}
_PACKAGE_SERIALIZATION = "sec-xbrl-package-v1"


@dataclass(frozen=True)
class SecXbrlPackageFinancialProduction:
    """Replay-bound financial rows retaining both SGML and package receipts."""

    request: SecSgmlFinancialsRequest
    source_observation: SnapshotObservationRef
    package_observation: SnapshotObservationRef
    package_availability: SecXbrlPackageAvailability
    projection_observation: SnapshotObservationRef
    vintage: SecCompanyFinancialVintage
    versions: tuple[SecNormalizedFinancialFactVersion, ...]
    parser_version: str = field(default=_PACKAGE_PARSER_VERSION_V1, kw_only=True)


def produce_sec_financials_from_xbrl_package(
    *,
    source_store: SnapshotStore,
    source_observation: SnapshotObservationRef,
    package_store: SnapshotStore,
    package_observation: SnapshotObservationRef,
    package_availability: SecXbrlPackageAvailability,
    projection_store: SnapshotStore,
    request: SecSgmlFinancialsRequest,
    produced_at: datetime,
    max_raw_bytes: int = 8 * 1024 * 1024,
    max_rows: int = 10_000,
    parser_version: str = _PACKAGE_PARSER_VERSION_V2,
) -> SecXbrlPackageFinancialProduction:
    """Build replay-bound rows from full SGML and one retained XBRL package."""
    _positive(max_raw_bytes, "max_raw_bytes", 8 * 1024 * 1024)
    _positive(max_rows, "max_rows", 10_000)
    if type(parser_version) is not str or parser_version not in _PACKAGE_PARSER_VERSIONS:
        raise ValueError("unsupported SEC XBRL package financial parser version")
    produced = _utc(produced_at, "produced_at")
    source_expected = RequestSpec(
        "sec",
        "company-filing-sgml",
        {"cik": request.cik, "accession_number": request.accession_number, "form": request.form},
    )
    if source_observation.serialization_identifier != _SERIALIZATION:
        raise ValueError("source observation is not SEC filing SGML")
    source_replay = source_store.replay_observation(
        source_observation, source_expected, max_raw_bytes
    )
    try:
        raw = source_replay.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SEC filing SGML must be UTF-8") from exc
    accepted, filed, period, company_name = _header(raw, request)
    if not accepted <= source_observation.snapshot_fetched_at <= produced:
        raise ValueError("SEC acceptance and production times are not causal")
    _documents(raw, require_traditional=False)

    package_expected = RequestSpec(
        "sec",
        "company-filing-xbrl-package",
        {"cik": request.cik, "accession_number": request.accession_number, "form": request.form},
    )
    if package_observation.serialization_identifier != _PACKAGE_SERIALIZATION:
        raise ValueError("package observation is not SEC XBRL package")
    if package_availability.package_observation != package_observation:
        raise ValueError("package availability does not bind the package observation")
    evidence: AvailabilityEvidence = package_availability.evidence
    if (
        evidence.availability_basis is not AvailabilityBasis.SOURCE_DECLARED
        or evidence.availability_precision is not AvailabilityPrecision.TIMESTAMP
        or type(evidence.source_available_at) is not datetime
        or evidence.snapshot_fetched_at != package_observation.snapshot_fetched_at
    ):
        raise ValueError("package availability must be bound SOURCE_DECLARED timestamp evidence")
    if package_observation.snapshot_fetched_at > produced:
        raise ValueError("package observation and production times are not causal")
    package_replay = package_store.replay_observation(
        package_observation, package_expected, max_raw_bytes
    )
    manifest_time = datetime.fromisoformat(package_replay.manifest["retrieved_at"]).astimezone(UTC)
    if evidence.provider_first_observed_at != manifest_time:
        raise ValueError("package availability does not match package manifest")
    package = decode_sec_xbrl_package(package_replay.payload)
    if (
        package.sgml_fact_version != source_observation.fact_version
        or package.sgml_observation_identity != source_observation.observation_identity
        or (package.cik, package.accession_number, package.form)
        != (request.cik, request.accession_number, request.form)
    ):
        raise ValueError("SEC XBRL package does not bind the source SGML observation")
    declared = evidence.source_available_at
    if (
        package.source_available_at != declared
        or declared > package_observation.snapshot_fetched_at
    ):
        raise ValueError("invalid SEC XBRL package declared availability")
    source_available = max(accepted, declared)
    if source_available > produced:
        raise ValueError("SEC package availability and production times are not causal")

    component_bytes = package.components.to_mapping()
    documents = {
        "EX-101.SCH": component_bytes["schema"].decode("utf-8"),
        "EX-101.PRE": component_bytes["presentation"].decode("utf-8"),
        "EX-101.LAB": component_bytes["labels"].decode("utf-8"),
        "EX-101.INS": component_bytes["instance"].decode("utf-8"),
    }
    if "calculation" in component_bytes:
        documents["EX-101.CAL"] = component_bytes["calculation"].decode("utf-8")
    if "definition" in component_bytes:
        documents["EX-101.DEF"] = component_bytes["definition"].decode("utf-8")
    if parser_version == _PACKAGE_PARSER_VERSION_V1:
        rows = _rows_from_documents(raw, documents, request, max_rows)
    else:
        units = decode_raw_units(package.components.instance, max_elements=200_000, max_depth=128)
        native_rows = _rows_from_documents(raw, documents, request, max_rows)
        rows_: list[SecStatementRow] = []
        for row in native_rows:
            if row.unit_ref is None:
                raise ValueError("selected raw XBRL unit reference is missing")
            try:
                rows_.append(replace(row, unit=units[row.unit_ref]))
            except KeyError as exc:
                raise ValueError("selected raw XBRL unit reference is missing") from exc
        rows = tuple(rows_)
    vintage = SecCompanyFinancialVintage(
        symbol=request.symbol,
        cik=request.cik,
        company_name=company_name,
        form=request.form,
        accession_number=request.accession_number,
        filing_date=filed,
        period_end=period,
        accepted_at=accepted,
        availability_anchor=source_available,
        availability_basis="SOURCE_DECLARED",
        availability_precision="SECOND",
        availability_policy="max-filing-acceptance-package-declared",
        availability_lag_days=0,
        is_amendment=request.form.endswith("/A"),
        rows=rows,
    )
    config = _hash(
        {
            "parser_version": parser_version,
            "statement_types": request.statement_types,
            "include_dimensions": request.include_dimensions,
        }
    )
    payload = serialize_sec_typed_rows_projection(
        vintage,
        source_artifact_identity=package_observation.fact_version,
        source_available_at=source_available,
    )
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("SEC typed-row projection exceeds limit")
    projection = projection_store.observe(
        RequestSpec("sec", "financial-typed-rows", {"accession": request.accession_number}, ()),
        payload,
        produced,
        _PROJECTION_SERIALIZATION,
    )
    decoded = _decode_projection(payload)
    versions = tuple(
        _version_from_replayed_projection(
            observation=projection,
            projection=decoded,
            source_available_at=source_available,
            accession_number=request.accession_number,
            vintage_identity=vintage.vintage_identity,
            row_ordinal=index,
            expected_row=row,
            schema_version=_NORMALIZED_SCHEMA_VERSION,
            adapter_version=_PACKAGE_ADAPTER_VERSIONS[parser_version],
            normalization_version=_NORMALIZATION_VERSION,
            configuration_identity=config,
            recorded_at=produced,
        )
        for index, row in enumerate(rows)
    )
    return SecXbrlPackageFinancialProduction(
        request,
        source_observation,
        package_observation,
        package_availability,
        projection,
        vintage,
        versions,
        parser_version=parser_version,
    )


__all__ = ["SecXbrlPackageFinancialProduction", "produce_sec_financials_from_xbrl_package"]
