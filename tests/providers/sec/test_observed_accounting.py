import socket
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, Inexact, localcontext

import pytest

from ohmydata.core.errors import ResourceLimitError
from ohmydata.providers.sec import observed_xbrl_financials as producer
from ohmydata.providers.sec.financials import SecStatementRow
from ohmydata.providers.sec.observed_accounting import (
    SecAccountingApplicability as A,
)
from ohmydata.providers.sec.observed_accounting import (
    SecAccountingRule as Rule,
)
from ohmydata.providers.sec.observed_accounting import (
    SecAccountingStatus as Status,
)
from ohmydata.providers.sec.observed_accounting import (
    SecAccountingTerm as Term,
)
from ohmydata.providers.sec.observed_accounting import (
    SecAccountingTolerance as Tol,
)
from ohmydata.providers.sec.observed_accounting import (
    evaluate_sec_observed_accounting as evaluate,
)
from tests.providers.sec.test_observed_xbrl_financials import _produce


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network access"))
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *_: pytest.fail("network access"))


def row(
    concept,
    value,
    *,
    context="c1",
    precision="0",
    start=date(2024, 1, 1),
    end=date(2024, 3, 31),
    kind="duration",
    **kwargs,
):
    return SecStatementRow(
        "IncomeStatement",
        concept,
        concept,
        concept,
        None if value is None else Decimal(value),
        value,
        "iso4217:USD",
        None,
        start,
        end,
        period_type=kind,
        context_ref=context,
        unit_ref="usd",
        decimals_native=precision,
        **kwargs,
    )


def make(tmp_path, monkeypatch, rows):
    # Provider parser boundary; the evaluator and its production seal are real.
    monkeypatch.setattr(producer, "_rows_from_documents", lambda *args: tuple(rows))
    return _produce(tmp_path)[0]


def rule(*, tolerance=Tol.EXACT, terms=None, applicability=A.SAME_CONTEXT, unit="iso4217:USD"):
    return Rule(
        "synthetic:equation",
        applicability,
        terms or (Term("IncomeStatement", "A", "c1", 1), Term("IncomeStatement", "B", "c1", -1)),
        unit,
        tolerance,
        "evidence:synthetic-comparable-scope",
    )


def run(production, rules=None, **kwargs):
    return evaluate(
        production,
        [rule()] if rules is None else rules,
        detected_at=production.produced_at,
        recorded_at=production.produced_at,
        **kwargs,
    )


@pytest.mark.parametrize(
    "values,tolerance,status,residual",
    [
        (("10", "10"), Tol.EXACT, Status.MATCH, "0"),
        (("10", "11"), Tol.EXACT, Status.MISMATCH, "-1"),
        (("10", "11"), Tol.ASSUME_NEAREST_REPORTED_DECIMALS, Status.MATCH, "-1"),
        (("10", "11.1"), Tol.ASSUME_NEAREST_REPORTED_DECIMALS, Status.MISMATCH, "-1.1"),
    ],
)
def test_exact_and_explicit_nearest(tmp_path, monkeypatch, values, tolerance, status, residual):
    production = make(tmp_path, monkeypatch, [row("A", values[0]), row("B", values[1])])
    result = run(production, [rule(tolerance=tolerance)]).checks[0]
    assert result.status is status and result.residual == Decimal(residual)
    assert result.tolerance == (0 if tolerance is Tol.EXACT else 1)


def test_missing_and_incomparable_are_both_preserved(tmp_path, monkeypatch):
    production = make(tmp_path, monkeypatch, [row("A", None)])
    result = run(production, [rule(unit="shares")]).checks[0]
    assert result.status is Status.MISSING
    assert result.missing_reasons == ((0, "VALUE_MISSING"), (1, "ROW_ABSENT"))
    assert result.incomparable_reasons == ((0, "UNIT_MISMATCH_OR_MISSING"),)
    assert result.residual is None and result.tolerance is None


