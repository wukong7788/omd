"""Canonical value normalization for vintage artifact hashing."""
# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def canonicalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "+inf" if value > 0 else "-inf"
        return value
    if isinstance(value, dict):
        return {str(k): canonicalize(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set)):
        return [canonicalize(item) for item in value]
    try:
        import numpy as np
        import pandas as pd

        if isinstance(value, np.generic):
            return canonicalize(value.item())
        if isinstance(value, np.ndarray):
            return canonicalize(value.tolist())
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value
