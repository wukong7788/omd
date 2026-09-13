"""Acceptance tests for offline 10-Q independent-quarter fact binding.

Covers:
- successful AAPL-like current/prior comparative declarations;
- deterministic identity;
- wrong accession/context/concept/statement/period/value/unit/dimension/period type;
- null/nonfinite/incomplete evidence;
- duplicate declarations/rows;
- Q4 or YTD/non-independent labels;
- exact/over input and text/payload bounds;
- mutation isolation;
- unbounded iterable consumption stops at max_declarations + 1;
- no inference from a 90-day row without explicit context evidence;
- inspect an actual synthetic result contract.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import pytest

from ohmydata.providers.sec import (
    SecCompanyFinancialVintage,
    SecIndependentQuarterDeclaration,
    SecQuarterBindingResult,
    SecQuarterBoundFact,
    SecQuarterFactDeclaration,
    SecStatementRow,
    bind_sec_independent_quarter_facts,
    bind_sec_quarter_facts,
)

_ACCESSION = "0000320193-24-000069"
_SYMBOL = "AAPL"
_CIK = "0000320193"
_VINTAGE_DATE = date(2024, 8, 2)


def _make_aapl_fixture() -> tuple[
    SecCompanyFinancialVintage, tuple[SecQuarterFactDeclaration, ...]
]:
    """Build an AAPL-like Q3 10-Q vintage with comparative current and prior quarters."""
    current_q3_start = date(2024, 3, 31)
    current_q3_end = date(2024, 6, 29)
    prior_q3_start = date(2023, 4, 2)
    prior_q3_end = date(2023, 7, 1)

    ytd_start = date(2023, 10, 1)
    ytd_end = date(2024, 6, 29)

    rows = (
        # Current Q3 discrete rows (context: c-current-q3)
        SecStatementRow(
            statement_type="income_statement",
            standard_concept="Revenue",
            concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            label="Total net sales",
            value=Decimal(85777000000),
            value_native="85777000000",
            unit="iso4217:USD",
            period_start=current_q3_start,
            period_end=current_q3_end,
            period_type="duration",
            context_ref="c-current-q3",
            dimension=None,
        ),
        SecStatementRow(
            statement_type="income_statement",
            standard_concept="GrossProfit",
            concept="us-gaap_GrossProfit",
            label="Gross margin",
            value=Decimal(39678000000),
            value_native="39678000000",
            unit="iso4217:USD",
            period_start=current_q3_start,
            period_end=current_q3_end,
            period_type="duration",
            context_ref="c-current-q3",
            dimension=None,
        ),
        SecStatementRow(
            statement_type="income_statement",
            standard_concept="OperatingIncome",
            concept="us-gaap_OperatingIncomeLoss",
            label="Operating income",
            value=Decimal(25352000000),
            value_native="25352000000",
            unit="iso4217:USD",
            period_start=current_q3_start,
            period_end=current_q3_end,
            period_type="duration",
            context_ref="c-current-q3",
            dimension=None,
        ),
        # Prior Q3 comparative discrete rows (context: c-prior-q3)
        SecStatementRow(
            statement_type="income_statement",
            standard_concept="Revenue",
            concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            label="Total net sales",
            value=Decimal(81797000000),
            value_native="81797000000",
            unit="iso4217:USD",
            period_start=prior_q3_start,
            period_end=prior_q3_end,
            period_type="duration",
            context_ref="c-prior-q3",
            dimension=None,
        ),
        SecStatementRow(
            statement_type="income_statement",
            standard_concept="GrossProfit",
            concept="us-gaap_GrossProfit",
            label="Gross margin",
            value=Decimal(35384000000),
            value_native="35384000000",
            unit="iso4217:USD",
            period_start=prior_q3_start,
            period_end=prior_q3_end,
            period_type="duration",
            context_ref="c-prior-q3",
            dimension=None,
        ),
        SecStatementRow(
            statement_type="income_statement",
            standard_concept="OperatingIncome",
            concept="us-gaap_OperatingIncomeLoss",
            label="Operating income",
            value=Decimal(22998000000),
            value_native="22998000000",
            unit="iso4217:USD",
            period_start=prior_q3_start,
            period_end=prior_q3_end,
            period_type="duration",
            context_ref="c-prior-q3",
            dimension=None,
        ),
        # Noise rows: Nine-month YTD rows (should never be bound as independent quarters)
        SecStatementRow(
            statement_type="income_statement",
            standard_concept="Revenue",
            concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            label="Total net sales (YTD)",
            value=Decimal(296245000000),
            value_native="296245000000",
            unit="iso4217:USD",
            period_start=ytd_start,
            period_end=ytd_end,
            period_type="duration",
            context_ref="c-ytd-9m",
            dimension=None,
        ),
        # Noise rows: Dimensional breakdown row
        SecStatementRow(
            statement_type="income_statement",
            standard_concept="Revenue",
            concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            label="Products revenue",
            value=Decimal(61564000000),
            value_native="61564000000",
            unit="iso4217:USD",
            period_start=current_q3_start,
            period_end=current_q3_end,
            period_type="duration",
            context_ref="c-products-dim",
            dimension='{"us-gaap:StatementClassAxis":"us-gaap:ProductMember"}',
        ),
        # Noise rows: Instant balance sheet row
        SecStatementRow(
            statement_type="balance_sheet",
            standard_concept="Cash",
            concept="us-gaap_CashAndCashEquivalentsAtCarryingValue",
            label="Cash and cash equivalents",
            value=Decimal(25573000000),
            value_native="25573000000",
            unit="iso4217:USD",
            period_start=None,
            period_end=current_q3_end,
            period_type="instant",
            context_ref="c-instant-bs",
            dimension=None,
        ),
        # Noise rows: Unreferenced ~90-day duration row
        SecStatementRow(
            statement_type="income_statement",
            standard_concept="Revenue",
            concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            label="Unreferenced quarter row",
            value=Decimal(99999999999),
            value_native="99999999999",
            unit="iso4217:USD",
            period_start=current_q3_start,
            period_end=current_q3_end,
            period_type="duration",
            context_ref="c-unreferenced-90day",
            dimension=None,
        ),
    )

    vintage = SecCompanyFinancialVintage(
        symbol=_SYMBOL,
        cik=_CIK,
        company_name="Apple Inc.",
        form="10-Q",
        accession_number=_ACCESSION,
        filing_date=_VINTAGE_DATE,
        fiscal_year=2024,
        fiscal_period="Q3",
        period_end=current_q3_end,
        rows=rows,
    )

    declarations = (
        # Current Q3 declarations
        SecQuarterFactDeclaration(
            accession_number=_ACCESSION,
            statement_type="income_statement",
            native_concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            context_ref="c-current-q3",
            period_kind="INDEPENDENT_QUARTER",
            fiscal_year=2024,
            fiscal_quarter=3,
            period_start=current_q3_start,
            period_end=current_q3_end,
            evidence_reference="html-table-1-cell-r1c1",
            unit="iso4217:USD",
            source_label="Three Months Ended",
        ),
        SecQuarterFactDeclaration(
            accession_number=_ACCESSION,
            statement_type="income_statement",
            native_concept="us-gaap_GrossProfit",
            context_ref="c-current-q3",
            period_kind="INDEPENDENT_QUARTER",
            fiscal_year=2024,
            fiscal_quarter=3,
            period_start=current_q3_start,
            period_end=current_q3_end,
            evidence_reference="html-table-1-cell-r2c1",
            unit="iso4217:USD",
            source_label="Three Months Ended",
        ),
        SecQuarterFactDeclaration(
            accession_number=_ACCESSION,
            statement_type="income_statement",
            native_concept="us-gaap_OperatingIncomeLoss",
            context_ref="c-current-q3",
            period_kind="INDEPENDENT_QUARTER",
            fiscal_year=2024,
            fiscal_quarter=3,
            period_start=current_q3_start,
            period_end=current_q3_end,
            evidence_reference="html-table-1-cell-r3c1",
            unit="iso4217:USD",
            source_label="Three Months Ended",
        ),
        # Prior Q3 comparative declarations
        SecQuarterFactDeclaration(
            accession_number=_ACCESSION,
            statement_type="income_statement",
            native_concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            context_ref="c-prior-q3",
            period_kind="INDEPENDENT_QUARTER",
            fiscal_year=2023,
            fiscal_quarter=3,
            period_start=prior_q3_start,
            period_end=prior_q3_end,
            evidence_reference="html-table-1-cell-r1c2",
            unit="iso4217:USD",
            source_label="Three Months Ended",
        ),
        SecQuarterFactDeclaration(
            accession_number=_ACCESSION,
            statement_type="income_statement",
            native_concept="us-gaap_GrossProfit",
            context_ref="c-prior-q3",
            period_kind="INDEPENDENT_QUARTER",
            fiscal_year=2023,
            fiscal_quarter=3,
            period_start=prior_q3_start,
            period_end=prior_q3_end,
            evidence_reference="html-table-1-cell-r2c2",
            unit="iso4217:USD",
            source_label="Three Months Ended",
        ),
        SecQuarterFactDeclaration(
            accession_number=_ACCESSION,
            statement_type="income_statement",
            native_concept="us-gaap_OperatingIncomeLoss",
            context_ref="c-prior-q3",
            period_kind="INDEPENDENT_QUARTER",
            fiscal_year=2023,
            fiscal_quarter=3,
            period_start=prior_q3_start,
            period_end=prior_q3_end,
            evidence_reference="html-table-1-cell-r3c2",
            unit="iso4217:USD",
            source_label="Three Months Ended",
        ),
    )

    return vintage, declarations


def test_successful_aapl_current_and_prior_comparative_declarations() -> None:
    """Validate successful AAPL-like binding of current and prior comparative quarters."""
    vintage, declarations = _make_aapl_fixture()
    result = bind_sec_quarter_facts(vintage=vintage, declarations=declarations)

    assert isinstance(result, SecQuarterBindingResult)
    assert result.symbol == "AAPL"
    assert result.cik == "0000320193"
    assert result.canonical_cik == "320193"
    assert result.accession_number == _ACCESSION
    assert result.form == "10-Q"
    assert result.vintage_identity == vintage.vintage_identity
    assert len(result.facts) == 6

    # Verify each fact preserves exact native fields and declaration
    rev_cur = result.get_fact(
        concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
        fiscal_year=2024,
        fiscal_quarter=3,
    )
    assert rev_cur is not None
    assert rev_cur.value == Decimal(85777000000)
    assert rev_cur.unit == "iso4217:USD"
    assert rev_cur.currency == "USD"
    assert rev_cur.row_ordinal == 0
    assert rev_cur.context_ref == "c-current-q3"
    assert rev_cur.declaration.evidence_reference == "html-table-1-cell-r1c1"

    rev_pri = result.get_fact(
        concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
        fiscal_year=2023,
        fiscal_quarter=3,
    )
    assert rev_pri is not None
    assert rev_pri.value == Decimal(81797000000)
    assert rev_pri.row_ordinal == 3
    assert rev_pri.context_ref == "c-prior-q3"

    gp_cur = result.get_fact(concept="us-gaap_GrossProfit", fiscal_year=2024, fiscal_quarter=3)
    assert gp_cur is not None and gp_cur.value == Decimal(39678000000)

    gp_pri = result.get_fact(concept="us-gaap_GrossProfit", fiscal_year=2023, fiscal_quarter=3)
    assert gp_pri is not None and gp_pri.value == Decimal(35384000000)

    op_cur = result.get_fact(
        concept="us-gaap_OperatingIncomeLoss", fiscal_year=2024, fiscal_quarter=3
    )
    assert op_cur is not None and op_cur.value == Decimal(25352000000)

    op_pri = result.get_fact(
        concept="us-gaap_OperatingIncomeLoss", fiscal_year=2023, fiscal_quarter=3
    )
    assert op_pri is not None and op_pri.value == Decimal(22998000000)

    # Verify public alias also works identically
    alias_result = bind_sec_independent_quarter_facts(vintage=vintage, declarations=declarations)
    assert alias_result.binding_identity == result.binding_identity


def test_deterministic_identity() -> None:
    """Binding identity must be bit-for-bit deterministic and sensitive to changes."""
    vintage, declarations = _make_aapl_fixture()
    result1 = bind_sec_quarter_facts(vintage=vintage, declarations=declarations)
    result2 = bind_sec_quarter_facts(vintage=vintage, declarations=declarations)

    assert result1.binding_identity == result2.binding_identity
    assert [f.fact_identity for f in result1.facts] == [f.fact_identity for f in result2.facts]

    # Ambient Decimal precision does not alter deterministic identity
    with localcontext() as ctx:
        ctx.prec = 2
        result_low = bind_sec_quarter_facts(vintage=vintage, declarations=declarations)
    with localcontext() as ctx:
        ctx.prec = 80
        result_high = bind_sec_quarter_facts(vintage=vintage, declarations=declarations)
    assert result_low.binding_identity == result1.binding_identity
    assert result_high.binding_identity == result1.binding_identity

    # Altering evidence reference changes fact identity and binding identity
    altered_decl = replace(declarations[0], evidence_reference="changed-ref")
    altered_decls = (altered_decl, *declarations[1:])
    result_altered = bind_sec_quarter_facts(vintage=vintage, declarations=altered_decls)
    assert result_altered.binding_identity != result1.binding_identity
    assert result_altered.facts[0].fact_identity != result1.facts[0].fact_identity
    assert result_altered.facts[1].fact_identity == result1.facts[1].fact_identity


def test_wrong_accession() -> None:
    """Wrong accession in declaration or vintage must fail explicitly."""
    vintage, declarations = _make_aapl_fixture()

    # Declaration accession does not match vintage accession
    wrong_decl = replace(declarations[0], accession_number="0000320193-24-000001")
    with pytest.raises(ValueError, match="does not match vintage accession_number"):
        bind_sec_quarter_facts(vintage=vintage, declarations=(wrong_decl, *declarations[1:]))

    # Vintage form is 10-K, not 10-Q
    vintage_10k = replace(vintage, form="10-K")
    with pytest.raises(ValueError, match="vintage form must be '10-Q' or '10-Q/A'"):
        bind_sec_quarter_facts(vintage=vintage_10k, declarations=declarations)


def test_wrong_context() -> None:
    """Declaration naming non-existent context_ref must fail."""
    vintage, declarations = _make_aapl_fixture()
    wrong_decl = replace(declarations[0], context_ref="c-nonexistent-context")
    with pytest.raises(ValueError, match="matched no native rows"):
        bind_sec_quarter_facts(vintage=vintage, declarations=(wrong_decl, *declarations[1:]))


def test_wrong_concept() -> None:
    """Declaration naming non-existent concept must fail."""
    vintage, declarations = _make_aapl_fixture()
    wrong_decl = replace(declarations[0], native_concept="us-gaap_NonExistentConcept")
    with pytest.raises(ValueError, match="matched no native rows"):
        bind_sec_quarter_facts(vintage=vintage, declarations=(wrong_decl, *declarations[1:]))


def test_wrong_statement() -> None:
    """Declaration naming wrong statement_type must fail."""
    vintage, declarations = _make_aapl_fixture()
    wrong_decl = replace(declarations[0], statement_type="cash_flow")
    with pytest.raises(ValueError, match="matched no native rows"):
        bind_sec_quarter_facts(vintage=vintage, declarations=(wrong_decl, *declarations[1:]))


def test_wrong_period() -> None:
    """Declaration naming wrong period dates must fail."""
    vintage, declarations = _make_aapl_fixture()
    # Shift period_start by 1 day
    wrong_decl = replace(declarations[0], period_start=date(2024, 4, 1))
    with pytest.raises(ValueError, match="matched no native rows"):
        bind_sec_quarter_facts(vintage=vintage, declarations=(wrong_decl, *declarations[1:]))


def test_wrong_value() -> None:
    """Row with null or non-finite value must fail."""
    vintage, declarations = _make_aapl_fixture()

    # Row with null value
    null_row = replace(vintage.rows[0], value=None)
    vintage_null = replace(vintage, rows=(null_row, *vintage.rows[1:]))
    with pytest.raises(ValueError, match="null value"):
        bind_sec_quarter_facts(vintage=vintage_null, declarations=declarations)

    # Row with non-finite value
    with pytest.raises(ValueError, match="non-finite"):
        replace(vintage.rows[0], value=Decimal("NaN"))


def test_wrong_unit() -> None:
    """Row with missing or empty unit must fail."""
    vintage, declarations = _make_aapl_fixture()

    # None unit
    unit_none_row = replace(vintage.rows[0], unit=None)
    vintage_unit_none = replace(vintage, rows=(unit_none_row, *vintage.rows[1:]))
    with pytest.raises(ValueError, match="missing or empty unit"):
        bind_sec_quarter_facts(vintage=vintage_unit_none, declarations=declarations)

    # Empty string unit
    unit_empty_row = replace(vintage.rows[0], unit="  ")
    vintage_unit_empty = replace(vintage, rows=(unit_empty_row, *vintage.rows[1:]))
    with pytest.raises(ValueError, match="missing or empty unit"):
        bind_sec_quarter_facts(vintage=vintage_unit_empty, declarations=declarations)

    # Unit mismatch between declaration and row
    decl_mismatched_unit = replace(declarations[0], unit="iso4217:EUR")
    with pytest.raises(ValueError, match="unit mismatch"):
        bind_sec_quarter_facts(
            vintage=vintage, declarations=(decl_mismatched_unit, *declarations[1:])
        )


def test_wrong_dimension() -> None:
    """Row with non-null dimension cannot be bound as consolidated fact."""
    vintage, declarations = _make_aapl_fixture()
    dim_row = replace(vintage.rows[0], dimension='{"StatementClassAxis": "ProductMember"}')
    vintage_dim = replace(vintage, rows=(dim_row, *vintage.rows[1:]))
    with pytest.raises(ValueError, match="expected non-dimensional"):
        bind_sec_quarter_facts(vintage=vintage_dim, declarations=declarations)


def test_wrong_period_type() -> None:
    """Row with period_type='instant' cannot be bound as quarter fact."""
    vintage, declarations = _make_aapl_fixture()
    instant_row = replace(vintage.rows[0], period_type="instant", period_start=None)
    vintage_instant = replace(vintage, rows=(instant_row, *vintage.rows[1:]))
    with pytest.raises(ValueError, match="matched no native rows"):
        bind_sec_quarter_facts(vintage=vintage_instant, declarations=declarations)


def test_null_nonfinite_incomplete_evidence() -> None:
    """Missing or invalid declaration evidence attributes must fail type/value validation."""
    _vintage, declarations = _make_aapl_fixture()
    base = declarations[0]

    # booleans are not integers
    with pytest.raises(TypeError, match="integer"):
        replace(base, fiscal_year=True)
    with pytest.raises(TypeError, match="integer"):
        replace(base, fiscal_quarter=False)

    # None values for required fields
    with pytest.raises((TypeError, ValueError)):
        replace(base, accession_number=None)
    with pytest.raises((TypeError, ValueError)):
        replace(base, native_concept=None)
    with pytest.raises((TypeError, ValueError)):
        replace(base, context_ref=None)
    with pytest.raises((TypeError, ValueError)):
        replace(base, evidence_reference=None)
    with pytest.raises((TypeError, ValueError)):
        replace(base, source_label=None)
    with pytest.raises((TypeError, ValueError)):
        replace(base, unit=None)

    # Empty string evidence
    with pytest.raises(ValueError, match="non-empty"):
        replace(base, evidence_reference="")
    with pytest.raises(ValueError, match="non-empty"):
        replace(base, source_label="")
    with pytest.raises(ValueError, match="non-empty"):
        replace(base, unit="")

    # Reversed dates
    with pytest.raises(ValueError, match="reversed period"):
        replace(base, period_start=date(2024, 6, 30), period_end=date(2024, 3, 31))

    # datetime instead of date
    with pytest.raises(TypeError, match="date, not datetime"):
        replace(base, period_start=datetime(2024, 3, 31, 0, 0, tzinfo=UTC))


def test_duplicate_declarations_and_rows() -> None:
    """Duplicate declarations or multiple matching rows must fail."""
    vintage, declarations = _make_aapl_fixture()

    # Duplicate declaration in input
    with pytest.raises(ValueError, match="duplicate or conflicting"):
        bind_sec_quarter_facts(vintage=vintage, declarations=(declarations[0], declarations[0]))

    # Multiple identical rows in vintage matching one declaration
    dup_rows = (vintage.rows[0], *vintage.rows)
    vintage_dup_rows = replace(vintage, rows=dup_rows)
    with pytest.raises(ValueError, match="matched multiple"):
        bind_sec_quarter_facts(vintage=vintage_dup_rows, declarations=declarations)

    # Two different declarations matching the exact same row
    decl_alias = replace(declarations[0], evidence_reference="different-ref")
    with pytest.raises(ValueError, match="duplicate or conflicting"):
        bind_sec_quarter_facts(vintage=vintage, declarations=(declarations[0], decl_alias))


def test_q4_or_ytd_non_independent_labels() -> None:
    """Q4 and non-independent/YTD labels must be rejected."""
    _, declarations = _make_aapl_fixture()
    base = declarations[0]

    # Q4 is invalid for 10-Q independent quarter
    with pytest.raises(ValueError, match="10-Q independent quarter declaration cannot be Q4"):
        replace(base, fiscal_quarter=4)

    # fiscal_quarter outside 1..3
    with pytest.raises(ValueError, match="fiscal_quarter must be in 1..3"):
        replace(base, fiscal_quarter=5)
    with pytest.raises(ValueError, match="fiscal_quarter must be in 1..3"):
        replace(base, fiscal_quarter=0)

    # period_kind != INDEPENDENT_QUARTER
    with pytest.raises(ValueError, match="period_kind must be 'INDEPENDENT_QUARTER'"):
        replace(base, period_kind="YTD")
    with pytest.raises(ValueError, match="period_kind must be 'INDEPENDENT_QUARTER'"):
        replace(base, period_kind="FY")

    # Non-independent duration source labels
    for bad_label in (
        "Nine Months Ended",
        "Six Months Ended",
        "Twelve Months Ended",
        "YTD",
        "Year to Date",
        "Full Year",
        "FY 2024",
        "Annual",
        "Arbitrary Label",
    ):
        with pytest.raises(ValueError, match="(non-independent|explicit independent quarter)"):
            replace(base, source_label=bad_label)


def test_exact_and_over_bounds() -> None:
    """Exact bounds must pass and over bounds must fail."""
    vintage, declarations = _make_aapl_fixture()
    base = declarations[0]

    # Text limit: 1024 UTF-8 bytes succeeds, 1025 fails
    exact_text = "a" * 1024
    over_text = "a" * 1025
    valid_decl = replace(base, evidence_reference=exact_text)
    assert valid_decl.evidence_reference == exact_text

    with pytest.raises(ValueError, match="1024"):
        replace(base, evidence_reference=over_text)

    # Multibyte UTF-8 boundary: '界' is 3 bytes. 341 * 3 + 1 = 1024 bytes
    exact_utf8 = "界" * 341 + "a"
    over_utf8 = exact_utf8 + "a"
    assert len(exact_utf8.encode("utf-8")) == 1024
    assert len(over_utf8.encode("utf-8")) == 1025
    replace(base, evidence_reference=exact_utf8)
    with pytest.raises(ValueError, match="1024"):
        replace(base, evidence_reference=over_utf8)

    # max_declarations exact and over
    # exact: 6 declarations with max_declarations=6 succeeds
    result = bind_sec_quarter_facts(vintage=vintage, declarations=declarations, max_declarations=6)
    assert len(result.facts) == 6

    # over: 6 declarations with max_declarations=5 fails
    with pytest.raises(ValueError, match="exceeds limit"):
        bind_sec_quarter_facts(vintage=vintage, declarations=declarations, max_declarations=5)

    # max_declarations validation
    with pytest.raises(ValueError, match="integer between 1 and 10000"):
        bind_sec_quarter_facts(vintage=vintage, declarations=declarations, max_declarations=0)
    with pytest.raises(ValueError, match="integer between 1 and 10000"):
        bind_sec_quarter_facts(vintage=vintage, declarations=declarations, max_declarations=10001)
    with pytest.raises(ValueError, match="integer between 1 and 10000"):
        bind_sec_quarter_facts(vintage=vintage, declarations=declarations, max_declarations=True)


def test_mutation_isolation() -> None:
    """Caller mutation of input collections or results must not alter bound contract."""
    vintage, declarations = _make_aapl_fixture()
    decl_list = list(declarations)

    result = bind_sec_quarter_facts(vintage=vintage, declarations=decl_list)
    initial_identity = result.binding_identity
    initial_count = len(result.facts)

    # Mutate the caller's list
    decl_list.pop()
    assert len(decl_list) == 5
    assert len(result.facts) == initial_count
    assert result.binding_identity == initial_identity

    # Result, facts, and declarations are frozen/immutable
    with pytest.raises(FrozenInstanceError):
        result.symbol = "MUTATED"  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        result.facts[0].value = Decimal(0)  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        result.facts[0].declaration.fiscal_year = 2099  # type: ignore[misc]


def test_no_inference_from_90_day_row_without_explicit_context_evidence() -> None:
    """A ~90-day row with matching dates must never be bound without explicit context evidence."""
    vintage, declarations = _make_aapl_fixture()

    # The fixture vintage contains row 9: 'c-unreferenced-90day' with exact Q3 dates and concept.
    # The caller provides 6 declarations for 'c-current-q3' and 'c-prior-q3'.
    result = bind_sec_quarter_facts(vintage=vintage, declarations=declarations)

    # Verify that 'c-unreferenced-90day' is NEVER bound into result.facts
    bound_contexts = {f.context_ref for f in result.facts}
    assert "c-unreferenced-90day" not in bound_contexts
    assert bound_contexts == {"c-current-q3", "c-prior-q3"}

    # If caller attempts to declare a non-existent context, OMD refuses to guess the 90-day row
    orphan_decl = SecQuarterFactDeclaration(
        accession_number=_ACCESSION,
        statement_type="income_statement",
        native_concept="us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
        context_ref="c-guessed-context",
        period_kind="INDEPENDENT_QUARTER",
        fiscal_year=2024,
        fiscal_quarter=3,
        period_start=date(2024, 3, 31),
        period_end=date(2024, 6, 29),
        evidence_reference="html-guess",
        unit="iso4217:USD",
        source_label="Three Months Ended",
    )
    with pytest.raises(ValueError, match="matched no native rows"):
        bind_sec_quarter_facts(vintage=vintage, declarations=(orphan_decl,))


def test_unbounded_iterable_consumption_stops_at_limit() -> None:
    """Consuming an unbounded iterable stops after max_declarations + 1 items."""
    vintage, declarations = _make_aapl_fixture()
    sample = declarations[0]
    consumed = 0

    def gen():
        nonlocal consumed
        while True:
            consumed += 1
            if consumed > 6:
                raise AssertionError("consumed beyond max_declarations + 1 sentinel")
            yield sample

    with pytest.raises(ValueError, match="exceeds limit"):
        bind_sec_quarter_facts(vintage=vintage, declarations=gen(), max_declarations=5)
    assert consumed == 6


def test_inspect_synthetic_contract() -> None:
    """Inspect the full synthetic result contract and verified field attributes."""
    vintage, declarations = _make_aapl_fixture()
    # Single declaration for focused contract inspection
    single_decl = declarations[0]
    result = bind_sec_quarter_facts(vintage=vintage, declarations=(single_decl,))

    # Inspect result contract fields
    assert result.symbol == "AAPL"
    assert result.cik == "0000320193"
    assert result.canonical_cik == "320193"
    assert result.accession_number == "0000320193-24-000069"
    assert result.form == "10-Q"
    assert len(result.vintage_identity) == 64
    assert len(result.binding_identity) == 64
    assert len(result.facts) == 1

    # Inspect bound fact contract fields
    fact = result.facts[0]
    assert isinstance(fact, SecQuarterBoundFact)
    assert fact.statement_type == "income_statement"
    assert fact.concept == "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax"
    assert fact.label == "Total net sales"
    assert fact.value == Decimal(85777000000)
    assert fact.value_native == "85777000000"
    assert fact.unit == "iso4217:USD"
    assert fact.currency == "USD"
    assert fact.period_start == date(2024, 3, 31)
    assert fact.period_end == date(2024, 6, 29)
    assert fact.context_ref == "c-current-q3"
    assert fact.row_ordinal == 0
    assert len(fact.fact_identity) == 64

    # Inspect declaration contract fields
    decl = fact.declaration
    assert isinstance(decl, SecQuarterFactDeclaration)
    assert decl.accession_number == "0000320193-24-000069"
    assert decl.native_concept == "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax"
    assert decl.context_ref == "c-current-q3"
    assert decl.period_kind == "INDEPENDENT_QUARTER"
    assert decl.fiscal_year == 2024
    assert decl.fiscal_quarter == 3
    assert decl.period_start == date(2024, 3, 31)
    assert decl.period_end == date(2024, 6, 29)
    assert decl.evidence_reference == "html-table-1-cell-r1c1"
    assert decl.unit == "iso4217:USD"
    assert decl.source_label == "Three Months Ended"

    # Verify alias SecIndependentQuarterDeclaration
    assert isinstance(decl, SecIndependentQuarterDeclaration)
