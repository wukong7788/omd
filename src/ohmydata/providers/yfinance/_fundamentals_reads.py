"""Bounded, attributable yfinance fundamentals source reads."""

from __future__ import annotations

import inspect
import random
import time
from collections.abc import Callable
from dataclasses import fields
from typing import Any

import pandas as pd

from ohmydata.core.errors import (
    PermanentProviderError,
    RetryExhaustedError,
    TransientProviderError,
)
from ohmydata.core.policy import AttemptRecord, RetryPolicy, execute_with_retry

from .fundamentals import (
    YFinanceFundamentalsSourceError,
    YFinanceFundamentalsSourceResult,
    YFinanceFundamentalsSourceStatus,
    YFinanceSymbolFundamentals,
)


class _TransientReadError(TransientProviderError):
    """Retry marker; upstream exception text is deliberately omitted."""


class _PermanentReadError(Exception):
    """Permanent source or schema error marker."""


def _transient(exc: Exception) -> bool:
    if isinstance(exc, (PermanentProviderError, PermissionError, FileNotFoundError)):
        return False
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (408, 429) or 500 <= status <= 599
    return isinstance(exc, (TransientProviderError, ConnectionError, TimeoutError, OSError)) or (
        type(exc).__name__ == "YFRateLimitError"
    )


def get_attribute(obj: Any, attr: str) -> Any:
    """A truly absent accessor is empty; an existing accessor that raises is a failure."""
    try:
        inspect.getattr_static(obj, attr)
    except AttributeError:
        return None
    return getattr(obj, attr)


def call_or_get(obj: Any, method_name: str, attr_name: str) -> Any:
    """Use the method when present; otherwise read the legacy property."""
    method = get_attribute(obj, method_name)
    if callable(method):
        return method()
    return get_attribute(obj, attr_name)


def read_source(
    source: str,
    fn: Callable[[], Any],
    policy: RetryPolicy,
    *,
    require_dict: bool = False,
    require_value: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random.random,
) -> tuple[Any, YFinanceFundamentalsSourceResult]:
    """Return a value plus successful/failed attempt evidence; retry only transient errors."""
    exception_types: list[str] = []

    def guarded() -> Any:
        try:
            value = fn()
            if require_dict and not isinstance(value, dict):
                raise TypeError("yfinance info must be a dict")
            if require_value and value is None:
                raise TypeError("yfinance ticker factory returned None")
            return value
        except Exception as exc:
            exception_types.append(type(exc).__name__)
            if _transient(exc):
                raise _TransientReadError from exc
            raise _PermanentReadError from exc

    try:
        result = execute_with_retry(guarded, policy=policy, sleep=sleep, random_value=random_value)
    except (RetryExhaustedError, _PermanentReadError) as exc:
        transient = isinstance(exc, RetryExhaustedError)
        status = (
            YFinanceFundamentalsSourceStatus.TRANSIENT_FAILURE
            if transient
            else YFinanceFundamentalsSourceStatus.PERMANENT_FAILURE
        )
        original = exc.__cause__
        if original is not None and original.__cause__ is not None:
            original = original.__cause__
        exception_type = type(original).__name__ if original is not None else type(exc).__name__
        attempts = (
            tuple(
                AttemptRecord(a.attempt, name, a.retry_delay_seconds)
                for a, name in zip(exc.attempts, exception_types, strict=True)
            )
            if transient
            else (AttemptRecord(1, exception_type, None),)
        )
        error = YFinanceFundamentalsSourceError(source, status, exception_type, attempts)
        return None, YFinanceFundamentalsSourceResult(status, attempts, error)

    value = result.value
    empty = value is None or (isinstance(value, (dict, pd.DataFrame)) and len(value) == 0)
    status = (
        YFinanceFundamentalsSourceStatus.EMPTY
        if empty
        else YFinanceFundamentalsSourceStatus.PRESENT
    )
    failures = iter(exception_types)
    attempts = tuple(
        AttemptRecord(
            a.attempt, next(failures) if a.exception_type else None, a.retry_delay_seconds
        )
        for a in result.attempts
    )
    return value, YFinanceFundamentalsSourceResult(status, attempts)


def has_material_data(record: YFinanceSymbolFundamentals) -> bool:
    """Exclude default metadata and false booleans from data-presence decisions."""
    if (
        record.is_excluded
        or record.report_date is not None
        or record.provider_report_date is not None
    ):
        return True
    for section in (record.valuation, record.financials, record.estimates):
        for field in fields(section):
            value = getattr(section, field.name)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and pd.notna(value):
                return True
    for field in fields(record.source_info):
        if field.name == "regular_market_time":
            continue
        value = getattr(record.source_info, field.name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and pd.notna(value):
            return True
    return False
