"""Offline coverage for SEC normalized-row structural annotations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
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
    SecCompanyFinancialVintage,
    SecNormalizedFinancialFactVersion,
    SecQualityFindingStatus,
    SecStatementRow,
    evaluate_sec_structural_quality,
    load_sec_pit_bundle,
    select_sec_quality_findings,
    serialize_sec_typed_rows_projection,
    write_sec_pit_bundle,
)
from tests.providers.sec.test_pit_bundle import _records

CONFIG = "c" * 64
ARTIFACT = "a" * 64
PRODUCTION = datetime(2024, 5, 2, 10, tzinfo=UTC)
EVALUATED = PRODUCTION + timedelta(hours=1)


def _row(**changes: object) -> SecStatementRow:
    values: dict[str, object] = {
        "statement_type": "income_statement",
        "standard_concept": "Revenue",
        "concept": "us-gaap:Revenue",
        "label": "Revenue",
        "value": Decimal("1.00"),
        "value_native": "1.00",
        "unit": "USD",
        "period_start": date(2024, 1, 1),
        "period_end": date(2024, 3, 31),
        "period_type": "duration",
        "context_ref": "ctx",
        "unit_ref": "u1",
    }
    values.update(changes)
    return SecStatementRow(**values)  # type: ignore[arg-type]


def _versions(
    root: Path,
    *rows: SecStatementRow,
    recipes: tuple[tuple[str, str, datetime], ...] = (("normalization-v1", CONFIG, PRODUCTION),),
) -> tuple[SecNormalizedFinancialFactVersion, ...]:
    vintage = SecCompanyFinancialVintage(
        "FAKE",
        "0000000001",
        "Synthetic",
        "10-Q",
        "0000000001-24-000001",
        date(2024, 5, 1),
        accepted_at=datetime(2024, 5, 1, 21, tzinfo=UTC),
        rows=rows or (_row(),),
    )
    store = SnapshotStore(root)
    source_at = datetime(2024, 5, 1, 21, tzinfo=UTC)
    observation = store.observe(
        RequestSpec("sec", "financial-typed-rows", {"accession": vintage.accession_number}),
        serialize_sec_typed_rows_projection(
            vintage, source_artifact_identity=ARTIFACT, source_available_at=source_at
        ),
        datetime(2024, 5, 2, 9, tzinfo=UTC),
        "sec-financial-typed-rows-projection-v1",
    )
    evidence = AvailabilityEvidence(
        source_at,
        observation.snapshot_fetched_at,
        observation.snapshot_fetched_at,
        AvailabilityBasis.SOURCE_DECLARED,
        AvailabilityPrecision.TIMESTAMP,
    )
    return tuple(
        SecNormalizedFinancialFactVersion.from_projection(
            store=store,
            observation=observation,
            availability=evidence,
            vintage=vintage,
            row_ordinal=index,
            schema_version="sec-financial-normalized-v1",
            adapter_version="adapter-v1",
            normalization_version=normalization_version,
            configuration_identity=configuration_identity,
            recorded_at=recorded_at,
        )
        for index in range(len(vintage.rows))
        for normalization_version, configuration_identity, recorded_at in recipes
    )


def _evaluate(*versions: SecNormalizedFinancialFactVersion):
    return evaluate_sec_structural_quality(versions, detected_at=EVALUATED, recorded_at=EVALUATED)


@pytest.mark.parametrize("field", ("concept", "context_ref", "unit", "unit_ref"))
def test_missing_identity_fields_are_separate_open_findings(tmp_path: Path, field: str) -> None:
    report = _evaluate(*_versions(tmp_path, _row(**{field: " "})))
    assert [
        (item.rule_id, item.affected_fields, item.missing_reason.value) for item in report.findings
    ] == [("sec.structural.field_missing", (field,), "FIELD_MISSING")]
    assert report.findings[0].status is SecQualityFindingStatus.OPEN


def test_value_presence_and_zero_negative_values_are_distinct(tmp_path: Path) -> None:
    missing = _evaluate(*_versions(tmp_path / "missing", _row(value=None)))
    present = _evaluate(
        *_versions(tmp_path / "present", _row(value=Decimal(0)), _row(value=Decimal(-2)))
    )
    assert [(item.affected_fields, item.finding_key) for item in missing.findings] == [
        (("value",), "value")
    ]
    assert {item.rule_id for item in present.findings} == {"sec.structural.duplicate_value"}


@pytest.mark.parametrize(
    ("row", "fields", "reason"),
    [
        (_row(period_type=None), (("period_type",),), "FIELD_MISSING"),
        (_row(period_type="annual"), (("period_type",),), None),
        (
            _row(period_type="instant", period_start=None, period_end=None),
            (("period_end",),),
            "FIELD_MISSING",
        ),
        (
            _row(period_type="duration", period_start=None, period_end=None),
            (("period_end",), ("period_start",)),
            "FIELD_MISSING",
        ),
    ],
)
def test_period_rules_are_explicit(
    tmp_path: Path, row: SecStatementRow, fields: tuple[tuple[str, ...], ...], reason: str | None
) -> None:
    report = _evaluate(*_versions(tmp_path, row))
    assert {item.affected_fields for item in report.findings} == set(fields)
    assert {
        None if item.missing_reason is None else item.missing_reason.value
        for item in report.findings
    } == {reason}


def test_duplicate_values_are_linear_and_exact(tmp_path: Path) -> None:
    versions = _versions(
        tmp_path,
        _row(value=Decimal("1.0"), value_native="1.0"),
        _row(value=Decimal("2.00"), value_native="2.00"),
        _row(value=Decimal("1.00"), value_native="1.00"),
    )
    report = _evaluate(*reversed(versions), versions[0])
    duplicates = [
        item for item in report.findings if item.rule_id == "sec.structural.duplicate_value"
    ]
    assert len(duplicates) == 3
    assert all(item.affected_fields == ("value",) for item in duplicates)
    assert report.input_record_count == 4
    assert report.checked_normalized_version_ids == tuple(
        sorted(item.normalized_version_id for item in versions)
    )


def test_large_duplicate_group_produces_one_finding_per_version(tmp_path: Path) -> None:
    versions = _versions(tmp_path, *(_row(value=Decimal(index)) for index in range(16)))
    report = _evaluate(*versions)
    assert len(report.findings) == len(versions)
    assert {item.finding_key for item in report.findings} == {"duplicate_value"}


def test_numerically_equal_decimals_do_not_conflict(tmp_path: Path) -> None:
    report = _evaluate(
        *_versions(tmp_path, _row(value=Decimal("1.0")), _row(value=Decimal("1.00")))
    )
    assert report.findings == ()


@pytest.mark.parametrize(
    "changed", ("unit", "period_end", "decimals", "context_ref", "dimension", "decimals_native")
)
def test_duplicate_rule_does_not_cross_identity_boundaries(tmp_path: Path, changed: str) -> None:
    first = _row(value=Decimal(1))
    value: object = {
        "unit": "EUR",
        "period_end": date(2024, 6, 30),
        "decimals": 2,
        "context_ref": "other",
        "dimension": "segment",
        "decimals_native": "2",
    }[changed]
    second = replace(first, value=Decimal(2), **{changed: value})
    report = _evaluate(*_versions(tmp_path, first, second))
    assert not any(item.rule_id == "sec.structural.duplicate_value" for item in report.findings)


def test_duplicate_rule_does_not_cross_observation_recipe_or_production(tmp_path: Path) -> None:
    first = _row(value=Decimal(1))
    second = _row(value=Decimal(2))
    separate_observations = _evaluate(
        _versions(tmp_path / "one", first)[0], _versions(tmp_path / "two", second)[0]
    )
    recipes = (
        ("normalization-v1", CONFIG, PRODUCTION),
        ("normalization-v2", "d" * 64, PRODUCTION + timedelta(minutes=1)),
    )
    same_observation = _versions(tmp_path / "recipe", first, second, recipes=recipes)
    separated_recipe_and_production = evaluate_sec_structural_quality(
        [same_observation[0], same_observation[3]],
        detected_at=EVALUATED,
        recorded_at=EVALUATED,
    )
    assert separate_observations.findings == ()
    assert separated_recipe_and_production.findings == ()


def test_report_is_order_independent_and_leaves_versions_unchanged(tmp_path: Path) -> None:
    versions = _versions(tmp_path, _row(value=Decimal(1)), _row(value=Decimal(2)))
    before = tuple(
        (item.row, item.content_identity, item.normalized_version_id) for item in versions
    )
    forward = _evaluate(*versions)
    reverse = _evaluate(*reversed(versions))
    assert forward == reverse
    assert (
        tuple((item.row, item.content_identity, item.normalized_version_id) for item in versions)
        == before
    )


def test_admission_time_and_bounds_fail_closed(tmp_path: Path) -> None:
    version = _versions(tmp_path)[0]
    with pytest.raises(ValueError, match="timestamps"):
        evaluate_sec_structural_quality(
            [version], detected_at=PRODUCTION - timedelta(seconds=1), recorded_at=EVALUATED
        )
    with pytest.raises(ValueError, match="exceeds max_records"):
        evaluate_sec_structural_quality(
            (version for _ in range(2)), detected_at=EVALUATED, recorded_at=EVALUATED, max_records=1
        )
    with pytest.raises(ValueError, match="exceeds max_findings"):
        incomplete = _versions(tmp_path / "incomplete", _row(value=None, context_ref=None))[0]
        evaluate_sec_structural_quality(
            [incomplete], detected_at=EVALUATED, recorded_at=EVALUATED, max_findings=1
        )


def test_bounded_generator_stops_after_max_plus_one(tmp_path: Path) -> None:
    version = _versions(tmp_path)[0]
    consumed = 0

    def values():
        nonlocal consumed
        while True:
            consumed += 1
            yield version

    with pytest.raises(ValueError, match="exceeds max_records"):
        evaluate_sec_structural_quality(
            values(), detected_at=EVALUATED, recorded_at=EVALUATED, max_records=2
        )
    assert consumed == 3


def test_tampered_immutable_version_is_rejected_without_repair(tmp_path: Path) -> None:
    version = _versions(tmp_path)[0]
    object.__setattr__(version, "normalized_version_id", "f" * 64)
    with pytest.raises(ValueError, match="identity does not match"):
        _evaluate(version)


def test_admission_rejects_oversized_row_text_before_binding_clone(tmp_path: Path) -> None:
    version = _versions(tmp_path)[0]
    object.__setattr__(version.row, "concept", "x" * 4_097)
    with pytest.raises(ValueError, match="text limit"):
        _evaluate(version)


@pytest.mark.parametrize("value", (Decimal("1" * 1_025), Decimal("1e10001")))
def test_admission_rejects_oversized_decimal(tmp_path: Path, value: Decimal) -> None:
    version = _versions(tmp_path)[0]
    object.__setattr__(version.row, "value", value)
    with pytest.raises(ValueError, match="decimal limit"):
        _evaluate(version)


def test_admission_rejects_observation_metadata_tampering_without_repair(tmp_path: Path) -> None:
    version = _versions(tmp_path)[0]
    object.__setattr__(version.observation, "provider", "other")
    with pytest.raises(ValueError, match="not SEC"):
        _evaluate(version)
    assert version.observation.provider == "other"


def test_generated_findings_round_trip_in_existing_v2_bundle(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    version = _versions(source_root, _row(value=None))[0]
    findings = _evaluate(version).findings
    quality, commit = _records(version)
    bundle_store = SnapshotStore(tmp_path / "bundle")
    reference = write_sec_pit_bundle(
        store=bundle_store,
        batch_identity="structural-v2",
        versions=[version],
        quality_records=[quality],
        consumer_commits=[commit],
        quality_findings=findings,
        source_store=SnapshotStore(source_root),
        resolve_observation=lambda identity: version.observation,
        captured_at=EVALUATED + timedelta(hours=2),
    )
    loaded = load_sec_pit_bundle(
        store=bundle_store,
        bundle_ref=reference,
        source_store=SnapshotStore(source_root),
        resolve_observation=lambda identity: version.observation,
    )
    assert loaded.quality_findings == findings
    assert (
        select_sec_quality_findings(
            loaded.quality_findings,
            normalized_version_id=version.normalized_version_id,
            rule_version="sec-structural-v1",
            cutoff=EVALUATED,
        )
        == findings
    )
