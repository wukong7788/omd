import socket
import sys
from datetime import UTC, date, datetime
from decimal import Decimal, Inexact, localcontext
from pathlib import Path

import pytest

from ohmydata.core.errors import ResourceLimitError
from ohmydata.providers.sec.financials import SecStatementRow
from ohmydata.providers.sec.known_by_ttm import (
    SecKnownByTtmInput,
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
    start=date(2023, 9, 24),
    end=date(2024, 9, 28),
    currency="USD",
    unit="iso4217:USD",
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
        period_type="duration",
        context_ref=context,
        unit_ref="usd",
        decimals_native="0",
        dimension=None,
    )


def make_doc(tmp_path, monkeypatch, rows):
    import ohmydata.providers.sec.document_financials as doc_producer

    monkeypatch.setattr(doc_producer, "_rows_from_documents", lambda *args: tuple(rows))
    result, *_ = build(tmp_path)
    return result


def test_valid_53_week_fiscal_dates(tmp_path, monkeypatch):
    # Synthetic 371-day (53-week) fiscal year ending in September.
    # Prior FY 2024: 2023-09-24..2024-09-28 (Q4)
    # Current YTD 2025: 2024-09-29..2025-06-28 (Q3)
    # Prior YTD 2024: 2023-09-24..2024-06-29 (Q3)
    fy_prod = make(
        tmp_path / "fy",
        monkeypatch,
        [row(value="1000", context="c_fy", start=date(2023, 9, 24), end=date(2024, 9, 28))],
    )
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(value="600", context="c_cur", start=date(2024, 9, 29), end=date(2025, 6, 28)),
            row(value="500", context="c_pri", start=date(2023, 9, 24), end=date(2024, 6, 29)),
        ],
    )
    decs = (
        SecKnownByTtmInput(
            fy_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_fy",
            2024,
            4,
            date(2023, 9, 24),
            date(2024, 9, 28),
            "ref_fy",
        ),
        SecKnownByTtmInput(
            ytd_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_cur",
            2025,
            3,
            date(2024, 9, 29),
            date(2025, 6, 28),
            "ref_cur",
        ),
        SecKnownByTtmInput(
            ytd_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_pri",
            2024,
            3,
            date(2023, 9, 24),
            date(2024, 6, 29),
            "ref_pri",
        ),
    )
    cutoff = datetime(2025, 12, 31, tzinfo=UTC)
    res = diagnose_sec_fy_ytd_ttm(
        productions=(fy_prod, ytd_prod),
        declarations=decs,
        canonical_cik="1",
        metric="REVENUE",
        accounting_scope="consolidated",
        attribution_scope="operating",
        comparability_cohort="synthetic-53w",
        security_basis="common-share",
        scope_reference="ref-scope",
        knowledge_cutoff=cutoff,
        diagnosed_at=cutoff,
    )
    assert res.value == Decimal(1100)
    assert res.period_start == date(2024, 6, 30)
    assert res.period_end == date(2025, 6, 28)


def test_api_production_tamper_fails_seal(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 9, 29), end=date(2025, 6, 28)),
            row(context="c_pri", start=date(2023, 9, 24), end=date(2024, 6, 29)),
        ],
    )
    tampered_id = "a" * 64
    object.__setattr__(fy_prod, "production_identity", tampered_id)
    decs = (
        SecKnownByTtmInput(
            tampered_id,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_fy",
            2024,
            4,
            date(2023, 9, 24),
            date(2024, 9, 28),
            "ref_fy",
        ),
        SecKnownByTtmInput(
            ytd_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_cur",
            2025,
            3,
            date(2024, 9, 29),
            date(2025, 6, 28),
            "ref_cur",
        ),
        SecKnownByTtmInput(
            ytd_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_pri",
            2024,
            3,
            date(2023, 9, 24),
            date(2024, 6, 29),
            "ref_pri",
        ),
    )
    cutoff = datetime(2025, 12, 31, tzinfo=UTC)
    with pytest.raises(ValueError):
        diagnose_sec_fy_ytd_ttm(
            productions=(fy_prod, ytd_prod),
            declarations=decs,
            canonical_cik="1",
            metric="REVENUE",
            accounting_scope="consolidated",
            attribution_scope="operating",
            comparability_cohort="cohort",
            security_basis="common-share",
            scope_reference="ref-scope",
            knowledge_cutoff=cutoff,
            diagnosed_at=cutoff,
        )


def test_document_production_future_cutoff(tmp_path, monkeypatch):
    doc_prod = make_doc(tmp_path / "doc", monkeypatch, [row(context="c_fy")])
    past_cutoff = datetime(2020, 1, 1, tzinfo=UTC)
    decs = (
        SecKnownByTtmInput(
            doc_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_fy",
            2024,
            4,
            date(2023, 9, 24),
            date(2024, 9, 28),
            "ref_fy",
        ),
        SecKnownByTtmInput(
            doc_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_cur",
            2025,
            3,
            date(2024, 9, 29),
            date(2025, 6, 28),
            "ref_cur",
        ),
        SecKnownByTtmInput(
            doc_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_pri",
            2024,
            3,
            date(2023, 9, 24),
            date(2024, 6, 29),
            "ref_pri",
        ),
    )
    with pytest.raises(ValueError, match="availability bound exceeds knowledge_cutoff"):
        diagnose_sec_fy_ytd_ttm(
            productions=(doc_prod,),
            declarations=decs,
            canonical_cik="1",
            metric="REVENUE",
            accounting_scope="consolidated",
            attribution_scope="operating",
            comparability_cohort="cohort",
            security_basis="common-share",
            scope_reference="ref-scope",
            knowledge_cutoff=past_cutoff,
            diagnosed_at=past_cutoff,
        )