@pytest.mark.parametrize("change", [None, "1"])
def test_identical_and_conflicting_duplicates(tmp_path, monkeypatch, change):
    a = row("A", "10")
    production = make(
        tmp_path, monkeypatch, [a, a if change is None else row("A", change), row("B", "10")]
    )
    result = run(production).checks[0]
    assert result.status is (Status.MATCH if change is None else Status.INCOMPARABLE)
    assert result.evidence[0].row_ordinals == (0, 1)


@pytest.mark.parametrize(
    "change",
    [
        {"dimension": "segment:other"},
        {"period_start": date(2024, 2, 1)},
        {"period_end": None},
        {"period_type": None},
    ],
)
def test_basis_mismatch(tmp_path, monkeypatch, change):
    production = make(tmp_path, monkeypatch, [row("A", "10"), replace(row("B", "10"), **change)])
    assert run(production).checks[0].status is Status.INCOMPARABLE


@pytest.mark.parametrize(
    "precision,expected",
    [
        (None, Status.INCOMPARABLE),
        ("oops", Status.INCOMPARABLE),
        ("10000", Status.INCOMPARABLE),
        ("-10000", Status.INCOMPARABLE),
        ("INF", Status.MATCH),
    ],
)
def test_precision_contract(tmp_path, monkeypatch, precision, expected):
    production = make(
        tmp_path,
        monkeypatch,
        [row("A", "10", precision=precision), row("B", "10", precision=precision)],
    )
    result = run(production, [rule(tolerance=Tol.ASSUME_NEAREST_REPORTED_DECIMALS)]).checks[0]
    assert result.status is expected
    if precision == "INF":
        assert result.tolerance == 0


@pytest.mark.parametrize("begin", [date(2023, 12, 31), date(2023, 12, 30)])
def test_cash_rollforward_dates_and_definition(tmp_path, monkeypatch, begin):
    rows = [
        row("Cash", "15", context="end", kind="instant", start=None),
        row("Cash", "10", context="begin", kind="instant", start=None, end=begin),
        row("Change", "5"),
    ]
    terms = (
        Term("IncomeStatement", "Cash", "end", 1),
        Term("IncomeStatement", "Cash", "begin", -1),
        Term("IncomeStatement", "Change", "c1", -1),
    )
    production = make(tmp_path, monkeypatch, rows)
    result = run(production, [rule(terms=terms, applicability=A.CASH_ROLLFORWARD)]).checks[0]
    assert result.status is (Status.MATCH if begin.day == 31 else Status.INCOMPARABLE)


def test_missing_fx_not_zero(tmp_path, monkeypatch):
    production = make(tmp_path, monkeypatch, [row("A", "10"), row("B", "10")])
    r = rule()
    result = run(
        production, [replace(r, terms=(*r.terms, Term("IncomeStatement", "FX", "c1", 1)))]
    ).checks[0]
    assert result.status is Status.MISSING and result.residual is None


def test_hostile_decimal_context_and_lineage(tmp_path, monkeypatch):
    production = make(tmp_path, monkeypatch, [row("A", "123456789.123"), row("B", "123456789.122")])
    before = run(production)
    with localcontext() as ctx:
        ctx.prec = 1
        ctx.traps[Inexact] = True
        assert run(production) == before
    assert before.checks[0].residual == Decimal("0.001")
    assert run(production, [rule(), rule()]) == before
    later = evaluate(
        production,
        [rule()],
        detected_at=production.produced_at,
        recorded_at=production.produced_at + timedelta(seconds=1),
    )
    assert later.report_identity != before.report_identity
    assert (
        run(production, [replace(rule(), scope_reference="evidence:changed")]).report_identity
        != before.report_identity
    )


def test_limits_before_hash_and_one_overflow(tmp_path, monkeypatch):
    production = make(tmp_path, monkeypatch, [row("A", "10"), row("B", "10")])
    for kwargs in [{"max_rules": True}, {"max_rows": 0}, {"max_rows": 10001}]:
        with pytest.raises(ValueError):
            run(production, **kwargs)
    with pytest.raises(ResourceLimitError):
        run(production, max_rows=1)
    seen = []

    def rules():
        for i in range(100):
            seen.append(i)
            yield rule()

    with pytest.raises(ResourceLimitError):
        run(production, rules(), max_rules=2)
    assert seen == [0, 1, 2]
    assert run(production, max_rows=2, max_rules=1)


