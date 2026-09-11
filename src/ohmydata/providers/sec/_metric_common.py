"""Explicit vocabulary and bounded canonical identities for metric graphs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from ._event_discovery_models import _stamp, _utc
from ._metric_arithmetic import validate_decimal


class SecMetricRecipe(str, Enum):
    FOUR_QUARTER_TTM_V1 = "FOUR_QUARTER_TTM_V1"
    FY_YTD_TTM_V1 = "FY_YTD_TTM_V1"
    CFO_CAPEX_V1 = "CFO_CAPEX_V1"
    YOY_V1 = "YOY_V1"
    MARGIN_V1 = "MARGIN_V1"
    PE_V1 = "PE_V1"
    PS_V1 = "PS_V1"
    FPE_V1 = "FPE_V1"


class SecMetricDomainPolicy(str, Enum):
    RAISE_NONPOSITIVE = "RAISE_NONPOSITIVE"
    MISSING_NONPOSITIVE = "MISSING_NONPOSITIVE"
    RAISE_ZERO_ABS = "RAISE_ZERO_ABS"
    MISSING_ZERO_ABS = "MISSING_ZERO_ABS"


class SecMetricCapexSign(str, Enum):
    POSITIVE_OUTFLOW = "POSITIVE_OUTFLOW"
    NEGATIVE_OUTFLOW = "NEGATIVE_OUTFLOW"


class SecMetricRounding(str, Enum):
    ROUND_05UP = "ROUND_05UP"
    ROUND_CEILING = "ROUND_CEILING"
    ROUND_DOWN = "ROUND_DOWN"
    ROUND_FLOOR = "ROUND_FLOOR"
    ROUND_HALF_DOWN = "ROUND_HALF_DOWN"
    ROUND_HALF_EVEN = "ROUND_HALF_EVEN"
    ROUND_HALF_UP = "ROUND_HALF_UP"
    ROUND_UP = "ROUND_UP"


ACTUAL_METRICS = frozenset(
    {"REVENUE", "NET_INCOME", "OPERATING_INCOME", "GROSS_PROFIT", "CFO", "CAPEX"}
)
EXTERNAL_METRICS = frozenset({"COMPANY_MARKET_CAP", "SECURITY_PRICE", "FORECAST_EPS"})
DURATION_KINDS = frozenset({"INDEPENDENT_QUARTER", "YTD", "FY"})
DIVISION_RECIPES = frozenset(
    {
        SecMetricRecipe.YOY_V1,
        SecMetricRecipe.MARGIN_V1,
        SecMetricRecipe.PE_V1,
        SecMetricRecipe.PS_V1,
        SecMetricRecipe.FPE_V1,
    }
)
VALUATION_RECIPES = frozenset(
    {SecMetricRecipe.PE_V1, SecMetricRecipe.PS_V1, SecMetricRecipe.FPE_V1}
)


def text(value: object, name: str) -> str:
    if type(value) is not str or not value or len(value) > 1024 or len(value.encode()) > 1024:
        raise ValueError(f"{name} must be nonempty and at most1024 UTF8 bytes")
    return value


def sha(value: object, name: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be a lowercase SHA256 identity")
    return value


def canonical(value: Any) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, Enum):
        return value.value
    if type(value) is Decimal:
        return {"decimal": str(validate_decimal(value))}
    if type(value) is datetime:
        return {"datetime": _stamp(_utc(value, "timestamp"))}
    if type(value) is date:
        return {"date": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: canonical(getattr(value, field.name))
            for field in fields(value)
            if field.init
        }
    if type(value) is tuple:
        return [canonical(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: canonical(item) for key, item in value.items()}
    raise TypeError("unsupported metric canonical value")


def encoded(value: Any) -> bytes:
    return json.dumps(
        canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def identity(schema: str, value: Any) -> str:
    return hashlib.sha256(encoded({"schema": schema, "payload": value})).hexdigest()


def dates(start: object, end: object) -> None:
    if type(start) is not date or type(end) is not date or start > end:
        raise ValueError("metric period requires ordered dates")


def currency(value: object) -> None:
    if type(value) is not str or re.fullmatch(r"[A-Z]{3}", value) is None:
        raise ValueError("metric currency must be an explicit three-letter code")
