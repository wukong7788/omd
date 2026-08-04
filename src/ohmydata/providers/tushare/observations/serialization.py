"""Deterministic provider-native frame serialization for Tushare observations.

The serialization preserves column order, per-column dtype identity, row order,
nulls, NaN, infinities, pandas missing sentinels, date strings, and supported
temporal cells in a stable byte form. The same input frame always produces the
same payload, so the payload bytes can serve as the snapshot response body and
its SHA-256 as the response/content hash.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

SERIALIZATION_IDENTIFIER = "tushare-frame-json-v1"
_SCHEMA_VERSION = 1


def _encode_cell(value: Any) -> Any:
    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value
    if type(value) is Decimal:
        return {"t": "decimal", "v": str(value)}
    if isinstance(value, float):
        if math.isnan(value):
            return {"t": "nan"}
        if math.isinf(value):
            return {"t": "inf"} if value > 0 else {"t": "-inf"}
        return value
    if type(value) is datetime:
        return {"t": "datetime", "v": value.isoformat()}
    if type(value) is date:
        return {"t": "date", "v": value.isoformat()}
    import numpy as np
    import pandas as pd

    if isinstance(value, np.bool_):
        return bool(cast(Any, value).item())
    if isinstance(value, np.integer):
        return int(cast(Any, value).item())
    if isinstance(value, np.floating):
        return _encode_cell(float(cast(Any, value).item()))
    if value is pd.NA:
        return {"t": "na"}
    if value is pd.NaT:
        return {"t": "nat"}
    if isinstance(value, np.datetime64):
        native = cast(Any, value)
        if pd.isna(native):
            return {"t": "nat"}
        return {"t": "datetime64", "v": pd.Timestamp(native).isoformat()}
    if isinstance(value, pd.Timestamp):
        return {"t": "timestamp", "v": value.isoformat()}
    raise TypeError(f"unsupported cell type for frame serialization: {type(value).__name__}")


def _decode_cell(value: Any) -> Any:
    if (
        value is None
        or type(value) is bool
        or type(value) is int
        or type(value) is float
        or type(value) is str
    ):
        return value
    if isinstance(value, dict):
        fields = cast(dict[str, Any], value)
        tag = fields.get("t")
        payload = fields.get("v")
        if tag == "nan":
            return float("nan")
        if tag == "inf":
            return float("inf")
        if tag == "-inf":
            return float("-inf")
        if tag == "na":
            import pandas as pd

            return pd.NA
        if tag == "nat":
            import pandas as pd

            return pd.NaT
        if tag == "decimal":
            return Decimal(str(payload))
        if tag == "datetime64":
            import numpy as np

            return np.datetime64(payload)
        if tag == "datetime":
            return datetime.fromisoformat(str(payload))
        if tag == "date":
            return date.fromisoformat(str(payload))
        if tag == "timestamp":
            import pandas as pd

            return pd.Timestamp(str(payload))
    raise ValueError("malformed serialized cell")


def serialize_tushare_frame(frame: Any) -> bytes:
    """Serialize a pandas DataFrame into the canonical deterministic payload."""
    import pandas as pd

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    columns = list(frame.columns)
    if any(type(column) is not str for column in columns):
        raise TypeError("frame columns must be strings")
    document: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "serialization_identifier": SERIALIZATION_IDENTIFIER,
        "columns": columns,
        "dtypes": {column: str(frame[column].dtype) for column in columns},
        "rows": [
            [_encode_cell(value) for value in row]
            for row in frame.itertuples(index=False, name=None)
        ],
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def deserialize_tushare_frame(payload: bytes) -> Any:
    """Rebuild the DataFrame from a canonical payload.

    The round trip is exact: serializing the rebuilt frame reproduces the same
    bytes, which the capture path verifies before returning.
    """
    import pandas as pd

    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed serialized frame") from exc
    if not isinstance(document, dict):
        raise TypeError("malformed serialized frame")
    doc = cast(dict[str, Any], document)
    if doc.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("serialization schema mismatch")
    if doc.get("serialization_identifier") != SERIALIZATION_IDENTIFIER:
        raise ValueError("serialization identifier mismatch")
    columns = doc.get("columns")
    dtypes = doc.get("dtypes")
    rows = doc.get("rows")
    if not isinstance(columns, list) or not isinstance(dtypes, dict) or not isinstance(rows, list):
        raise TypeError("malformed serialized frame")
    column_list = cast(list[str], columns)
    dtype_map = cast(dict[str, Any], dtypes)
    row_list = cast(list[list[Any]], rows)
    if any(type(column) is not str for column in column_list) or set(column_list) != set(dtype_map):
        raise ValueError("malformed serialized frame")
    decoded = [[_decode_cell(cell) for cell in row] for row in row_list]
    data = {column: [row[index] for row in decoded] for index, column in enumerate(column_list)}
    frame = pd.DataFrame(index=range(len(row_list)))
    for column in column_list:
        dtype = dtype_map[column]
        if not isinstance(dtype, str):
            raise TypeError("malformed serialized frame")
        try:
            frame[column] = pd.Series(data[column], dtype=dtype, index=frame.index)
        except (TypeError, ValueError) as exc:
            raise ValueError("malformed serialized frame") from exc
    return frame


__all__ = ["SERIALIZATION_IDENTIFIER", "deserialize_tushare_frame", "serialize_tushare_frame"]
