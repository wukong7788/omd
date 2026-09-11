from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
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
    SecConsumerCommit,
    SecNormalizedFinancialFactVersion,
    SecPitMode,
    SecPitPolicy,
    SecQualityRecord,
    SecQualityStatus,
    SecStatementRow,
    serialize_sec_typed_rows_projection,
)
from ohmydata.providers.sec.quarter_ttm import (
    SecQuarterDeclaration,
    SecQuarterTtmConfig,
    SecQuarterTtmRecipe,
    compute_sec_four_quarter_ttm,
)

_CONFIG = "c" * 64
_ARTIFACT = "a" * 64
_CUT = datetime(2025, 3, 1, tzinfo=UTC)


def _versions(
    root: Path,
    values: tuple[Decimal | None, ...] = (Decimal("1.10"),) * 4,
    units: tuple[str | None, ...] = ("USD",) * 4,
    periods: tuple[tuple[date, date], ...] | None = None,
):
    store = SnapshotStore(root)
    periods = periods or (
        (date(2023, 1, 1), date(2023, 3, 31)),
        (date(2023, 4, 1), date(2023, 6, 30)),
        (date(2023, 7, 1), date(2023, 9, 30)),
        (date(2023, 10, 1), date(2023, 12, 31)),
    )
    output = []
    for ordinal, ((start, end), value, unit) in enumerate(zip(periods, values, units, strict=True)):
        row = SecStatementRow(
            "income_statement",
            "Revenue",
            "us-gaap:Revenue",
            "Revenue",
            value,
            str(value),
            unit,
            period_start=start,
            period_end=end,
            period_type="duration",
            dimension=None,
        )
        vintage = SecCompanyFinancialVintage(
            symbol="FAKE",
            cik="1",
            company_name="Synthetic",
            form="10-Q",
            accession_number=f"0000000001-24-00000{ordinal + 1}",
            filing_date=end,
            rows=(row,),
        )
        source_at = datetime(2024, ordinal + 1, 1, tzinfo=UTC)
        observed = source_at + timedelta(hours=1)
        payload = serialize_sec_typed_rows_projection(
            vintage, source_artifact_identity=_ARTIFACT, source_available_at=source_at
        )
        observation = store.observe(
            RequestSpec("sec", "financial-typed-rows", {"accession": vintage.accession_number}, ()),
            payload,
            observed,
            "sec-financial-typed-rows-projection-v1",
        )
        output.append(
            SecNormalizedFinancialFactVersion.from_projection(
                store=store,
                observation=observation,
                availability=AvailabilityEvidence(
                    source_at,
                    observed,
                    observed,
                    AvailabilityBasis.SOURCE_DECLARED,
                    AvailabilityPrecision.TIMESTAMP,
                ),
                vintage=vintage,
                row_ordinal=0,
                schema_version="sec-financial-normalized-v1",
                adapter_version="adapter-v1",
                normalization_version="normalization-v1",
                configuration_identity=_CONFIG,
                recorded_at=observed,
            )
        )
    return tuple(output)


def _config(versions, *, declarations=None, mode=SecPitMode.MARKET_KNOWN):
    starts = (date(2023, 1, 1), date(2023, 4, 1), date(2023, 7, 1), date(2023, 10, 1))
    ends = (date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31))
    quarters = declarations or tuple(
        SecQuarterDeclaration(
            v.normalized_version_id,
            2023,
            index + 1,
            True,
            start,
            end,
            "us-gaap:Revenue",
            "consolidated",
            "all-attributable",
            "cohort-v1",
            f"decl-{index}",
        )
        for index, (v, start, end) in enumerate(zip(versions, starts, ends, strict=True))
    )
    return SecQuarterTtmConfig(
        SecQuarterTtmRecipe.REVENUE_V1,
        "1",
        "revenue",
        "issuer-ref",
        "metric-ref",
        quarters,
        mode,
        _CUT,
        SecPitPolicy(
            "sec-financial-normalized-v1",
            "adapter-v1",
            "normalization-v1",
            _CONFIG,
            "quality-v1",
            _CUT,
        ),
    )


def _qualities(versions):
    return tuple(
        SecQualityRecord(
            v.normalized_version_id, "quality-v1", SecQualityStatus.PASS, v.recorded_at
        )
        for v in versions
    )


