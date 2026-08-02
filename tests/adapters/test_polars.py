import datetime as dt
import decimal
import math
import subprocess
import sys

import pandas as pd
import pytest

pl = pytest.importorskip("polars")

from ohmydata.adapters.polars import pandas_to_polars, polars_to_pandas
from ohmydata.core import SchemaMismatchError


def test_round_trip_preserves_order_null_nan_and_infinity() -> None:
    source = pd.DataFrame(
        {
            "text": pd.Series(["x", None], dtype="string"),
            "bytes": pd.Series([b"x", None], dtype=object),
            "value": pd.Series([float("nan"), float("-inf")], dtype="float64"),
            "integer": pd.Series([1, None], dtype="Int16"),
            "day": [dt.date(2024, 1, 1), None],
            "naive": pd.Series([pd.Timestamp("2024-01-01 12:34:56"), pd.NaT]),
        }
    )
    converted = pandas_to_polars(source)
    assert converted.columns == list(source.columns)
    assert converted.shape == source.shape
    assert converted["text"].to_list() == ["x", None]
    assert math.isnan(converted["value"][0])
    assert converted["bytes"].to_list() == [b"x", None]
    assert converted["value"][1] == float("-inf")

    result = polars_to_pandas(converted)
    assert list(result.columns) == list(source.columns)
    assert result["text"].iloc[0] == "x" and pd.isna(result["text"].iloc[1])
    assert pd.isna(result["value"][0]) and result["value"][1] == float("-inf")
    assert result["day"].iloc[0] == source["day"].iloc[0] and pd.isna(result["day"].iloc[1])
    assert result["naive"].iloc[0] == source["naive"].iloc[0]

    converted = converted.with_columns(converted["text"].fill_null("changed"))
    assert source["text"].iloc[0] == "x" and pd.isna(source["text"].iloc[1])


def test_supported_widths_and_timezone() -> None:
    source = pd.DataFrame(
        {
            "i8": pd.Series([1, -1], dtype="int8"),
            "i16": pd.Series([1, -1], dtype="int16"),
            "i32": pd.Series([1, -1], dtype="int32"),
            "i64": pd.Series([1, -1], dtype="int64"),
            "u8": pd.Series([0, 255], dtype="uint8"),
            "u16": pd.Series([0, 65535], dtype="uint16"),
            "u32": pd.Series([0, 2**32 - 1], dtype="uint32"),
            "u64": pd.Series([0, 2**64 - 1], dtype="uint64"),
            "nullable": pd.Series([1, None], dtype="Int64"),
            "f32": pd.Series([1.5, 2.5], dtype="float32"),
            "when": pd.Series(
                [pd.Timestamp("2024-01-01T00:00:00+08:00"), pd.NaT],
                dtype="datetime64[ns, Asia/Shanghai]",
            ),
        }
    )
    result = polars_to_pandas(pandas_to_polars(source))
    assert "Asia/Shanghai" in str(result["when"].dtype)
    assert result["u64"].tolist() == [0, 2**64 - 1]


def test_empty_supported_dtypes_are_allowed() -> None:
    source = pd.DataFrame(
        {
            "text": pd.Series([], dtype="string"),
            "number": pd.Series([], dtype="int32"),
            "when": pd.Series([], dtype="datetime64[ns]"),
        }
    )
    result = pandas_to_polars(source)
    assert result.shape == (0, 3)


def test_empty_object_policy_defaults_to_error() -> None:
    source = pd.DataFrame({"x": pd.Series([], dtype=object)})
    with pytest.raises(SchemaMismatchError, match="empty object"):
        pandas_to_polars(source)


def test_empty_object_policy_string_converts_zero_rows_to_string() -> None:
    source = pd.DataFrame({"x": pd.Series([], dtype=object)})
    result = pandas_to_polars(source, empty_object_policy="string")
    assert result.shape == (0, 1)
    assert result.dtypes == [pl.String]


def test_empty_object_policy_string_preserves_all_nulls_and_source() -> None:
    source = pd.DataFrame({"x": pd.Series([None, pd.NA], dtype=object)})
    original_dtype = source["x"].dtype
    result = pandas_to_polars(source, empty_object_policy="string")
    assert result.shape == (2, 1)
    assert result.dtypes == [pl.String]
    assert result["x"].to_list() == [None, None]
    assert source["x"].dtype == original_dtype
    assert source["x"].tolist() == [None, pd.NA]


def test_empty_object_policy_string_still_rejects_populated_objects() -> None:
    frames = [
        pd.DataFrame({"x": ["a", 1]}),
        pd.DataFrame({"x": [[1, 2]]}),
        pd.DataFrame({"x": [decimal.Decimal("1.20")]}),
    ]
    for frame in frames:
        with pytest.raises(SchemaMismatchError):
            pandas_to_polars(frame, empty_object_policy="string")


