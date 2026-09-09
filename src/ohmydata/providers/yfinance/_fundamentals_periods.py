"""Date-bound quarterly selection, independent of provider metadata freshness."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class YFinanceMetricPeriod:
    """Source identity for one pair; dates describe columns even when values are missing."""

    metric: str
    statement: str
    source_row: str | None
    latest_period: date | None
    prior_period: date | None
    latest_native: str | None = None
    prior_native: str | None = None
    coverage: str = "missing_row"
    yoy_policy: str = "calendar-anniversary-within-7-days-unique"


def column_date(value: Any) -> date:
    if not isinstance(value, (str, date, datetime, pd.Timestamp)):
        raise TypeError("financial statement columns must be actual dates")
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("unrecognized financial statement period") from exc
    if pd.isna(parsed) or parsed.year < 1990:
        raise ValueError("invalid financial statement period")
    return parsed.date()


def latest_statement_date(stmt: pd.DataFrame | None) -> date | None:
    if stmt is None or len(stmt.columns) == 0:
        return None
    return max(column_date(col) for col in stmt.columns)


def dated_values(series: pd.Series) -> dict[date, Any]:
    values: dict[date, Any] = {}
    for col, value in series.items():
        period = column_date(col)
        if period in values:
            previous = values[period]
            both_missing = bool(pd.isna(previous)) and bool(pd.isna(value))
            if not both_missing and (
                bool(pd.isna(previous)) or bool(pd.isna(value)) or previous != value
            ):
                raise ValueError("conflicting duplicate financial statement period")
        else:
            values[period] = value
    return values


def prior_period(values: dict[date, Any], target: date) -> date | None:
    try:
        anniversary = target.replace(year=target.year - 1)
    except ValueError:  # February 29
        anniversary = target.replace(year=target.year - 1, day=28)
    candidates = [period for period in values if abs((period - anniversary).days) <= 7]
    if len(candidates) > 1:
        raise ValueError("ambiguous prior-year financial statement period")
    return candidates[0] if candidates else None


def select_metric(
    metric: str, statement: str, stmt: pd.DataFrame | None, keys: list[str], target: date | None
) -> tuple[YFinanceMetricPeriod, Any, Any]:
    """Bind values and source metadata to one shared report end."""
    source_row = None
    values: dict[date, Any] = {}
    if stmt is not None and not stmt.empty:
        for key in keys:
            matches = [idx for idx in stmt.index if str(idx).strip().lower() == key.lower()]
            if len(matches) > 1:
                raise ValueError("duplicate financial statement metric row")
            if matches:
                source_row = str(matches[0])
                row = stmt.loc[matches[0]]
                if not isinstance(row, pd.Series):
                    raise ValueError("duplicate financial statement metric row")
                values = dated_values(row)
                break
    previous = prior_period(values, target) if target else None
    latest_value = values.get(target)
    previous_value = values.get(previous)
    try:
        valid = latest_value is not None and math.isfinite(float(latest_value))
    except (TypeError, ValueError):
        valid = False
    coverage = (
        "missing_row"
        if source_row is None
        else "missing_period"
        if target not in values
        else "missing_value"
        if not valid
        else "present"
    )

    def native(value: Any) -> str | None:
        return None if value is None or pd.isna(value) else str(value)

    metadata = YFinanceMetricPeriod(
        metric,
        statement,
        source_row,
        target if target in values else None,
        previous,
        native(latest_value),
        native(previous_value),
        coverage,
    )
    return metadata, latest_value, previous_value
