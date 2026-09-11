from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest

from ohmydata.providers.sec import (
    SecConsumerCommit,
    SecPitMode,
    SecPitPolicy,
    SecQualityRecord,
    SecQualityStatus,
)
from ohmydata.providers.sec.metric_graph import (
    SecMetricCapexSign as Sign,
)
from ohmydata.providers.sec.metric_graph import (
    SecMetricDomainPolicy as Domain,
)
from ohmydata.providers.sec.metric_graph import (
    SecMetricExternalInput,
    SecMetricGraphConfig,
    SecMetricStep,
    SecMetricTerminalDeclaration,
    compute_sec_metric_graph,
)
from ohmydata.providers.sec.metric_graph import (
    SecMetricRecipe as Recipe,
)
from ohmydata.providers.sec.metric_graph import (
    SecMetricRounding as Rounding,
)
from tests.providers.sec.test_quarter_ttm import _CUT, _qualities, _versions


def declaration(
    version, name, *, metric="REVENUE", kind="INDEPENDENT_QUARTER", year=2023, quarter=4
):
    return SecMetricTerminalDeclaration(
        name,
        "NORMALIZED_FACT",
        version.normalized_version_id,
        metric,
        kind,
        year,
        quarter,
        version.row.period_start,
        version.row.period_end,
        version.row.concept,
        "USD",
        "synthetic-GAAP",
        "company-common-equity",
        None,
        "synthetic-cohort",
        "synthetic-security",
        "synthetic-declaration",
    )


def step(name, recipe, refs, *, domain=None, sign=None, precision=12):
    division = recipe in {
        Recipe.YOY_V1,
        Recipe.MARGIN_V1,
        Recipe.PE_V1,
        Recipe.PS_V1,
        Recipe.FPE_V1,
    }
    return SecMetricStep(
        name,
        recipe,
        refs,
        (domain or Domain.RAISE_NONPOSITIVE) if division else None,
        sign,
        precision if division else None,
        Rounding.ROUND_HALF_EVEN if division else None,
    )


def config(versions, declarations, steps, *, mode=SecPitMode.MARKET_KNOWN, valuation=None):
    return SecMetricGraphConfig(
        "1",
        tuple(declarations),
        tuple(steps),
        mode,
        _CUT,
        SecPitPolicy(
            "sec-financial-normalized-v1",
            "adapter-v1",
            "normalization-v1",
            "c" * 64,
            "quality-v1",
            _CUT,
        ),
        valuation,
        "synthetic-scope",
    )


def external(metric, value, *, unit="USD", horizon=None):
    forecast = metric == "FORECAST_EPS"
    return SecMetricExternalInput(
        metric,
        Decimal(value),
        unit,
        "USD",
        _CUT,
        date(2025, 4, 1) if forecast else _CUT.date(),
        date(2026, 3, 31) if forecast else _CUT.date(),
        horizon,
        "synthetic-GAAP",
        "company-common-equity",
        "synthetic-security",
        metric == "COMPANY_MARKET_CAP",
        "synthetic-source-attestation",
        _CUT,
        "a" * 64,
        _CUT,
        _CUT,
        "synthetic-quality-attestation",
        _CUT,
        "b" * 64,
        _CUT,
    )


def external_declaration(item, name):
    return SecMetricTerminalDeclaration(
        name,
        "EXTERNAL_ATTESTATION",
        item.external_id,
        item.metric,
        "FORECAST" if item.metric == "FORECAST_EPS" else "INSTANT",
        None,
        None,
        item.period_start,
        item.period_end,
        None,
        item.currency,
        item.accounting_scope,
        item.attribution_scope,
        None,
        "synthetic-cohort",
        item.security_basis,
        "synthetic-external-declaration",
    )


def compute(cfg, versions, *, externals=(), qualities=None, commits=()):
    return compute_sec_metric_graph(
        config=cfg,
        versions=versions,
        quality_records=_qualities(versions) if qualities is None else qualities,
        consumer_commits=commits,
        external_inputs=externals,
    )


