from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ohmydata.providers.sec import SecPitMode
from ohmydata.providers.sec.metric_graph import (
    SecMetricCapexSign,
    SecMetricRounding,
    compute_sec_metric_graph,
)
from ohmydata.providers.sec.metric_graph import (
    SecMetricDomainPolicy as Domain,
)
from ohmydata.providers.sec.metric_graph import (
    SecMetricRecipe as Recipe,
)
from tests.providers.sec.test_metric_graph import (
    bridge,
    compute,
    config,
    declaration,
    external,
    external_declaration,
    step,
)
from tests.providers.sec.test_quarter_ttm import _CUT, _qualities, _versions


def test_full_text_limit_and_53_week_four_quarter_graph(tmp_path):
    periods = (
        (date(2023, 1, 1), date(2023, 4, 7)),
        (date(2023, 4, 8), date(2023, 7, 14)),
        (date(2023, 7, 15), date(2023, 10, 21)),
        (date(2023, 10, 22), date(2024, 1, 6)),
    )
    versions = _versions(tmp_path, periods=periods)
    declarations = tuple(
        declaration(version, f"q{i}", quarter=i + 1) for i, version in enumerate(versions)
    )
    item = step(
        "é" * 512, Recipe.FOUR_QUARTER_TTM_V1, tuple(value.local_id for value in declarations)
    )
    cfg = config(versions, declarations, (item,))
    result = compute(cfg, versions)
    assert result.final.value == Decimal("4.40")
    assert result.final.metadata.period_end == date(2024, 1, 6)
    with pytest.raises(ValueError, match="1024"):
        replace(item, step_id="é" * 513)


@pytest.mark.parametrize("problem", ["source", "quality", "commit", "missing_commit"])
def test_external_temporal_gates(tmp_path, problem):
    price = external("SECURITY_PRICE", "120", unit="USD/security")
    eps = external("FORECAST_EPS", "10", unit="USD/security", horizon="NTM")
    later = _CUT + timedelta(seconds=1)
    if problem == "source":
        eps = replace(
            eps,
            source_available_at=later,
            recorded_at=later,
            quality_recorded_at=later,
            committed_at=later,
        )
    elif problem == "quality":
        eps = replace(eps, quality_recorded_at=later, committed_at=later)
    elif problem == "commit":
        eps = replace(eps, committed_at=later)
    else:
        eps = replace(eps, committed_at=None, commit_identity=None)
    cfg = config(
        (),
        (external_declaration(price, "price"), external_declaration(eps, "eps")),
        (step("fpe", Recipe.FPE_V1, ("price", "eps")),),
        mode=SecPitMode.SYSTEM_REPLAY,
        valuation=_CUT,
    )
    with pytest.raises(ValueError, match="(unavailable|commit)"):
        compute(cfg, (), externals=(price, eps))


@pytest.mark.parametrize("denominator", ["0", "-1"])
def test_nonpositive_forecast_never_becomes_valid_fpe(denominator):
    price = external("SECURITY_PRICE", "120", unit="USD/security")
    eps = external("FORECAST_EPS", denominator, unit="USD/security", horizon="FY1")
    cfg = config(
        (),
        (external_declaration(price, "price"), external_declaration(eps, "eps")),
        (step("fpe", Recipe.FPE_V1, ("price", "eps"), domain=Domain.MISSING_NONPOSITIVE),),
        valuation=_CUT,
    )
    assert (
        compute(cfg, (), externals=(price, eps)).final.missing_reason == "NONPOSITIVE_DENOMINATOR"
    )
    with pytest.raises(ValueError, match="NONPOSITIVE"):
        compute(
            replace(cfg, steps=(replace(cfg.steps[0], domain_policy=Domain.RAISE_NONPOSITIVE),)),
            (),
            externals=(price, eps),
        )


