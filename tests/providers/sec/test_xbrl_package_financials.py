"""Offline production tests for retained SEC XBRL component packages."""

import socket
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ohmydata.core import (
    AvailabilityBasis,
    AvailabilityEvidence,
    AvailabilityPrecision,
    RequestSpec,
    SnapshotStore,
)
from ohmydata.providers.sec import (
    SecXbrlPackageAvailability,
    SecXbrlPackageComponents,
    produce_sec_financials_from_xbrl_package,
    serialize_sec_xbrl_package,
)
from ohmydata.providers.sec.sgml_financials import _documents

sys.path.insert(0, str(Path(__file__).parent))
from test_sgml_financials import _observation, _raw, _request

pytest.importorskip("edgar")


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def _inline_raw(*, acceptance: str = "20240501170000", dimension: bool = False) -> bytes:
    return _raw(acceptance=acceptance, components=False, dimension=dimension).replace(
        b"</SEC-HEADER>\n",
        b"</SEC-HEADER>\n<DOCUMENT>\n<TYPE>10-Q\n<TEXT><html/></TEXT>\n</DOCUMENT>\n",
    )


def _package_observation(
    tmp_path,
    source_observation,
    *,
    embedded: dict[str, str],
    declared: datetime,
    observed: datetime,
):
    payload = serialize_sec_xbrl_package(
        sgml_observation=source_observation,
        cik="0000000001",
        accession_number="0000000001-24-000001",
        form="10-Q",
        source_available_at=declared,
        components=SecXbrlPackageComponents(
            embedded["EX-101.SCH"].encode(),
            embedded["EX-101.PRE"].encode(),
            embedded["EX-101.LAB"].encode(),
            embedded["EX-101.INS"].encode(),
        ),
    )
    store = SnapshotStore(tmp_path / "packages")
    observation = store.observe(
        RequestSpec(
            "sec",
            "company-filing-xbrl-package",
            {"cik": "0000000001", "accession_number": "0000000001-24-000001", "form": "10-Q"},
        ),
        payload,
        observed,
        "sec-xbrl-package-v1",
    )
    evidence = AvailabilityEvidence.from_observation(
        store,
        observation,
        source_available_at=declared,
        availability_basis=AvailabilityBasis.SOURCE_DECLARED,
        availability_precision=AvailabilityPrecision.TIMESTAMP,
    )
    return store, observation, SecXbrlPackageAvailability(observation, evidence)


def _package_components(embedded: dict[str, str]) -> tuple[bytes, bytes, bytes, bytes]:
    return (
        embedded["EX-101.SCH"].encode(),
        embedded["EX-101.PRE"].encode(),
        embedded["EX-101.LAB"].encode(),
        embedded["EX-101.INS"].encode(),
    )


def test_retained_xbrl_package_reuses_real_parser_and_binds_availability(tmp_path):
    source, observation = _observation(tmp_path, _inline_raw())
    packages, package, availability = _package_observation(
        tmp_path,
        observation,
        embedded=_documents(_raw().decode()),
        declared=datetime(2024, 5, 1, 22, tzinfo=UTC),
        observed=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
    )
    production = produce_sec_financials_from_xbrl_package(
        source_store=source,
        source_observation=observation,
        package_store=packages,
        package_observation=package,
        package_availability=availability,
        projection_store=SnapshotStore(tmp_path / "projection-package"),
        request=_request(),
        produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
    )
    assert production.versions[0].source_artifact_identity == package.fact_version
    assert production.versions[0].source_available_at == datetime(2024, 5, 1, 22, tzinfo=UTC)
    assert production.vintage.availability_anchor == datetime(2024, 5, 1, 22, tzinfo=UTC)
    assert production.vintage.availability_policy == "max-filing-acceptance-package-declared"
    assert production.vintage.rows[0].value == Decimal(123)
    assert production.vintage.rows[0].value_native == "123"
    assert production.vintage.rows[0].unit == "iso4217:USD"
    assert production.vintage.rows[0].currency == "USD"
    assert production.vintage.rows[0].decimals_native == "0"


