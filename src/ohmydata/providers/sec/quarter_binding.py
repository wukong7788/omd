"""Public offline independent-quarter SEC statement fact binding for single 10-Q filings.

Binds caller-supplied explicit independent-quarter evidence to native SEC financial
facts in a single 10-Q accession without inferring discrete quarters from dates or
duration alone.

This module provides structural lineage binding only. It does not certify issuer data
quality, arithmetic correctness, point-in-time publication, or market availability
(no quality PASS or PIT eligibility claim).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from itertools import islice
from typing import Any

from .financials import SecCompanyFinancialVintage, SecStatementRow
from .observed_xbrl_financials import SecObservedFinancialVintage

_MAX_TEXT_BYTES = 1_024
_MAX_DECLARATIONS = 100
_MAX_PAYLOAD_BYTES = 65_536

_NON_INDEPENDENT_LABELS = frozenset(
    {
        "six months",
        "6 months",
        "nine months",
        "9 months",
        "twelve months",
        "12 months",
        "year ended",
        "year to date",
        "ytd",
        "annual",
        "six-month",
        "nine-month",
        "full year",
        "fy ",
        "fy20",
    }
)

_VALID_QUARTER_LABELS = frozenset(
    {
        "three months ended",
        "3 months ended",
        "three-month ended",
        "quarter ended",
        "13 weeks ended",
        "13-week ended",
        "three months",
        "3 months",
    }
)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_TEXT_BYTES:
        raise ValueError(f"{name} exceeds maximum byte limit of 1024 UTF-8 bytes")
    return value


def _canonical(value: Any) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is Decimal:
        return {"decimal": str(value)}
    if type(value) is datetime:
        return {"datetime": value.isoformat()}
    if type(value) is date:
        return {"date": value.isoformat()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _payload_bytes(value: Any) -> bytes:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class SecQuarterFactDeclaration:
    """Caller-attested declaration of an independent quarter SEC fact."""

    accession_number: str
    statement_type: str
    native_concept: str
    context_ref: str
    period_start: date
    period_end: date
    fiscal_year: int
    fiscal_quarter: int
    evidence_reference: str
    unit: str
    source_label: str = "Three Months Ended"
    period_kind: str = "INDEPENDENT_QUARTER"

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if type(self.accession_number) is not str:
            raise TypeError("accession_number must be str")
        _text(self.accession_number, "accession_number")

        if type(self.context_ref) is not str:
            raise TypeError("context_ref must be str")
        _text(self.context_ref, "context_ref")

        if type(self.statement_type) is not str:
            raise TypeError("statement_type must be str")
        _text(self.statement_type, "statement_type")

        if type(self.native_concept) is not str:
            raise TypeError("native_concept must be str")
        _text(self.native_concept, "native_concept")

        if type(self.period_start) is not date or isinstance(self.period_start, datetime):
            raise TypeError("period_start must be date, not datetime")
        if type(self.period_end) is not date or isinstance(self.period_end, datetime):
            raise TypeError("period_end must be date, not datetime")
        if self.period_start > self.period_end:
            raise ValueError("reversed period: period_start must be before period_end")

        if type(self.fiscal_year) is not int or isinstance(self.fiscal_year, bool):
            raise TypeError("fiscal_year must be an integer, not bool")
        if not 1 <= self.fiscal_year <= 9999:
            raise ValueError("fiscal_year must be in 1..9999")

        if type(self.fiscal_quarter) is not int or isinstance(self.fiscal_quarter, bool):
            raise TypeError("fiscal_quarter must be an integer, not bool")
        if self.fiscal_quarter == 4:
            raise ValueError("10-Q independent quarter declaration cannot be Q4")
        if not 1 <= self.fiscal_quarter <= 3:
            raise ValueError(
                f"fiscal_quarter must be in 1..3 for independent 10-Q quarter, got {self.fiscal_quarter}"
            )

        if type(self.period_kind) is not str:
            raise TypeError("period_kind must be str")
        if self.period_kind != "INDEPENDENT_QUARTER":
            raise ValueError(f"period_kind must be 'INDEPENDENT_QUARTER', got '{self.period_kind}'")

        if type(self.source_label) is not str:
            raise TypeError("source_label must be str")
        _text(self.source_label, "source_label")
        norm_label = self.source_label.lower().strip()
        for forbidden in _NON_INDEPENDENT_LABELS:
            if forbidden in norm_label:
                raise ValueError(
                    f"source_label '{self.source_label}' indicates a non-independent duration"
                )
        if not any(valid in norm_label for valid in _VALID_QUARTER_LABELS):
            raise ValueError(
                f"source_label '{self.source_label}' must declare an explicit independent quarter (e.g. 'Three Months Ended')"
            )

        if type(self.unit) is not str:
            raise TypeError("unit must be str")
        _text(self.unit, "unit")

        if type(self.evidence_reference) is not str:
            raise TypeError("evidence_reference must be str")
        _text(self.evidence_reference, "evidence_reference")

    @property
    def concept(self) -> str:
        return self.native_concept

    @property
    def declaration_reference(self) -> str:
        return self.evidence_reference

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "accession_number": self.accession_number,
            "context_ref": self.context_ref,
            "statement_type": self.statement_type,
            "concept": self.native_concept,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "fiscal_year": self.fiscal_year,
            "fiscal_quarter": self.fiscal_quarter,
            "period_kind": self.period_kind,
            "source_label": self.source_label,
            "unit": self.unit,
            "evidence_reference": self.evidence_reference,
        }


SecIndependentQuarterDeclaration = SecQuarterFactDeclaration


@dataclass(frozen=True)
class SecQuarterBoundFact:
    """Immutable lineage binding one explicit caller declaration to one native SEC statement row."""

    accession_number: str
    vintage_identity: str
    row_ordinal: int
    context_ref: str
    declaration: SecQuarterFactDeclaration
    row: SecStatementRow
    fact_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.accession_number) is not str:
            raise TypeError("accession_number must be str")
        if type(self.vintage_identity) is not str:
            raise TypeError("vintage_identity must be str")
        if (
            type(self.row_ordinal) is not int
            or isinstance(self.row_ordinal, bool)
            or self.row_ordinal < 0
        ):
            raise TypeError("row_ordinal must be a non-negative integer")
        if type(self.context_ref) is not str:
            raise TypeError("context_ref must be str")
        if not isinstance(self.declaration, SecQuarterFactDeclaration):
            raise TypeError("declaration must be SecQuarterFactDeclaration")
        if not isinstance(self.row, SecStatementRow):
            raise TypeError("row must be SecStatementRow")

        payload = {
            "schema": "sec-quarter-bound-fact-v1",
            "accession_number": self.accession_number,
            "vintage_identity": self.vintage_identity,
            "row_ordinal": self.row_ordinal,
            "context_ref": self.context_ref,
            "statement_type": self.statement_type,
            "concept": self.concept,
            "value": str(self.value),
            "unit": self.unit,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "declaration": self.declaration.canonical_dict(),
            "row": self.row.to_dict(),
        }
        encoded = _payload_bytes(payload)
        if len(encoded) > _MAX_PAYLOAD_BYTES:
            raise ValueError(f"canonical payload exceeds limit of {_MAX_PAYLOAD_BYTES} bytes")
        object.__setattr__(
            self,
            "fact_identity",
            hashlib.sha256(encoded).hexdigest(),
        )

    @property
    def statement_type(self) -> str:
        return self.row.statement_type

    @property
    def concept(self) -> str:
        return self.row.concept

    @property
    def label(self) -> str:
        return self.row.label

    @property
    def value(self) -> Decimal:
        assert self.row.value is not None
        return self.row.value

    @property
    def value_native(self) -> str | None:
        return self.row.value_native

    @property
    def unit(self) -> str:
        assert self.row.unit is not None
        return self.row.unit

    @property
    def currency(self) -> str | None:
        return self.row.currency

    @property
    def period_start(self) -> date:
        assert self.row.period_start is not None
        return self.row.period_start

    @property
    def period_end(self) -> date:
        assert self.row.period_end is not None
        return self.row.period_end

    @property
    def binding_identity(self) -> str:
        return self.fact_identity


@dataclass(frozen=True)
class SecQuarterBindingResult:
    """Result of binding caller-attested quarter evidence to a 10-Q vintage."""

    symbol: str
    cik: str
    canonical_cik: str
    company_name: str
    form: str
    accession_number: str
    vintage_identity: str
    facts: tuple[SecQuarterBoundFact, ...]
    binding_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.symbol) is not str:
            raise TypeError("symbol must be str")
        if type(self.cik) is not str:
            raise TypeError("cik must be str")
        if type(self.canonical_cik) is not str:
            raise TypeError("canonical_cik must be str")
        if type(self.company_name) is not str:
            raise TypeError("company_name must be str")
        if type(self.form) is not str:
            raise TypeError("form must be str")
        if type(self.accession_number) is not str:
            raise TypeError("accession_number must be str")
        if type(self.vintage_identity) is not str:
            raise TypeError("vintage_identity must be str")
        if type(self.facts) is not tuple or any(
            not isinstance(b, SecQuarterBoundFact) for b in self.facts
        ):
            raise TypeError("facts must be a tuple of SecQuarterBoundFact")

        payload = {
            "schema": "sec-quarter-binding-result-v1",
            "symbol": self.symbol,
            "cik": self.cik,
            "canonical_cik": self.canonical_cik,
            "company_name": self.company_name,
            "form": self.form,
            "accession_number": self.accession_number,
            "vintage_identity": self.vintage_identity,
            "facts": [f.fact_identity for f in self.facts],
        }
        encoded = _payload_bytes(payload)
        if len(encoded) > _MAX_PAYLOAD_BYTES:
            raise ValueError(f"canonical payload exceeds limit of {_MAX_PAYLOAD_BYTES} bytes")
        object.__setattr__(
            self,
            "binding_identity",
            hashlib.sha256(encoded).hexdigest(),
        )

    @property
    def bindings(self) -> tuple[SecQuarterBoundFact, ...]:
        return self.facts

    @property
    def rows(self) -> tuple[SecStatementRow, ...]:
        return tuple(f.row for f in self.facts)

    def get_fact(
        self, concept: str, fiscal_year: int, fiscal_quarter: int
    ) -> SecQuarterBoundFact | None:
        for f in self.facts:
            if (
                f.concept == concept
                and f.declaration.fiscal_year == fiscal_year
                and f.declaration.fiscal_quarter == fiscal_quarter
            ):
                return f
        return None

    def get_binding(
        self, concept: str, context_ref: str | None = None
    ) -> SecQuarterBoundFact | None:
        for f in self.facts:
            if f.concept == concept and (context_ref is None or f.context_ref == context_ref):
                return f
        return None


def _match_row(
    vintage: SecCompanyFinancialVintage | SecObservedFinancialVintage,
    declaration: SecQuarterFactDeclaration,
) -> tuple[int, SecStatementRow]:
    if declaration.accession_number != vintage.accession_number:
        raise ValueError(
            f"declaration accession_number '{declaration.accession_number}' "
            f"does not match vintage accession_number '{vintage.accession_number}'"
        )

    candidates: list[tuple[int, SecStatementRow]] = []
    for idx, r in enumerate(vintage.rows):
        if (
            r.context_ref == declaration.context_ref
            and r.concept == declaration.native_concept
            and r.statement_type == declaration.statement_type
            and r.period_type == "duration"
            and r.period_start == declaration.period_start
            and r.period_end == declaration.period_end
        ):
            candidates.append((idx, r))

    if not candidates:
        raise ValueError(
            f"declaration matched no native rows for concept '{declaration.native_concept}' "
            f"and context_ref '{declaration.context_ref}'"
        )

    valid_candidates: list[tuple[int, SecStatementRow]] = []
    for idx, r in candidates:
        if r.dimension is not None:
            raise ValueError(f"row has dimension '{r.dimension}'; expected non-dimensional row")
        if r.value is None:
            raise ValueError("row has null value; finite Decimal value required")
        if not isinstance(r.value, Decimal) or not r.value.is_finite():
            raise ValueError("row has non-finite value; finite Decimal value required")
        if r.unit is None or not r.unit.strip():
            raise ValueError("row has missing or empty unit")
        if r.unit != declaration.unit:
            raise ValueError(f"unit mismatch: declared '{declaration.unit}', row has '{r.unit}'")
        valid_candidates.append((idx, r))

    if len(valid_candidates) > 1:
        raise ValueError(
            f"declaration matched multiple candidate rows in vintage for concept '{declaration.native_concept}'"
        )

    return valid_candidates[0]


def bind_sec_quarter_fact(
    vintage: SecCompanyFinancialVintage | SecObservedFinancialVintage,
    declaration: SecQuarterFactDeclaration,
) -> SecQuarterBoundFact:
    """Bind one explicit caller-declared independent quarter fact to a native 10-Q statement row.

    Requires exact agreement for accession, context_ref, concept, statement_type,
    period dates, and unit against a single finite non-dimensional duration row in the
    supplied 10-Q vintage.

    This is a structural lineage binding, not financial quality approval (no PASS claim),
    point-in-time publication, or metric correctness assertion.
    """
    if not isinstance(vintage, (SecCompanyFinancialVintage, SecObservedFinancialVintage)):
        raise TypeError("vintage must be a SecCompanyFinancialVintage")
    if type(vintage.form) is not str or vintage.form.upper() not in {"10-Q", "10-Q/A"}:
        raise ValueError(f"vintage form must be '10-Q' or '10-Q/A', got '{vintage.form}'")
    if not isinstance(declaration, SecQuarterFactDeclaration):
        raise TypeError("declaration must be a SecQuarterFactDeclaration")

    ordinal, row = _match_row(vintage, declaration)

    return SecQuarterBoundFact(
        accession_number=vintage.accession_number,
        vintage_identity=vintage.vintage_identity,
        row_ordinal=ordinal,
        context_ref=row.context_ref or declaration.context_ref,
        declaration=declaration,
        row=row,
    )


def bind_sec_quarter_facts(
    vintage: SecCompanyFinancialVintage | SecObservedFinancialVintage,
    declarations: Iterable[SecQuarterFactDeclaration],
    *,
    max_declarations: int = _MAX_DECLARATIONS,
) -> SecQuarterBindingResult:
    """Bind explicit caller-declared independent quarter facts to a native 10-Q vintage.

    Validates:
    - vintage is a 10-Q or 10-Q/A filing
    - declarations is non-empty and does not exceed max_declarations
    - no duplicate declarations exist
    - every declaration binds to exactly one matching non-dimensional duration row
    - no two declarations bind to the same row
    - deterministic binding identity is produced

    This is a structural lineage binding, not financial quality approval (no PASS claim),
    point-in-time publication, or metric correctness assertion.
    """
    if not isinstance(vintage, (SecCompanyFinancialVintage, SecObservedFinancialVintage)):
        raise TypeError("vintage must be a SecCompanyFinancialVintage")
    if type(vintage.form) is not str or vintage.form.upper() not in {"10-Q", "10-Q/A"}:
        raise ValueError(f"vintage form must be '10-Q' or '10-Q/A', got '{vintage.form}'")
    if (
        type(max_declarations) is not int
        or isinstance(max_declarations, bool)
        or not 1 <= max_declarations <= 10_000
    ):
        raise ValueError(
            f"max_declarations must be an integer between 1 and 10000, got {max_declarations}"
        )

    decl_tuple = tuple(islice(declarations, max_declarations + 1))
    if len(decl_tuple) == 0:
        raise ValueError("declarations must not be empty")
    if len(decl_tuple) > max_declarations:
        raise ValueError(f"declarations count exceeds limit of {max_declarations}")

    for d in decl_tuple:
        if not isinstance(d, SecQuarterFactDeclaration):
            raise TypeError("all items in declarations must be SecQuarterFactDeclaration")

    seen_declarations: set[tuple[str, str, str, str, date, date]] = set()
    for decl in decl_tuple:
        key = (
            decl.accession_number,
            decl.context_ref,
            decl.statement_type,
            decl.native_concept,
            decl.period_start,
            decl.period_end,
        )
        if key in seen_declarations:
            raise ValueError(
                f"duplicate or conflicting declaration for concept '{decl.native_concept}' "
                f"and context_ref '{decl.context_ref}'"
            )
        seen_declarations.add(key)

    facts: list[SecQuarterBoundFact] = []
    bound_ordinals: set[int] = set()

    for decl in decl_tuple:
        ordinal, row = _match_row(vintage, decl)
        if ordinal in bound_ordinals:
            raise ValueError(
                f"duplicate or conflicting binding: row ordinal {ordinal} already bound"
            )
        bound_ordinals.add(ordinal)

        facts.append(
            SecQuarterBoundFact(
                accession_number=vintage.accession_number,
                vintage_identity=vintage.vintage_identity,
                row_ordinal=ordinal,
                context_ref=row.context_ref or decl.context_ref,
                declaration=decl,
                row=row,
            )
        )

    canonical_cik = str(int(vintage.cik)) if vintage.cik.isdecimal() else vintage.cik
    return SecQuarterBindingResult(
        symbol=vintage.symbol,
        cik=vintage.cik,
        canonical_cik=canonical_cik,
        company_name=vintage.company_name,
        form=vintage.form,
        accession_number=vintage.accession_number,
        vintage_identity=vintage.vintage_identity,
        facts=tuple(facts),
    )


bind_sec_independent_quarter_facts = bind_sec_quarter_facts