def test_external_attestation_and_policy_changes_reidentify_result():
    price = external("SECURITY_PRICE", "1", unit="USD/security")
    eps = external("FORECAST_EPS", "3", unit="USD/security", horizon="FY1")
    cfg = config(
        (),
        (external_declaration(price, "price"), external_declaration(eps, "eps")),
        (step("fpe", Recipe.FPE_V1, ("price", "eps")),),
        valuation=_CUT,
    )
    first = compute(cfg, (), externals=(price, eps))
    changed = replace(eps, source_reference="different-synthetic-evidence")
    changed_cfg = replace(
        cfg, declarations=(cfg.declarations[0], external_declaration(changed, "eps"))
    )
    second = compute(changed_cfg, (), externals=(price, changed))
    assert (
        first.final.value == second.final.value and first.result_identity != second.result_identity
    )
    up = replace(cfg, steps=(replace(cfg.steps[0], rounding=SecMetricRounding.ROUND_UP),))
    rounded = compute(up, (), externals=(price, eps))
    assert (
        rounded.final.value > first.final.value and rounded.result_identity != first.result_identity
    )


def test_equity_attestation_forecast_and_cutoff_guards():
    cap = external("COMPANY_MARKET_CAP", "1000")
    with pytest.raises(ValueError, match="total-equity"):
        replace(cap, company_total_equity=False)
    eps = external("FORECAST_EPS", "1", unit="USD/security", horizon="FY1")
    with pytest.raises(ValueError, match="nonempty"):
        replace(eps, forecast_horizon="")
    price = external("SECURITY_PRICE", "1", unit="USD/security")
    declarations = (external_declaration(price, "price"), external_declaration(eps, "eps"))
    with pytest.raises(ValueError, match="exceeds valuation"):
        config(
            (),
            declarations,
            (step("fpe", Recipe.FPE_V1, ("price", "eps")),),
            valuation=_CUT - timedelta(seconds=1),
        )


def test_aggregate_bound_counts_quality_and_external_records(tmp_path):
    versions, declarations = bridge(tmp_path)
    cfg = config(
        versions, declarations, (step("ttm", Recipe.FY_YTD_TTM_V1, ("fy", "current", "prior")),)
    )
    qualities = _qualities(versions)
    assert compute_sec_metric_graph(
        config=cfg, versions=versions, quality_records=qualities, max_input_records=6
    ).final.value == Decimal(140)
    with pytest.raises(ValueError, match="budget"):
        compute_sec_metric_graph(
            config=cfg, versions=versions, quality_records=qualities, max_input_records=5
        )
    with pytest.raises(ValueError, match="integer"):
        compute_sec_metric_graph(
            config=cfg, versions=versions, quality_records=qualities, max_input_records=True
        )


def test_row_matching_overlap_reaches_bridge_guard(tmp_path):
    periods = (
        (date(2022, 1, 1), date(2022, 12, 31)),
        (date(2022, 12, 31), date(2023, 6, 30)),
        (date(2022, 1, 1), date(2022, 6, 30)),
    )
    versions = _versions(tmp_path, (Decimal(100), Decimal(60), Decimal(20)), ("USD",) * 3, periods)
    declarations = (
        declaration(versions[0], "fy", kind="FY", year=2022),
        declaration(versions[1], "current", kind="YTD", year=2023, quarter=2),
        declaration(versions[2], "prior", kind="YTD", year=2022, quarter=2),
    )
    cfg = config(
        versions, declarations, (step("ttm", Recipe.FY_YTD_TTM_V1, ("fy", "current", "prior")),)
    )
    with pytest.raises(ValueError, match="FY plus YTD bridge"):
        compute(cfg, versions)


def test_structurally_valid_graph_rejects_oversize_canonical_config(tmp_path):
    versions, originals = bridge(tmp_path)
    declarations = tuple(
        replace(
            originals[0],
            local_id=f"input{i}",
            source_identity=f"{i:064x}",
            declaration_reference="x" * 1024,
            accounting_scope="y" * 1024,
        )
        for i in range(33)
    )
    steps = [
        step(
            "result0",
            Recipe.CFO_CAPEX_V1,
            ("input0", "input1"),
            sign=SecMetricCapexSign.POSITIVE_OUTFLOW,
        )
    ]
    for i in range(1, 32):
        steps.append(
            step(
                f"result{i}",
                Recipe.CFO_CAPEX_V1,
                (f"result{i - 1}", f"input{i + 1}"),
                sign=SecMetricCapexSign.POSITIVE_OUTFLOW,
            )
        )
    with pytest.raises(ValueError, match="configuration exceeds65536"):
        config(versions, declarations, steps)