def test_package_evidence_manifest_mismatch_fails_before_projection(tmp_path):
    source, observation = _observation(tmp_path, _raw())
    packages, package, availability = _package_observation(
        tmp_path,
        observation,
        embedded=_documents(_raw().decode()),
        declared=datetime(2024, 5, 1, 21, tzinfo=UTC),
        observed=datetime(2024, 5, 1, 22, tzinfo=UTC),
    )
    projection = SnapshotStore(tmp_path / "projection")
    with pytest.raises(ValueError, match="manifest"):
        produce_sec_financials_from_xbrl_package(
            source_store=source,
            source_observation=observation,
            package_store=packages,
            package_observation=package,
            package_availability=SecXbrlPackageAvailability(
                package,
                replace(
                    availability.evidence,
                    provider_first_observed_at=datetime(2024, 5, 1, 20, tzinfo=UTC),
                ),
            ),
            projection_store=projection,
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
        )
    assert not list(projection.root.rglob("response.bin"))


@pytest.mark.parametrize(
    ("acceptance", "source_observed", "declared", "package_observed", "expected"),
    [
        (
            "20240501170000",
            datetime(2024, 5, 1, 22, tzinfo=UTC),
            datetime(2024, 5, 1, 22, tzinfo=UTC),
            datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
            datetime(2024, 5, 1, 22, tzinfo=UTC),
        ),
        (
            "20240501180000",
            datetime(2024, 5, 1, 23, tzinfo=UTC),
            datetime(2024, 5, 1, 21, tzinfo=UTC),
            datetime(2024, 5, 1, 23, 30, tzinfo=UTC),
            datetime(2024, 5, 1, 22, tzinfo=UTC),
        ),
    ],
)
def test_package_vintage_and_versions_use_availability_maximum(
    tmp_path, acceptance, source_observed, declared, package_observed, expected
):
    source, observation = _observation(
        tmp_path, _inline_raw(acceptance=acceptance), source_observed
    )
    packages, package, availability = _package_observation(
        tmp_path,
        observation,
        embedded=_documents(_raw(acceptance=acceptance).decode()),
        declared=declared,
        observed=package_observed,
    )
    production = produce_sec_financials_from_xbrl_package(
        source_store=source,
        source_observation=observation,
        package_store=packages,
        package_observation=package,
        package_availability=availability,
        projection_store=SnapshotStore(tmp_path / "projection"),
        request=_request(),
        produced_at=datetime(2024, 5, 2, tzinfo=UTC),
    )
    assert production.vintage.availability_anchor == expected
    assert all(version.source_available_at == expected for version in production.versions)


def test_package_replay_restart_is_idempotent_and_preserves_dimensions(tmp_path):
    source, observation = _observation(tmp_path, _inline_raw(dimension=True))
    packages, package, availability = _package_observation(
        tmp_path,
        observation,
        embedded=_documents(_raw(dimension=True).decode()),
        declared=datetime(2024, 5, 1, 22, tzinfo=UTC),
        observed=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
    )
    request = replace(_request(), include_dimensions=True)
    projection = SnapshotStore(tmp_path / "projection")
    kwargs = {
        "source_observation": observation,
        "package_observation": package,
        "package_availability": availability,
        "request": request,
        "produced_at": datetime(2024, 5, 1, 23, tzinfo=UTC),
    }
    first = produce_sec_financials_from_xbrl_package(
        source_store=source, package_store=packages, projection_store=projection, **kwargs
    )
    second = produce_sec_financials_from_xbrl_package(
        source_store=SnapshotStore(source.root),
        package_store=SnapshotStore(packages.root),
        projection_store=SnapshotStore(projection.root),
        **kwargs,
    )
    assert first.projection_observation == second.projection_observation
    assert first.vintage.rows[0].dimension == (
        '{"us-gaap:ProductOrServiceAxis":"us-gaap:ProductMember"}'
    )
    assert first.vintage.rows[0].value == Decimal(123)
    assert first.vintage.rows[0].unit == "iso4217:USD"
    assert first.vintage.rows[0].period_start == date(2024, 1, 1)
    assert first.vintage.rows[0].period_end == date(2024, 3, 31)