def test_empty_object_policy_rejects_unknown_values_before_conversion() -> None:
    with pytest.raises(ValueError, match="empty_object_policy"):
        pandas_to_polars(object(), empty_object_policy="guess")


@pytest.mark.parametrize(
    "frame",
    [
        pd.DataFrame({"x": pd.Series([], dtype=object)}),
        pd.DataFrame({"x": ["x", 1]}),
        pd.DataFrame({"x": pd.Series(["a", "b"], dtype="category")}),
        pd.DataFrame({"x": pd.Series([dt.timedelta(days=1)])}),
        pd.DataFrame({"x": pd.period_range("2024-01", periods=1, freq="M")}),
        pd.DataFrame({"x": pd.IntervalIndex.from_tuples([(0, 1)])}),
        pd.DataFrame({"x": pd.arrays.SparseArray([1, 0])}),
        pd.DataFrame({"x": pd.Series([1 + 2j])}),
        pd.DataFrame({"x": [decimal.Decimal("1.20")]}),
        pd.DataFrame({"x": [[1, 2]]}),
    ],
)
def test_unsupported_pandas_dtypes_fail_closed(frame: pd.DataFrame) -> None:
    with pytest.raises(SchemaMismatchError) as exc_info:
        pandas_to_polars(frame)
    assert "1.0+2.0j" not in str(exc_info.value)
    assert "1.20" not in str(exc_info.value)


def test_wrong_source_and_duplicate_columns() -> None:
    with pytest.raises(TypeError):
        pandas_to_polars([1, 2])
    duplicate = pd.DataFrame([[1, 2]], columns=["x", "x"])
    with pytest.raises(SchemaMismatchError, match="duplicate"):
        pandas_to_polars(duplicate)


def test_polars_nested_dtype_rejected() -> None:
    frames = [
        pl.DataFrame({"nested": [[1, 2]]}),
        pl.DataFrame({"nested": [{"x": 1}]}),
        pl.DataFrame({"array": [[1, 2]]}).with_columns(pl.col("array").cast(pl.Array(pl.Int64, 2))),
        pl.DataFrame({"duration": [1]}).with_columns(pl.col("duration").cast(pl.Duration("ms"))),
        pl.DataFrame({"time": [1]}).with_columns(pl.col("time").cast(pl.Time)),
        pl.DataFrame({"decimal": ["1.20"]}).with_columns(pl.col("decimal").cast(pl.Decimal(10, 2))),
        pl.DataFrame({"category": ["a"]}).with_columns(pl.col("category").cast(pl.Categorical)),
    ]
    for frame in frames:
        with pytest.raises(SchemaMismatchError) as exc_info:
            polars_to_pandas(frame)
        assert "1.20" not in str(exc_info.value)


def test_polars_float_states_and_mutation_isolation() -> None:
    source = pl.DataFrame({"f": [None, float("nan"), float("inf"), float("-inf")]})
    pandas_frame = polars_to_pandas(source)
    assert pd.isna(pandas_frame["f"].iloc[0])
    assert math.isnan(pandas_frame["f"].iloc[1])
    assert pandas_frame["f"].iloc[2] == float("inf")
    assert pandas_frame["f"].iloc[3] == float("-inf")
    pandas_frame.iloc[0, 0] = 99.0
    assert source["f"][0] is None
    round_trip = pandas_to_polars(polars_to_pandas(source))
    assert round_trip["f"][0] is None and math.isnan(round_trip["f"][1])


def test_wrong_polars_source_and_order() -> None:
    with pytest.raises(TypeError):
        polars_to_pandas({"x": [1]})
    source = pd.DataFrame({"b": [2, 1], "a": ["second", "first"]})
    result = pandas_to_polars(source)
    assert result.columns == ["b", "a"]
    assert result.to_dicts() == [{"b": 2, "a": "second"}, {"b": 1, "a": "first"}]


def test_lazy_optional_import_guidance() -> None:
    script = "\n".join(  # noqa: FLY002
        [
            "import builtins",
            "from ohmydata.adapters import pandas_to_polars, polars_to_pandas",
            "import pandas, polars, pyarrow",
            "real = builtins.__import__",
            "def blocked(name, *args, **kwargs):",
            "    if name.split('.')[0] in {'pandas', 'polars', 'pyarrow'}: raise ImportError('blocked')",
            "    return real(name, *args, **kwargs)",
            "for missing in ('pandas', 'polars', 'pyarrow'):",
            "    def selective(name, *args, **kwargs):",
            "        if name.split('.')[0] == missing: raise ImportError('blocked')",
            "        return real(name, *args, **kwargs)",
            "    builtins.__import__ = selective",
            "    for function in (pandas_to_polars, polars_to_pandas):",
            "        try: function(object())",
            "        except ImportError as exc: assert 'ohmydata[polars]' in str(exc)",
            "        else: raise AssertionError('expected ImportError')",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