def test_pre_iteration_validation_does_not_consume_generator():
    def unreached_gen():
        raise AssertionError("generator should not be consumed on invalid arguments")
        yield  # pragma: no cover

    valid_decs = (
        SecKnownByTtmInput(
            "a" * 64, "s", "c", "ctx", 2024, 4, date(2024, 1, 1), date(2024, 12, 31), "ref"
        ),
        SecKnownByTtmInput(
            "a" * 64, "s", "c", "ctx", 2025, 1, date(2025, 1, 1), date(2025, 3, 31), "ref"
        ),
        SecKnownByTtmInput(
            "a" * 64, "s", "c", "ctx", 2024, 1, date(2024, 1, 1), date(2024, 3, 31), "ref"
        ),
    )
    cutoff = datetime(2025, 12, 31, tzinfo=UTC)

    # Invalid metric
    with pytest.raises(ValueError, match="metric must be REVENUE or NET_INCOME"):
        diagnose_sec_fy_ytd_ttm(
            productions=unreached_gen(),
            declarations=valid_decs,
            canonical_cik="1",
            metric="OPERATING_INCOME",
            accounting_scope="scope",
            attribution_scope="scope",
            comparability_cohort="cohort",
            security_basis="sec",
            scope_reference="ref",
            knowledge_cutoff=cutoff,
            diagnosed_at=cutoff,
        )

    # bool max_rows
    with pytest.raises(ValueError, match="max_rows must be integer"):
        diagnose_sec_fy_ytd_ttm(
            productions=unreached_gen(),
            declarations=valid_decs,
            canonical_cik="1",
            metric="REVENUE",
            accounting_scope="scope",
            attribution_scope="scope",
            comparability_cohort="cohort",
            security_basis="sec",
            scope_reference="ref",
            knowledge_cutoff=cutoff,
            diagnosed_at=cutoff,
            max_rows=True,
        )


def test_sentinel_generator_fifth_access_not_performed(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(context="c_cur", start=date(2024, 9, 29), end=date(2025, 6, 28)),
            row(context="c_pri", start=date(2023, 9, 24), end=date(2024, 6, 29)),
        ],
    )
    decs = (
        SecKnownByTtmInput(
            fy_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_fy",
            2024,
            4,
            date(2023, 9, 24),
            date(2024, 9, 28),
            "ref_fy",
        ),
        SecKnownByTtmInput(
            ytd_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_cur",
            2025,
            3,
            date(2024, 9, 29),
            date(2025, 6, 28),
            "ref_cur",
        ),
        SecKnownByTtmInput(
            ytd_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_pri",
            2024,
            3,
            date(2023, 9, 24),
            date(2024, 6, 29),
            "ref_pri",
        ),
    )

    def sentinel_gen():
        yield fy_prod
        yield ytd_prod
        yield fy_prod
        yield ytd_prod  # 4th item (sentinel exceeding 3)
        raise AssertionError("5th item accessed!")

    cutoff = datetime(2025, 12, 31, tzinfo=UTC)
    with pytest.raises(ResourceLimitError, match="production count budget exceeded"):
        diagnose_sec_fy_ytd_ttm(
            productions=sentinel_gen(),
            declarations=decs,
            canonical_cik="1",
            metric="REVENUE",
            accounting_scope="scope",
            attribution_scope="scope",
            comparability_cohort="cohort",
            security_basis="sec",
            scope_reference="ref",
            knowledge_cutoff=cutoff,
            diagnosed_at=cutoff,
        )


def test_determinism_under_hostile_inexact_trap(tmp_path, monkeypatch):
    fy_prod = make(tmp_path / "fy", monkeypatch, [row(value="1000.123456789", context="c_fy")])
    ytd_prod = make(
        tmp_path / "ytd",
        monkeypatch,
        [
            row(
                value="600.000000001",
                context="c_cur",
                start=date(2024, 9, 29),
                end=date(2025, 6, 28),
            ),
            row(
                value="500.000000001",
                context="c_pri",
                start=date(2023, 9, 24),
                end=date(2024, 6, 29),
            ),
        ],
    )
    decs = (
        SecKnownByTtmInput(
            fy_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_fy",
            2024,
            4,
            date(2023, 9, 24),
            date(2024, 9, 28),
            "ref_fy",
        ),
        SecKnownByTtmInput(
            ytd_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_cur",
            2025,
            3,
            date(2024, 9, 29),
            date(2025, 6, 28),
            "ref_cur",
        ),
        SecKnownByTtmInput(
            ytd_prod.production_identity,
            "IncomeStatement",
            "us-gaap:Revenues",
            "c_pri",
            2024,
            3,
            date(2023, 9, 24),
            date(2024, 6, 29),
            "ref_pri",
        ),
    )
    cutoff = datetime(2025, 12, 31, tzinfo=UTC)
    kwargs = {
        "productions": (fy_prod, ytd_prod),
        "declarations": decs,
        "canonical_cik": "1",
        "metric": "REVENUE",
        "accounting_scope": "scope",
        "attribution_scope": "scope",
        "comparability_cohort": "cohort",
        "security_basis": "sec",
        "scope_reference": "ref",
        "knowledge_cutoff": cutoff,
        "diagnosed_at": cutoff,
    }
    standard_res = diagnose_sec_fy_ytd_ttm(**kwargs)

    with localcontext() as ctx:
        ctx.traps[Inexact] = True
        ctx.prec = 5
        trapped_res = diagnose_sec_fy_ytd_ttm(**kwargs)

    assert trapped_res.value == standard_res.value
    assert trapped_res.result_identity == standard_res.result_identity
