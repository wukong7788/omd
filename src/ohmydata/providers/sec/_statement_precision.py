"""Bounded native XBRL duplicate-fact precision checks and selection."""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from fractions import Fraction
from typing import Any

_MAX_FACT_ARITHMETIC_DIGITS = 10_000


def parse_fact_precision(fact: Any, error_cls: type[Exception]) -> int | str | None:
    """Return finite precision, None for missing, and ``INF`` for infinity."""
    decimals = fact.decimals
    if decimals is None or type(decimals).__name__ in {"NAType", "NaTType"}:
        return None
    if isinstance(decimals, str) and decimals.strip() == "INF":
        return "INF"
    if isinstance(decimals, bool):
        raise error_cls("invalid fact precision")
    if isinstance(decimals, int):
        return decimals
    if isinstance(decimals, str) and re.fullmatch(r"[+-]?\d+", decimals.strip()):
        return int(decimals.strip())
    raise error_cls("invalid fact precision")


def fact_interval(
    value: Decimal, precision: int | str | None, error_cls: type[Exception]
) -> tuple[Fraction, Fraction]:
    value_tuple = value.as_tuple()
    if not isinstance(value_tuple.exponent, int):
        raise error_cls("non-finite statement fact")
    if (
        abs(value_tuple.exponent) > _MAX_FACT_ARITHMETIC_DIGITS
        or len(value_tuple.digits) > _MAX_FACT_ARITHMETIC_DIGITS
    ):
        raise error_cls("statement fact exceeds arithmetic bounds")
    if isinstance(precision, int) and abs(precision) > _MAX_FACT_ARITHMETIC_DIGITS:
        raise error_cls("statement fact exceeds arithmetic bounds")
    exact = Fraction(value)
    if precision is None or precision == "INF":
        return exact, exact
    if not isinstance(precision, int):
        raise error_cls("invalid fact precision")
    half_unit = Fraction(10 ** (-precision), 2) if precision < 0 else Fraction(1, 2 * 10**precision)
    return exact - half_unit, exact + half_unit


def select_native_facts(
    facts: list[Any],
    *,
    parse_number: Callable[[Any], Decimal | None],
    error_cls: type[Exception],
) -> list[Any]:
    """Validate one duplicate group and select its best fact per context."""
    parsed: list[tuple[Any, Decimal, int | str | None]] = []
    unit_refs = set()
    intervals = []
    missing_precision = False
    values_by_precision: dict[int | str, set[Decimal]] = {}
    best_by_context: dict[str, tuple[tuple[int, int], tuple[str, str], Any]] = {}

    def rank(precision: int | str | None) -> tuple[int, int]:
        if precision is None:
            return (0, 0)
        if precision == "INF":
            return (2, 0)
        if not isinstance(precision, int):
            raise error_cls("invalid fact precision")
        return (1, precision)

    for fact in facts:
        value = parse_number(fact.value)
        precision = parse_fact_precision(fact, error_cls)
        missing_precision |= precision is None
        unit_refs.add(fact.unit_ref)
        if value is None:
            continue
        parsed.append((fact, value, precision))
        intervals.append(fact_interval(value, precision, error_cls))
        if precision is not None:
            values_by_precision.setdefault(precision, set()).add(value)
        context = fact.context_ref
        tie_break = (str(fact.value), str(fact.decimals))
        candidate = (rank(precision), tie_break, fact)
        current = best_by_context.get(context)
        if (
            current is None
            or candidate[0] > current[0]
            or (candidate[0] == current[0] and candidate[1] < current[1])
        ):
            best_by_context[context] = candidate
    if len(unit_refs) != 1:
        raise error_cls("conflicting native facts for period and dimensions")
    if not parsed:
        return []
    if len(parsed) != len(facts):
        raise error_cls("non-numeric statement fact")
    lower = max(interval[0] for interval in intervals)
    upper = min(interval[1] for interval in intervals)
    if missing_precision and any(precision is not None for _, _, precision in parsed):
        raise error_cls("conflicting native facts for period and dimensions")
    if missing_precision and len({value for _, value, _ in parsed}) != 1:
        raise error_cls("conflicting native facts for period and dimensions")
    for values in values_by_precision.values():
        if len(values) != 1:
            raise error_cls("conflicting native facts for period and dimensions")
    if lower > upper:
        raise error_cls("conflicting native facts for period and dimensions")
    return [best_by_context[context_ref][2] for context_ref in sorted(best_by_context)]


__all__ = ["fact_interval", "parse_fact_precision", "select_native_facts"]