def test_four_quarter_ttm_is_exact_stable_and_retains_lineage(tmp_path: Path) -> None:
    versions = _versions(
        tmp_path, (Decimal("1.10"), Decimal("2.20"), Decimal("3.30"), Decimal("4.40"))
    )
    config = _config(versions)
    with localcontext() as context:
        context.prec = 2
        first = compute_sec_four_quarter_ttm(
            config=config, versions=versions, quality_records=_qualities(versions)
        )
    with localcontext() as context:
        context.prec = 50
        second = compute_sec_four_quarter_ttm(
            config=config, versions=versions, quality_records=_qualities(versions)
        )
    assert first.value == Decimal("11.00")
    assert first.value.as_tuple().exponent == -2
    assert first.result_identity == second.result_identity
    assert first.input_availability_bound == max(v.source_available_at for v in versions)
    assert [item.version.normalized_version_id for item in first.inputs] == [
        v.normalized_version_id for v in versions
    ]


@pytest.mark.parametrize("field", ["period", "concept", "scope"])
def test_declarations_are_structurally_enforced(tmp_path: Path, field: str) -> None:
    versions = _versions(tmp_path)
    declarations = list(_config(versions).quarters)
    original = declarations[1]
    if field == "period":
        declarations[1] = SecQuarterDeclaration(
            original.normalized_version_id,
            2023,
            2,
            True,
            date(2023, 4, 2),
            original.period_end,
            original.native_concept,
            original.accounting_scope,
            original.attribution_scope,
            original.comparability_revision_cohort,
            original.declaration_reference,
        )
    elif field == "concept":
        declarations[1] = SecQuarterDeclaration(
            original.normalized_version_id,
            2023,
            2,
            True,
            original.period_start,
            original.period_end,
            "us-gaap:Other",
            original.accounting_scope,
            original.attribution_scope,
            original.comparability_revision_cohort,
            original.declaration_reference,
        )
    else:
        declarations[1] = SecQuarterDeclaration(
            original.normalized_version_id,
            2023,
            2,
            True,
            original.period_start,
            original.period_end,
            original.native_concept,
            "different",
            original.attribution_scope,
            original.comparability_revision_cohort,
            original.declaration_reference,
        )
    with pytest.raises(ValueError, match="(gap|match|scope)"):
        compute_sec_four_quarter_ttm(
            config=_config(versions, declarations=tuple(declarations)),
            versions=versions,
            quality_records=_qualities(versions),
        )


def test_unavailable_or_excess_inputs_fail_closed(tmp_path: Path) -> None:
    versions = _versions(tmp_path)
    with pytest.raises(ValueError, match="unavailable"):
        compute_sec_four_quarter_ttm(
            config=_config(versions), versions=versions, quality_records=_qualities(versions[:-1])
        )
    consumed = 0

    def endless():
        nonlocal consumed
        while True:
            consumed += 1
            yield versions[0]

    with pytest.raises(ValueError, match="limit"):
        compute_sec_four_quarter_ttm(
            config=_config(versions), versions=endless(), quality_records=(), max_input_records=3
        )
    assert consumed == 4


def test_system_replay_requires_and_retains_commits(tmp_path: Path) -> None:
    versions = _versions(tmp_path)
    qualities = _qualities(versions)
    commits = tuple(
        SecConsumerCommit(v.normalized_version_id, q.quality_record_id, "d" * 64, _CUT)
        for v, q in zip(versions, qualities, strict=True)
    )
    result = compute_sec_four_quarter_ttm(
        config=_config(versions, mode=SecPitMode.SYSTEM_REPLAY),
        versions=versions,
        quality_records=qualities,
        consumer_commits=commits,
    )
    assert all(item.consumer_commit is not None for item in result.inputs)
    assert result.input_availability_bound == _CUT


