import socket
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from ohmydata.core.errors import ResourceLimitError
from ohmydata.providers.sec.financials import SecStatementRow
from ohmydata.providers.sec.known_by_ttm import (
    SecKnownByTtmInput,
    SecKnownByTtmResult,
    diagnose_sec_fy_ytd_ttm,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_document_financials import build
from test_observed_accounting import make


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def row(
    concept="us-gaap:Revenues",
    value="1000",
    *,
    context="c1",
    start=date(2023, 1, 1),
    end=date(2023, 12, 31),
    kind="duration",
    currency="USD",
    unit="iso4217:USD",
    dimension=None,
):
    return SecStatementRow(
        "IncomeStatement",
        concept,
        concept,
        concept,
        None if value is None else Decimal(value),
        value,
        unit,
        None,
        start,
        end,
        period_type=kind,
        context_ref=context,
        unit_ref="usd",
        decimals_native="0",
        dimension=dimension,
    )


def make_doc(tmp_path, monkeypatch, rows, *, change=None):
    import ohmydata.providers.sec.document_financials as doc_producer

    monkeypatch.setattr(doc_producer, "_rows_from_documents", lambda *args: tuple(rows))
    result, *_ = build(tmp_path, change=change)
    return result


def sample_inputs(fy_id, ytd_id, *, prior_ytd_id=None, concept="us-gaap:Revenues"):
    py_id = prior_ytd_id or ytd_id
    return (
        SecKnownByTtmInput(
            fy_id,
            "IncomeStatement",
            concept,
            "c_fy",
            2023,
            4,
            date(2023, 1, 1),
            date(2023, 12, 31),
            "ref_fy",
        ),
        SecKnownByTtmInput(
            ytd_id,
            "IncomeStatement",
            concept,
            "c_cur",
            2024,
            2,
            date(2024, 1, 1),
            date(2024, 6, 30),
            "ref_cur",
        ),
        SecKnownByTtmInput(
            py_id,
            "IncomeStatement",
            concept,
            "c_pri",
            2023,
            2,
            date(2023, 1, 1),
            date(2023, 6, 30),
            "ref_pri",
        ),
    )


def standard_eval(productions, declarations, **kwargs):
    cutoff = datetime(2024, 12, 31, 0, 0, tzinfo=UTC)
    params = {
        "productions": productions,
        "declarations": declarations,
        "canonical_cik": "1",
        "metric": "REVENUE",
        "accounting_scope": "consolidated",
        "attribution_scope": "operating",
        "comparability_cohort": "standard-cohort",
        "security_basis": "common-share",
        "scope_reference": "ref-scope",
        "knowledge_cutoff": cutoff,
        "diagnosed_at": cutoff,
    }
    params.update(kwargs)
    return diagnose_sec_fy_ytd_ttm(**params)


def test_positive_observed_productions(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(value="1000", context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(value="600", context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(value="500", context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    res = standard_eval((fy_prod, ytd_prod), decs)
    assert isinstance(res, SecKnownByTtmResult)
    assert res.value == Decimal(1100)
    assert res.period_start == date(2023, 7, 1)
    assert res.period_end == date(2024, 6, 30)
    assert len(res.evidences) == 3
    assert res.unit == "iso4217:USD"
    assert res.currency == "USD"


def test_positive_document_productions(tmp_path, monkeypatch):
    fy_prod = make_doc(tmp_path / "fy", monkeypatch, [row(value="2000", context="c_fy")])
    ytd_prod = make_doc(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(value="1200", context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(value="1000", context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    res = standard_eval((fy_prod, ytd_prod), decs)
    assert res.value == Decimal(2200)


def test_positive_mixed_productions(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(value="1500", context="c_fy")])
    ytd_prod = make_doc(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(value="800", context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(value="700", context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    res = standard_eval((fy_prod, ytd_prod), decs)
    assert res.value == Decimal(1600)


def test_positive_three_distinct_productions(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(value="1000", context="c_fy")])
    cur_prod = make(
        tmp_path / "cur",
        monkeypatch,
        [row(value="600", context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30))],
    )
    pri_prod = make_doc(
        tmp_path / "pri",
        monkeypatch,
        [row(value="500", context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30))],
    )
    decs = sample_inputs(
        fy_prod.production_identity,
        cur_prod.production_identity,
        prior_ytd_id=pri_prod.production_identity,
    )
    res = standard_eval((fy_prod, cur_prod, pri_prod), decs)
    assert res.value == Decimal(1100)


def test_hostile_decimal_context_isolation(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(value="1000.123456789", context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(
                value="600.000000001",
                context="c_cur",
                start=date(2024, 1, 1),
                end=date(2024, 6, 30),
            ),
            row(
                value="500.000000001",
                context="c_pri",
                start=date(2023, 1, 1),
                end=date(2023, 6, 30),
            ),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    with localcontext() as ctx:
        ctx.prec = 2
        res = standard_eval((fy_prod, ytd_prod), decs)
    assert res.value == Decimal("1100.123456789")


def test_future_cutoff_rejected(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    past_cutoff = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="availability bound exceeds knowledge_cutoff"):
        diagnose_sec_fy_ytd_ttm(
            productions=(fy_prod, ytd_prod),
            declarations=decs,
            canonical_cik="1",
            metric="REVENUE",
            accounting_scope="consolidated",
            attribution_scope="operating",
            comparability_cohort="standard-cohort",
            security_basis="common-share",
            scope_reference="ref-scope",
            knowledge_cutoff=past_cutoff,
            diagnosed_at=past_cutoff,
        )


def test_diagnosed_at_precedes_cutoff(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    cutoff = datetime(2024, 12, 31, tzinfo=UTC)
    earlier = datetime(2024, 6, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="diagnosed_at cannot precede knowledge_cutoff"):
        standard_eval((fy_prod, ytd_prod), decs, knowledge_cutoff=cutoff, diagnosed_at=earlier)


def test_tampered_declaration_revalidation(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = list(sample_inputs(fy_prod.production_identity, ytd_prod.production_identity))
    object.__setattr__(decs[0], "fiscal_year", 0)  # Invalid year
    with pytest.raises(ValueError, match="fiscal_year must be integer 1..9999"):
        standard_eval((fy_prod, ytd_prod), tuple(decs))


def test_identity_changes_on_cohort_scope(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    res1 = standard_eval((fy_prod, ytd_prod), decs, comparability_cohort="cohort-A")
    res2 = standard_eval((fy_prod, ytd_prod), decs, comparability_cohort="cohort-B")
    assert res1.result_identity != res2.result_identity


def test_exact_row_missing(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="wrong_ctx")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    with pytest.raises(ValueError, match="matched 0 rows, expected exactly 1"):
        standard_eval((fy_prod, ytd_prod), decs)


def test_exact_row_duplicate_match(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy"), row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    with pytest.raises(ValueError, match="matched 2 rows, expected exactly 1"):
        standard_eval((fy_prod, ytd_prod), decs)


def test_dimension_rejected(tmp_path, monkeypatch):
    fy_prod = make(
        tmp_path / "fy",
        monkeypatch,
        [row(context="c_fy", dimension='{"dim":"val"}')],
    )
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    with pytest.raises(ValueError, match="dimensions are rejected"):
        standard_eval((fy_prod, ytd_prod), decs)


def test_row_null_value(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(value=None, context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    with pytest.raises(ValueError, match="matched row value must not be None"):
        standard_eval((fy_prod, ytd_prod), decs)


def test_row_unit_mismatch(tmp_path, monkeypatch):
    def change_eur(payloads):
        payloads["instance"] = payloads["instance"].replace(b"iso4217:USD", b"iso4217:EUR")

    fy_prod = make_doc(
        tmp_path / "fy",
        monkeypatch,
        [row(context="c_fy", unit="iso4217:EUR", currency="EUR")],
        change=change_eur,
    )
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    with pytest.raises(ValueError, match="identical unit and currency"):
        standard_eval((fy_prod, ytd_prod), decs)


def test_cik_mismatch(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    with pytest.raises(ValueError, match="production CIK does not match canonical_cik"):
        standard_eval((fy_prod, ytd_prod), decs, canonical_cik="99999")


def test_fiscal_bridge_mismatches(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    # Wrong quarter for prior FY (3 instead of 4)
    decs_bad_q = (
        replace(
            sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)[0],
            fiscal_end_quarter=3,
        ),
        sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)[1],
        sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)[2],
    )
    with pytest.raises(ValueError, match="invalid FY plus YTD bridge declarations"):
        standard_eval((fy_prod, ytd_prod), decs_bad_q)


def test_production_count_sentinel_generator(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)

    def gen():
        yield fy_prod
        yield ytd_prod
        yield fy_prod
        yield ytd_prod
        yield ytd_prod  # 5th item should not be needed

    with pytest.raises(ResourceLimitError, match="production count budget exceeded"):
        standard_eval(gen(), decs)


def test_duplicate_or_unused_productions(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    unused_prod = make_doc(tmp_path / "unused", monkeypatch, [row(context="c_un")])
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)

    # Unused production passed
    with pytest.raises(ValueError, match="productions do not exactly match declared"):
        standard_eval((fy_prod, ytd_prod, unused_prod), decs)

    # Duplicate production object passed
    with pytest.raises(ValueError, match="duplicate production object"):
        standard_eval((fy_prod, ytd_prod, fy_prod), decs)


def test_aggregate_row_cap(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 1, 1), end=date(2024, 6, 30)),
            row(context="c_pri", start=date(2023, 1, 1), end=date(2023, 6, 30)),
        ],
    )
    decs = sample_inputs(fy_prod.production_identity, ytd_prod.production_identity)
    with pytest.raises(ResourceLimitError, match="aggregate row budget exceeded"):
        standard_eval((fy_prod, ytd_prod), decs, max_rows=2)
