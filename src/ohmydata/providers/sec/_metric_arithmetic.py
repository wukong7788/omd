"""Context-independent bounded Decimal operations for named financial recipes."""

from __future__ import annotations

from decimal import (
    ROUND_05UP,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Context,
    Decimal,
    localcontext,
)

from .quarter_ttm import _decimal_sum


def validate_decimal(value: object) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("metric values must be finite Decimal values")
    parts = value.as_tuple()
    if (
        len(parts.digits) > 10_000
        or type(parts.exponent) is not int
        or abs(parts.exponent) > 10_000
        or abs(value.adjusted()) > 10_000
    ):
        raise ValueError("metric Decimal exceeds arithmetic limits")
    return value


def exact_sum(values: tuple[Decimal, ...]) -> Decimal:
    if not values or len(values) > 4:
        raise ValueError("metric sum accepts one to four explicit terms")
    return _decimal_sum(tuple(validate_decimal(value) for value in values))


def negate(value: Decimal) -> Decimal:
    return validate_decimal(value).copy_negate()


def rounded_ratio(
    numerator: Decimal, denominator: Decimal, *, precision: int, rounding: str
) -> tuple[Decimal, Decimal]:
    """Return a rounded ratio and conservative absolute last-place error bound."""
    validate_decimal(numerator)
    validate_decimal(denominator)
    if denominator == 0:
        raise ValueError("metric denominator is zero")
    modes = {
        ROUND_05UP,
        ROUND_CEILING,
        ROUND_DOWN,
        ROUND_FLOOR,
        ROUND_HALF_DOWN,
        ROUND_HALF_EVEN,
        ROUND_HALF_UP,
        ROUND_UP,
    }
    if type(precision) is not int or not 1 <= precision <= 100 or rounding not in modes:
        raise ValueError("metric division requires precision1..100 and an explicit ROUND_* mode")
    with localcontext(Context(prec=precision, rounding=rounding, Emin=-30_000, Emax=30_000)):
        result = numerator / denominator
    validate_decimal(result)
    # One unit in the last place bounds all supported Decimal rounding modes.
    bound = Decimal((0, (1,), result.adjusted() - precision + 1)) if result else Decimal(0)
    return result, bound