def test_explicit_independent_declaration_and_duplicate_or_missing_ids_fail(tmp_path: Path) -> None:
    versions = _versions(tmp_path)
    first = _config(versions).quarters[0]
    with pytest.raises(ValueError, match="independent"):
        SecQuarterDeclaration(
            first.normalized_version_id,
            first.fiscal_year,
            first.fiscal_quarter,
            False,
            first.period_start,
            first.period_end,
            first.native_concept,
            first.accounting_scope,
            first.attribution_scope,
            first.comparability_revision_cohort,
            first.declaration_reference,
        )
    declarations = list(_config(versions).quarters)
    declarations[-1] = first
    with pytest.raises(ValueError, match="distinct"):
        _config(versions, declarations=tuple(declarations))
    missing = "b" * 64
    declarations = list(_config(versions).quarters)
    original = declarations[-1]
    declarations[-1] = SecQuarterDeclaration(
        missing,
        original.fiscal_year,
        original.fiscal_quarter,
        True,
        original.period_start,
        original.period_end,
        original.native_concept,
        original.accounting_scope,
        original.attribution_scope,
        original.comparability_revision_cohort,
        original.declaration_reference,
    )
    with pytest.raises(ValueError, match="unavailable"):
        compute_sec_four_quarter_ttm(
            config=_config(versions, declarations=tuple(declarations)),
            versions=versions,
            quality_records=_qualities(versions),
        )


@pytest.mark.parametrize(
    ("values", "units", "message"),
    [
        ((Decimal(1),) * 4, ("USD", "EUR", "USD", "USD"), "unit"),
        ((Decimal(1), None, Decimal(1), Decimal(1)), ("USD",) * 4, "match"),
    ],
)
def test_null_values_and_incompatible_units_fail(
    tmp_path: Path, values, units, message: str
) -> None:
    versions = _versions(tmp_path, values, units)
    with pytest.raises(ValueError, match=message):
        compute_sec_four_quarter_ttm(
            config=_config(versions), versions=versions, quality_records=_qualities(versions)
        )


def test_future_or_revoked_quality_cannot_supply_a_quarter(tmp_path: Path) -> None:
    versions = _versions(tmp_path)
    qualities = list(_qualities(versions))
    qualities[-1] = SecQualityRecord(
        versions[-1].normalized_version_id,
        "quality-v1",
        SecQualityStatus.PASS,
        _CUT + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="unavailable"):
        compute_sec_four_quarter_ttm(
            config=_config(versions), versions=versions, quality_records=qualities
        )


def test_fiscal_year_rollover_and_53_week_declarations_are_not_rederived(tmp_path: Path) -> None:
    periods = (
        (date(2023, 1, 1), date(2023, 4, 7)),
        (date(2023, 4, 8), date(2023, 7, 14)),
        (date(2023, 7, 15), date(2023, 10, 21)),
        (date(2023, 10, 22), date(2024, 1, 6)),
    )
    versions = _versions(tmp_path, periods=periods)
    quarters = tuple(
        SecQuarterDeclaration(
            v.normalized_version_id,
            2023 if index < 3 else 2024,
            index + 2 if index < 3 else 1,
            True,
            start,
            end,
            "us-gaap:Revenue",
            "consolidated",
            "all-attributable",
            "cohort-v1",
            f"week-{index}",
        )
        for index, (v, (start, end)) in enumerate(zip(versions, periods, strict=True))
    )
    result = compute_sec_four_quarter_ttm(
        config=_config(versions, declarations=quarters),
        versions=versions,
        quality_records=_qualities(versions),
    )
    assert result.aggregate_period_start == periods[0][0]
    assert result.aggregate_period_end == periods[-1][1]


def test_revoked_quality_and_future_system_commit_are_unavailable(tmp_path: Path) -> None:
    versions = _versions(tmp_path)
    qualities = list(_qualities(versions))
    prior = qualities[-1]
    qualities.append(
        SecQualityRecord(
            versions[-1].normalized_version_id,
            "quality-v1",
            SecQualityStatus.REVOKED,
            _CUT,
            prior.quality_record_id,
        )
    )
    with pytest.raises(ValueError, match="unavailable"):
        compute_sec_four_quarter_ttm(
            config=_config(versions), versions=versions, quality_records=qualities
        )
    qualities = _qualities(versions)
    commits = tuple(
        SecConsumerCommit(
            v.normalized_version_id, q.quality_record_id, "d" * 64, _CUT + timedelta(days=1)
        )
        for v, q in zip(versions, qualities, strict=True)
    )
    with pytest.raises(ValueError, match="unavailable"):
        compute_sec_four_quarter_ttm(
            config=_config(versions, mode=SecPitMode.SYSTEM_REPLAY),
            versions=versions,
            quality_records=qualities,
            consumer_commits=commits,
        )
