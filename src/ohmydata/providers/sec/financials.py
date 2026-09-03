"""Domain models and value objects for SEC company financial statements (PIT)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

STATEMENT_TYPES = ("balance_sheet", "income_statement", "cash_flow")
StatementType = Literal["balance_sheet", "income_statement", "cash_flow"]


def _canonical_val(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, (date, datetime)):
        return val.isoformat()
    if isinstance(val, (list, tuple)):
        items: list[Any] = [_canonical_val(x) for x in val]  # pyright: ignore[reportUnknownVariableType]
        return items
    if isinstance(val, (dict, Mapping)):
        d: dict[str, Any] = {
            str(k): _canonical_val(v)  # pyright: ignore[reportUnknownArgumentType]
            for k, v in sorted(val.items())  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        }
        return d
    return str(val)


@dataclass(frozen=True)
class SecStatementRow:
    """Individual financial statement line item."""

    statement_type: str
    standard_concept: str
    concept: str
    label: str
    value: Decimal | None
    value_native: str | None
    unit: str | None = None
    decimals: int | None = None
    period_start: date | None = None
    period_end: date | None = None
    is_point_in_time: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_type": self.statement_type,
            "standard_concept": self.standard_concept,
            "concept": self.concept,
            "label": self.label,
            "value": self.value,
            "value_native": self.value_native,
            "unit": self.unit,
            "decimals": self.decimals,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "is_point_in_time": self.is_point_in_time,
        }


@dataclass(frozen=True)
class SecCompanyFinancialVintage:
    """Point-in-Time financial statement vintage for one SEC filing."""

    symbol: str
    cik: str
    company_name: str
    form: str
    accession_number: str
    filing_date: date
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    period_end: date | None = None
    accepted_at: datetime | None = None
    availability_anchor: datetime | None = None
    availability_basis: str = "ACCEPTED_AT_SOURCE"
    availability_precision: str = "SECOND"
    availability_policy: str = "accepted-at-plus-lag"
    availability_lag_days: int = 0
    is_amendment: bool = False
    quality_flags: tuple[str, ...] = ()
    rows: tuple[SecStatementRow, ...] = ()
    _cached_vintage_identity: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.accepted_at is not None:
            if self.accepted_at.tzinfo is None:
                raise ValueError("accepted_at must be timezone-aware")
            object.__setattr__(self, "accepted_at", self.accepted_at.astimezone(UTC))
        if self.availability_policy == "observation-only" and self.availability_lag_days != 0:
            raise ValueError("availability_lag_days is invalid for observation-only policy")
        if self.availability_policy == "observation-only":
            object.__setattr__(self, "availability_basis", "OBSERVATION_DATE_ONLY")
            object.__setattr__(self, "availability_precision", "DAY")
            object.__setattr__(self, "availability_anchor", None)
        elif self.availability_anchor is not None:
            if self.availability_anchor.tzinfo is None:
                raise ValueError("availability_anchor must be timezone-aware")
            object.__setattr__(
                self, "availability_anchor", self.availability_anchor.astimezone(UTC)
            )
        elif self.accepted_at is not None:
            anchor = self.accepted_at + timedelta(days=self.availability_lag_days)
            object.__setattr__(self, "availability_anchor", anchor)

    @property
    def vintage_identity(self) -> str:
        cached = getattr(self, "_cached_vintage_identity", None)
        if cached is not None:
            return cached
        payload = {
            "symbol": self.symbol,
            "cik": self.cik,
            "form": self.form,
            "accession_number": self.accession_number,
            "filing_date": self.filing_date.isoformat(),
            "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "rows_count": len(self.rows),
            "rows": [r.to_dict() for r in self.rows],
            "schema_version": "sec-company-financials-v1",
        }
        h = hashlib.sha256(
            json.dumps(_canonical_val(payload), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        object.__setattr__(self, "_cached_vintage_identity", h)
        return h

    def filter_statement(self, statement_type: StatementType) -> tuple[SecStatementRow, ...]:
        return tuple(r for r in self.rows if r.statement_type == statement_type)


@dataclass(frozen=True)
class SecFinancialsRequest:
    """Request specification for company financial statements."""

    symbols: tuple[str, ...]
    forms: tuple[str, ...] = ("10-K", "10-Q")
    start_year: int | None = None
    end_year: int | None = None
    availability_policy: str = "accepted-at-plus-lag"
    lag_days: int = 0
    include_amendments: bool = True
    limit: int | None = None

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("symbols cannot be empty")
        for sym in self.symbols:
            if not sym or not sym.strip():
                raise ValueError(f"invalid symbol: {sym}")
        if self.availability_policy not in ("accepted-at-plus-lag", "observation-only"):
            raise ValueError(f"unsupported availability_policy: {self.availability_policy}")
        if self.availability_policy == "observation-only" and self.lag_days != 0:
            raise ValueError("lag_days is invalid for observation-only policy")
        if not 0 <= self.lag_days <= 30:
            raise ValueError(f"lag_days must be between 0 and 30, got {self.lag_days}")
        if self.limit is not None and self.limit <= 0:
            raise ValueError("limit must be positive")