def test_package_source_binding_and_production_causality_fail_before_projection(tmp_path):
    inline = _inline_raw()
    source, observation = _observation(tmp_path, inline)
    _, other_observation = _observation(
        tmp_path / "other", inline, datetime(2024, 5, 1, 22, 15, tzinfo=UTC)
    )
    packages, package, availability = _package_observation(
        tmp_path,
        other_observation,
        embedded=_documents(_raw().decode()),
        declared=datetime(2024, 5, 1, 22, tzinfo=UTC),
        observed=datetime(2024, 5, 1, 23, 30, tzinfo=UTC),
    )
    projection = SnapshotStore(tmp_path / "projection")
    with pytest.raises(ValueError, match="does not bind"):
        produce_sec_financials_from_xbrl_package(
            source_store=source,
            source_observation=observation,
            package_store=packages,
            package_observation=package,
            package_availability=availability,
            projection_store=projection,
            request=_request(),
            produced_at=datetime(2024, 5, 2, tzinfo=UTC),
        )
    assert not list(projection.root.rglob("response.bin"))


@pytest.mark.parametrize("failure", ["wrapper", "declared-mismatch", "declared-late"])
def test_package_evidence_binding_failures_write_no_projection(tmp_path, failure):
    source, observation = _observation(tmp_path, _inline_raw())
    packages, package, availability = _package_observation(
        tmp_path,
        observation,
        embedded=_documents(_raw().decode()),
        declared=datetime(2024, 5, 1, 22, tzinfo=UTC),
        observed=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
    )
    if failure == "wrapper":
        alternate = packages.observe(
            RequestSpec(
                "sec",
                "company-filing-xbrl-package",
                {"cik": "0000000001", "accession_number": "0000000001-24-000001", "form": "10-Q"},
            ),
            serialize_sec_xbrl_package(
                sgml_observation=observation,
                cik="0000000001",
                accession_number="0000000001-24-000001",
                form="10-Q",
                source_available_at=datetime(2024, 5, 1, 22, tzinfo=UTC),
                components=SecXbrlPackageComponents(
                    *_package_components(_documents(_raw().decode())), calculation=b"<calculation/>"
                ),
            ),
            datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
            "sec-xbrl-package-v1",
        )
        availability = SecXbrlPackageAvailability(
            alternate,
            AvailabilityEvidence.from_observation(
                packages,
                alternate,
                source_available_at=datetime(2024, 5, 1, 22, tzinfo=UTC),
                availability_basis=AvailabilityBasis.SOURCE_DECLARED,
                availability_precision=AvailabilityPrecision.TIMESTAMP,
            ),
        )
        message = "does not bind"
    elif failure == "declared-mismatch":
        availability = SecXbrlPackageAvailability(
            package,
            replace(
                availability.evidence, source_available_at=datetime(2024, 5, 1, 21, tzinfo=UTC)
            ),
        )
        message = "declared availability"
    else:
        packages, package, availability = _package_observation(
            tmp_path / "late-declared",
            observation,
            embedded=_documents(_raw().decode()),
            declared=datetime(2024, 5, 1, 23, tzinfo=UTC),
            observed=datetime(2024, 5, 1, 22, 30, tzinfo=UTC),
        )
        message = "declared availability"
    projection = SnapshotStore(tmp_path / "projection")
    with pytest.raises(ValueError, match=message):
        produce_sec_financials_from_xbrl_package(
            source_store=source,
            source_observation=observation,
            package_store=packages,
            package_observation=package,
            package_availability=availability,
            projection_store=projection,
            request=_request(),
            produced_at=datetime(2024, 5, 2, tzinfo=UTC),
        )
    assert not list(projection.root.rglob("response.bin"))
    packages, package, availability = _package_observation(
        tmp_path / "late",
        observation,
        embedded=_documents(_raw().decode()),
        declared=datetime(2024, 5, 1, 22, tzinfo=UTC),
        observed=datetime(2024, 5, 1, 23, 30, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="production times"):
        produce_sec_financials_from_xbrl_package(
            source_store=source,
            source_observation=observation,
            package_store=packages,
            package_observation=package,
            package_availability=availability,
            projection_store=projection,
            request=_request(),
            produced_at=datetime(2024, 5, 1, 23, tzinfo=UTC),
        )
    assert not list(projection.root.rglob("response.bin"))