def bridge(tmp_path, *, metric="REVENUE"):
    periods = (
        (date(2022, 1, 1), date(2022, 12, 31)),
        (date(2023, 1, 1), date(2023, 6, 30)),
        (date(2022, 1, 1), date(2022, 6, 30)),
    )
    versions = _versions(tmp_path, (Decimal(100), Decimal(60), Decimal(20)), ("USD",) * 3, periods)
    declarations = (
        declaration(versions[0], "fy", metric=metric, kind="FY", year=2022),
        declaration(versions[1], "current", metric=metric, kind="YTD", year=2023, quarter=2),
        declaration(versions[2], "prior", metric=metric, kind="YTD", year=2022, quarter=2),
    )
    return versions, declarations


def test_four_quarter_ttm_reuses_exact_periods_and_lineage(tmp_path):
    versions = _versions(tmp_path, (Decimal("1.1"), Decimal("2.2"), Decimal("3.3"), Decimal("4.4")))
    declarations = tuple(
        declaration(version, f"q{i}", quarter=i + 1) for i, version in enumerate(versions)
    )
    cfg = config(
        versions,
        declarations,
        (step("ttm", Recipe.FOUR_QUARTER_TTM_V1, tuple(item.local_id for item in declarations)),),
    )
    with localcontext() as context:
        context.prec = 2
        result = compute(cfg, versions)
    assert result.final.value == Decimal("11.0") and result.final.metadata.period_kind == "TTM"
    assert all(
        item.sec_evidence is not None and item.external_attestation is None
        for item in result.terminals
    )
    assert result.final.input_availability_bound == max(
        version.source_available_at for version in versions
    )
    assert (
        result.result_identity
        == compute(
            cfg, reversed(versions), qualities=reversed(_qualities(versions))
        ).result_identity
    )


@pytest.mark.parametrize("recipe,metric", [(Recipe.PE_V1, "NET_INCOME"), (Recipe.PS_V1, "REVENUE")])
def test_fy_ytd_bridge_feeds_company_valuation(tmp_path, recipe, metric):
    versions, declarations = bridge(tmp_path, metric=metric)
    cap = external("COMPANY_MARKET_CAP", "1400")
    cfg = config(
        versions,
        declarations + (external_declaration(cap, "cap"),),
        (
            step("ttm", Recipe.FY_YTD_TTM_V1, ("fy", "current", "prior")),
            step("valuation", recipe, ("cap", "ttm")),
        ),
        valuation=_CUT,
    )
    result = compute(cfg, versions, externals=(cap,))
    assert result.steps[0].value == Decimal(140)
    assert result.steps[0].metadata.period_start == date(2022, 7, 1)
    assert result.steps[0].metadata.period_end == date(2023, 6, 30)
    assert result.final.value == Decimal(10) and result.final.unit == "pure"
    assert result.final.input_availability_bound == _CUT
    assert (
        result.terminals[-1].external_attestation == cap
        and result.terminals[-1].sec_evidence is None
    )


@pytest.mark.parametrize(
    "sign,capex", [(Sign.POSITIVE_OUTFLOW, "25"), (Sign.NEGATIVE_OUTFLOW, "-25")]
)
def test_cfo_capex_explicit_sign(tmp_path, sign, capex):
    periods = ((date(2023, 1, 1), date(2023, 12, 31)),) * 2
    versions = _versions(tmp_path, (Decimal(100), Decimal(capex)), ("USD",) * 2, periods)
    declarations = (
        declaration(versions[0], "cfo", metric="CFO", kind="FY"),
        declaration(versions[1], "capex", metric="CAPEX", kind="FY"),
    )
    cfg = config(
        versions, declarations, (step("fcf", Recipe.CFO_CAPEX_V1, ("cfo", "capex"), sign=sign),)
    )
    assert compute(cfg, versions).final.value == Decimal(75)
    wrong = Sign.NEGATIVE_OUTFLOW if sign is Sign.POSITIVE_OUTFLOW else Sign.POSITIVE_OUTFLOW
    with pytest.raises(ValueError, match="sign"):
        compute(replace(cfg, steps=(replace(cfg.steps[0], capex_sign=wrong),)), versions)


