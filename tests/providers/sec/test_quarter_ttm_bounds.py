"""Public TTM calculation bounds using real offline projection/selection evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal, Inexact, localcontext

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
    SecPitMode,
    SecPitPolicy,
    SecQualityRecord,
    SecQualityStatus,
    SecQuarterDeclaration,
    SecQuarterTtmConfig,
    SecQuarterTtmRecipe,
    SecStatementRow,
    compute_sec_four_quarter_ttm,
    serialize_sec_typed_rows_projection,
)


def case(root, values=None, dimensions=(None,) * 4):
    values = values or (Decimal(1),) * 4
    periods = (
        (date(2024, 1, 1), date(2024, 3, 31)),
        (date(2024, 4, 1), date(2024, 6, 30)),
        (date(2024, 7, 1), date(2024, 9, 30)),
        (date(2024, 10, 1), date(2024, 12, 31)),
    )
    rows = tuple(
        SecStatementRow(
            "income_statement",
            "SyntheticRevenue",
            "fake:Revenue",
            "Synthetic revenue",
            value,
            str(value),
            "USD",
            period_start=start,
            period_end=end,
            period_type="duration",
            dimension=dimension,
        )
        for value, (start, end), dimension in zip(values, periods, dimensions, strict=True)
    )
    vintage = SecCompanyFinancialVintage(
        "FAKE", "1", "Synthetic", "10-K", "0000000001-25-000001", date(2025, 1, 1), rows=rows
    )
    at = datetime(2025, 1, 2, tzinfo=UTC)
    cutoff = datetime(2025, 2, 1, tzinfo=UTC)
    store = SnapshotStore(root)
    payload = serialize_sec_typed_rows_projection(
        vintage, source_artifact_identity="a" * 64, source_available_at=at
    )
    observation = store.observe(
        RequestSpec("sec", "financial-typed-rows", {"accession": vintage.accession_number}),
        payload,
        at,
        "sec-financial-typed-rows-projection-v1",
    )
    availability = AvailabilityEvidence(
        at, at, at, AvailabilityBasis.SOURCE_DECLARED, AvailabilityPrecision.TIMESTAMP
    )
    versions = tuple(
        SecNormalizedFinancialFactVersion.from_projection(
            store=store,
            observation=observation,
            availability=availability,
            vintage=vintage,
            row_ordinal=index,
            schema_version="synthetic-schema-v1",
            adapter_version="synthetic-adapter-v1",
            normalization_version="synthetic-normalization-v1",
            configuration_identity="b" * 64,
            recorded_at=at,
        )
        for index in range(4)
    )
    quarters = tuple(
        SecQuarterDeclaration(
            v.normalized_version_id,
            2024,
            index + 1,
            True,
            start,
            end,
            "fake:Revenue",
            "synthetic-consolidated",
            "synthetic-common",
            "synthetic-cohort",
            "synthetic-reference",
        )
        for index, (v, (start, end)) in enumerate(zip(versions, periods, strict=True))
    )
    config = SecQuarterTtmConfig(
        SecQuarterTtmRecipe.REVENUE_V1,
        "1",
        "revenue",
        "synthetic-issuer",
        "synthetic-metric",
        quarters,
        SecPitMode.MARKET_KNOWN,
        cutoff,
        SecPitPolicy(
            "synthetic-schema-v1",
            "synthetic-adapter-v1",
            "synthetic-normalization-v1",
            "b" * 64,
            "synthetic-quality-v1",
            cutoff,
        ),
    )
    qualities = tuple(
        SecQualityRecord(v.normalized_version_id, "synthetic-quality-v1", SecQualityStatus.PASS, at)
        for v in versions
    )
    return config, versions, qualities


def calculate(config, versions, qualities, **kwargs):
    return compute_sec_four_quarter_ttm(
        config=config, versions=versions, quality_records=qualities, **kwargs
    )


def test_overlapping_quarters_are_rejected(tmp_path):
    config, versions, qualities = case(tmp_path)
    overlapping = replace(config.quarters[1], period_start=config.quarters[0].period_end)
    config = replace(config, quarters=(config.quarters[0], overlapping, *config.quarters[2:]))
    with pytest.raises(ValueError, match="overlap"):
        calculate(config, versions, qualities)


def test_dimension_mismatch_is_rejected_on_selected_rows(tmp_path):
    config, versions, qualities = case(
        tmp_path, dimensions=(None, None, None, '{"fake:Axis":"fake:Member"}')
    )
    with pytest.raises(ValueError, match="incompatible unit or dimensions"):
        calculate(config, versions, qualities)


def test_normalization_policy_mismatch_remains_unavailable(tmp_path):
    config, versions, qualities = case(tmp_path)
    config = replace(
        config, policy=replace(config.policy, normalization_version="different-normalization-v1")
    )
    with pytest.raises(ValueError, match="unavailable"):
        calculate(config, versions, qualities)


def test_record_cap_is_aggregate_and_exact_limit_is_permitted(tmp_path):
    config, versions, qualities = case(tmp_path)
    assert calculate(config, versions, qualities, max_input_records=8).value == 4
    with pytest.raises(ValueError, match="record"):
        calculate(config, versions, qualities, max_input_records=7)
    expanded = versions + (versions[0],) * 9992
    assert calculate(config, expanded, qualities).value == 4


def test_excess_generator_stops_at_one_sentinel(tmp_path):
    config, versions, qualities = case(tmp_path)
    consumed = []

    def infinite():
        while True:
            consumed.append(1)
            yield versions[0]

    with pytest.raises(ValueError, match="record"):
        calculate(config, infinite(), qualities, max_input_records=8)
    assert len(consumed) == 9


@pytest.mark.parametrize("limit", [True, 0, -1, 10001, 8.0])
def test_invalid_record_cap_is_rejected_before_enumeration(tmp_path, limit):
    config, _, _ = case(tmp_path)

    def forbidden():
        pytest.fail("input consumed before cap validation")
        yield

    with pytest.raises((ValueError, TypeError)):
        calculate(config, forbidden(), (), max_input_records=limit)


def test_text_limit_counts_utf8_bytes(tmp_path):
    config, _, _ = case(tmp_path)
    exact = "界" * 341 + "a"
    quarter = replace(config.quarters[0], declaration_reference=exact)
    assert replace(config, quarters=(quarter, *config.quarters[1:])).configuration_identity
    with pytest.raises(ValueError, match="1024"):
        replace(quarter, declaration_reference=exact + "a")


def test_canonical_config_budget_includes_json_escaping(tmp_path):
    config, _, _ = case(tmp_path)
    quarters = tuple(
        replace(
            quarter,
            native_concept="\0" * 1024,
            accounting_scope="\0" * 1024,
            attribution_scope="\0" * 1024,
            comparability_revision_cohort="\0" * 1024,
            declaration_reference="\0" * 1024,
        )
        for quarter in config.quarters
    )
    with pytest.raises(ValueError, match="configuration exceeds byte limit"):
        replace(config, quarters=quarters)


@pytest.mark.parametrize("exponent", [-10000, 10000])
def test_exact_exponent_boundary_without_expanding_scale(tmp_path, exponent):
    value = Decimal((0, (1,), exponent))
    config, versions, qualities = case(tmp_path, (value,) * 4)
    assert (
        calculate(config, versions, qualities).value.as_tuple()
        == Decimal((0, (4,), exponent)).as_tuple()
    )


def test_exact_large_arithmetic_ignores_ambient_context(tmp_path):
    value = Decimal((0, (9,) * 9999, 0))
    config, versions, qualities = case(tmp_path, (value,) * 4)
    with localcontext() as context:
        context.prec = 1
        context.traps[Inexact] = True
        result = calculate(config, versions, qualities)
    assert result.value.as_tuple() == Decimal((0, (3,) + (9,) * 9998 + (6,), 0)).as_tuple()


def test_cancellation_is_exact_under_tiny_precision(tmp_path):
    values = (Decimal("1E+100"), Decimal("-1E+100"), Decimal("0.001"), Decimal("-0.001"))
    config, versions, qualities = case(tmp_path, values)
    baseline = calculate(config, versions, qualities)
    with localcontext() as context:
        context.prec = 1
        context.traps[Inexact] = True
        result = calculate(config, versions, qualities)
    assert result.value.as_tuple() == Decimal("0.000").as_tuple()
    assert result.result_identity == baseline.result_identity


@pytest.mark.parametrize(
    "value",
    [Decimal("1E+10001"), Decimal("1E-10001"), Decimal((0, (1,) * 10001, 0)), Decimal("1E+9999")],
)
def test_oversized_arithmetic_is_rejected(tmp_path, value):
    config, versions, qualities = case(tmp_path, (value, Decimal(1), Decimal(1), Decimal(1)))
    with pytest.raises(ValueError, match="digit bounds"):
        calculate(config, versions, qualities)
