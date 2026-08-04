# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnknownLambdaType=false, reportArgumentType=false, reportUnnecessaryIsInstance=false
"""Deterministic frame serialization tests for Tushare observations."""

import math
from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from ohmydata.providers.tushare.observations import (
    SERIALIZATION_IDENTIFIER,
    deserialize_tushare_frame,
    serialize_tushare_frame,
)


def test_serialization_is_deterministic_across_runs():
    frame = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "600000.SH"],
            "trade_date": ["20240102", "20240103"],
            "weight": [0.8656, 1.133],
            "note": [None, "x"],
        }
    )
    first = serialize_tushare_frame(frame)
    second = serialize_tushare_frame(frame)
    assert first == second
    assert isinstance(first, bytes)
    assert SERIALIZATION_IDENTIFIER == "tushare-frame-json-v1"


def test_serialization_preserves_column_order_and_dtype_identity():
    frame = pd.DataFrame(
        {
            "b": pd.Series([1, 2], dtype="int64"),
            "a": pd.Series([1.5, np.nan], dtype="float64"),
            "c": pd.Series(["x", "y"], dtype="object"),
        }
    )
    payload = serialize_tushare_frame(frame)
    rebuilt = deserialize_tushare_frame(payload)
    assert list(rebuilt.columns) == ["b", "a", "c"]
    assert str(rebuilt["b"].dtype) == "int64"
    assert str(rebuilt["a"].dtype) == "float64"
    assert str(rebuilt["c"].dtype) == "object"
    assert serialize_tushare_frame(rebuilt) == payload


def test_serialization_preserves_nan_infinity_and_row_order():
    frame = pd.DataFrame(
        {
            "v": pd.Series([1.0, math.nan, math.inf, -math.inf], dtype="float64"),
            "label": ["a", "b", "c", "d"],
        }
    )
    payload = serialize_tushare_frame(frame)
    rebuilt = deserialize_tushare_frame(payload)
    values = rebuilt["v"].tolist()
    assert math.isnan(values[1])
    assert values[2] == math.inf
    assert values[3] == -math.inf
    assert list(rebuilt["label"]) == ["a", "b", "c", "d"]
    assert serialize_tushare_frame(rebuilt) == payload


def test_serialization_preserves_null_nat_na_and_temporal_cells():
    frame = pd.DataFrame(
        {
            "nullable": pd.Series([1, pd.NA, 3], dtype="Int64"),
            "at": pd.Series([pd.Timestamp("2024-01-02"), pd.NaT, pd.Timestamp("2024-01-04")]),
            "as_date": pd.Series([date(2024, 1, 2), None, date(2024, 1, 4)], dtype="object"),
            "dec": pd.Series([Decimal("1.5"), None, Decimal("2.5")], dtype="object"),
        }
    )
    payload = serialize_tushare_frame(frame)
    rebuilt = deserialize_tushare_frame(payload)
    assert pd.isna(rebuilt["nullable"].iloc[1])
    assert rebuilt["nullable"].iloc[0] == 1
    assert pd.isna(rebuilt["at"].iloc[1])
    assert rebuilt["at"].iloc[0] == pd.Timestamp("2024-01-02")
    assert rebuilt["as_date"].iloc[0] == date(2024, 1, 2)
    assert rebuilt["dec"].iloc[0] == Decimal("1.5")
    assert serialize_tushare_frame(rebuilt) == payload


def test_serialization_preserves_aware_datetimes_and_naive_ordering():
    frame = pd.DataFrame(
        {
            "ts": pd.Series(
                [
                    datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
                    None,
                ],
                dtype="object",
            )
        }
    )
    payload = serialize_tushare_frame(frame)
    rebuilt = deserialize_tushare_frame(payload)
    assert rebuilt["ts"].iloc[0] == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert rebuilt["ts"].iloc[1] is None
    assert serialize_tushare_frame(rebuilt) == payload


def test_serialization_rejects_unsupported_cell_types():
    frame = pd.DataFrame({"x": [{"a": 1}]})
    with pytest.raises(TypeError):
        serialize_tushare_frame(frame)


def test_deserialization_rejects_tampered_payloads():
    frame = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    payload = serialize_tushare_frame(frame)
    text = payload.decode("utf-8")
    assert deserialize_tushare_frame(payload) is not None
    # asymmetric tamper: rename a column in columns but not in dtypes
    with pytest.raises(ValueError):
        deserialize_tushare_frame(
            text.replace('"columns":["a","b"]', '"columns":["z","b"]').encode("utf-8")
        )
    with pytest.raises(ValueError):
        deserialize_tushare_frame(b"not json")
    with pytest.raises(ValueError):
        deserialize_tushare_frame(
            payload.decode("utf-8").replace("tushare-frame-json-v1", "other-v9").encode("utf-8")
        )


def test_serialization_rejects_non_frame_inputs():
    with pytest.raises(TypeError):
        serialize_tushare_frame([1, 2, 3])
    with pytest.raises(TypeError):
        serialize_tushare_frame(pd.DataFrame({"a": [1]}).rename(columns={"a": 1}))