@pytest.mark.parametrize(
    "prior,domain,expected,reason",
    [
        ("50", Domain.RAISE_NONPOSITIVE, "1", None),
        ("-50", Domain.RAISE_ZERO_ABS, "3", None),
        ("0", Domain.MISSING_ZERO_ABS, None, "ZERO_DENOMINATOR"),
        ("-50", Domain.MISSING_NONPOSITIVE, None, "NONPOSITIVE_DENOMINATOR"),
    ],
)
def test_yoy_negative_and_zero_policy(tmp_path, prior, domain, expected, reason):
    periods = ((date(2023, 1, 1), date(2023, 12, 31)), (date(2022, 1, 1), date(2022, 12, 31)))
    versions = _versions(tmp_path, (Decimal(100), Decimal(prior)), ("USD",) * 2, periods)
    declarations = (
        declaration(versions[0], "current", kind="FY"),
        declaration(versions[1], "prior", kind="FY", year=2022),
    )
    cfg = config(
        versions, declarations, (step("yoy", Recipe.YOY_V1, ("current", "prior"), domain=domain),)
    )
    final = compute(cfg, versions).final
    assert final.value == (None if expected is None else Decimal(expected))
    assert final.missing_reason == reason and final.missing_origins == (("yoy",) if reason else ())


def test_margin_negative_profit_and_fpe_forecast_basis(tmp_path):
    periods = ((date(2023, 1, 1), date(2023, 12, 31)),) * 2
    versions = _versions(tmp_path, (Decimal(-25), Decimal(100)), ("USD",) * 2, periods)
    declarations = (
        declaration(versions[0], "profit", metric="NET_INCOME", kind="FY"),
        declaration(versions[1], "revenue", kind="FY"),
    )
    cfg = config(versions, declarations, (step("margin", Recipe.MARGIN_V1, ("profit", "revenue")),))
    assert compute(cfg, versions).final.value == Decimal("-0.25")
    price, eps = (
        external("SECURITY_PRICE", "120", unit="USD/security"),
        external("FORECAST_EPS", "10", unit="USD/security", horizon="FY1"),
    )
    cfg = config(
        (),
        (external_declaration(price, "price"), external_declaration(eps, "eps")),
        (step("fpe", Recipe.FPE_V1, ("price", "eps")),),
        valuation=_CUT,
    )
    result = compute(cfg, (), externals=(price, eps))
    assert result.final.value == Decimal(12) and result.final.metadata.forecast_horizon == "FY1"


@pytest.mark.parametrize(
    "field", ["security_basis", "currency", "accounting_scope", "unit", "valuation_at"]
)
def test_fpe_rejects_mismatched_basis(tmp_path, field):
    price = external("SECURITY_PRICE", "120", unit="USD/security")
    eps = external("FORECAST_EPS", "10", unit="USD/security", horizon="FY1")
    value = {
        "security_basis": "different-ADR",
        "currency": "EUR",
        "accounting_scope": "adjusted",
        "unit": "USD/shares",
        "valuation_at": _CUT + timedelta(days=1),
    }[field]
    eps = replace(eps, **{field: value})
    cfg = config(
        (),
        (external_declaration(price, "price"), external_declaration(eps, "eps")),
        (step("fpe", Recipe.FPE_V1, ("price", "eps")),),
        valuation=_CUT,
    )
    with pytest.raises(ValueError, match="(basis|currency|valuation|units)"):
        compute(cfg, (), externals=(price, eps))