def test_bad_seals_and_time_do_not_repair_input(tmp_path, monkeypatch):
    production = make(tmp_path, monkeypatch, [row("A", "10"), row("B", "10")])
    with pytest.raises(ValueError):
        evaluate(
            production,
            [rule()],
            detected_at=production.produced_at - timedelta(seconds=1),
            recorded_at=production.produced_at,
        )
    object.__setattr__(production.vintage, "vintage_identity", "f" * 64)
    with pytest.raises(ValueError):
        run(production)
    assert production.vintage.vintage_identity == "f" * 64


def test_invalid_rule_shapes():
    with pytest.raises(ValueError):
        Term("IncomeStatement", "A", "c1", True)
    with pytest.raises(ValueError):
        rule(terms=(rule().terms[0], rule().terms[0]))
    with pytest.raises(ValueError):
        rule(applicability=A.CASH_ROLLFORWARD)


def test_eight_terms_and_nearest_allowance(tmp_path, monkeypatch):
    rows = [row(f"C{i}", "999999999999999999.1") for i in range(8)]
    terms = tuple(Term("IncomeStatement", f"C{i}", "c1", 1 if i < 4 else -1) for i in range(8))
    production = make(tmp_path, monkeypatch, rows)
    exact = rule(terms=terms)
    nearest = replace(
        exact, rule_id="nearest", tolerance_policy=Tol.ASSUME_NEAREST_REPORTED_DECIMALS
    )
    with localcontext() as ctx:
        ctx.prec = 1
        ctx.traps[Inexact] = True
        results = run(production, [exact, nearest]).checks
    assert all(r.status is Status.MATCH and r.residual == 0 for r in results)
    assert {r.rule.rule_id: r.tolerance for r in results} == {
        exact.rule_id: Decimal(0),
        "nearest": Decimal(4),
    }


def test_report_budget_for_repeated_duplicate_evidence(tmp_path, monkeypatch):
    production = make(tmp_path, monkeypatch, [row("A", "10"), row("B", "10")] * 500)
    rules = [replace(rule(), rule_id=f"equation-{i}") for i in range(64)]
    with pytest.raises(ResourceLimitError, match="evidence budget"):
        run(production, rules)


def test_excess_rows_rejected_without_seal_repair(tmp_path, monkeypatch):
    production = make(tmp_path, monkeypatch, [row("A", "10"), row("B", "10")])
    original = production.vintage.vintage_identity
    object.__setattr__(production.vintage, "rows", production.vintage.rows * 5001)
    with pytest.raises(ResourceLimitError, match="row budget"):
        run(production)
    assert production.vintage.vintage_identity == original


@pytest.mark.parametrize(
    "precision,expected",
    [("-9999", Status.MATCH), ("9998", Status.MATCH), ("9999", Status.INCOMPARABLE)],
)
def test_precision_exact_boundaries(tmp_path, monkeypatch, precision, expected):
    production = make(
        tmp_path,
        monkeypatch,
        [row("A", "0", precision=precision), row("B", "0", precision=precision)],
    )
    assert (
        run(production, [rule(tolerance=Tol.ASSUME_NEAREST_REPORTED_DECIMALS)]).checks[0].status
        is expected
    )


def test_forged_nonfinite_fails_without_changing_input(tmp_path, monkeypatch):
    production = make(tmp_path, monkeypatch, [row("A", "10"), row("B", "10")])
    original = production.production_identity
    object.__setattr__(production.vintage.rows[0], "value", Decimal("NaN"))
    with pytest.raises(ValueError):
        run(production)
    assert production.production_identity == original
    assert production.vintage.rows[0].value.is_nan()
