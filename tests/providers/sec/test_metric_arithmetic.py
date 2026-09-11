from decimal import ROUND_HALF_EVEN, ROUND_UP, Decimal, localcontext

import pytest

from ohmydata.providers.sec._metric_arithmetic import exact_sum, negate, rounded_ratio


def test_arithmetic_ignores_ambient_decimal_context():
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        assert exact_sum(
            (Decimal("100000000.01"), Decimal("0.02"), negate(Decimal(100000000)))
        ) == Decimal("0.03")
        result, bound = rounded_ratio(Decimal(1), Decimal(3), precision=8, rounding=ROUND_HALF_EVEN)
    assert result == Decimal("0.33333333") and bound == Decimal("0.00000001")
    with localcontext() as context:
        context.prec = 40
        assert abs(result - Decimal(1) / Decimal(3)) <= bound


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("1e10001")])
def test_invalid_arithmetic_values_fail(value):
    with pytest.raises(ValueError):
        exact_sum((value,))


def test_division_requires_explicit_finite_policy():
    with pytest.raises(ValueError, match="zero"):
        rounded_ratio(Decimal(1), Decimal(0), precision=8, rounding=ROUND_HALF_EVEN)
    with pytest.raises(ValueError, match="precision"):
        rounded_ratio(Decimal(1), Decimal(3), precision=True, rounding=ROUND_HALF_EVEN)
    with pytest.raises(ValueError, match="ROUND_"):
        rounded_ratio(Decimal(1), Decimal(3), precision=8, rounding="UNSUPPORTED")