def test_system_replay_future_quality_and_commit_are_rejected(tmp_path):
    versions, declarations = bridge(tmp_path)
    cfg = config(
        versions,
        declarations,
        (step("ttm", Recipe.FY_YTD_TTM_V1, ("fy", "current", "prior")),),
        mode=SecPitMode.SYSTEM_REPLAY,
    )
    qualities = _qualities(versions)
    commits = tuple(
        SecConsumerCommit(version.normalized_version_id, quality.quality_record_id, "d" * 64, _CUT)
        for version, quality in zip(versions, qualities, strict=True)
    )
    assert compute(cfg, versions, commits=commits).final.input_availability_bound == _CUT
    with pytest.raises(ValueError, match="unavailable"):
        compute(
            cfg,
            versions,
            commits=tuple(
                replace(item, committed_at=_CUT + timedelta(seconds=1)) for item in commits
            ),
        )
    future = tuple(
        SecQualityRecord(
            version.normalized_version_id,
            "quality-v1",
            SecQualityStatus.PASS,
            _CUT + timedelta(seconds=1),
        )
        for version in versions
    )
    with pytest.raises(ValueError, match="unavailable"):
        compute(replace(cfg, mode=SecPitMode.MARKET_KNOWN), versions, qualities=future)


def test_lineage_identity_changes_with_selected_quality(tmp_path):
    versions, declarations = bridge(tmp_path)
    cfg = config(
        versions, declarations, (step("ttm", Recipe.FY_YTD_TTM_V1, ("fy", "current", "prior")),)
    )
    first = compute(cfg, versions)
    qualities = tuple(
        SecQualityRecord(
            version.normalized_version_id,
            "quality-v1",
            SecQualityStatus.PASS,
            version.recorded_at + timedelta(seconds=1),
        )
        for version in versions
    )
    second = compute(cfg, versions, qualities=qualities)
    assert first.final.value == second.final.value
    assert (
        first.result_identity != second.result_identity
        and first.final.value_identity != second.final.value_identity
    )


@pytest.mark.parametrize("problem", ["cohort", "overlap", "quarter", "dimension"])
def test_ytd_declaration_failures(tmp_path, problem):
    versions, declarations = bridge(tmp_path)
    declarations = list(declarations)
    if problem == "cohort":
        declarations[1] = replace(declarations[1], comparability_cohort="different")
    elif problem == "overlap":
        declarations[1] = replace(declarations[1], period_start=date(2022, 12, 31))
    elif problem == "quarter":
        declarations[1] = replace(declarations[1], fiscal_end_quarter=3)
    else:
        declarations[1] = replace(declarations[1], dimension="segment")
    cfg = config(
        versions, declarations, (step("ttm", Recipe.FY_YTD_TTM_V1, ("fy", "current", "prior")),)
    )
    with pytest.raises(ValueError):
        compute(cfg, versions)


def test_graph_shape_unused_forward_duplicate_and_ratio_reuse(tmp_path):
    versions, declarations = bridge(tmp_path)
    valid = step("ttm", Recipe.FY_YTD_TTM_V1, ("fy", "current", "prior"))
    for steps in (
        (replace(valid, input_refs=("unknown", "current", "prior")),),
        (valid, step("unused", Recipe.CFO_CAPEX_V1, ("fy", "current"), sign=Sign.POSITIVE_OUTFLOW)),
    ):
        with pytest.raises(ValueError):
            config(versions, declarations, steps)
    with pytest.raises(ValueError, match="duplicate"):
        config(versions, declarations + (declarations[0],), (valid,))
    with pytest.raises(ValueError, match="rounded ratio"):
        config(
            versions,
            declarations,
            (
                step("ratio", Recipe.MARGIN_V1, ("fy", "current")),
                step("again", Recipe.MARGIN_V1, ("ratio", "prior")),
            ),
        )


def test_aggregate_input_limit_consumes_only_one_sentinel(tmp_path):
    versions, declarations = bridge(tmp_path)
    cfg = config(
        versions, declarations, (step("ttm", Recipe.FY_YTD_TTM_V1, ("fy", "current", "prior")),)
    )
    consumed = []

    def endless():
        for i in range(100):
            consumed.append(i)
            yield versions[0]

    with pytest.raises(ValueError, match="budget"):
        compute_sec_metric_graph(
            config=cfg, versions=endless(), quality_records=(), max_input_records=3
        )
    assert consumed == [0, 1, 2, 3]
